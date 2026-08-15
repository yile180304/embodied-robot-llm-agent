"""Strict contracts and lifecycle coordination for bounded MQTT fault runs."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from ..mqtt_device_service import MqttDeviceService
from ..mqtt_transport import (
    MqttConfig,
    MqttConnectionError,
    MqttRequestClient,
    MqttResponseTimeout,
    MqttTopics,
    MqttTransportError,
)
from ..schemas import CommandMessage, ObservationMessage, ObservationStatus, ToolName
from .run_gate import RuntimeRunGate, RuntimeRunGateBusyError, RuntimeRunLease


LOGGER = logging.getLogger(__name__)
MAX_FAULT_EVIDENCE = 64
FAULT_TIMEOUT_S = 1.0
FAULT_COMMAND_TIMEOUT_S = 2.0

FaultScenario: TypeAlias = Literal[
    "device_disconnect",
    "response_timeout",
    "duplicate_delivery",
    "out_of_order",
]
FaultRunState: TypeAlias = Literal["accepted", "running", "finished"]
FaultRunResult: TypeAlias = Literal["passed", "failed"]
FaultEvidenceStage: TypeAlias = Literal[
    "prepare",
    "published",
    "observation",
    "timeout",
    "service_disconnected",
    "service_restored",
    "final",
]


class FaultRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: FaultScenario


class FaultRunAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    run_id: StrictStr = Field(..., min_length=1, max_length=64)
    status: Literal["accepted"] = "accepted"


class FaultPrepareEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: FaultScenario


class FaultPublishedEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: StrictStr = Field(..., min_length=1, max_length=256)
    qos: Literal[1]
    command: CommandMessage
    published: Literal[True]


class FaultObservationEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation: ObservationMessage
    replayed: StrictBool = False


class FaultTimeoutEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_ms: StrictInt = Field(..., ge=1)
    error_message: StrictStr = Field(..., min_length=1, max_length=256)


class FaultServiceDisconnectedEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: StrictStr = Field(..., min_length=1, max_length=64)
    connected: Literal[False] = False
    last_will: Literal[False] = False


class FaultServiceRestoredEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: StrictStr = Field(..., min_length=1, max_length=64)
    connected: Literal[True] = True


class FaultFinalEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: FaultRunResult
    summary: StrictStr = Field(..., min_length=1, max_length=512)


FaultEvidencePayload: TypeAlias = (
    FaultPrepareEvidencePayload
    | FaultPublishedEvidencePayload
    | FaultObservationEvidencePayload
    | FaultTimeoutEvidencePayload
    | FaultServiceDisconnectedEvidencePayload
    | FaultServiceRestoredEvidencePayload
    | FaultFinalEvidencePayload
)

_EVIDENCE_PAYLOAD_TYPES: dict[FaultEvidenceStage, type[BaseModel]] = {
    "prepare": FaultPrepareEvidencePayload,
    "published": FaultPublishedEvidencePayload,
    "observation": FaultObservationEvidencePayload,
    "timeout": FaultTimeoutEvidencePayload,
    "service_disconnected": FaultServiceDisconnectedEvidencePayload,
    "service_restored": FaultServiceRestoredEvidencePayload,
    "final": FaultFinalEvidencePayload,
}


class FaultEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: StrictStr = Field(..., min_length=1, max_length=64)
    timestamp_ms: StrictInt = Field(..., ge=0)
    stage: FaultEvidenceStage
    task_id: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    seq: StrictInt | None = Field(default=None, ge=1)
    payload: FaultEvidencePayload

    @model_validator(mode="after")
    def validate_stage_payload(self) -> "FaultEvidence":
        expected = _EVIDENCE_PAYLOAD_TYPES[self.stage]
        if not isinstance(self.payload, expected):
            raise ValueError(f"stage {self.stage} requires {expected.__name__}")
        if self.stage in {"published", "observation", "timeout"}:
            if self.task_id is None or self.seq is None:
                raise ValueError(f"stage {self.stage} requires task_id and seq")
        elif self.task_id is not None or self.seq is not None:
            raise ValueError(f"stage {self.stage} does not accept task_id or seq")
        return self


class FaultRunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    run_id: StrictStr = Field(..., min_length=1, max_length=64)
    scenario: FaultScenario
    status: FaultRunState
    accepted_at_ms: StrictInt = Field(..., ge=0)
    started_at_ms: StrictInt | None = Field(default=None, ge=0)
    finished_at_ms: StrictInt | None = Field(default=None, ge=0)
    result: FaultRunResult | None = None
    summary: StrictStr | None = Field(default=None, max_length=512)
    evidence: list[FaultEvidence] = Field(default_factory=list, max_length=MAX_FAULT_EVIDENCE)


class FaultEvidenceOverflow(RuntimeError):
    """Raised before the bounded journal would silently discard evidence."""


class FaultRunner(Protocol):
    def __call__(
        self,
        request: FaultRunRequest,
        run_id: str,
        emit: "FaultEvidenceEmitter",
    ) -> tuple[FaultRunResult, str]: ...


class FaultCleanup(Protocol):
    def __call__(self, request: FaultRunRequest, run_id: str) -> None: ...


class FaultEvidenceEmitter(Protocol):
    def __call__(
        self,
        *,
        stage: FaultEvidenceStage,
        payload: FaultEvidencePayload,
        task_id: str | None = None,
        seq: int | None = None,
    ) -> FaultEvidence: ...


class FaultCoordinator:
    """Run one bounded fault scenario and retain only its current evidence journal."""

    def __init__(
        self,
        runner: FaultRunner,
        *,
        cleanup_handler: FaultCleanup | None = None,
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[str], str] | None = None,
        run_gate: RuntimeRunGate | None = None,
        run_listener: Callable[[FaultRunSnapshot], None] | None = None,
        completion_listener: Callable[[FaultRunSnapshot], None] | None = None,
    ) -> None:
        self._runner = runner
        self._cleanup_handler = cleanup_handler
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex[:16]}")
        self._run_gate = run_gate or RuntimeRunGate()
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fault-run")
        self._current: FaultRunSnapshot | None = None
        self._active_run_id: str | None = None
        self._run_lease: RuntimeRunLease | None = None
        self._run_listeners: list[Callable[[FaultRunSnapshot], None]] = []
        self._completion_listeners: list[Callable[[FaultRunSnapshot], None]] = []
        if run_listener is not None:
            self._run_listeners.append(run_listener)
        if completion_listener is not None:
            self._completion_listeners.append(completion_listener)

    def add_run_listener(self, listener: Callable[[FaultRunSnapshot], None]) -> None:
        with self._lock:
            if listener not in self._run_listeners:
                self._run_listeners.append(listener)

    def add_completion_listener(self, listener: Callable[[FaultRunSnapshot], None]) -> None:
        with self._lock:
            if listener not in self._completion_listeners:
                self._completion_listeners.append(listener)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active_run_id is not None

    @property
    def run_gate(self) -> RuntimeRunGate:
        return self._run_gate

    def submit(self, request: FaultRunRequest) -> FaultRunAccepted:
        with self._lock:
            run_id = self._id_factory("fault")
            try:
                lease = self._run_gate.acquire("fault", run_id)
            except RuntimeRunGateBusyError:
                raise
            accepted_at = self._clock_ms()
            self._current = FaultRunSnapshot(
                run_id=run_id,
                scenario=request.scenario,
                status="accepted",
                accepted_at_ms=accepted_at,
            )
            self._active_run_id = run_id
            self._run_lease = lease
            try:
                self._executor.submit(self._run, request, run_id)
            except Exception:
                self._active_run_id = None
                self._run_lease = None
                self._run_gate.release(lease)
                raise
        return FaultRunAccepted(run_id=run_id)

    def current(self) -> FaultRunSnapshot | None:
        with self._lock:
            return self._current.model_copy(deep=True) if self._current is not None else None

    def wait_for_idle(self, timeout_s: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.active:
                return True
            time.sleep(0.005)
        return not self.active

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _run(self, request: FaultRunRequest, run_id: str) -> None:
        started_at = self._clock_ms()
        with self._lock:
            snapshot = self._require_current(run_id)
            snapshot.status = "running"
            snapshot.started_at_ms = started_at
            started_snapshot = snapshot.model_copy(deep=True)
        self._notify_run(started_snapshot)
        result: FaultRunResult = "failed"
        summary = "fault run failed"
        try:
            outcome = self._runner(request, run_id, self._emit)
            result, summary = outcome
            final = FaultFinalEvidencePayload(result=result, summary=summary)
        except FaultEvidenceOverflow:
            result = "failed"
            summary = "fault evidence journal overflowed its 64-entry limit"
            final = FaultFinalEvidencePayload(result=result, summary=summary)
        except Exception as exc:
            LOGGER.exception("fault run failed run_id=%s", run_id)
            result = "failed"
            summary = f"fault runner failed: {type(exc).__name__}: {exc}"[:512]
            final = FaultFinalEvidencePayload(result=result, summary=summary)
        try:
            if self._cleanup_handler is not None:
                self._cleanup_handler(request, run_id)
        except Exception as exc:
            LOGGER.exception("fault cleanup failed run_id=%s", run_id)
            result = "failed"
            summary = f"fault cleanup failed: {type(exc).__name__}: {exc}"[:512]
            final = FaultFinalEvidencePayload(result=result, summary=summary)
        finally:
            with self._lock:
                try:
                    self._emit_locked(stage="final", payload=final)
                except FaultEvidenceOverflow:
                    LOGGER.error("fault final evidence could not be recorded run_id=%s", run_id)
                snapshot = self._require_current(run_id)
                snapshot.status = "finished"
                snapshot.finished_at_ms = self._clock_ms()
                snapshot.result = result
                snapshot.summary = summary
                lease = self._run_lease
                self._active_run_id = None
                self._run_lease = None
                completed_snapshot = snapshot.model_copy(deep=True)
            self._notify_completion(completed_snapshot)
            if lease is not None:
                self._run_gate.release(lease)

    def _emit(
        self,
        *,
        stage: FaultEvidenceStage,
        payload: FaultEvidencePayload,
        task_id: str | None = None,
        seq: int | None = None,
    ) -> FaultEvidence:
        if stage == "final":
            raise ValueError("fault final evidence is coordinator-owned")
        with self._lock:
            return self._emit_locked(stage=stage, payload=payload, task_id=task_id, seq=seq)

    def _emit_locked(
        self,
        *,
        stage: FaultEvidenceStage,
        payload: FaultEvidencePayload,
        task_id: str | None = None,
        seq: int | None = None,
    ) -> FaultEvidence:
        if self._current is None:
            raise RuntimeError("cannot emit fault evidence without a current run")
        if len(self._current.evidence) >= MAX_FAULT_EVIDENCE:
            raise FaultEvidenceOverflow("fault evidence journal reached its 64 event limit")
        if stage != "final" and len(self._current.evidence) >= MAX_FAULT_EVIDENCE - 1:
            raise FaultEvidenceOverflow("fault evidence journal must reserve a final entry")
        evidence = FaultEvidence(
            event_id=self._id_factory("fault-evt"),
            timestamp_ms=self._clock_ms(),
            stage=stage,
            task_id=task_id,
            seq=seq,
            payload=payload,
        )
        self._current.evidence.append(evidence)
        return evidence

    def _require_current(self, run_id: str) -> FaultRunSnapshot:
        if self._current is None or self._current.run_id != run_id:
            raise RuntimeError(f"fault run state is unavailable for {run_id}")
        return self._current

    def _notify_run(self, snapshot: FaultRunSnapshot) -> None:
        with self._lock:
            listeners = tuple(self._run_listeners)
        for listener in listeners:
            try:
                listener(snapshot.model_copy(deep=True))
            except Exception:
                LOGGER.exception("fault run listener failed run_id=%s", snapshot.run_id)

    def _notify_completion(self, snapshot: FaultRunSnapshot) -> None:
        with self._lock:
            listeners = tuple(self._completion_listeners)
        for listener in listeners:
            try:
                listener(snapshot.model_copy(deep=True))
            except Exception:
                LOGGER.exception("fault completion listener failed run_id=%s", snapshot.run_id)


class _FaultPublishObserver:
    def __init__(self, emit: FaultEvidenceEmitter) -> None:
        self._emit = emit

    def on_published(self, command: CommandMessage, *, topic: str, qos: int) -> None:
        self._emit(
            stage="published",
            task_id=command.task_id,
            seq=command.seq,
            payload=FaultPublishedEvidencePayload(
                topic=topic,
                qos=qos,
                command=command,
                published=True,
            ),
        )


class MqttFaultRunner:
    """Run fixed fault scenarios through the local MQTT request/response path."""

    def __init__(
        self,
        service: MqttDeviceService,
        config: MqttConfig,
        topics: MqttTopics,
    ) -> None:
        self.service = service
        self.config = config
        self.topics = topics

    def __call__(
        self,
        request: FaultRunRequest,
        run_id: str,
        emit: FaultEvidenceEmitter,
    ) -> tuple[FaultRunResult, str]:
        emit(stage="prepare", payload=FaultPrepareEvidencePayload(scenario=request.scenario))
        if request.scenario == "device_disconnect":
            return self._device_disconnect(run_id, emit)
        if request.scenario == "response_timeout":
            return self._response_timeout(run_id, emit)
        if request.scenario == "duplicate_delivery":
            return self._duplicate_delivery(run_id, emit)
        return self._out_of_order(run_id, emit)

    def cleanup(self, request: FaultRunRequest, run_id: str) -> None:
        self.service.clear_fault_injection()
        self.service.restore_after_fault()

    def _device_disconnect(
        self,
        run_id: str,
        emit: FaultEvidenceEmitter,
    ) -> tuple[FaultRunResult, str]:
        self.service.disconnect_for_fault()
        if self.service.connected:
            return "failed", "device service remained connected after controlled disconnect"
        emit(
            stage="service_disconnected",
            payload=FaultServiceDisconnectedEvidencePayload(device_id=self.service.backend.device_id),
        )
        timeout_observation = self._request_state(
            run_id,
            1,
            emit,
            timeout_s=min(self.config.response_timeout_s, FAULT_TIMEOUT_S),
        )
        if timeout_observation is not None:
            return "failed", "device returned an Observation while disconnected"
        self.service.restore_after_fault()
        if not self.service.connected:
            return "failed", "device service did not reconnect after controlled restore"
        emit(
            stage="service_restored",
            payload=FaultServiceRestoredEvidencePayload(device_id=self.service.backend.device_id),
        )
        restored = self._request_state(
            run_id,
            2,
            emit,
            timeout_s=min(self.config.response_timeout_s, FAULT_COMMAND_TIMEOUT_S),
        )
        if restored is None or restored.status is not ObservationStatus.SUCCESS:
            return "failed", "restored device did not complete the MQTT round trip"
        return "passed", "device service disconnected cleanly and recovered"

    def _response_timeout(
        self,
        run_id: str,
        emit: FaultEvidenceEmitter,
    ) -> tuple[FaultRunResult, str]:
        self.service.suppress_next_observation()
        observation = self._request_state(
            run_id,
            1,
            emit,
            timeout_s=min(self.config.response_timeout_s, FAULT_TIMEOUT_S),
        )
        if observation is not None:
            return "failed", "one-shot Observation suppression did not take effect"
        return "passed", "caller timed out after a real published Command"

    def _duplicate_delivery(
        self,
        run_id: str,
        emit: FaultEvidenceEmitter,
    ) -> tuple[FaultRunResult, str]:
        before = self.service.backend.telemetry().state
        before_count = self._motion_execution_count()
        command = self._command(
            run_id,
            1,
            "turn_robot",
            {"angle_deg": 30.0, "angular_speed_dps": 180.0},
        )
        first = self._request_command(
            command,
            emit,
            timeout_s=min(self.config.response_timeout_s, FAULT_COMMAND_TIMEOUT_S),
            replayed=False,
        )
        second = self._request_command(
            command,
            emit,
            timeout_s=min(self.config.response_timeout_s, FAULT_COMMAND_TIMEOUT_S),
            replayed=True,
        )
        if first is None or second is None:
            return "failed", "duplicate delivery did not receive both Observations"
        if first.model_dump(mode="json") != second.model_dump(mode="json"):
            return "failed", "duplicate delivery returned different Observations"
        after = self.service.backend.telemetry().state
        yaw_delta = (after.yaw_deg - before.yaw_deg + 180.0) % 360.0 - 180.0
        if abs(yaw_delta - 30.0) > 1e-6:
            return "failed", f"duplicate delivery changed yaw by {yaw_delta:.6f} degrees"
        if self._motion_execution_count() != before_count + 1:
            return "failed", "duplicate delivery executed motion more than once"
        return "passed", "same command replayed without a second turn"

    def _out_of_order(
        self,
        run_id: str,
        emit: FaultEvidenceEmitter,
    ) -> tuple[FaultRunResult, str]:
        before = self.service.backend.telemetry().state
        before_pose = (before.x_m, before.y_m, before.yaw_deg)
        before_count = self._motion_execution_count()
        current = self._request_command(
            self._command(run_id, 2, "get_robot_state", {}),
            emit,
            timeout_s=min(self.config.response_timeout_s, FAULT_COMMAND_TIMEOUT_S),
        )
        stale = self._request_command(
            self._command(run_id, 1, "get_robot_state", {}),
            emit,
            timeout_s=min(self.config.response_timeout_s, FAULT_COMMAND_TIMEOUT_S),
        )
        if current is None or current.status is not ObservationStatus.SUCCESS:
            return "failed", "seq 2 state request did not succeed"
        if (
            stale is None
            or stale.status is not ObservationStatus.REJECTED
            or stale.error_code != "stale_sequence"
        ):
            return "failed", "seq 1 state request was not rejected as stale_sequence"
        after = self.service.backend.telemetry().state
        after_pose = (after.x_m, after.y_m, after.yaw_deg)
        if after_pose != before_pose:
            return "failed", "out-of-order state requests changed the authoritative pose"
        if self._motion_execution_count() != before_count:
            return "failed", "out-of-order state requests changed motion execution count"
        return "passed", "older sequence was rejected without changing pose"

    def _request_state(
        self,
        run_id: str,
        seq: int,
        emit: FaultEvidenceEmitter,
        *,
        timeout_s: float,
    ) -> ObservationMessage | None:
        return self._request_command(
            self._command(run_id, seq, "get_robot_state", {}),
            emit,
            timeout_s=timeout_s,
        )

    def _request_command(
        self,
        command: CommandMessage,
        emit: FaultEvidenceEmitter,
        *,
        timeout_s: float,
        replayed: bool = False,
    ) -> ObservationMessage | None:
        try:
            with MqttRequestClient(
                self.config,
                self.topics,
                client_id=f"fault-agent-{command.task_id[:20]}-{command.seq}",
                publish_observer=_FaultPublishObserver(emit),
            ) as client:
                observation = client.execute(command, timeout_s=timeout_s)
        except MqttResponseTimeout as exc:
            emit(
                stage="timeout",
                task_id=command.task_id,
                seq=command.seq,
                payload=FaultTimeoutEvidencePayload(
                    timeout_ms=max(1, int(timeout_s * 1_000)),
                    error_message=str(exc),
                ),
            )
            return None
        except (MqttConnectionError, MqttTransportError):
            raise
        emit(
            stage="observation",
            task_id=observation.task_id,
            seq=observation.seq,
            payload=FaultObservationEvidencePayload(
                observation=observation,
                replayed=replayed,
            ),
        )
        return observation

    @staticmethod
    def _command(
        run_id: str,
        seq: int,
        tool: ToolName,
        params: dict[str, Any],
    ) -> CommandMessage:
        return CommandMessage(
            version=1,
            task_id=f"{run_id[:48]}-task",
            seq=seq,
            tool=tool,
            params=params,
            deadline_ms=10_000,
            sent_at_ms=int(time.time() * 1_000),
        )

    def _motion_execution_count(self) -> int:
        count = getattr(self.service.backend, "executed_motion_count", None)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RuntimeError("fault runner requires a public motion execution count")
        return count


__all__ = [
    "FaultCoordinator",
    "FaultCleanup",
    "FaultEvidence",
    "FaultEvidenceEmitter",
    "FaultEvidenceOverflow",
    "FaultEvidencePayload",
    "FaultEvidenceStage",
    "FaultFinalEvidencePayload",
    "FaultObservationEvidencePayload",
    "FaultPrepareEvidencePayload",
    "FaultPublishedEvidencePayload",
    "FaultRunAccepted",
    "FaultRunRequest",
    "FaultRunResult",
    "FaultRunSnapshot",
    "FaultRunState",
    "FaultRunner",
    "FaultScenario",
    "FaultServiceDisconnectedEvidencePayload",
    "FaultServiceRestoredEvidencePayload",
    "FaultTimeoutEvidencePayload",
    "FAULT_COMMAND_TIMEOUT_S",
    "FAULT_TIMEOUT_S",
    "MqttFaultRunner",
    "MAX_FAULT_EVIDENCE",
]
