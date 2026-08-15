"""MQTT-facing service that exposes the Python DeviceSimulator as one device."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Protocol

import paho.mqtt.client as mqtt

from .device_simulator import DeviceSimulator
from .mqtt_transport import MqttConfig, MqttConnectionError, MqttTopics
from .schemas import DeviceEventMessage, ObservationMessage, TelemetryMessage


LOGGER = logging.getLogger(__name__)

ObservationCallback = Callable[[ObservationMessage], None]


class DeviceBackend(Protocol):
    """Minimal asynchronous device boundary consumed by the MQTT service."""

    device_id: str

    def submit_payload(self, payload: bytes, on_complete: ObservationCallback) -> None: ...

    def telemetry(self) -> TelemetryMessage: ...


class _SynchronousDeviceBackend:
    def __init__(self, simulator: DeviceSimulator) -> None:
        self._simulator = simulator
        self.device_id = simulator.device_id

    def submit_payload(self, payload: bytes, on_complete: ObservationCallback) -> None:
        on_complete(self._simulator.process_payload(payload))

    def telemetry(self) -> TelemetryMessage:
        return self._simulator.telemetry()


class MqttDeviceService:
    """Subscribe to command messages and publish structured device feedback."""

    def __init__(
        self,
        simulator: DeviceSimulator | DeviceBackend | None = None,
        config: MqttConfig | None = None,
        topics: MqttTopics | None = None,
        *,
        client_id: str | None = None,
        client_factory: Callable[..., mqtt.Client] = mqtt.Client,
        heartbeat_interval_s: float = 15.0,
    ) -> None:
        if heartbeat_interval_s <= 0:
            raise ValueError("heartbeat_interval_s must be positive")
        self.simulator = simulator or DeviceSimulator()
        if callable(getattr(self.simulator, "submit_payload", None)):
            self.backend: DeviceBackend = self.simulator
        elif isinstance(self.simulator, DeviceSimulator):
            self.backend = _SynchronousDeviceBackend(self.simulator)
        else:
            raise TypeError("simulator must implement DeviceBackend or be a DeviceSimulator")
        self.config = config or MqttConfig()
        self.topics = topics or MqttTopics(self.backend.device_id)
        self.client_id = client_id or f"device-{self.backend.device_id}-{uuid.uuid4().hex[:8]}"
        self.heartbeat_interval_s = heartbeat_interval_s
        self._client = client_factory(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            protocol=mqtt.MQTTv311,
            reconnect_on_failure=True,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_connect_fail = self._on_connect_fail
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe
        self._client.reconnect_delay_set(
            min_delay=self.config.reconnect_min_delay_s,
            max_delay=self.config.reconnect_max_delay_s,
        )
        self._connected = threading.Event()
        self._started = False
        self._connect_error: str | None = None
        self._subscription_mid: int | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._reconnect_attempts = 0
        self._reconnect_exhausted = False
        self._fault_lock = threading.Lock()
        self._suppress_next_observation = False
        self._fault_loop_stopped = False

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def reconnect_attempts(self) -> int:
        return self._reconnect_attempts

    @property
    def reconnect_exhausted(self) -> bool:
        return self._reconnect_exhausted

    def start(self) -> None:
        if self._started:
            return
        with self._fault_lock:
            self._fault_loop_stopped = False
        self._connect_error = None
        self._reconnect_attempts = 0
        self._reconnect_exhausted = False
        will_payload = DeviceEventMessage(
            version=1,
            device_id=self.backend.device_id,
            event="device_offline",
            reported_at_ms=int(time.time() * 1000),
        ).model_dump_json(exclude_none=True)
        self._client.will_set(self.topics.event, will_payload, qos=1, retain=True)
        rc = self._client.connect_async(
            self.config.host,
            self.config.port,
            self.config.keepalive_s,
        )
        if rc not in (None, mqtt.MQTT_ERR_SUCCESS):
            raise MqttConnectionError(f"MQTT device connection initiation failed with rc={rc}")
        self._client.loop_start()
        self._started = True
        if not self._connected.wait(self.config.connect_timeout_s):
            self.stop()
            reason = f": {self._connect_error}" if self._connect_error else ""
            raise MqttConnectionError(f"MQTT device connection timed out{reason}")
        self._start_heartbeat()

    def stop(self) -> None:
        if not self._started:
            return
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=self.heartbeat_interval_s + 0.5)
            self._heartbeat_thread = None
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
            self._connected.clear()
            self._started = False
            self.clear_fault_injection()
            with self._fault_lock:
                self._fault_loop_stopped = False

    def disconnect_for_fault(self) -> None:
        """Cleanly disconnect this device client without publishing its Last Will."""

        if not self._started:
            raise MqttConnectionError("MQTT device service is not started")
        self._client.disconnect()
        self._client.loop_stop()
        self._connected.clear()
        with self._fault_lock:
            self._fault_loop_stopped = True

    def restore_after_fault(self) -> None:
        """Reconnect the existing client and wait for its command subscription."""

        if not self._started:
            raise MqttConnectionError("MQTT device service is not started")
        if self.connected:
            return
        with self._fault_lock:
            restart_loop = self._fault_loop_stopped
        rc = self._client.connect_async(
            self.config.host,
            self.config.port,
            self.config.keepalive_s,
        )
        if rc not in (None, mqtt.MQTT_ERR_SUCCESS):
            raise MqttConnectionError(f"MQTT device restore initiation failed with rc={rc}")
        if restart_loop:
            self._client.loop_start()
            with self._fault_lock:
                self._fault_loop_stopped = False
        if not self._connected.wait(self.config.connect_timeout_s):
            reason = f": {self._connect_error}" if self._connect_error else ""
            raise MqttConnectionError(f"MQTT device restore timed out{reason}")

    def suppress_next_observation(self) -> None:
        """Drop exactly one backend Observation publish for a controlled timeout."""

        with self._fault_lock:
            self._suppress_next_observation = True

    def clear_fault_injection(self) -> None:
        with self._fault_lock:
            self._suppress_next_observation = False

    def __enter__(self) -> "MqttDeviceService":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            self._connect_error = str(reason_code)
            self._connected.clear()
            return
        self._reconnect_attempts = 0
        self._reconnect_exhausted = False
        result, mid = client.subscribe(self.topics.command, qos=self.config.qos)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self._connect_error = f"command subscribe failed with rc={result}"
            self._connected.clear()
            return
        self._subscription_mid = mid

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        reason_codes: list[mqtt.ReasonCode],
        properties: mqtt.Properties | None,
    ) -> None:
        if mid != self._subscription_mid:
            return
        if any(reason.is_failure for reason in reason_codes):
            self._connect_error = "command subscription rejected"
            self._connected.clear()
            return
        self._connected.set()

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        self._connected.clear()
        if reason_code.is_failure:
            LOGGER.warning("MQTT device service disconnected: %s", reason_code)

    def _on_connect_fail(self, client: mqtt.Client, userdata: Any) -> None:
        self._reconnect_attempts += 1
        LOGGER.warning(
            "MQTT device service reconnect failed (%s/%s)",
            self._reconnect_attempts,
            self.config.max_reconnect_attempts,
        )
        if self._reconnect_attempts >= self.config.max_reconnect_attempts:
            self._reconnect_exhausted = True
            self._connect_error = (
                f"reconnect attempts exhausted after {self._reconnect_attempts} failures"
            )
            client.disconnect()

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        try:
            self.backend.submit_payload(
                message.payload,
                lambda result: self._publish_observation(client, result),
            )
        except Exception:
            LOGGER.exception("device backend rejected command dispatch")

    def _publish_observation(
        self,
        client: mqtt.Client,
        observation: ObservationMessage,
    ) -> None:
        with self._fault_lock:
            if self._suppress_next_observation:
                self._suppress_next_observation = False
                LOGGER.info(
                    "suppressed one Observation task_id=%s seq=%s",
                    observation.task_id,
                    observation.seq,
                )
                return
        info = client.publish(
            self.topics.status,
            payload=observation.model_dump_json(exclude_none=True),
            qos=self.config.qos,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error(
                "failed to publish Observation task_id=%s seq=%s rc=%s",
                observation.task_id,
                observation.seq,
                info.rc,
            )

    def publish_heartbeat(self) -> None:
        """Publish a valid TelemetryMessage as the application heartbeat."""

        if not self._started or not self.connected:
            raise MqttConnectionError("MQTT device service is not connected")
        payload = self.backend.telemetry().model_dump_json(exclude_none=True)
        info = self._client.publish(
            self.topics.telemetry,
            payload=payload,
            qos=0,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise MqttConnectionError(f"MQTT heartbeat publish failed with rc={info.rc}")

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"heartbeat-{self.backend.device_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.is_set():
            try:
                if self.connected:
                    self.publish_heartbeat()
            except MqttConnectionError as exc:
                LOGGER.warning("device heartbeat publish failed: %s", exc)
            self._heartbeat_stop.wait(self.heartbeat_interval_s)


__all__ = ["DeviceBackend", "MqttDeviceService", "ObservationCallback"]
