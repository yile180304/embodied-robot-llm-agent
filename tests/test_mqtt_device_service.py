from __future__ import annotations

from types import SimpleNamespace

import paho.mqtt.client as mqtt

from embodied_agent import MqttDeviceService, ObservationMessage, ObservationStatus, RobotState, TelemetryMessage


class DeferredBackend:
    device_id = "deferred-device"

    def __init__(self) -> None:
        self.completions = []

    def submit_payload(self, payload: bytes, on_complete) -> None:
        self.completions.append(on_complete)

    def telemetry(self) -> TelemetryMessage:
        return TelemetryMessage(
            version=1,
            device_id=self.device_id,
            state=RobotState(front_distance_cm=1_000.0),
            reported_at_ms=1,
        )


class FakeClient:
    def __init__(self, **kwargs) -> None:
        self.published: list[tuple[str, str, int, bool]] = []

    def reconnect_delay_set(self, **kwargs) -> None:
        return None

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)


def test_async_backend_publishes_only_after_completion_callback() -> None:
    backend = DeferredBackend()
    service = MqttDeviceService(backend, client_factory=FakeClient)
    message = SimpleNamespace(payload=b"deferred")

    service._on_message(service._client, None, message)
    assert service._client.published == []

    backend.completions[0](
        ObservationMessage(
            version=1,
            task_id="async-task",
            seq=1,
            status=ObservationStatus.SUCCESS,
            observation={},
            received_at_ms=2,
        )
    )

    assert len(service._client.published) == 1
    assert service._client.published[0][0] == service.topics.status


def test_one_shot_observation_suppression_does_not_leak() -> None:
    backend = DeferredBackend()
    service = MqttDeviceService(backend, client_factory=FakeClient)
    message = SimpleNamespace(payload=b"deferred")
    first = ObservationMessage(
        version=1,
        task_id="suppressed-task",
        seq=1,
        status=ObservationStatus.SUCCESS,
        observation={},
        received_at_ms=2,
    )
    second = first.model_copy(update={"seq": 2, "received_at_ms": 3})

    service.suppress_next_observation()
    service._on_message(service._client, None, message)
    backend.completions[0](first)
    assert service._client.published == []

    service._on_message(service._client, None, message)
    backend.completions[1](second)
    assert len(service._client.published) == 1
    assert '"seq":2' in service._client.published[0][1]
