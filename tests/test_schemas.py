from __future__ import annotations

import pytest
from pydantic import ValidationError

from embodied_agent.schemas import (
    CommandMessage,
    DeviceEventMessage,
    ObservationMessage,
    ObservationStatus,
    RobotState,
    TelemetryMessage,
    schema_bundle,
)


def command_payload(**overrides):
    payload = {
        "version": 1,
        "task_id": "task-schema-1",
        "seq": 1,
        "tool": "move_robot",
        "params": {"distance_m": 1.0, "speed_mps": 0.2},
        "deadline_ms": 3_000,
        "sent_at_ms": 1_000,
    }
    payload.update(overrides)
    return payload


def test_command_schema_normalizes_and_exports_json_schema():
    command = CommandMessage.model_validate(command_payload())
    assert command.params == {"distance_m": 1.0, "speed_mps": 0.2}
    bundle = schema_bundle()
    assert set(bundle) == {"command", "observation", "telemetry", "event", "tool_params"}
    assert bundle["command"]["additionalProperties"] is False
    assert "move_robot" in bundle["tool_params"]


@pytest.mark.parametrize(
    "field",
    ["task_id", "seq", "tool", "params", "deadline_ms", "sent_at_ms", "version"],
)
def test_required_command_fields_are_required(field):
    payload = command_payload()
    payload.pop(field)
    with pytest.raises(ValidationError):
        CommandMessage.model_validate(payload)


def test_extra_command_and_param_fields_are_rejected():
    with pytest.raises(ValidationError):
        CommandMessage.model_validate(command_payload(unexpected=True))
    with pytest.raises(ValidationError):
        CommandMessage.model_validate(
            command_payload(params={"distance_m": 1.0, "speed_mps": 0.2, "pwm": 1000})
        )


@pytest.mark.parametrize(
    "params",
    [
        {"distance_m": 10.0, "speed_mps": 0.2},
        {"distance_m": 1.0, "speed_mps": 5.0},
        {"distance_m": 1.0, "speed_mps": 0.01},
    ],
)
def test_motion_ranges_are_rejected(params):
    with pytest.raises(ValidationError):
        CommandMessage.model_validate(command_payload(params=params))


def test_unknown_tool_and_missing_tool_parameter_are_rejected():
    with pytest.raises(ValidationError):
        CommandMessage.model_validate(command_payload(tool="run_python", params={}))
    with pytest.raises(ValidationError):
        CommandMessage.model_validate(command_payload(params={"distance_m": 1.0}))


def test_expiry_is_checked_with_an_injected_clock():
    command = CommandMessage.model_validate(command_payload(sent_at_ms=1_000, deadline_ms=100))
    assert command.is_expired(1_100)
    assert not command.is_expired(1_099)


def test_observation_and_telemetry_contracts_forbid_unknown_fields():
    observation = ObservationMessage(
        version=1,
        task_id="task-observation-1",
        seq=1,
        status=ObservationStatus.BLOCKED,
        observation={"front_distance_cm": 18.0},
        received_at_ms=1_000,
    )
    assert observation.status.value == "blocked"
    telemetry = TelemetryMessage(
        version=1,
        device_id="dog01",
        state=RobotState(),
        reported_at_ms=1_000,
    )
    assert telemetry.state.front_distance_cm == 18.0
    with pytest.raises(ValidationError):
        ObservationMessage(
            version=1,
            task_id="task-observation-1",
            seq=1,
            status="success",
            observation={},
            received_at_ms=1_000,
            extra_field="reject-me",
        )


def test_device_event_schema_is_strict_and_versioned():
    event = DeviceEventMessage(
        version=1,
        device_id="dog01",
        event="device_offline",
        reported_at_ms=123,
    )
    assert event.details == {}
    with pytest.raises(ValidationError):
        DeviceEventMessage(
            version=1,
            device_id="dog01",
            event="device_offline",
            reported_at_ms=123,
            unexpected=True,
        )
