from __future__ import annotations

import pytest
from pydantic import ValidationError
from embodied_agent.simulation.engine import SimulationEngine
from embodied_agent.simulation.faults import (
    FaultCoordinator,
    FaultFinalEvidencePayload,
    FaultPrepareEvidencePayload,
    FaultRunRequest,
    FaultRunSnapshot,
)
from embodied_agent.simulation.mission import (
    FinalPayload,
    MissionTaskCoordinator,
    MissionTaskRequest,
    MissionTaskResult,
    MissionTaskSnapshot,
    RuntimeCapabilities,
    RuntimeEvent,
    ObservationPayload,
    PublishedPayload,
    SafetyRejectedPayload,
    ToolCallPayload,
    UserGoalPayload,
)
from embodied_agent.simulation.replay import MAX_REPLAY_FRAMES, ReplayBundle, ReplayRecorder
from embodied_agent.simulation.runtime import SimulationRuntime, create_app
from embodied_agent.simulation.world import obstacle_world_config
from embodied_agent.schemas import ObservationMessage, ObservationStatus
from embodied_agent.schemas import CommandMessage
from embodied_agent.tool_registry import ToolCall


def mission_events(task_id: str, started_at_ms: int = 1_000) -> list[RuntimeEvent]:
    return [
        RuntimeEvent(
            event_id="evt-goal",
            timestamp_ms=started_at_ms,
            task_id=task_id,
            seq=0,
            phase="user_goal",
            payload=UserGoalPayload(goal="前进 1 米", planner="fake", max_steps=8),
        ),
        RuntimeEvent(
            event_id="evt-final",
            timestamp_ms=started_at_ms + 600,
            task_id=task_id,
            seq=0,
            phase="final",
            payload=FinalPayload(
                final_status="success",
                final_message="mission completed",
                duration_ms=600,
                step_count=1,
            ),
        ),
    ]


def finished_mission(task_id: str = "mission-replay") -> MissionTaskSnapshot:
    events = mission_events(task_id)
    return MissionTaskSnapshot(
        task_id=task_id,
        goal="前进 1 米",
        planner="fake",
        max_steps=8,
        status="finished",
        accepted_at_ms=1_000,
        started_at_ms=1_001,
        finished_at_ms=1_600,
        final_status="success",
        final_message="mission completed",
        events=events,
    )


def bridge_capabilities() -> RuntimeCapabilities:
    return RuntimeCapabilities(
        mode="bridge",
        device_id="dog-replay-test",
        mqtt_endpoint="127.0.0.1:1883",
        model_configured=False,
        fault_injection=True,
    )


def model_snapshot(*, include_publish: bool = True, observation_seq: int = 1) -> MissionTaskSnapshot:
    task_id = "mission-model-evidence"
    command = CommandMessage(
        version=1,
        task_id=task_id,
        seq=1,
        tool="get_robot_state",
        params={},
        deadline_ms=3_000,
        sent_at_ms=1_020,
    )
    observation = ObservationMessage(
        version=1,
        task_id=task_id,
        seq=observation_seq,
        status=ObservationStatus.SUCCESS,
        observation={"x_m": 0.0},
        received_at_ms=1_030,
    )
    events = [
        RuntimeEvent(
            event_id="evt-model-goal",
            timestamp_ms=1_000,
            task_id=task_id,
            seq=0,
            phase="user_goal",
            payload=UserGoalPayload(goal="读取状态", planner="model", max_steps=4),
        ),
        RuntimeEvent(
            event_id="evt-model-call",
            timestamp_ms=1_010,
            task_id=task_id,
            seq=1,
            phase="tool_call",
            tool="get_robot_state",
            payload=ToolCallPayload(planner_message="read state", arguments={}),
        ),
    ]
    if include_publish:
        events.append(
            RuntimeEvent(
                event_id="evt-model-publish",
                timestamp_ms=1_020,
                task_id=task_id,
                seq=1,
                phase="published",
                tool="get_robot_state",
                payload=PublishedPayload(
                    topic="robot/dog-replay-test/cmd",
                    qos=1,
                    command=command,
                    published=True,
                ),
            )
        )
    events.extend(
        [
            RuntimeEvent(
                event_id="evt-model-observation",
                timestamp_ms=1_030,
                task_id=task_id,
                seq=1,
                phase="observation",
                tool="get_robot_state",
                payload=ObservationPayload(observation=observation, latency_ms=10),
            ),
            RuntimeEvent(
                event_id="evt-model-final",
                timestamp_ms=1_040,
                task_id=task_id,
                seq=0,
                phase="final",
                payload=FinalPayload(
                    final_status="success",
                    final_message="state observed",
                    duration_ms=40,
                    step_count=1,
                ),
            ),
        ]
    )
    return MissionTaskSnapshot(
        task_id=task_id,
        goal="读取状态",
        planner="model",
        max_steps=4,
        status="finished",
        accepted_at_ms=1_000,
        started_at_ms=1_001,
        finished_at_ms=1_040,
        final_status="success",
        final_message="state observed",
        events=events,
    )


