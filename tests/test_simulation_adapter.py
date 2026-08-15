from __future__ import annotations

import json

import pytest

from embodied_agent import CommandMessage, ObservationStatus
from embodied_agent.simulation import (
    SimulationAdapter,
    SimulationEngine,
    WorldConfig,
    obstacle_world_config,
)
from embodied_agent.simulation.runtime import SimulationRuntime


def make_command(
    task_id: str,
    seq: int,
    tool: str,
    params: dict,
    *,
    sent_at_ms: int = 1_000,
    deadline_ms: int = 10_000,
) -> CommandMessage:
    return CommandMessage(
        version=1,
        task_id=task_id,
        seq=seq,
        tool=tool,
        params=params,
        sent_at_ms=sent_at_ms,
        deadline_ms=deadline_ms,
    )


def bridge(*, queue_size: int = 8, world_config: WorldConfig | None = None):
    clock = [1_000]
    engine = SimulationEngine(demo_mode=False, world_config=world_config)
    adapter = SimulationAdapter(engine, queue_size=queue_size, clock_ms=lambda: clock[0])
    runtime = SimulationRuntime(engine, adapter=adapter)
    return clock, engine, adapter, runtime


def submit(adapter: SimulationAdapter, command: CommandMessage, results: list) -> None:
    adapter.submit_payload(command.model_dump_json(), results.append)


def run_ticks(runtime: SimulationRuntime, clock: list[int], count: int) -> None:
    for _ in range(count):
        runtime.tick_once(now_ms=clock[0])
        clock[0] += runtime.tick_ms


def test_external_move_emits_progress_then_success_observation() -> None:
    clock, engine, adapter, runtime = bridge()
    results = []
    command = make_command(
        "move-task",
        1,
        "move_robot",
        {"distance_m": 1.0, "speed_mps": 0.5},
    )

    submit(adapter, command, results)
    run_ticks(runtime, clock, 10)
    middle = engine.snapshot()

    assert results == []
    assert middle.active_command is not None
    assert middle.active_command.task_id == "move-task"
    assert middle.active_command.progress == pytest.approx(0.25)
    assert middle.robot.x_m == pytest.approx(0.25)

    run_ticks(runtime, clock, 30)
    final = engine.snapshot()

    assert len(results) == 1
    assert results[0].status is ObservationStatus.SUCCESS
    assert results[0].observation["state"]["x_m"] == pytest.approx(1.0)
    assert final.active_command is None
    assert final.robot.x_m == pytest.approx(1.0)
    assert adapter.executed_motion_count == 1
    assert [entry["event"] for entry in adapter.event_log] == [
        "receive",
        "queued",
        "started",
        "completed",
    ]


def test_pending_duplicate_and_completed_replay_share_one_motion() -> None:
    clock, engine, adapter, runtime = bridge()
    command = make_command(
        "duplicate-task",
        1,
        "move_robot",
        {"distance_m": 0.5, "speed_mps": 0.5},
    )
    first: list = []
    second: list = []
    replay: list = []

    submit(adapter, command, first)
    submit(adapter, command, second)
    run_ticks(runtime, clock, 20)
    submit(adapter, command, replay)

    assert len(first) == len(second) == len(replay) == 1
    assert first[0].model_dump(mode="json") == second[0].model_dump(mode="json")
    assert replay[0].model_dump(mode="json") == first[0].model_dump(mode="json")
    assert engine.snapshot().robot.x_m == pytest.approx(0.5)
    assert adapter.executed_motion_count == 1
    assert "duplicate_pending" in {entry["event"] for entry in adapter.event_log}
    assert "duplicate" in {entry["event"] for entry in adapter.event_log}


def test_blocked_completion_replays_without_moving_again_and_scan_matches_frame() -> None:
    clock, engine, adapter, runtime = bridge(world_config=obstacle_world_config("left"))
    command = make_command(
        "blocked-task",
        1,
        "move_robot",
        {"distance_m": 2.0, "speed_mps": 0.5},
    )
    first: list = []
    replay: list = []
    scan: list = []

    submit(adapter, command, first)
    run_ticks(runtime, clock, 20)
    blocked_frame = engine.snapshot()
    submit(adapter, command, replay)
    submit(
        adapter,
        make_command("blocked-task", 2, "scan_obstacles", {}, sent_at_ms=clock[0]),
        scan,
    )

    assert len(first) == len(replay) == len(scan) == 1
    assert first[0].status is ObservationStatus.BLOCKED
    assert first[0].error_code == "front_obstacle"
    assert first[0].observation["reason"] == "front_obstacle"
    assert first[0].observation["requested_distance_m"] == pytest.approx(2.0)
    assert first[0].observation["moved_distance_m"] == pytest.approx(0.35)
    assert first[0].observation["remaining_distance_m"] == pytest.approx(1.65)
    assert first[0].observation["state"]["x_m"] == pytest.approx(blocked_frame.robot.x_m)
    assert first[0].observation["front_distance_cm"] == pytest.approx(
        blocked_frame.sensors.front_distance_cm
    )
    assert first[0].observation["left_distance_cm"] == pytest.approx(
        blocked_frame.sensors.left_distance_cm
    )
    assert first[0].observation["right_distance_cm"] == pytest.approx(
        blocked_frame.sensors.right_distance_cm
    )
    assert replay[0].model_dump(mode="json") == first[0].model_dump(mode="json")
    assert scan[0].observation["front_distance_cm"] == pytest.approx(
        blocked_frame.sensors.front_distance_cm
    )
    assert scan[0].observation["left_distance_cm"] == pytest.approx(
        blocked_frame.sensors.left_distance_cm
    )
    assert scan[0].observation["right_distance_cm"] == pytest.approx(
        blocked_frame.sensors.right_distance_cm
    )
    assert engine.snapshot().robot.x_m == pytest.approx(0.35)
    assert adapter.executed_motion_count == 1
    assert "blocked" in {entry["event"] for entry in adapter.event_log}
    assert "duplicate" in {entry["event"] for entry in adapter.event_log}


