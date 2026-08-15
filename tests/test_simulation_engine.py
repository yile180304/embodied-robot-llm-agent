import pytest

from embodied_agent.simulation import (
    DEFAULT_TICK_MS,
    SimulationAction,
    SimulationEngine,
    obstacle_world_config,
)


def _run_demo(engine: SimulationEngine):
    frames = [engine.snapshot()]
    for _ in range(140):
        frames.append(engine.advance(DEFAULT_TICK_MS))
    return frames


def test_demo_program_reaches_expected_pose() -> None:
    final = _run_demo(SimulationEngine())[-1]

    assert final.robot.x_m == pytest.approx(2.0)
    assert final.robot.y_m == pytest.approx(1.0)
    assert final.robot.yaw_deg == pytest.approx(90.0)
    assert final.robot.gait == "stopped"
    assert final.active_command is None
    assert final.sim_time_ms == 7_000


def test_same_ticks_produce_identical_frames() -> None:
    first = [frame.model_dump(mode="json") for frame in _run_demo(SimulationEngine())]
    second = [frame.model_dump(mode="json") for frame in _run_demo(SimulationEngine())]

    assert first == second


def test_revision_is_monotonic_and_reset_does_not_go_backwards() -> None:
    engine = SimulationEngine()
    revisions = [engine.snapshot().revision]
    revisions.extend(engine.advance().revision for _ in range(5))
    reset = engine.reset()

    assert revisions == sorted(set(revisions))
    assert reset.revision > revisions[-1]
    assert reset.sim_time_ms == 0
    assert reset.robot.x_m == 0.0
    assert reset.robot.y_m == 0.0
    assert reset.robot.yaw_deg == 0.0
    assert reset.active_command is not None
    assert reset.active_command.seq == 1
    assert reset.active_command.progress == 0.0


def test_pause_freezes_time_pose_and_progress_until_resume() -> None:
    engine = SimulationEngine()
    before_pause = engine.advance()
    paused = engine.pause()
    frozen = engine.advance()

    assert engine.paused is True
    assert frozen == paused
    assert frozen.sim_time_ms == before_pause.sim_time_ms
    assert frozen.robot.x_m == before_pause.robot.x_m
    assert frozen.active_command == before_pause.active_command

    resumed = engine.resume()
    after_resume = engine.advance()
    assert engine.paused is False
    assert resumed.revision > paused.revision
    assert after_resume.sim_time_ms == before_pause.sim_time_ms + DEFAULT_TICK_MS
    assert after_resume.robot.x_m > before_pause.robot.x_m


def test_external_move_stops_before_obstacle_with_blocked_completion() -> None:
    engine = SimulationEngine(demo_mode=False, world_config=obstacle_world_config())
    engine.submit_action(SimulationAction("blocked-task", 1, "move_robot", 2.0, 0.5))

    for _ in range(15):
        frame = engine.advance()

    completion = engine.take_action_completion()
    assert completion is not None
    assert completion.status == "blocked"
    assert completion.reason == "front_obstacle"
    assert completion.moved_amount == pytest.approx(0.35)
    assert completion.remaining_amount == pytest.approx(1.65)
    assert frame.robot.x_m == pytest.approx(0.35)
    assert frame.sensors.front_distance_cm == pytest.approx(25.0)
    assert frame.active_command is None


def test_turn_and_reset_refresh_spatial_sensors_from_authoritative_pose() -> None:
    engine = SimulationEngine(demo_mode=False, world_config=obstacle_world_config())
    initial = engine.snapshot()
    engine.submit_action(SimulationAction("turn-task", 1, "turn_robot", 90.0, 90.0))

    for _ in range(20):
        turned = engine.advance()

    completion = engine.take_action_completion()
    assert completion is not None
    assert completion.status == "success"
    assert turned.robot.yaw_deg == pytest.approx(90.0)
    assert turned.sensors.front_distance_cm == pytest.approx(initial.sensors.left_distance_cm)

    reset = engine.reset()
    assert reset.robot.x_m == 0.0
    assert reset.robot.y_m == 0.0
    assert reset.robot.yaw_deg == 0.0
    assert reset.sensors == initial.sensors


@pytest.mark.parametrize("dt_ms", [0, -1, True, 1.5])
def test_advance_rejects_invalid_time_steps(dt_ms) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        SimulationEngine().advance(dt_ms)
