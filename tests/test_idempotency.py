from __future__ import annotations

from embodied_agent.device_simulator import DeviceSimulator
from embodied_agent.schemas import ObservationStatus, RobotState


def move_payload(seq: int, *, task_id: str = "task-idempotent"):
    return {
        "version": 1,
        "task_id": task_id,
        "seq": seq,
        "tool": "move_robot",
        "params": {"distance_m": 1.0, "speed_mps": 0.2},
        "deadline_ms": 10_000,
        "sent_at_ms": 1_000,
    }


def test_duplicate_qos1_delivery_executes_once_and_replays_original_result():
    simulator = DeviceSimulator(
        clock_ms=lambda: 1_001,
        initial_state=RobotState(front_distance_cm=100.0),
        obstacle_on_first_move=False,
    )
    first = simulator.process_payload(move_payload(1))
    state_after_first = simulator.state
    second = simulator.process_payload(move_payload(1))

    assert first.status is ObservationStatus.SUCCESS
    assert second.model_dump(mode="json") == first.model_dump(mode="json")
    assert simulator.state == state_after_first
    assert simulator.executed_command_count == 1
    assert simulator.commands_seen == 1


def test_same_key_with_different_payload_is_rejected_without_reexecution():
    simulator = DeviceSimulator(
        clock_ms=lambda: 1_001,
        initial_state=RobotState(front_distance_cm=100.0),
        obstacle_on_first_move=False,
    )
    simulator.process_payload(move_payload(1))
    conflict = simulator.process_payload(
        {
            **move_payload(1),
            "params": {"distance_m": 2.0, "speed_mps": 0.2},
        }
    )
    assert conflict.status is ObservationStatus.REJECTED
    assert conflict.error_code == "duplicate_conflict"
    assert simulator.state.x_m == 1.0
    assert simulator.executed_command_count == 1


def test_older_sequence_is_rejected_after_newer_sequence():
    simulator = DeviceSimulator(
        clock_ms=lambda: 1_001,
        initial_state=RobotState(front_distance_cm=100.0),
        obstacle_on_first_move=False,
    )
    simulator.process_payload(move_payload(2))
    stale = simulator.process_payload(move_payload(1))
    assert stale.status is ObservationStatus.REJECTED
    assert stale.error_code == "stale_sequence"
    assert simulator.state.x_m == 1.0
