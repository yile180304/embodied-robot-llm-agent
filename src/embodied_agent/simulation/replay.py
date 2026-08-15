"""Strict replay evidence contracts and thread-safe source recording."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Callable, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from .faults import FaultRunSnapshot
from .mission import MissionTaskSnapshot, RuntimeCapabilities, RuntimeEvent
from .models import SimulationFrame
from .world import WorldConfig


MAX_REPLAY_FRAMES = 512
REPLAY_FRAME_CADENCE_MS = 200

ReplayRecorderState: TypeAlias = Literal["idle", "recording", "ready", "invalid"]
ReplayFrameReason: TypeAlias = Literal[
    "start",
    "cadence",
    "command_change",
    "state_change",
    "terminal",
]
AcceptanceScenarioStatus: TypeAlias = Literal["passed", "failed", "skipped"]


def has_verified_native_tool_chain(snapshot: MissionTaskSnapshot) -> bool:
    tool_calls: dict[tuple[int, str], int] = {}
    published: dict[tuple[int, str], int] = {}
    completed_indices: list[int] = []
    for index, event in enumerate(snapshot.events):
        if event.phase == "final":
            if any(completed_index < index for completed_index in completed_indices):
                return True
            continue
        if event.task_id != snapshot.task_id or event.tool is None:
            continue
        key = (event.seq, event.tool)
        if event.phase == "tool_call":
            tool_calls.setdefault(key, index)
            continue
        if event.phase == "published":
            command = event.payload.command
            call_index = tool_calls.get(key)
            if (
                call_index is not None
                and call_index < index
                and event.payload.published is True
                and command.task_id == snapshot.task_id
                and command.seq == event.seq
                and command.tool == event.tool
            ):
                published.setdefault(key, index)
            continue
        if event.phase == "observation":
            observation = event.payload.observation
            publish_index = published.get(key)
            if (
                publish_index is not None
                and publish_index < index
                and observation.task_id == snapshot.task_id
                and observation.seq == event.seq
            ):
                completed_indices.append(index)
    return False


class MissionReplayRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["mission"] = "mission"
    snapshot: MissionTaskSnapshot


class FaultReplayRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fault"] = "fault"
    snapshot: FaultRunSnapshot


ReplayRun = Annotated[MissionReplayRun | FaultReplayRun, Field(discriminator="kind")]


class ReplayFrameSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset_ms: StrictInt = Field(..., ge=0)
    reason: ReplayFrameReason
    frame: SimulationFrame


class ReplayFrameCapture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cadence_ms: Literal[REPLAY_FRAME_CADENCE_MS] = REPLAY_FRAME_CADENCE_MS
    count: StrictInt = Field(..., ge=1, le=MAX_REPLAY_FRAMES)
    truncated: StrictBool


class TruthfulnessProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planner: Literal["fake_planner", "openai_compatible_provider", "not_applicable"]
    perception_source: Literal["python_simulation_ground_truth"] = "python_simulation_ground_truth"
    mqtt_scope: Literal["localhost_mqtt", "configured_remote_or_unknown", "not_used"]
    model_transcript: Literal["not_verified", "native_tool_calls_verified", "not_applicable"]
    hardware: Literal["not_tested"] = "not_tested"
    emqx: Literal["not_tested"] = "not_tested"
    claims: tuple[StrictStr, ...] = Field(..., min_length=1, max_length=8)


class ReplayBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["simulation.replay"] = "simulation.replay"
    version: Literal[1] = 1
    replay_id: StrictStr = Field(..., min_length=1, max_length=64)
    created_at_ms: StrictInt = Field(..., ge=0)
    run: ReplayRun
    world: WorldConfig
    frame_capture: ReplayFrameCapture
    frames: list[ReplayFrameSample] = Field(..., min_length=1, max_length=MAX_REPLAY_FRAMES)
    truthfulness: TruthfulnessProfile

    @model_validator(mode="after")
    def validate_frame_contract(self) -> "ReplayBundle":
        if self.frame_capture.count != len(self.frames):
            raise ValueError("frame_capture count must match frames")
        offsets = [sample.offset_ms for sample in self.frames]
        if offsets != sorted(offsets):
            raise ValueError("replay frame offsets must be monotonic")
        if self.frames[0].reason != "start":
            raise ValueError("replay frames must begin with a start sample")
        if self.frames[-1].reason != "terminal":
            raise ValueError("replay frames must end with a terminal sample")
        if isinstance(self.run, MissionReplayRun):
            snapshot = self.run.snapshot
            expected_planner = (
                "fake_planner" if snapshot.planner == "fake" else "openai_compatible_provider"
            )
            expected_transcript = (
                "native_tool_calls_verified"
                if snapshot.planner == "model" and has_verified_native_tool_chain(snapshot)
                else "not_verified"
            )
        else:
            expected_planner = "not_applicable"
            expected_transcript = "not_applicable"
        if self.truthfulness.planner != expected_planner:
            raise ValueError("truthfulness planner does not match replay run")
        if self.truthfulness.model_transcript != expected_transcript:
            raise ValueError("truthfulness model transcript does not match source events")
        return self


class AcceptanceScenarioResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: StrictStr = Field(..., min_length=1, max_length=64)
    status: AcceptanceScenarioStatus
    bundle_path: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: StrictStr | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_result_contract(self) -> "AcceptanceScenarioResult":
        has_bundle = self.bundle_path is not None and self.sha256 is not None
        if self.status == "passed" and not has_bundle:
            raise ValueError("passed acceptance scenarios require a bundle and sha256")
        if self.status != "passed" and self.reason is None:
            raise ValueError("failed or skipped acceptance scenarios require a reason")
        if (self.bundle_path is None) != (self.sha256 is None):
            raise ValueError("bundle_path and sha256 must be present together")
        return self


class AcceptanceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["simulation.acceptance-manifest"] = "simulation.acceptance-manifest"
    version: Literal[1] = 1
    generated_at_ms: StrictInt = Field(..., ge=0)
    device_id: StrictStr = Field(..., min_length=1, max_length=64)
    scenarios: list[AcceptanceScenarioResult] = Field(..., min_length=1, max_length=16)


class ReplayEvidenceInvalid(RuntimeError):
    """Raised when a recorder cannot provide a trustworthy bundle."""


@dataclass
class _ReplayCapture:
    kind: Literal["mission", "fault"]
    run_id: str
    started_at_ms: int
    frames: list[ReplayFrameSample] = field(default_factory=list)
    seen_event_ids: set[str] = field(default_factory=set)
    truncated: bool = False


class ReplayRecorder:
    """Observe authoritative sources and retain one completed replay bundle."""

    def __init__(
        self,
        world: WorldConfig,
        capabilities: RuntimeCapabilities,
        *,
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._world = world
        self._capabilities = capabilities
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex[:16]}")
        self._lock = threading.RLock()
        self._state: ReplayRecorderState = "idle"
        self._capture: _ReplayCapture | None = None
        self._latest_frame: SimulationFrame | None = None
        self._bundle: ReplayBundle | None = None
        self._invalid_reason: str | None = None

    @property
    def state(self) -> ReplayRecorderState:
        with self._lock:
            return self._state

    @property
    def invalid_reason(self) -> str | None:
        with self._lock:
            return self._invalid_reason

    def on_frame(self, frame: SimulationFrame) -> None:
        with self._lock:
            self._latest_frame = frame.model_copy(deep=True)
            capture = self._capture
            if capture is None:
                return
            reason = self._frame_reason(capture, frame)
            if reason is None:
                return
            self._append_frame(capture, frame, reason=reason)

    def on_mission_event(self, event: RuntimeEvent) -> None:
        with self._lock:
            if event.phase == "user_goal":
                self._start_capture("mission", event.task_id, event.timestamp_ms)
            capture = self._capture
            if capture is None or capture.kind != "mission" or capture.run_id != event.task_id:
                return
            if event.event_id in capture.seen_event_ids:
                return
            capture.seen_event_ids.add(event.event_id)

    def on_fault_started(self, snapshot: FaultRunSnapshot) -> None:
        self._start_capture("fault", snapshot.run_id, snapshot.accepted_at_ms)

    def complete_mission(self, snapshot: MissionTaskSnapshot) -> None:
        self._complete(MissionReplayRun(snapshot=snapshot), snapshot.task_id)

    def complete_fault(self, snapshot: FaultRunSnapshot) -> None:
        self._complete(FaultReplayRun(snapshot=snapshot), snapshot.run_id)

    def current_bundle(self) -> ReplayBundle | None:
        with self._lock:
            if self._state == "recording":
                raise ReplayEvidenceInvalid("evidence capture is still active")
            if self._state == "invalid":
                raise ReplayEvidenceInvalid(self._invalid_reason or "evidence capture is invalid")
            return self._bundle.model_copy(deep=True) if self._bundle is not None else None

    def _start_capture(self, kind: Literal["mission", "fault"], run_id: str, started_at_ms: int) -> None:
        with self._lock:
            if self._capture is not None and self._capture.kind == kind and self._capture.run_id == run_id:
                return
            self._capture = _ReplayCapture(kind=kind, run_id=run_id, started_at_ms=started_at_ms)
            self._state = "recording"
            self._invalid_reason = None
            if self._latest_frame is not None:
                self._append_frame(self._capture, self._latest_frame, reason="start")

    def _complete(self, run: ReplayRun, run_id: str) -> None:
        with self._lock:
            capture = self._capture
            if capture is None or capture.run_id != run_id:
                self._state = "invalid"
                self._invalid_reason = f"missing replay capture for {run_id}"
                return
            if not capture.frames and self._latest_frame is not None:
                self._append_frame(capture, self._latest_frame, reason="start")
            if self._latest_frame is not None:
                self._append_frame(capture, self._latest_frame, reason="terminal", force=True)
            if not capture.frames:
                self._state = "invalid"
                self._invalid_reason = f"no SimulationFrame was recorded for {run_id}"
                self._capture = None
                return
            if capture.frames[-1].reason != "terminal":
                capture.frames[-1] = capture.frames[-1].model_copy(update={"reason": "terminal"})
            try:
                truthfulness = self._truthfulness(run)
                bundle = ReplayBundle(
                    replay_id=self._id_factory("replay"),
                    created_at_ms=self._clock_ms(),
                    run=run,
                    world=self._world,
                    frame_capture=ReplayFrameCapture(
                        count=len(capture.frames),
                        truncated=capture.truncated,
                    ),
                    frames=capture.frames,
                    truthfulness=truthfulness,
                )
            except Exception as exc:
                self._state = "invalid"
                self._invalid_reason = f"replay bundle validation failed: {type(exc).__name__}: {exc}"[:512]
                self._capture = None
                return
            self._bundle = bundle
            self._capture = None
            self._state = "ready"

    def _append_frame(
        self,
        capture: _ReplayCapture,
        frame: SimulationFrame,
        *,
        reason: ReplayFrameReason,
        force: bool = False,
    ) -> None:
        if capture.frames and capture.frames[-1].frame.revision == frame.revision:
            if not force or reason != "terminal" or capture.frames[-1].reason == "terminal":
                return
        offset_ms = 0 if not capture.frames else max(0, self._clock_ms() - capture.started_at_ms)
        sample = ReplayFrameSample(offset_ms=offset_ms, reason=reason, frame=frame)
        if len(capture.frames) >= MAX_REPLAY_FRAMES:
            capture.truncated = True
            if force:
                capture.frames[-1] = sample
            return
        capture.frames.append(sample)

    def _frame_reason(self, capture: _ReplayCapture, frame: SimulationFrame) -> ReplayFrameReason | None:
        if not capture.frames:
            return "start"
        previous = capture.frames[-1]
        previous_command = previous.frame.active_command
        current_command = frame.active_command
        previous_key = (
            None
            if previous_command is None
            else (previous_command.task_id, previous_command.seq, previous_command.tool)
        )
        current_key = (
            None
            if current_command is None
            else (current_command.task_id, current_command.seq, current_command.tool)
        )
        if previous_key != current_key:
            return "command_change"
        if (
            previous.frame.robot.gait != frame.robot.gait
            or previous.frame.robot.emergency_stopped != frame.robot.emergency_stopped
        ):
            return "state_change"
        offset_ms = max(0, self._clock_ms() - capture.started_at_ms)
        if offset_ms - previous.offset_ms >= REPLAY_FRAME_CADENCE_MS:
            return "cadence"
        return None

    def _truthfulness(self, run: ReplayRun) -> TruthfulnessProfile:
        endpoint = self._capabilities.mqtt_endpoint or ""
        host = endpoint.rsplit(":", 1)[0].strip("[]").lower()
        mqtt_scope: Literal["localhost_mqtt", "configured_remote_or_unknown", "not_used"]
        if self._capabilities.mode != "bridge" or not endpoint:
            mqtt_scope = "not_used"
        elif host in {"127.0.0.1", "localhost", "::1"}:
            mqtt_scope = "localhost_mqtt"
        else:
            mqtt_scope = "configured_remote_or_unknown"
        if isinstance(run, MissionReplayRun):
            planner = "fake_planner" if run.snapshot.planner == "fake" else "openai_compatible_provider"
            model_transcript = (
                "native_tool_calls_verified"
                if run.snapshot.planner == "model" and has_verified_native_tool_chain(run.snapshot)
                else "not_verified"
            )
        else:
            planner = "not_applicable"
            model_transcript = "not_applicable"
        return TruthfulnessProfile(
            planner=planner,
            mqtt_scope=mqtt_scope,
            model_transcript=model_transcript,
            claims=(
                "MQTT endpoint scope only; broker product is not proven by the bundle",
                "Python simulation and simulation ground truth only",
                "No camera, YOLO, VLM, EMQX, STM32, Wi-Fi, or Unitree hardware evidence",
            ),
        )

__all__ = [
    "AcceptanceManifest",
    "AcceptanceScenarioResult",
    "FaultReplayRun",
    "MAX_REPLAY_FRAMES",
    "MissionReplayRun",
    "REPLAY_FRAME_CADENCE_MS",
    "ReplayBundle",
    "ReplayEvidenceInvalid",
    "ReplayFrameCapture",
    "ReplayFrameSample",
    "ReplayRecorder",
    "ReplayRecorderState",
    "TruthfulnessProfile",
    "has_verified_native_tool_chain",
]
