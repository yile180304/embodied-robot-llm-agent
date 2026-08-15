from __future__ import annotations

from embodied_agent.schemas import CommandMessage, RobotState
from embodied_agent.safety import SafetyGuard, SafetyStatus


def make_command(tool: str, params: dict, *, sent_at_ms: int = 1_000, deadline_ms: int = 10_000, seq: int = 1):
    return CommandMessage(
        version=1,
        task_id="task-safety-1",
        seq=seq,
        tool=tool,
        params=params,
        deadline_ms=deadline_ms,
        sent_at_ms=sent_at_ms,
    )


def test_front_obstacle_is_a_blocked_safety_decision():
    command = make_command("move_robot", {"distance_m": 1.0, "speed_mps": 0.2})
    decision = SafetyGuard().check(command, RobotState(front_distance_cm=18.0), 1_001)
    assert not decision.allowed
    assert decision.status is SafetyStatus.BLOCKED
    assert decision.code == "front_obstacle"


def test_unstable_pose_rejects_motion():
    command = make_command("move_robot", {"distance_m": 1.0, "speed_mps": 0.2})
    decision = SafetyGuard().check(command, RobotState(front_distance_cm=100.0, roll_deg=25.0), 1_001)
    assert not decision.allowed
    assert decision.status is SafetyStatus.REJECTED
    assert decision.code == "unsafe_roll"


def test_emergency_stop_blocks_subsequent_motion():
    command = make_command("move_robot", {"distance_m": 1.0, "speed_mps": 0.2})
    decision = SafetyGuard().check(
        command,
        RobotState(front_distance_cm=100.0, emergency_stopped=True),
        1_001,
    )
    assert decision.status is SafetyStatus.REJECTED
    assert decision.code == "emergency_stopped"


def test_expired_command_is_a_timeout_decision():
    command = make_command("get_robot_state", {}, sent_at_ms=1_000, deadline_ms=100)
    decision = SafetyGuard().check(command, RobotState(), 1_100)
    assert decision.status is SafetyStatus.TIMEOUT
    assert decision.code == "deadline_expired"