def test_conflict_stale_and_queue_full_are_rejected_without_motion() -> None:
    _, engine, adapter, _ = bridge(queue_size=1)
    accepted: list = []
    conflict: list = []
    stale: list = []
    busy: list = []
    first = make_command(
        "ordered-task",
        2,
        "move_robot",
        {"distance_m": 1.0, "speed_mps": 0.5},
    )

    submit(adapter, first, accepted)
    submit(
        adapter,
        first.model_copy(update={"params": {"distance_m": 0.5, "speed_mps": 0.5}}),
        conflict,
    )
    submit(
        adapter,
        make_command("ordered-task", 1, "get_robot_state", {}),
        stale,
    )
    submit(
        adapter,
        make_command(
            "other-task",
            1,
            "move_robot",
            {"distance_m": 0.5, "speed_mps": 0.5},
        ),
        busy,
    )

    assert accepted == []
    assert conflict[0].error_code == "duplicate_conflict"
    assert stale[0].error_code == "stale_sequence"
    assert busy[0].error_code == "device_busy"
    assert engine.snapshot().robot.x_m == 0.0
    assert adapter.executed_motion_count == 0


def test_expired_and_insufficient_timeout_budget_do_not_enter_engine() -> None:
    clock, engine, adapter, runtime = bridge()
    expired: list = []
    insufficient: list = []
    clock[0] = 2_000

    submit(
        adapter,
        make_command("expired", 1, "get_robot_state", {}, sent_at_ms=1_000, deadline_ms=500),
        expired,
    )
    submit(
        adapter,
        make_command(
            "budget",
            1,
            "move_robot",
            {"distance_m": 1.0, "speed_mps": 0.5, "timeout_ms": 500},
            sent_at_ms=2_000,
        ),
        insufficient,
    )
    run_ticks(runtime, clock, 1)

    assert expired[0].status is ObservationStatus.TIMEOUT
    assert insufficient[0].status is ObservationStatus.TIMEOUT
    assert insufficient[0].error_code == "action_timeout_budget"
    assert engine.snapshot().robot.x_m == 0.0
    assert adapter.executed_motion_count == 0


def test_pause_freezes_progress_but_wall_clock_deadline_still_cancels() -> None:
    clock, engine, adapter, runtime = bridge()
    results: list = []
    submit(
        adapter,
        make_command(
            "pause-timeout",
            1,
            "move_robot",
            {"distance_m": 1.0, "speed_mps": 0.5},
            deadline_ms=3_000,
        ),
        results,
    )
    run_ticks(runtime, clock, 5)
    runtime.pause()
    frozen_x = engine.snapshot().robot.x_m
    clock[0] = 4_100
    runtime.tick_once(now_ms=clock[0])

    assert results[0].status is ObservationStatus.TIMEOUT
    assert results[0].error_code == "deadline_expired"
    assert engine.snapshot().robot.x_m == frozen_x
    assert engine.snapshot().active_command is None


def test_emergency_stop_completes_active_queued_and_stop_requests() -> None:
    clock, engine, adapter, runtime = bridge()
    active: list = []
    queued: list = []
    stopped: list = []
    submit(
        adapter,
        make_command(
            "active-task",
            1,
            "move_robot",
            {"distance_m": 2.0, "speed_mps": 0.5},
        ),
        active,
    )
    run_ticks(runtime, clock, 2)
    submit(
        adapter,
        make_command(
            "queued-task",
            1,
            "turn_robot",
            {"angle_deg": 90.0, "angular_speed_dps": 90.0},
            sent_at_ms=clock[0],
        ),
        queued,
    )
    submit(
        adapter,
        make_command(
            "stop-task",
            1,
            "emergency_stop",
            {"reason": "operator test"},
            sent_at_ms=clock[0],
        ),
        stopped,
    )
    runtime.tick_once(now_ms=clock[0])

    assert active[0].status is ObservationStatus.EMERGENCY_STOP
    assert active[0].error_code == "emergency_cancelled"
    assert queued[0].status is ObservationStatus.REJECTED
    assert queued[0].error_code == "emergency_stopped"
    assert stopped[0].status is ObservationStatus.EMERGENCY_STOP
    assert engine.snapshot().robot.emergency_stopped is True
    assert engine.snapshot().active_command is None
    assert adapter.pending_count == 0


