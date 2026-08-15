from __future__ import annotations

from embodied_agent.device_simulator import DeviceSimulator
from embodied_agent.schemas import ObservationStatus, RobotState


def command(task_id: str, seq: int, tool: str, params: dict, *, sent_at_ms: int = 1_000, deadline_ms: int = 10_000):
    return {
        "version": 1,
        "task_id": task_id,
        "seq": seq,
        "tool": tool,
        "params": params,
        "deadline_ms": deadline_ms,
        "sent_at_ms": sent_at_ms,
    }


def test_valid_move_changes_state_and_returns_success(clear_state):
    simulator = DeviceSimulator(
        clock_ms=lambda: 1_001,
        initial_state=clear_state,
        obstacle_on_first_move=False,
    )
    result = simulator.process_payload(
        command("task-success", 1, "move_robot", {"distance_m": 1.0, "speed_mps": 0.2})
    )
    assert result.status is ObservationStatus.SUCCESS
    assert simulator.state.x_m == 1.0
    assert result.observation["moved_distance_m"] == 1.0
    assert result.observation["last_seq"] == 1


def test_first_forward_move_returns_structured_blocked_observation():
    simulator = DeviceSimulator(clock_ms=lambda: 1_001)
    result = simulator.process_payload(
        command("task-blocked", 1, "move_robot", {"distance_m": 2.0, "speed_mps": 0.2})
    )
    assert result.status is ObservationStatus.BLOCKED
    assert result.error_code == "front_obstacle"
    assert result.observation["front_distance_cm"] == 18.0
    assert result.observation["left_distance_cm"] == 120.0
    assert result.observation["right_distance_cm"] == 35.0
    assert result.observation["last_seq"] == 1
    assert simulator.state.x_m == 0.0


def test_turning_toward_wider_side_then_moving_can_succeed():
    simulator = DeviceSimulator(clock_ms=lambda: 1_001)
    blocked = simulator.process_payload(
        command("task-replan", 1, "move_robot", {"distance_m": 2.0, "speed_mps": 0.2})
    )
    assert blocked.status is ObservationStatus.BLOCKED
    turned = simulator.process_payload(
        command("task-replan", 2, "turn_robot", {"angle_deg": 90.0, "angular_speed_dps": 45.0})
    )
    moved = simulator.process_payload(
        command("task-replan", 3, "move_robot", {"distance_m": 1.0, "speed_mps": 0.2})
    )
    assert turned.status is ObservationStatus.SUCCESS
    assert moved.status is ObservationStatus.SUCCESS
    assert simulator.state.y_m == 1.0


def test_expired_command_returns_timeout_without_state_change():
    simulator = DeviceSimulator(clock_ms=lambda: 2_000, initial_state=RobotState(front_distance_cm=100.0))
    result = simulator.process_payload(
        command("task-timeout", 1, "move_robot", {"distance_m": 1.0, "speed_mps": 0.2}, deadline_ms=100)
    )
    assert result.status is ObservationStatus.TIMEOUT
    assert result.error_code == "deadline_expired"
    assert simulator.state.x_m == 0.0


def test_dangerous_raw_parameters_are_rejected_before_execution():
    simulator = DeviceSimulator(clock_ms=lambda: 1_001)
    result = simulator.process_payload(
        command("task-dangerous", 1, "move_robot", {"distance_m": 10.0, "speed_mps": 5.0})
    )
    assert result.status is ObservationStatus.REJECTED
    assert result.error_code == "dangerous_parameter"
    assert simulator.state.x_m == 0.0
    assert simulator.executed_command_count == 0


def test_emergency_stop_is_terminal_for_motion_until_operator_reset(clear_state):
    simulator = DeviceSimulator(
        clock_ms=lambda: 1_001,
        initial_state=clear_state,
        obstacle_on_first_move=False,
    )
    stopped = simulator.process_payload(
        command("task-stop", 1, "emergency_stop", {"reason": "tilt alarm"})
    )
    rejected = simulator.process_payload(
        command("task-stop", 2, "move_robot", {"distance_m": 1.0, "speed_mps": 0.2})
    )
    assert stopped.status is ObservationStatus.EMERGENCY_STOP
    assert stopped.observation["emergency_stopped"] is True
    assert rejected.status is ObservationStatus.REJECTED
    assert rejected.error_code == "emergency_stopped"
    simulator.clear_emergency_stop()
    resumed = simulator.process_payload(
        command("task-stop", 3, "move_robot", {"distance_m": 1.0, "speed_mps": 0.2})
    )
    assert resumed.status is ObservationStatus.SUCCESS