def test_replay_recorder_freezes_mission_bundle_with_start_and_terminal_frames() -> None:
    now = [1_000]
    engine = SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left"))
    recorder = ReplayRecorder(
        engine.world_config,
        bridge_capabilities(),
        clock_ms=lambda: now[0],
        id_factory=lambda prefix: f"{prefix}-fixed",
    )
    initial = engine.snapshot()
    recorder.on_frame(initial)
    events = mission_events("mission-replay")
    recorder.on_mission_event(events[0])
    recorder.on_mission_event(events[0])
    assert recorder.state == "recording"

    now[0] = 1_250
    moving = initial.model_copy(
        update={
            "revision": initial.revision + 1,
            "robot": initial.robot.model_copy(update={"gait": "walk", "x_m": 0.2}),
        }
    )
    recorder.on_frame(moving)
    now[0] = 1_600
    recorder.on_frame(moving.model_copy(update={"revision": moving.revision + 1}))
    recorder.complete_mission(finished_mission())

    bundle = recorder.current_bundle()
    assert isinstance(bundle, ReplayBundle)
    assert bundle.run.kind == "mission"
    assert bundle.run.snapshot.events == events
    assert bundle.frames[0].reason == "start"
    assert bundle.frames[-1].reason == "terminal"
    assert bundle.frame_capture.count == len(bundle.frames)
    assert bundle.truthfulness.planner == "fake_planner"
    assert bundle.truthfulness.mqtt_scope == "localhost_mqtt"
    assert recorder.state == "ready"


def test_static_fault_bundle_keeps_start_and_terminal_at_same_revision() -> None:
    now = [2_000]
    engine = SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left"))
    recorder = ReplayRecorder(engine.world_config, bridge_capabilities(), clock_ms=lambda: now[0])
    frame = engine.snapshot()
    recorder.on_frame(frame)
    accepted = FaultRunSnapshot(
        run_id="fault-replay",
        scenario="device_disconnect",
        status="running",
        accepted_at_ms=2_000,
        started_at_ms=2_001,
    )
    recorder.on_fault_started(accepted)
    now[0] = 2_500
    finished = accepted.model_copy(
        update={
            "status": "finished",
            "finished_at_ms": 2_500,
            "result": "passed",
            "summary": "fault completed",
            "evidence": [
                {
                    "event_id": "fault-prepare",
                    "timestamp_ms": 2_000,
                    "stage": "prepare",
                    "task_id": None,
                    "seq": None,
                    "payload": FaultPrepareEvidencePayload(scenario="device_disconnect"),
                },
                {
                    "event_id": "fault-final",
                    "timestamp_ms": 2_500,
                    "stage": "final",
                    "task_id": None,
                    "seq": None,
                    "payload": FaultFinalEvidencePayload(result="passed", summary="fault completed"),
                },
            ],
        }
    )
    recorder.complete_fault(FaultRunSnapshot.model_validate(finished))

    bundle = recorder.current_bundle()
    assert bundle is not None
    assert bundle.run.kind == "fault"
    assert [sample.reason for sample in bundle.frames] == ["start", "terminal"]
    assert bundle.frames[0].frame.revision == bundle.frames[1].frame.revision
    assert bundle.truthfulness.planner == "not_applicable"


def test_model_replay_marks_only_complete_correlated_native_tool_chain_verified() -> None:
    engine = SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left"))
    recorder = ReplayRecorder(engine.world_config, bridge_capabilities(), clock_ms=lambda: 1_100)
    snapshot = model_snapshot()
    recorder.on_frame(engine.snapshot())
    recorder.on_mission_event(snapshot.events[0])
    recorder.complete_mission(snapshot)

    bundle = recorder.current_bundle()
    assert bundle is not None
    assert bundle.truthfulness.planner == "openai_compatible_provider"
    assert bundle.truthfulness.model_transcript == "native_tool_calls_verified"


