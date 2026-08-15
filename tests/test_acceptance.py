from __future__ import annotations

import hashlib
import json

import pytest
from types import SimpleNamespace

from embodied_agent.cli import build_parser
from embodied_agent.simulation.acceptance import (
    SCENARIO_IDS,
    AcceptanceScenarioOutcome,
    _real_model_outcome,
    write_acceptance_pack,
)
from embodied_agent.simulation.engine import SimulationEngine
from embodied_agent.simulation.mission import FinalPayload, MissionTaskSnapshot, RuntimeEvent, UserGoalPayload
from embodied_agent.simulation.mission import ObservationPayload, PublishedPayload, ToolCallPayload
from embodied_agent.simulation.replay import (
    MissionReplayRun,
    ReplayBundle,
    ReplayFrameCapture,
    ReplayFrameSample,
    TruthfulnessProfile,
)
from embodied_agent.simulation.world import obstacle_world_config
from embodied_agent.schemas import CommandMessage, ObservationMessage, ObservationStatus


def replay_bundle() -> ReplayBundle:
    frame = SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left")).snapshot()
    events = [
        RuntimeEvent(
            event_id="evt-goal",
            timestamp_ms=1_000,
            task_id="mission-acceptance",
            seq=0,
            phase="user_goal",
            payload=UserGoalPayload(goal="读取当前机器人状态", planner="fake", max_steps=4),
        ),
        RuntimeEvent(
            event_id="evt-final",
            timestamp_ms=1_100,
            task_id="mission-acceptance",
            seq=0,
            phase="final",
            payload=FinalPayload(
                final_status="success",
                final_message="done",
                duration_ms=100,
                step_count=1,
            ),
        ),
    ]
    snapshot = MissionTaskSnapshot(
        task_id="mission-acceptance",
        goal="读取当前机器人状态",
        planner="fake",
        max_steps=4,
        status="finished",
        accepted_at_ms=1_000,
        started_at_ms=1_001,
        finished_at_ms=1_100,
        final_status="success",
        final_message="done",
        events=events,
    )
    return ReplayBundle(
        replay_id="replay-acceptance",
        created_at_ms=1_100,
        run=MissionReplayRun(snapshot=snapshot),
        world=obstacle_world_config("left"),
        frame_capture=ReplayFrameCapture(count=2, truncated=False),
        frames=[
            ReplayFrameSample(offset_ms=0, reason="start", frame=frame),
            ReplayFrameSample(offset_ms=100, reason="terminal", frame=frame),
        ],
        truthfulness=TruthfulnessProfile(
            planner="fake_planner",
            mqtt_scope="localhost_mqtt",
            model_transcript="not_verified",
            claims=("Python simulation only",),
        ),
    )


def test_acceptance_pack_writes_recomputable_bundle_hash_and_reports(tmp_path) -> None:
    outcomes = [
        AcceptanceScenarioOutcome(SCENARIO_IDS[0], "passed", bundle=replay_bundle()),
        *[
            AcceptanceScenarioOutcome(scenario_id, "skipped", reason="not_run_in_unit_test")
            for scenario_id in SCENARIO_IDS[1:-1]
        ],
        AcceptanceScenarioOutcome(
            "real_model_tool_calling",
            "skipped",
            reason="provider_unconfigured",
        ),
    ]

    pack = write_acceptance_pack(
        tmp_path,
        device_id="dog-accept-test",
        outcomes=outcomes,
        generated_at_ms=2_000,
    )

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    passed = manifest["scenarios"][0]
    bundle_path = tmp_path / passed["bundle_path"]
    assert passed["sha256"] == hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    assert pack.truthfulness.passed == 1
    assert pack.truthfulness.failed == 0
    assert pack.truthfulness.skipped == 9
    assert pack.exit_code == 0
    assert (tmp_path / "truthfulness.json").is_file()
    markdown = (tmp_path / "truthfulness.md").read_text(encoding="utf-8")
    assert "Python simulation ground truth" in markdown
    assert "provider_unconfigured" in markdown


