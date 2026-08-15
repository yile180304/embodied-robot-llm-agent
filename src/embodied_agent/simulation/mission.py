"""Mission task contracts and the single-task runtime coordinator."""

from __future__ import annotations

import logging
import inspect
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Literal, Protocol, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ..agent_graph import FinalStatus
from ..schemas import CommandMessage, ObservationMessage, ObservationStatus, ToolName
from ..tool_registry import ToolCall
from .run_gate import RuntimeRunGate, RuntimeRunGateBusyError, RuntimeRunLease


LOGGER = logging.getLogger(__name__)
DEFAULT_MISSION_STEPS = 8
MAX_MISSION_STEPS = 16
MAX_MISSION_EVENTS = 128

PlannerMode: TypeAlias = Literal["fake", "model"]
MissionTaskState: TypeAlias = Literal["accepted", "running", "finished"]
RuntimeEventPhase: TypeAlias = Literal[
    "user_goal",
    "planning",
    "tool_call",
    "published",
    "observation",
    "replanning",
    "final",
    "safety_rejected",
]


class MissionTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: StrictStr = Field(..., min_length=1, max_length=512)
    planner: PlannerMode = "fake"
    max_steps: StrictInt = Field(default=DEFAULT_MISSION_STEPS, ge=1, le=MAX_MISSION_STEPS)

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("goal must not be blank")
        return normalized


class MissionTaskAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    task_id: StrictStr = Field(..., min_length=1, max_length=64)
    status: Literal["accepted"] = "accepted"


class MissionTaskCancelAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    task_id: StrictStr = Field(..., min_length=1, max_length=64)
    status: Literal["cancel_requested"] = "cancel_requested"


class UserGoalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: StrictStr = Field(..., min_length=1, max_length=512)
    planner: PlannerMode
    max_steps: StrictInt = Field(..., ge=1, le=MAX_MISSION_STEPS)


class PlanningPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_count: StrictInt = Field(..., ge=0, le=MAX_MISSION_STEPS)
    max_steps: StrictInt = Field(..., ge=1, le=MAX_MISSION_STEPS)
    blocked_triggered: StrictBool


class ToolCallPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planner_message: StrictStr = Field(..., min_length=1, max_length=512)
    arguments: dict[str, Any]


class PublishedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: StrictStr = Field(..., min_length=1, max_length=256)
    qos: Literal[1]
    command: CommandMessage
    published: Literal[True]


class ObservationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation: ObservationMessage
    latency_ms: StrictInt = Field(..., ge=0)


class ReplanningPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StrictStr = Field(..., min_length=1, max_length=256)
    last_status: ObservationStatus
    next_seq: StrictInt = Field(..., ge=1)


class SafetyRejectedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call: ToolCall
    observation: ObservationMessage
    published: Literal[False]


class FinalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_status: FinalStatus
    final_message: StrictStr = Field(..., min_length=1, max_length=512)
    duration_ms: StrictInt = Field(..., ge=0)
    step_count: StrictInt = Field(..., ge=0, le=MAX_MISSION_STEPS)


RuntimeEventPayload: TypeAlias = (
    UserGoalPayload
    | PlanningPayload
    | ToolCallPayload
    | PublishedPayload
    | ObservationPayload
    | ReplanningPayload
    | SafetyRejectedPayload
    | FinalPayload
)

_PAYLOAD_TYPES: dict[str, type[BaseModel]] = {
    "user_goal": UserGoalPayload,
    "planning": PlanningPayload,
    "tool_call": ToolCallPayload,
    "published": PublishedPayload,
    "observation": ObservationPayload,
    "replanning": ReplanningPayload,
    "safety_rejected": SafetyRejectedPayload,
    "final": FinalPayload,
}
_TOOL_PHASES = {"tool_call", "published", "observation", "safety_rejected"}


class RuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["agent.step"] = "agent.step"
    version: Literal[1] = 1
    event_id: StrictStr = Field(..., min_length=1, max_length=64)
    timestamp_ms: StrictInt = Field(..., ge=0)
    task_id: StrictStr = Field(..., min_length=1, max_length=64)
    seq: StrictInt = Field(..., ge=0)
    phase: RuntimeEventPhase
    tool: ToolName | None = None
    payload: RuntimeEventPayload

    @model_validator(mode="after")
    def validate_phase_contract(self) -> "RuntimeEvent":
        expected = _PAYLOAD_TYPES[self.phase]
        if not isinstance(self.payload, expected):
            raise ValueError(f"phase {self.phase} requires {expected.__name__}")
        if self.phase in {"user_goal", "final"} and self.seq != 0:
            raise ValueError(f"phase {self.phase} requires seq=0")
        if self.phase not in {"user_goal", "final"} and self.seq < 1:
            raise ValueError(f"phase {self.phase} requires seq>=1")
        if self.phase in _TOOL_PHASES and self.tool is None:
            raise ValueError(f"phase {self.phase} requires tool")
        if self.phase not in _TOOL_PHASES and self.tool is not None:
            raise ValueError(f"phase {self.phase} does not accept tool")
        return self


class MissionTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_status: FinalStatus
    final_message: StrictStr = Field(..., min_length=1, max_length=512)
    step_count: StrictInt = Field(..., ge=0, le=MAX_MISSION_STEPS)


class MissionTaskSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    task_id: StrictStr = Field(..., min_length=1, max_length=64)
    goal: StrictStr = Field(..., min_length=1, max_length=512)
    planner: PlannerMode
    max_steps: StrictInt = Field(..., ge=1, le=MAX_MISSION_STEPS)
    status: MissionTaskState
    accepted_at_ms: StrictInt = Field(..., ge=0)
    started_at_ms: StrictInt | None = Field(default=None, ge=0)
    finished_at_ms: StrictInt | None = Field(default=None, ge=0)
    final_status: FinalStatus | None = None
    final_message: StrictStr | None = Field(default=None, max_length=512)
    events: list[RuntimeEvent] = Field(default_factory=list, max_length=MAX_MISSION_EVENTS)


class RuntimeCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    mode: Literal["preview", "bridge"]
    device_id: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    mqtt_endpoint: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    fake_planner: Literal[True] = True
    model_configured: StrictBool
    fault_injection: StrictBool = False


class RuntimeApiError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    error_code: StrictStr = Field(..., min_length=1, max_length=64)
    error_message: StrictStr = Field(..., min_length=1, max_length=256)


class MissionTaskActiveError(RuntimeError):
    pass


class MissionTaskNotActiveError(RuntimeError):
    pass


class MissionEventOverflow(RuntimeError):
    pass


class RuntimeEventEmitter(Protocol):
    def __call__(
        self,
        *,
        phase: RuntimeEventPhase,
        seq: int,
        payload: RuntimeEventPayload,
        tool: ToolName | None = None,
    ) -> RuntimeEvent: ...


class MissionTaskRunner(Protocol):
    def __call__(
        self,
        request: MissionTaskRequest,
        task_id: str,
        emit: RuntimeEventEmitter,
        cancellation_event: threading.Event,
    ) -> MissionTaskResult: ...