def test_model_replay_keeps_incomplete_or_mismatched_chain_not_verified() -> None:
    engine = SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left"))
    for snapshot in (model_snapshot(include_publish=False), model_snapshot(observation_seq=2)):
        recorder = ReplayRecorder(engine.world_config, bridge_capabilities(), clock_ms=lambda: 1_100)
        recorder.on_frame(engine.snapshot())
        recorder.on_mission_event(snapshot.events[0])
        recorder.complete_mission(snapshot)
        bundle = recorder.current_bundle()
        assert bundle is not None
        assert bundle.truthfulness.model_transcript == "not_verified"


def test_model_replay_requires_a_final_event_after_the_correlated_chain() -> None:
    engine = SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left"))
    snapshot = model_snapshot().model_copy(update={"events": model_snapshot().events[:-1]})
    recorder = ReplayRecorder(engine.world_config, bridge_capabilities(), clock_ms=lambda: 1_100)
    recorder.on_frame(engine.snapshot())
    recorder.on_mission_event(snapshot.events[0])
    recorder.complete_mission(snapshot)
    bundle = recorder.current_bundle()
    assert bundle is not None
    assert bundle.truthfulness.model_transcript == "not_verified"


def test_replay_bundle_rejects_forged_verified_truthfulness() -> None:
    engine = SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left"))
    recorder = ReplayRecorder(engine.world_config, bridge_capabilities(), clock_ms=lambda: 1_100)
    snapshot = model_snapshot(include_publish=False)
    recorder.on_frame(engine.snapshot())
    recorder.on_mission_event(snapshot.events[0])
    recorder.complete_mission(snapshot)
    bundle = recorder.current_bundle()
    assert bundle is not None
    payload = bundle.model_dump(mode="json")
    payload["truthfulness"]["model_transcript"] = "native_tool_calls_verified"
    with pytest.raises(ValidationError, match="does not match source events"):
        ReplayBundle.model_validate(payload)


def test_replay_recorder_marks_frame_overflow_without_losing_terminal_sample() -> None:
    now = [3_000]
    engine = SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left"))
    recorder = ReplayRecorder(engine.world_config, bridge_capabilities(), clock_ms=lambda: now[0])
    base = engine.snapshot()
    recorder.on_frame(base)
    recorder.on_mission_event(mission_events("mission-replay", 3_000)[0])
    for revision in range(1, MAX_REPLAY_FRAMES + 5):
        now[0] += 200
        recorder.on_frame(base.model_copy(update={"revision": revision}))
    recorder.complete_mission(finished_mission())

    bundle = recorder.current_bundle()
    assert bundle is not None
    assert len(bundle.frames) == MAX_REPLAY_FRAMES
    assert bundle.frame_capture.truncated is True
    assert bundle.frames[-1].reason == "terminal"


def test_coordinator_lifecycle_listeners_receive_completed_snapshots() -> None:
    mission_completed: list[MissionTaskSnapshot] = []

    def mission_runner(request, task_id, emit, cancellation_event):
        return MissionTaskResult(final_status="success", final_message="done", step_count=0)

    mission = MissionTaskCoordinator(mission_runner, completion_listener=mission_completed.append)
    mission.submit(MissionTaskRequest(goal="前进 1 米"))
    assert mission.wait_for_idle()
    assert len(mission_completed) == 1
    assert mission_completed[0].status == "finished"
    mission.shutdown()

    fault_started: list[FaultRunSnapshot] = []
    fault_completed: list[FaultRunSnapshot] = []

    def fault_runner(request, run_id, emit):
        emit(stage="prepare", payload=FaultPrepareEvidencePayload(scenario=request.scenario))
        return "passed", "done"

    fault = FaultCoordinator(
        fault_runner,
        run_listener=fault_started.append,
        completion_listener=fault_completed.append,
    )
    fault.submit(FaultRunRequest(scenario="duplicate_delivery"))
    assert fault.wait_for_idle()
    assert len(fault_started) == len(fault_completed) == 1
    assert fault_started[0].status == "running"
    assert fault_completed[0].status == "finished"
    assert fault_completed[0].result == "passed"
    fault.shutdown()