def test_acceptance_pack_rejects_reordered_or_partial_registry(tmp_path) -> None:
    with pytest.raises(ValueError, match="fixed scenario registry"):
        write_acceptance_pack(
            tmp_path,
            device_id="dog-accept-test",
            outcomes=[AcceptanceScenarioOutcome("semantic_mission", "skipped", reason="partial")],
        )


def test_cli_exposes_fixed_simulation_acceptance_command() -> None:
    args = build_parser().parse_args(
        [
            "simulation-acceptance",
            "--device-id",
            "dog-accept-cli",
            "--output",
            "reports/acceptance-test",
        ]
    )
    assert args.command == "simulation-acceptance"
    assert args.device_id == "dog-accept-cli"
    assert args.include_real_model is False
    assert str(args.output).replace("\\", "/") == "reports/acceptance-test"

    opted_in = build_parser().parse_args(["simulation-acceptance", "--include-real-model"])
    assert opted_in.include_real_model is True


def test_real_model_acceptance_requires_explicit_opt_in_before_execution() -> None:
    calls: list[object] = []
    runtime = SimpleNamespace(
        capabilities=SimpleNamespace(model_configured=True),
        mission_bundle=lambda request: calls.append(request),
    )
    outcome = _real_model_outcome(runtime, include_real_model=False)
    assert outcome.status == "skipped"
    assert outcome.reason == "provider_opt_in_required"
    assert calls == []


@pytest.mark.parametrize("include_real_model", [False, True])
def test_real_model_acceptance_skips_unconfigured_provider(include_real_model) -> None:
    runtime = SimpleNamespace(
        capabilities=SimpleNamespace(model_configured=False),
        mission_bundle=lambda request: pytest.fail("unconfigured provider must not execute"),
    )
    outcome = _real_model_outcome(runtime, include_real_model=include_real_model)
    assert outcome.status == "skipped"
    assert outcome.reason == "provider_unconfigured"


def test_real_model_acceptance_passes_only_verified_bundle() -> None:
    base = replay_bundle()
    task_id = base.run.snapshot.task_id
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
        seq=1,
        status=ObservationStatus.SUCCESS,
        observation={"x_m": 0.0},
        received_at_ms=1_030,
    )
    model_events = [
        base.run.snapshot.events[0].model_copy(
            update={"payload": UserGoalPayload(goal="读取状态", planner="model", max_steps=4)}
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
        RuntimeEvent(
            event_id="evt-model-published",
            timestamp_ms=1_020,
            task_id=task_id,
            seq=1,
            phase="published",
            tool="get_robot_state",
            payload=PublishedPayload(
                topic="robot/dog-accept-test/cmd",
                qos=1,
                command=command,
                published=True,
            ),
        ),
        RuntimeEvent(
            event_id="evt-model-observation",
            timestamp_ms=1_030,
            task_id=task_id,
            seq=1,
            phase="observation",
            tool="get_robot_state",
            payload=ObservationPayload(observation=observation, latency_ms=10),
        ),
        base.run.snapshot.events[-1],
    ]
    bundle = base.model_copy(
        update={
            "run": base.run.model_copy(
                update={
                    "snapshot": base.run.snapshot.model_copy(
                        update={"planner": "model", "max_steps": 4, "events": model_events}
                    )
                }
            ),
            "truthfulness": TruthfulnessProfile(
                planner="openai_compatible_provider",
                mqtt_scope="localhost_mqtt",
                model_transcript="native_tool_calls_verified",
                claims=("Normalized native Tool Call evidence retained",),
            ),
        }
    )
    runtime = SimpleNamespace(
        capabilities=SimpleNamespace(model_configured=True),
        mission_bundle=lambda request: bundle,
    )
    outcome = _real_model_outcome(runtime, include_real_model=True)
    assert outcome.status == "passed"
    assert outcome.bundle == bundle


def test_real_model_acceptance_failure_reason_does_not_echo_provider_details() -> None:
    runtime = SimpleNamespace(
        capabilities=SimpleNamespace(model_configured=True),
        mission_bundle=lambda request: (_ for _ in ()).throw(
            RuntimeError("Authorization: Bearer secret at https://private-provider.example/v1")
        ),
    )
    outcome = _real_model_outcome(runtime, include_real_model=True)
    assert outcome.status == "failed"
    assert outcome.reason == "real_model_scenario_failed:RuntimeError"