def test_task_scoped_cancel_stops_only_matching_active_command() -> None:
    clock, engine, adapter, runtime = bridge()
    cancelled: list = []
    other: list = []
    submit(
        adapter,
        make_command(
            "cancel-target",
            1,
            "move_robot",
            {"distance_m": 2.0, "speed_mps": 0.5},
        ),
        cancelled,
    )
    run_ticks(runtime, clock, 2)
    submit(
        adapter,
        make_command(
            "other-task",
            1,
            "turn_robot",
            {"angle_deg": 30.0, "angular_speed_dps": 90.0},
            sent_at_ms=clock[0],
        ),
        other,
    )
    assert adapter.cancel_task("cancel-target") is True
    runtime.tick_once(now_ms=clock[0])

    assert cancelled[0].status is ObservationStatus.REJECTED
    assert cancelled[0].error_code == "operator_cancelled"
    assert engine.snapshot().robot.emergency_stopped is False
    assert engine.snapshot().active_command is not None
    assert engine.snapshot().active_command.task_id == "other-task"
    assert adapter.executed_motion_count == 2


def test_cancel_before_command_arrival_rejects_late_motion_without_execution() -> None:
    _, engine, adapter, _ = bridge()
    results: list = []
    assert adapter.cancel_task("late-task") is False
    submit(
        adapter,
        make_command(
            "late-task",
            1,
            "move_robot",
            {"distance_m": 1.0, "speed_mps": 0.5},
        ),
        results,
    )

    assert results[0].status is ObservationStatus.REJECTED
    assert results[0].error_code == "operator_cancelled"
    assert adapter.pending_count == 0
    assert adapter.executed_motion_count == 0
    assert engine.snapshot().robot.x_m == 0.0


def test_emergency_queue_wins_over_task_scoped_cancel_on_same_tick() -> None:
    clock, engine, adapter, runtime = bridge()
    active: list = []
    emergency: list = []
    submit(
        adapter,
        make_command(
            "race-target",
            1,
            "move_robot",
            {"distance_m": 2.0, "speed_mps": 0.5},
        ),
        active,
    )
    run_ticks(runtime, clock, 2)
    assert adapter.cancel_task("race-target") is True
    submit(
        adapter,
        make_command(
            "race-stop",
            1,
            "emergency_stop",
            {"reason": "race"},
            sent_at_ms=clock[0],
        ),
        emergency,
    )
    runtime.tick_once(now_ms=clock[0])

    assert active[0].status is ObservationStatus.EMERGENCY_STOP
    assert active[0].error_code == "emergency_cancelled"
    assert emergency[0].status is ObservationStatus.EMERGENCY_STOP
    assert engine.snapshot().robot.emergency_stopped is True


def test_reset_completes_pending_callbacks_and_restores_initial_world() -> None:
    clock, engine, adapter, runtime = bridge()
    active: list = []
    queued: list = []
    submit(
        adapter,
        make_command(
            "reset-active",
            1,
            "move_robot",
            {"distance_m": 2.0, "speed_mps": 0.5},
        ),
        active,
    )
    run_ticks(runtime, clock, 2)
    submit(
        adapter,
        make_command(
            "reset-queued",
            1,
            "turn_robot",
            {"angle_deg": 90.0, "angular_speed_dps": 90.0},
            sent_at_ms=clock[0],
        ),
        queued,
    )

    frame = runtime.reset()

    assert active[0].error_code == "simulation_reset"
    assert queued[0].error_code == "simulation_reset"
    assert frame.robot.x_m == 0.0
    assert frame.robot.y_m == 0.0
    assert frame.robot.yaw_deg == 0.0
    assert frame.active_command is None
    assert adapter.pending_count == 0


def test_state_and_scan_queries_map_latest_simulation_frame() -> None:
    _, engine, adapter, _ = bridge()
    state: list = []
    scan: list = []
    submit(adapter, make_command("query", 1, "get_robot_state", {}), state)
    submit(adapter, make_command("query", 2, "scan_obstacles", {}), scan)

    assert state[0].status is ObservationStatus.SUCCESS
    assert state[0].observation["state"]["x_m"] == 0.0
    frame = engine.snapshot()
    assert scan[0].observation["front_distance_cm"] == frame.sensors.front_distance_cm
    assert scan[0].observation["left_distance_cm"] == frame.sensors.left_distance_cm
    assert scan[0].observation["right_distance_cm"] == frame.sensors.right_distance_cm


def test_invalid_payload_returns_structured_rejection() -> None:
    _, _, adapter, _ = bridge()
    results: list = []
    adapter.submit_payload(json.dumps({"task_id": "invalid", "seq": 1}).encode(), results.append)

    assert results[0].status is ObservationStatus.REJECTED
    assert results[0].error_code == "schema_validation_error"