class MissionTaskCoordinator:
    """Run one mission at a time and retain its bounded event journal."""

    def __init__(
        self,
        runner: MissionTaskRunner,
        *,
        event_listener: Callable[[RuntimeEvent], None] | None = None,
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[str], str] | None = None,
        run_gate: RuntimeRunGate | None = None,
        cancel_handler: Callable[[str], object] | None = None,
        completion_listener: Callable[[MissionTaskSnapshot], None] | None = None,
    ) -> None:
        self._runner = runner
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex[:16]}")
        self._run_gate = run_gate or RuntimeRunGate()
        self._cancel_handler = cancel_handler
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mission-task")
        self._current: MissionTaskSnapshot | None = None
        self._active_task_id: str | None = None
        self._future: Future[None] | None = None
        self._cancellation_event: threading.Event | None = None
        self._run_lease: RuntimeRunLease | None = None
        self._event_listeners: list[Callable[[RuntimeEvent], None]] = []
        self._completion_listeners: list[Callable[[MissionTaskSnapshot], None]] = []
        if event_listener is not None:
            self._event_listeners.append(event_listener)
        if completion_listener is not None:
            self._completion_listeners.append(completion_listener)

    def add_event_listener(self, listener: Callable[[RuntimeEvent], None]) -> None:
        with self._lock:
            if listener not in self._event_listeners:
                self._event_listeners.append(listener)

    def add_completion_listener(self, listener: Callable[[MissionTaskSnapshot], None]) -> None:
        with self._lock:
            if listener not in self._completion_listeners:
                self._completion_listeners.append(listener)

    def submit(self, request: MissionTaskRequest) -> MissionTaskAccepted:
        with self._lock:
            if self._active_task_id is not None:
                raise MissionTaskActiveError("another mission task is running")
            task_id = self._id_factory("mission")
            try:
                lease = self._run_gate.acquire("mission", task_id)
            except RuntimeRunGateBusyError as exc:
                if exc.active.kind == "mission":
                    raise MissionTaskActiveError("another mission task is running") from exc
                raise
            accepted_at = self._clock_ms()
            self._current = MissionTaskSnapshot(
                task_id=task_id,
                goal=request.goal,
                planner=request.planner,
                max_steps=request.max_steps,
                status="accepted",
                accepted_at_ms=accepted_at,
            )
            self._active_task_id = task_id
            self._cancellation_event = threading.Event()
            self._run_lease = lease
            user_goal_event = self._emit_locked(
                phase="user_goal",
                seq=0,
                payload=UserGoalPayload(
                    goal=request.goal,
                    planner=request.planner,
                    max_steps=request.max_steps,
                ),
            )
        self._notify_listener(user_goal_event)
        with self._lock:
            try:
                self._future = self._executor.submit(self._run, request, task_id)
            except Exception:
                self._active_task_id = None
                self._cancellation_event = None
                self._run_lease = None
                self._run_gate.release(lease)
                raise
        return MissionTaskAccepted(task_id=task_id)

    def current(self) -> MissionTaskSnapshot | None:
        with self._lock:
            return self._current.model_copy(deep=True) if self._current is not None else None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active_task_id is not None

    @property
    def run_gate(self) -> RuntimeRunGate:
        return self._run_gate

    def cancel_current(self) -> MissionTaskCancelAccepted:
        """Request cooperative cancellation for the current mission exactly once."""

        with self._lock:
            task_id = self._active_task_id
            snapshot = self._current
            event = self._cancellation_event
            if task_id is None or snapshot is None or event is None:
                raise MissionTaskNotActiveError("no mission task is active")
            if snapshot.events and snapshot.events[-1].phase == "final":
                raise MissionTaskNotActiveError("mission task has already reached a final event")
            already_requested = event.is_set()
            event.set()
        if not already_requested and self._cancel_handler is not None:
            try:
                self._cancel_handler(task_id)
            except Exception:
                LOGGER.exception("mission task cancel handler failed task_id=%s", task_id)
        return MissionTaskCancelAccepted(task_id=task_id)

    def cancel(self) -> MissionTaskCancelAccepted:
        """Compatibility alias for callers that treat the coordinator as current-task scoped."""

        return self.cancel_current()

    def wait_for_idle(self, timeout_s: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.active:
                return True
            time.sleep(0.005)
        return not self.active

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _run(self, request: MissionTaskRequest, task_id: str) -> None:
        started_at = self._clock_ms()
        with self._lock:
            snapshot = self._require_current(task_id)
            snapshot.status = "running"
            snapshot.started_at_ms = started_at

        cancellation_event = self._require_cancellation_event(task_id)
        lease = self._require_lease(task_id)
        try:
            result = self._invoke_runner(request, task_id, cancellation_event)
            with self._lock:
                terminal_status = self._terminal_final_status_locked()
            if cancellation_event.is_set() and terminal_status != "emergency_stop":
                result = MissionTaskResult(
                    final_status="cancelled",
                    final_message="mission cancelled by operator",
                    step_count=result.step_count,
                )
            with self._lock:
                snapshot = self._require_current(task_id)
                has_final = bool(snapshot.events and snapshot.events[-1].phase == "final")
            if not has_final:
                result = MissionTaskResult(
                    final_status=result.final_status,
                    final_message=result.final_message,
                    step_count=result.step_count,
                )
                self._emit(
                    phase="final",
                    seq=0,
                    payload=FinalPayload(
                        final_status=result.final_status,
                        final_message=result.final_message,
                        duration_ms=max(0, self._clock_ms() - started_at),
                        step_count=result.step_count,
                    ),
                )
        except Exception as exc:
            LOGGER.exception("mission task failed task_id=%s", task_id)
            final_status: FinalStatus = "cancelled" if cancellation_event.is_set() else "planner_error"
            final_message = (
                "mission cancelled by operator"
                if final_status == "cancelled"
                else f"mission runner failed: {type(exc).__name__}: {exc}"[:512]
            )
            result = MissionTaskResult(
                final_status=final_status,
                final_message=final_message,
                step_count=0,
            )
            try:
                with self._lock:
                    has_final = bool(self._current and self._current.events and self._current.events[-1].phase == "final")
                if not has_final:
                    self._emit(
                        phase="final",
                        seq=0,
                        payload=FinalPayload(
                            final_status=result.final_status,
                            final_message=result.final_message,
                            duration_ms=max(0, self._clock_ms() - started_at),
                            step_count=result.step_count,
                        ),
                    )
            except MissionEventOverflow:
                LOGGER.error("mission event journal overflow task_id=%s", task_id)
        finally:
            finished_at = self._clock_ms()
            with self._lock:
                snapshot = self._require_current(task_id)
                snapshot.status = "finished"
                snapshot.finished_at_ms = finished_at
                terminal = self._terminal_final_status_locked()
                snapshot.final_status = terminal or result.final_status
                snapshot.final_message = (
                    "mission cancelled by operator"
                    if snapshot.final_status == "cancelled"
                    else result.final_message
                )
                self._active_task_id = None
                self._cancellation_event = None
                self._run_lease = None
                completed_snapshot = snapshot.model_copy(deep=True)
            self._notify_completion(completed_snapshot)
            self._run_gate.release(lease)

    def _invoke_runner(
        self,
        request: MissionTaskRequest,
        task_id: str,
        cancellation_event: threading.Event,
    ) -> MissionTaskResult:
        """Pass the new signal while retaining compatibility with simple test runners."""

        try:
            inspect.signature(self._runner).bind(request, task_id, self._emit, cancellation_event)
        except (TypeError, ValueError):
            return self._runner(request, task_id, self._emit)  # type: ignore[call-arg]
        return self._runner(request, task_id, self._emit, cancellation_event)

    def _emit(
        self,
        *,
        phase: RuntimeEventPhase,
        seq: int,
        payload: RuntimeEventPayload,
        tool: ToolName | None = None,
    ) -> RuntimeEvent:
        with self._lock:
            if phase == "final" and isinstance(payload, FinalPayload):
                cancellation_event = self._cancellation_event
                if (
                    cancellation_event is not None
                    and cancellation_event.is_set()
                    and payload.final_status != "emergency_stop"
                ):
                    payload = payload.model_copy(
                        update={
                            "final_status": "cancelled",
                            "final_message": "mission cancelled by operator",
                        }
                    )
            event = self._emit_locked(phase=phase, seq=seq, payload=payload, tool=tool)
        self._notify_listener(event)
        return event

    def _emit_locked(
        self,
        *,
        phase: RuntimeEventPhase,
        seq: int,
        payload: RuntimeEventPayload,
        tool: ToolName | None = None,
    ) -> RuntimeEvent:
        if self._current is None:
            raise RuntimeError("cannot emit a mission event without a current task")
        if len(self._current.events) >= MAX_MISSION_EVENTS:
            raise MissionEventOverflow("mission event journal reached its 128 event limit")
        event = RuntimeEvent(
            event_id=self._id_factory("evt"),
            timestamp_ms=self._clock_ms(),
            task_id=self._current.task_id,
            seq=seq,
            phase=phase,
            tool=tool,
            payload=payload,
        )
        self._current.events.append(event)
        return event

    def _notify_listener(self, event: RuntimeEvent) -> None:
        with self._lock:
            listeners = tuple(self._event_listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                LOGGER.exception("mission event listener failed event_id=%s", event.event_id)

    def _notify_completion(self, snapshot: MissionTaskSnapshot) -> None:
        with self._lock:
            listeners = tuple(self._completion_listeners)
        for listener in listeners:
            try:
                listener(snapshot.model_copy(deep=True))
            except Exception:
                LOGGER.exception("mission completion listener failed task_id=%s", snapshot.task_id)

    def _require_current(self, task_id: str) -> MissionTaskSnapshot:
        if self._current is None or self._current.task_id != task_id:
            raise RuntimeError(f"mission task state is unavailable for {task_id}")
        return self._current

    def _require_cancellation_event(self, task_id: str) -> threading.Event:
        with self._lock:
            if self._active_task_id != task_id or self._cancellation_event is None:
                raise RuntimeError(f"mission cancellation state is unavailable for {task_id}")
            return self._cancellation_event

    def _require_lease(self, task_id: str) -> RuntimeRunLease:
        with self._lock:
            if self._active_task_id != task_id or self._run_lease is None:
                raise RuntimeError(f"mission run lease is unavailable for {task_id}")
            return self._run_lease

    def _terminal_final_status_locked(self) -> FinalStatus | None:
        if self._current is None or not self._current.events:
            return None
        final_event = next(
            (event for event in reversed(self._current.events) if event.phase == "final"),
            None,
        )
        if final_event is None or not isinstance(final_event.payload, FinalPayload):
            return None
        return final_event.payload.final_status


__all__ = [
    "FinalPayload",
    "MAX_MISSION_EVENTS",
    "MAX_MISSION_STEPS",
    "DEFAULT_MISSION_STEPS",
    "MissionEventOverflow",
    "MissionTaskAccepted",
    "MissionTaskActiveError",
    "MissionTaskCancelAccepted",
    "MissionTaskCoordinator",
    "MissionTaskRequest",
    "MissionTaskResult",
    "MissionTaskRunner",
    "MissionTaskSnapshot",
    "MissionTaskNotActiveError",
    "ObservationPayload",
    "PlannerMode",
    "PlanningPayload",
    "PublishedPayload",
    "ReplanningPayload",
    "RuntimeApiError",
    "RuntimeCapabilities",
    "RuntimeEvent",
    "RuntimeEventEmitter",
    "RuntimeEventPayload",
    "RuntimeEventPhase",
    "SafetyRejectedPayload",
    "ToolCallPayload",
    "UserGoalPayload",
]
