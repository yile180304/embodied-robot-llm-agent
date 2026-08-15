"""Standard MQTT request/response transport with strict command correlation."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from .schemas import CommandMessage, ObservationMessage


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MqttTopics:
    device_id: str = "dog01"

    @property
    def command(self) -> str:
        return f"robot/{self.device_id}/cmd"

    @property
    def status(self) -> str:
        return f"robot/{self.device_id}/status"

    @property
    def telemetry(self) -> str:
        return f"robot/{self.device_id}/telemetry"

    @property
    def event(self) -> str:
        return f"robot/{self.device_id}/event"


@dataclass(frozen=True)
class MqttConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    keepalive_s: int = 30
    connect_timeout_s: float = 3.0
    response_timeout_s: float = 3.0
    qos: int = 1
    reconnect_min_delay_s: int = 1
    reconnect_max_delay_s: int = 8
    max_reconnect_attempts: int = 3

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("MQTT host must not be empty")
        if not 1 <= self.port <= 65_535:
            raise ValueError("MQTT port must be in 1..65535")
        if self.qos not in (0, 1, 2):
            raise ValueError("MQTT qos must be 0, 1, or 2")
        if self.connect_timeout_s <= 0 or self.response_timeout_s <= 0:
            raise ValueError("MQTT timeouts must be positive")
        if self.reconnect_min_delay_s < 1 or self.reconnect_max_delay_s < self.reconnect_min_delay_s:
            raise ValueError("MQTT reconnect delays must be positive and ordered")
        if self.max_reconnect_attempts < 1:
            raise ValueError("max_reconnect_attempts must be positive")


class MqttTransportError(RuntimeError):
    """Base class for observable transport failures."""


class MqttConnectionError(MqttTransportError):
    pass


class MqttResponseTimeout(MqttTransportError):
    pass


class MqttPublishObserver(Protocol):
    def on_published(self, command: CommandMessage, *, topic: str, qos: int) -> None: ...


@dataclass
class _PendingRequest:
    event: threading.Event
    observation: ObservationMessage | None = None


class MqttRequestClient:
    """Publish one Command and wait for the strictly correlated Observation."""

    def __init__(
        self,
        config: MqttConfig | None = None,
        topics: MqttTopics | None = None,
        *,
        client_id: str | None = None,
        client_factory: Callable[..., mqtt.Client] = mqtt.Client,
        publish_observer: MqttPublishObserver | None = None,
    ) -> None:
        self.config = config or MqttConfig()
        self.topics = topics or MqttTopics()
        self.client_id = client_id or f"agent-{uuid.uuid4().hex[:10]}"
        self.publish_observer = publish_observer
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
        self._connect_error: str | None = None
        self._pending: dict[tuple[str, int], _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._started = False
        self._subscription_mid: int | None = None
        self._reconnect_attempts = 0
        self._reconnect_exhausted = False

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
        self._connect_error = None
        self._reconnect_attempts = 0
        self._reconnect_exhausted = False
        try:
            rc = self._client.connect_async(
                self.config.host,
                self.config.port,
                self.config.keepalive_s,
            )
        except OSError as exc:
            raise MqttConnectionError(f"unable to connect to MQTT broker: {exc}") from exc
        if rc not in (None, mqtt.MQTT_ERR_SUCCESS):
            raise MqttConnectionError(f"MQTT connect initiation failed with rc={rc}")
        self._client.loop_start()
        self._started = True
        if not self._connected.wait(self.config.connect_timeout_s):
            self.stop()
            reason = f": {self._connect_error}" if self._connect_error else ""
            raise MqttConnectionError(f"MQTT broker connection timed out{reason}")

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
            self._connected.clear()
            self._started = False

    def execute(
        self,
        command: CommandMessage,
        *,
        timeout_s: float | None = None,
    ) -> ObservationMessage:
        if not self._started or not self.connected:
            raise MqttConnectionError("MQTT request client is not connected")
        key = (command.task_id, command.seq)
        pending = _PendingRequest(event=threading.Event())
        with self._pending_lock:
            if key in self._pending:
                raise MqttTransportError(f"request already pending for {key}")
            self._pending[key] = pending
        try:
            payload = command.model_dump_json(exclude_none=True)
            info = self._client.publish(
                self.topics.command,
                payload=payload,
                qos=self.config.qos,
                retain=False,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise MqttConnectionError(f"MQTT publish failed with rc={info.rc}")
            if self.publish_observer is not None:
                self.publish_observer.on_published(
                    command,
                    topic=self.topics.command,
                    qos=self.config.qos,
                )
            wait_s = self.config.response_timeout_s if timeout_s is None else timeout_s
            if wait_s <= 0:
                raise ValueError("timeout_s must be positive")
            if not pending.event.wait(wait_s):
                raise MqttResponseTimeout(
                    f"timed out waiting for Observation task_id={command.task_id} seq={command.seq}"
                )
            if pending.observation is None:
                raise MqttTransportError("pending request completed without an Observation")
            return pending.observation
        finally:
            with self._pending_lock:
                self._pending.pop(key, None)

    def __enter__(self) -> "MqttRequestClient":
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
        result, mid = client.subscribe(self.topics.status, qos=self.config.qos)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self._connect_error = f"status subscribe failed with rc={result}"
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
            self._connect_error = "status subscription rejected"
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
            LOGGER.warning("MQTT request client disconnected: %s", reason_code)

    def _on_connect_fail(self, client: mqtt.Client, userdata: Any) -> None:
        self._reconnect_attempts += 1
        LOGGER.warning(
            "MQTT request client reconnect failed (%s/%s)",
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
            raw = json.loads(message.payload)
            observation = ObservationMessage.model_validate(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError, ValueError) as exc:
            LOGGER.warning("ignored invalid Observation payload: %s", exc)
            return
        key = (observation.task_id, observation.seq)
        with self._pending_lock:
            pending = self._pending.get(key)
            if pending is None:
                LOGGER.info("ignored uncorrelated Observation task_id=%s seq=%s", *key)
                return
            pending.observation = observation
            pending.event.set()


__all__ = [
    "MqttConfig",
    "MqttConnectionError",
    "MqttPublishObserver",
    "MqttRequestClient",
    "MqttResponseTimeout",
    "MqttTopics",
    "MqttTransportError",
]
