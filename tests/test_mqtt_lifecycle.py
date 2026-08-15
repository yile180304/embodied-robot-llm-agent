from __future__ import annotations

from dataclasses import dataclass

import pytest

from embodied_agent import DeviceSimulator, MqttConfig, MqttDeviceService, MqttRequestClient, MqttTopics


@dataclass
class DisconnectRecorder:
    calls: int = 0

    def disconnect(self):
        self.calls += 1
        return 0


def test_mqtt_config_requires_finite_positive_reconnect_budget():
    with pytest.raises(ValueError):
        MqttConfig(max_reconnect_attempts=0)


def test_request_client_stops_retrying_after_configured_connect_failures():
    client = MqttRequestClient(
        MqttConfig(max_reconnect_attempts=2),
        MqttTopics("dog-reconnect-agent"),
    )
    recorder = DisconnectRecorder()

    client._on_connect_fail(recorder, None)
    assert client.reconnect_exhausted is False
    client._on_connect_fail(recorder, None)

    assert client.reconnect_attempts == 2
    assert client.reconnect_exhausted is True
    assert recorder.calls == 1


def test_device_service_stops_retrying_after_configured_connect_failures():
    simulator = DeviceSimulator(device_id="dog-reconnect-device")
    service = MqttDeviceService(
        simulator,
        MqttConfig(max_reconnect_attempts=2),
        MqttTopics(simulator.device_id),
    )
    recorder = DisconnectRecorder()

    service._on_connect_fail(recorder, None)
    service._on_connect_fail(recorder, None)

    assert service.reconnect_attempts == 2
    assert service.reconnect_exhausted is True
    assert recorder.calls == 1
