"""Fixed local simulation scenarios and traceable Acceptance Pack output."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from ..mqtt_transport import MqttConfig
from ..schemas import ObservationStatus
from .faults import FaultCoordinator, FaultRunRequest, FaultRunSnapshot, FaultScenario
from .mission import MissionTaskCoordinator, MissionTaskRequest, MissionTaskSnapshot, RuntimeCapabilities
from .replay import (
    AcceptanceManifest,
    AcceptanceScenarioResult,
    ReplayBundle,
    ReplayRecorder,
    TruthfulnessProfile,
    has_verified_native_tool_chain,
)
from .runtime import SimulationRuntime, create_bridge_app


AcceptanceStatus = Literal["passed", "failed", "skipped"]
SCENARIO_IDS = (
    "obstacle_detour",
    "semantic_mission",
    "safety_rejection",
    "operator_cancel",
    "emergency_stop",
    "fault_device_disconnect",
    "fault_response_timeout",
    "fault_duplicate_delivery",
    "fault_out_of_order",
    "real_model_tool_calling",
)


class AcceptanceTruthfulnessScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: StrictStr = Field(..., min_length=1, max_length=64)
    status: AcceptanceStatus
    bundle_path: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    truthfulness: TruthfulnessProfile | None = None
    reason: StrictStr | None = Field(default=None, min_length=1, max_length=512)


class AcceptanceTruthfulnessReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["simulation.acceptance-truthfulness"] = "simulation.acceptance-truthfulness"
    version: Literal[1] = 1
    generated_at_ms: StrictInt = Field(..., ge=0)
    device_id: StrictStr = Field(..., min_length=1, max_length=64)
    passed: StrictInt = Field(..., ge=0)
    failed: StrictInt = Field(..., ge=0)
    skipped: StrictInt = Field(..., ge=0)
    scenarios: list[AcceptanceTruthfulnessScenario] = Field(..., min_length=1, max_length=16)
    claims: tuple[StrictStr, ...] = Field(..., min_length=1, max_length=8)


@dataclass(frozen=True)
class AcceptanceScenarioOutcome:
    scenario_id: str
    status: AcceptanceStatus
    bundle: ReplayBundle | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AcceptancePackResult:
    output_dir: Path
    manifest: AcceptanceManifest
    truthfulness: AcceptanceTruthfulnessReport

    @property
    def exit_code(self) -> int:
        return 2 if self.truthfulness.failed else 0


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_json(path: Path, value: object) -> bytes:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _truthfulness_markdown(report: AcceptanceTruthfulnessReport) -> str:
    lines = [
        "# Simulation Acceptance Truthfulness Report",
        "",
        f"- Device ID: `{report.device_id}`",
        f"- Generated at: `{report.generated_at_ms}` ms Unix time",
        f"- Results: `{report.passed}` passed, `{report.failed}` failed, `{report.skipped}` skipped",
        "",
        "## Scenario Results",
        "",
        "| Scenario | Status | Bundle | Reason |",
        "|---|---|---|---|",
    ]
    for scenario in report.scenarios:
        bundle = f"`{scenario.bundle_path}`" if scenario.bundle_path else "-"
        reason = scenario.reason or "-"
        lines.append(f"| `{scenario.scenario_id}` | `{scenario.status}` | {bundle} | {reason} |")
    lines.extend(["", "## Evidence Boundaries", ""])
    lines.extend(f"- {claim}" for claim in report.claims)
    lines.extend(["", "## Bundle Truthfulness Profiles", ""])
    for scenario in report.scenarios:
        if scenario.truthfulness is None:
            continue
        profile = scenario.truthfulness
        lines.extend(
            [
                f"### `{scenario.scenario_id}`",
                "",
                f"- Planner: `{profile.planner}`",
                f"- Perception: `{profile.perception_source}`",
                f"- MQTT scope: `{profile.mqtt_scope}`",
                f"- Model transcript: `{profile.model_transcript}`",
                f"- Hardware: `{profile.hardware}`",
                f"- EMQX: `{profile.emqx}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_acceptance_pack(
    output_dir: Path,
    *,
    device_id: str,
    outcomes: list[AcceptanceScenarioOutcome],
    generated_at_ms: int | None = None,
) -> AcceptancePackResult:
    if [outcome.scenario_id for outcome in outcomes] != list(SCENARIO_IDS):
        raise ValueError("Acceptance Pack outcomes must follow the fixed scenario registry")
    generated = int(time.time() * 1_000) if generated_at_ms is None else generated_at_ms
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_results: list[AcceptanceScenarioResult] = []
    truthfulness_results: list[AcceptanceTruthfulnessScenario] = []

    for outcome in outcomes:
        bundle_path: str | None = None
        digest: str | None = None
        profile: TruthfulnessProfile | None = None
        if outcome.bundle is not None:
            bundle_path = f"bundles/{outcome.scenario_id}.replay.json"
            payload = _write_json(
                output_dir / bundle_path,
                outcome.bundle.model_dump(mode="json"),
            )
            digest = _sha256(payload)
            profile = outcome.bundle.truthfulness
        manifest_results.append(
            AcceptanceScenarioResult(
                scenario_id=outcome.scenario_id,
                status=outcome.status,
                bundle_path=bundle_path,
                sha256=digest,
                reason=outcome.reason,
            )
        )
        truthfulness_results.append(
            AcceptanceTruthfulnessScenario(
                scenario_id=outcome.scenario_id,
                status=outcome.status,
                bundle_path=bundle_path,
                truthfulness=profile,
                reason=outcome.reason,
            )
        )

    manifest = AcceptanceManifest(
        generated_at_ms=generated,
        device_id=device_id,
        scenarios=manifest_results,
    )
    counts = {
        status: sum(outcome.status == status for outcome in outcomes)
        for status in ("passed", "failed", "skipped")
    }
    truthfulness = AcceptanceTruthfulnessReport(
        generated_at_ms=generated,
        device_id=device_id,
        passed=counts["passed"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        scenarios=truthfulness_results,
        claims=(
            "Passed bundles prove only the fixed local scenarios recorded in this pack",
            "MQTT claims are limited to the endpoint scope recorded in each bundle",
            "Perception evidence is Python simulation ground truth, not camera, YOLO, or VLM output",
            "No bundle proves EMQX, STM32, Wi-Fi, Unitree hardware, or physical robot behavior",
            "A real model is passed only with configured provider evidence and an independently verified transcript",
        ),
    )
    _write_json(output_dir / "manifest.json", manifest.model_dump(mode="json"))
    _write_json(output_dir / "truthfulness.json", truthfulness.model_dump(mode="json"))
    (output_dir / "truthfulness.md").write_text(
        _truthfulness_markdown(truthfulness),
        encoding="utf-8",
        newline="\n",
    )
    return AcceptancePackResult(output_dir=output_dir, manifest=manifest, truthfulness=truthfulness)


class _AcceptanceRuntime:
    def __init__(self, app: FastAPI) -> None:
        self.simulation: SimulationRuntime = app.state.simulation
        self.mission: MissionTaskCoordinator = app.state.mission_coordinator
        self.fault: FaultCoordinator = app.state.fault_coordinator
        self.recorder: ReplayRecorder = app.state.replay_recorder
        self.capabilities: RuntimeCapabilities = app.state.runtime_capabilities
        self.emergency_stop: Callable[[], object] = app.state.emergency_stop_handler

    def reset(self) -> None:
        if self.mission.active or self.fault.active:
            raise RuntimeError("cannot reset while an acceptance scenario is active")
        self.simulation.reset()
        time.sleep(0.05)

    def mission_bundle(
        self,
        request: MissionTaskRequest,
        *,
        interrupt: Literal["cancel", "emergency"] | None = None,
        timeout_s: float = 120.0,
    ) -> ReplayBundle:
        self.reset()
        accepted = self.mission.submit(request)
        if interrupt is not None:
            self._wait_for_active_command(accepted.task_id)
            if interrupt == "cancel":
                self.mission.cancel_current()
            else:
                observation = self.emergency_stop()
                status = getattr(observation, "status", None)
                if status is not ObservationStatus.EMERGENCY_STOP:
                    raise RuntimeError("operator emergency stop did not return emergency_stop")
        if not self.mission.wait_for_idle(timeout_s):
            raise RuntimeError(f"mission scenario timed out after {timeout_s:g} seconds")
        snapshot = self.mission.current()
        bundle = self.recorder.current_bundle()
        if snapshot is None or bundle is None or bundle.run.kind != "mission":
            raise RuntimeError("mission scenario did not produce a mission Replay Bundle")
        if snapshot.task_id != accepted.task_id or bundle.run.snapshot.task_id != accepted.task_id:
            raise RuntimeError("mission Replay Bundle does not match the accepted task")
        return bundle

    def fault_bundle(self, scenario: FaultScenario, *, timeout_s: float = 15.0) -> ReplayBundle:
        self.reset()
        accepted = self.fault.submit(FaultRunRequest(scenario=scenario))
        if not self.fault.wait_for_idle(timeout_s):
            raise RuntimeError(f"fault scenario timed out after {timeout_s:g} seconds")
        snapshot = self.fault.current()
        bundle = self.recorder.current_bundle()
        if snapshot is None or bundle is None or bundle.run.kind != "fault":
            raise RuntimeError("fault scenario did not produce a fault Replay Bundle")
        if snapshot.run_id != accepted.run_id or bundle.run.snapshot.run_id != accepted.run_id:
            raise RuntimeError("fault Replay Bundle does not match the accepted run")
        return bundle

    def _wait_for_active_command(self, task_id: str, timeout_s: float = 3.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame = self.simulation.engine.snapshot()
            command = frame.active_command
            if command is not None and command.task_id == task_id:
                return
            if not self.mission.active:
                break
            time.sleep(0.02)
        raise RuntimeError("mission never exposed an active command for operator interruption")


def _mission_snapshot(bundle: ReplayBundle) -> MissionTaskSnapshot:
    if bundle.run.kind != "mission":
        raise ValueError("expected a mission Replay Bundle")
    if bundle.frame_capture.truncated:
        raise ValueError("Replay Bundle frame capture is truncated")
    return bundle.run.snapshot


def _fault_snapshot(bundle: ReplayBundle) -> FaultRunSnapshot:
    if bundle.run.kind != "fault":
        raise ValueError("expected a fault Replay Bundle")
    if bundle.frame_capture.truncated:
        raise ValueError("Replay Bundle frame capture is truncated")
    return bundle.run.snapshot


def _validate_obstacle(bundle: ReplayBundle) -> None:
    snapshot = _mission_snapshot(bundle)
    if snapshot.final_status != "success":
        raise ValueError(f"obstacle mission ended with {snapshot.final_status}")
    blocked = [
        event
        for event in snapshot.events
        if event.phase == "observation"
        and getattr(event.payload, "observation", None) is not None
        and event.payload.observation.status is ObservationStatus.BLOCKED
    ]
    if not blocked:
        raise ValueError("obstacle mission did not retain a blocked Observation")


def _validate_semantic(bundle: ReplayBundle) -> None:
    snapshot = _mission_snapshot(bundle)
    if snapshot.final_status != "success":
        raise ValueError(f"semantic mission ended with {snapshot.final_status}")
    confirmations: list[tuple[str, str]] = []
    for event in snapshot.events:
        if event.phase != "observation" or getattr(event.payload, "observation", None) is None:
            continue
        observation = event.payload.observation.observation
        if "objects" not in observation:
            continue
        if observation.get("source") != "simulation_ground_truth":
            raise ValueError("semantic Observation did not declare simulation_ground_truth")
        for item in observation.get("objects", []):
            if isinstance(item, dict) and item.get("within_interaction_radius") is True:
                confirmations.append((str(item.get("kind")), str(item.get("color"))))
    if confirmations != [("bottle", "red"), ("goal_zone", "blue")]:
        raise ValueError(f"semantic confirmations were {confirmations!r}")


def _validate_safety(bundle: ReplayBundle) -> None:
    snapshot = _mission_snapshot(bundle)
    rejected = [event for event in snapshot.events if event.phase == "safety_rejected"]
    if snapshot.final_status != "rejected" or len(rejected) != 1:
        raise ValueError("safety scenario did not retain one rejected final path")
    if rejected[0].payload.published is not False:
        raise ValueError("safety rejection incorrectly claims MQTT publication")


def _validate_cancel(bundle: ReplayBundle) -> None:
    snapshot = _mission_snapshot(bundle)
    if snapshot.final_status != "cancelled":
        raise ValueError(f"cancel scenario ended with {snapshot.final_status}")
    if not any(
        event.phase == "observation"
        and getattr(event.payload, "observation", None) is not None
        and event.payload.observation.error_code == "operator_cancelled"
        for event in snapshot.events
    ):
        raise ValueError("cancel scenario did not retain operator_cancelled evidence")


def _validate_emergency(bundle: ReplayBundle) -> None:
    snapshot = _mission_snapshot(bundle)
    if snapshot.final_status != "emergency_stop":
        raise ValueError(f"emergency scenario ended with {snapshot.final_status}")
    if not any(
        event.phase == "observation"
        and getattr(event.payload, "observation", None) is not None
        and event.payload.observation.status is ObservationStatus.EMERGENCY_STOP
        for event in snapshot.events
    ):
        raise ValueError("emergency scenario did not retain emergency_stop evidence")


def _validate_fault(bundle: ReplayBundle, scenario: FaultScenario) -> None:
    snapshot = _fault_snapshot(bundle)
    if snapshot.scenario != scenario or snapshot.result != "passed":
        raise ValueError(f"fault {scenario} ended with {snapshot.result}")
    if not snapshot.evidence or snapshot.evidence[-1].stage != "final":
        raise ValueError(f"fault {scenario} did not retain terminal evidence")


def _validate_real_model(bundle: ReplayBundle) -> None:
    snapshot = _mission_snapshot(bundle)
    if snapshot.planner != "model":
        raise ValueError("real model scenario did not retain planner=model")
    if snapshot.final_status != "success":
        raise ValueError(f"real model scenario ended with {snapshot.final_status}")
    if bundle.truthfulness.planner != "openai_compatible_provider":
        raise ValueError("real model scenario did not retain provider planner evidence")
    if bundle.truthfulness.model_transcript != "native_tool_calls_verified":
        raise ValueError("real model scenario did not retain a verified native Tool Call chain")
    if not has_verified_native_tool_chain(snapshot):
        raise ValueError("real model scenario truthfulness is not supported by source events")


def _real_model_outcome(
    runtime: _AcceptanceRuntime,
    *,
    include_real_model: bool,
) -> AcceptanceScenarioOutcome:
    if not runtime.capabilities.model_configured:
        return AcceptanceScenarioOutcome(
            "real_model_tool_calling",
            "skipped",
            reason="provider_unconfigured",
        )
    if not include_real_model:
        return AcceptanceScenarioOutcome(
            "real_model_tool_calling",
            "skipped",
            reason="provider_opt_in_required",
        )
    bundle: ReplayBundle | None = None
    try:
        bundle = runtime.mission_bundle(
            MissionTaskRequest(
                goal="调用 get_robot_state 读取当前机器人状态，然后根据 Observation 结束任务",
                planner="model",
                max_steps=4,
            )
        )
        _validate_real_model(bundle)
        return AcceptanceScenarioOutcome(
            "real_model_tool_calling",
            "passed",
            bundle=bundle,
        )
    except Exception as exc:
        return AcceptanceScenarioOutcome(
            "real_model_tool_calling",
            "failed",
            bundle=bundle,
            reason=f"real_model_scenario_failed:{type(exc).__name__}",
        )


def _run_fixed_registry(
    runtime: _AcceptanceRuntime,
    *,
    include_real_model: bool = False,
) -> list[AcceptanceScenarioOutcome]:
    scenarios: list[tuple[str, Callable[[], ReplayBundle], Callable[[ReplayBundle], None]]] = [
        (
            "obstacle_detour",
            lambda: runtime.mission_bundle(
                MissionTaskRequest(
                    goal="前进 2 米，如果遇到障碍就从更宽的一侧绕开",
                    planner="fake",
                    max_steps=8,
                )
            ),
            _validate_obstacle,
        ),
        (
            "semantic_mission",
            lambda: runtime.mission_bundle(
                MissionTaskRequest(
                    goal="找到红色瓶子并前往蓝色目标区",
                    planner="fake",
                    max_steps=16,
                )
            ),
            _validate_semantic,
        ),
        (
            "safety_rejection",
            lambda: runtime.mission_bundle(
                MissionTaskRequest(goal="全速前进 10 米", planner="fake", max_steps=4)
            ),
            _validate_safety,
        ),
        (
            "operator_cancel",
            lambda: runtime.mission_bundle(
                MissionTaskRequest(goal="前进 1 米", planner="fake", max_steps=8),
                interrupt="cancel",
            ),
            _validate_cancel,
        ),
        (
            "emergency_stop",
            lambda: runtime.mission_bundle(
                MissionTaskRequest(goal="前进 1 米", planner="fake", max_steps=8),
                interrupt="emergency",
            ),
            _validate_emergency,
        ),
    ]
    fault_scenarios: tuple[tuple[str, FaultScenario], ...] = (
        ("fault_device_disconnect", "device_disconnect"),
        ("fault_response_timeout", "response_timeout"),
        ("fault_duplicate_delivery", "duplicate_delivery"),
        ("fault_out_of_order", "out_of_order"),
    )
    for scenario_id, scenario in fault_scenarios:
        scenarios.append(
            (
                scenario_id,
                lambda scenario=scenario: runtime.fault_bundle(scenario),
                lambda bundle, scenario=scenario: _validate_fault(bundle, scenario),
            )
        )

    outcomes: list[AcceptanceScenarioOutcome] = []
    for scenario_id, execute, validate in scenarios:
        bundle: ReplayBundle | None = None
        try:
            bundle = execute()
            validate(bundle)
            outcomes.append(AcceptanceScenarioOutcome(scenario_id, "passed", bundle=bundle))
        except Exception as exc:
            outcomes.append(
                AcceptanceScenarioOutcome(
                    scenario_id,
                    "failed",
                    bundle=bundle,
                    reason=f"{type(exc).__name__}: {exc}"[:512],
                )
            )
    outcomes.append(_real_model_outcome(runtime, include_real_model=include_real_model))
    return outcomes


def generate_acceptance_pack(
    *,
    mqtt_config: MqttConfig,
    device_id: str,
    output_dir: Path,
    include_real_model: bool = False,
) -> AcceptancePackResult:
    app = create_bridge_app(mqtt_config, device_id=device_id)

    async def run() -> AcceptancePackResult:
        async with app.router.lifespan_context(app):
            outcomes = await asyncio.to_thread(
                _run_fixed_registry,
                _AcceptanceRuntime(app),
                include_real_model=include_real_model,
            )
            return write_acceptance_pack(
                output_dir,
                device_id=device_id,
                outcomes=outcomes,
            )

    return asyncio.run(run())


__all__ = [
    "AcceptancePackResult",
    "AcceptanceScenarioOutcome",
    "AcceptanceTruthfulnessReport",
    "SCENARIO_IDS",
    "generate_acceptance_pack",
    "write_acceptance_pack",
]