def test_replay_export_api_has_explicit_empty_and_ready_contracts() -> None:
    recorder = ReplayRecorder(
        SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left")).world_config,
        bridge_capabilities(),
        clock_ms=lambda: 1_000,
    )
    app = create_app(SimulationRuntime(), replay_recorder=recorder)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        empty = client.get("/api/replays/current")
        assert empty.status_code == 204

        frame = app.state.simulation.engine.snapshot()
        recorder.on_frame(frame)
        recorder.on_mission_event(mission_events("mission-api")[0])
        recorder.complete_mission(finished_mission("mission-api"))
        exported = client.get("/api/replays/current")
        assert exported.status_code == 200
        payload = exported.json()
        assert payload["type"] == "simulation.replay"
        assert payload["run"]["snapshot"]["task_id"] == "mission-api"


def test_replay_export_api_rejects_active_capture() -> None:
    recorder = ReplayRecorder(
        SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left")).world_config,
        bridge_capabilities(),
        clock_ms=lambda: 1_000,
    )
    app = create_app(SimulationRuntime(), replay_recorder=recorder)
    recorder.on_frame(app.state.simulation.engine.snapshot())
    recorder.on_mission_event(mission_events("mission-active")[0])
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get("/api/replays/current")
        assert response.status_code == 409
        assert response.json()["error_code"] == "evidence_active"


def test_replay_export_api_rejects_invalid_capture() -> None:
    recorder = ReplayRecorder(
        SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left")).world_config,
        bridge_capabilities(),
        clock_ms=lambda: 1_000,
    )
    app = create_app(SimulationRuntime(), replay_recorder=recorder)
    recorder.complete_mission(finished_mission("mission-missing-capture"))
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get("/api/replays/current")
        assert response.status_code == 409
        assert response.json()["error_code"] == "evidence_invalid"


def test_blocked_and_safety_rejection_mission_bundles_are_schema_valid() -> None:
    frame = SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left")).snapshot()
    blocked_observation = ObservationMessage(
        version=1,
        task_id="mission-blocked",
        seq=1,
        status=ObservationStatus.BLOCKED,
        observation={"reason": "front_obstacle", "moved_distance_m": 0.35},
        received_at_ms=1_100,
    )
    blocked_event = RuntimeEvent(
        event_id="evt-blocked",
        timestamp_ms=1_100,
        task_id="mission-blocked",
        seq=1,
        phase="observation",
        tool="move_robot",
        payload=ObservationPayload(observation=blocked_observation, latency_ms=10),
    )
    blocked_snapshot = finished_mission("mission-blocked").model_copy(
        update={"events": [mission_events("mission-blocked")[0], blocked_event, mission_events("mission-blocked")[1]]}
    )
    safety_observation = ObservationMessage(
        version=1,
        task_id="mission-safety",
        seq=1,
        status=ObservationStatus.REJECTED,
        error_code="dangerous_parameter",
        error_message="speed exceeds safety limit",
        received_at_ms=1_100,
    )
    safety_event = RuntimeEvent(
        event_id="evt-safety",
        timestamp_ms=1_100,
        task_id="mission-safety",
        seq=1,
        phase="safety_rejected",
        tool="move_robot",
        payload=SafetyRejectedPayload(
            tool_call=ToolCall(name="move_robot", arguments={"distance_m": 10.0, "speed_mps": 5.0}),
            observation=safety_observation,
            published=False,
        ),
    )
    safety_snapshot = finished_mission("mission-safety").model_copy(
        update={
            "final_status": "rejected",
            "final_message": "dangerous parameter rejected",
            "events": [mission_events("mission-safety")[0], safety_event, mission_events("mission-safety")[1]],
        }
    )

    for snapshot in (blocked_snapshot, safety_snapshot):
        recorder = ReplayRecorder(
            SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left")).world_config,
            bridge_capabilities(),
            clock_ms=lambda: 1_200,
        )
        recorder.on_frame(frame)
        recorder.on_mission_event(snapshot.events[0])
        recorder.complete_mission(snapshot)
        bundle = recorder.current_bundle()
        assert bundle is not None
        assert ReplayBundle.model_validate(bundle.model_dump(mode="json")).run.kind == "mission"
