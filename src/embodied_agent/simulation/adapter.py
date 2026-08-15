"""MQTT command adapter for the continuous simulation world."""

from __future__ import annotations

import json
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from ..safety import SafetyGuard, SafetyStatus
from ..schemas import (
    CommandMessage,
    ObservationMessage,
    ObservationStatus,
    RobotState,
    TelemetryMessage,
)
from .engine import SimulationAction, SimulationActionCompletion, SimulationEngine
from .models import SimulationFrame
from .semantic import query_semantic_world


Clock = Callable[[], int]
ObservationCallback = Callable[[ObservationMessage], None]
CacheKey = tuple[str, int]


@dataclass(frozen=True)
class _CachedResult:
    fingerprint: str
    observation: ObservationMessage


@dataclass
class _PendingCommand:
    command: CommandMessage
    fingerprint: str
    callbacks: list[ObservationCallback]
    action: SimulationAction | None = None
    started_at_ms: int | None = None


class SimulationAdapter:
    """Validate MQTT commands and bridge them into one simulation tick owner."""

    def __init__(
        self,
        engine: SimulationEngine,
        *,
        device_id: str = "dog01",
        safety_guard: SafetyGuard | None = None,
        queue_size: int = 8,
        cache_size: int = 256,
        clock_ms: Clock | None = None,
    ) -> None:
        if engine.demo_mode:
            raise ValueError("SimulationAdapter requires a bridge-mode engine")
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must be a non-empty string")
        if queue_size < 1 or cache_size < 1:
            raise ValueError("queue_size and cache_size must be positive")
        self.engine = engine
        self.device_id = device_id.strip()
        self.queue_size = queue_size
        self._cache_size = cache_size
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._safety = safety_guard or SafetyGuard()
        self._lock = threading.RLock()
        self._pending: dict[CacheKey, _PendingCommand] = {}
        self._completed: OrderedDict[CacheKey, _CachedResult] = OrderedDict()
        self._latest_seq: dict[str, int] = {}
        self._normal_queue: deque[CacheKey] = deque()
        self._emergency_queue: deque[CacheKey] = deque()
        self._cancel_task_ids: set[str] = set()
        self._cancelled_tasks: OrderedDict[str, None] = OrderedDict()
        self._active_key: CacheKey | None = None
        self._last_task_id: str | None = None
        self._last_seq: int | None = None
        self._latest_frame = engine.snapshot()
        self._latest_state = self._state_from_frame(self._latest_frame)
        self._emergency_latched = False
        self.executed_motion_count = 0
        self.event_log: list[dict[str, Any]] = []

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def active_key(self) -> CacheKey | None:
        with self._lock:
            return self._active_key

    def submit_payload(self, payload: bytes | str | Mapping[str, Any], on_complete: ObservationCallback) -> None:
        """Accept an untrusted payload without waiting for continuous motion."""

        if not callable(on_complete):
            raise TypeError("on_complete must be callable")
        received_at = self._now()
        try:
            raw: Any
            if isinstance(payload, (bytes, bytearray, str)):
                raw = json.loads(payload)
            else:
                raw = payload
            command = CommandMessage.model_validate(raw)
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._emit(on_complete, self._schema_rejection(payload, exc, received_at))
            return

        key = (command.task_id, command.seq)
        fingerprint = command.fingerprint()
        immediate: ObservationMessage | None = None
        with self._lock:
            self._log("receive", command, None)
            cached = self._completed.get(key)
            cancelled = (
                command.tool != "emergency_stop"
                and command.task_id in self._cancelled_tasks
                and not self._emergency_latched
                and not self._emergency_queue
            )
            if cached is not None:
                self._completed.move_to_end(key)
                if cached.fingerprint == fingerprint:
                    self._log("duplicate", command, cached.observation)
                    immediate = cached.observation.model_copy(deep=True)
                else:
                    immediate = self._feedback(
                        command,
                        ObservationStatus.REJECTED,
                        received_at,
                        code="duplicate_conflict",
                        message="same task_id and seq carried different command data",
                    )
                    self._log("duplicate_conflict", command, immediate)
            elif cancelled:
                immediate = self._feedback(
                    command,
                    ObservationStatus.REJECTED,
                    received_at,
                    code="operator_cancelled",
                    message="command cancelled by operator",
                )
                self._remember(command, immediate, fingerprint)
                self._log("operator_cancelled", command, immediate)
            else:
                pending = self._pending.get(key)
                if pending is not None:
                    if pending.fingerprint == fingerprint:
                        pending.callbacks.append(on_complete)
                        self._log("duplicate_pending", command, None)
                        return
                    immediate = self._feedback(
                        command,
                        ObservationStatus.REJECTED,
                        received_at,
                        code="duplicate_conflict",
                        message="same task_id and seq carried different command data",
                    )
                    self._log("duplicate_conflict", command, immediate)
                else:
                    latest = self._latest_seq.get(command.task_id)
                    if latest is not None and command.seq < latest:
                        immediate = self._feedback(
                            command,
                            ObservationStatus.REJECTED,
                            received_at,
                            code="stale_sequence",
                            message=f"sequence {command.seq} is older than latest sequence {latest}",
                        )
                        self._remember(command, immediate, fingerprint, update_latest=False)
                        self._log("stale_sequence", command, immediate)
                    else:
                        decision = self._safety.check(command, self._latest_state, received_at)
                        if decision.status is not SafetyStatus.ALLOW:
                            status = self._status_from_safety(decision.status)
                            immediate = self._feedback(
                                command,
                                status,
                                received_at,
                                code=decision.code,
                                message=decision.message,
                                extra={"reason": decision.code} if status is ObservationStatus.BLOCKED else None,
                            )
                            self._remember(command, immediate, fingerprint)
                            if status is ObservationStatus.TIMEOUT:
                                event = "timeout"
                            elif status is ObservationStatus.BLOCKED:
                                event = "blocked"
                            else:
                                event = "safety_rejected"
                            self._log(event, command, immediate)
                        elif command.tool in {"get_robot_state", "scan_obstacles", "inspect_semantic_world"}:
                            immediate = self._query_observation(command, received_at)
                            self._remember(command, immediate, fingerprint)
                            self._log("completed", command, immediate)
                        elif command.tool == "emergency_stop":
                            self._pending[key] = _PendingCommand(command, fingerprint, [on_complete])
                            self._latest_seq[command.task_id] = command.seq
                            self._emergency_queue.append(key)
                            self._log("queued", command, None)
                            return
                        else:
                            action = self._action_from_command(command)
                            budget_error = self._budget_error(command, action, received_at)
                            if budget_error is not None:
                                immediate = self._feedback(
                                    command,
                                    ObservationStatus.TIMEOUT,
                                    received_at,
                                    code=budget_error,
                                    message="motion cannot finish within its available timeout budget",
                                )
                                self._remember(command, immediate, fingerprint)
                                self._log("timeout", command, immediate)
                            elif len(self._normal_queue) >= self.queue_size:
                                immediate = self._feedback(
                                    command,
                                    ObservationStatus.REJECTED,
                                    received_at,
                                    code="device_busy",
                                    message="simulation action queue is full",
                                )
                                self._remember(command, immediate, fingerprint)
                                self._log("device_busy", command, immediate)
                            else:
                                self._pending[key] = _PendingCommand(
                                    command,
                                    fingerprint,
                                    [on_complete],
                                    action=action,
                                )
                                self._latest_seq[command.task_id] = command.seq
                                self._normal_queue.append(key)
                                self._log("queued", command, None)
                                return
        if immediate is not None:
            self._emit(on_complete, immediate)

    def telemetry(self) -> TelemetryMessage:
        with self._lock:
            state = self._latest_state.model_copy(deep=True)
        return TelemetryMessage(
            version=1,
            device_id=self.device_id,
            state=state,
            reported_at_ms=self._now(),
        )

    def update_frame(self, frame: SimulationFrame) -> None:
        with self._lock:
            self._latest_frame = frame.model_copy(deep=True)
            self._latest_state = self._state_from_frame(self._latest_frame)

    def before_tick(self, now_ms: int) -> None:
        """Run priority controls, expiry and one normal action on the tick owner."""

        self._process_emergency(now_ms)
        self._expire_queued(now_ms)
        self._process_operator_cancellation(now_ms)
        with self._lock:
            active_key = self._active_key
            pending = self._pending.get(active_key) if active_key is not None else None
        if pending is not None:
            timeout_code = self._active_timeout_code(pending, now_ms)
            if timeout_code is not None:
                action = self.engine.cancel_action()
                frame = self.engine.snapshot()
                self.update_frame(frame)
                if action is not None:
                    observation = self._feedback_for_action(
                        pending.command,
                        ObservationStatus.TIMEOUT,
                        now_ms,
                        code=timeout_code,
                        message="active simulation action exceeded its timeout budget",
                        frame=frame,
                    )
                    self._complete(active_key, observation, "timeout")
                return
            if not self.engine.paused and self._active_would_miss_deadline(pending, now_ms):
                action = self.engine.cancel_action()
                frame = self.engine.snapshot()
                self.update_frame(frame)
                if action is not None:
                    observation = self._feedback_for_action(
                        pending.command,
                        ObservationStatus.TIMEOUT,
                        now_ms,
                        code="deadline_budget_exceeded",
                        message="active simulation action cannot finish before command deadline",
                        frame=frame,
                    )
                    self._complete(active_key, observation, "timeout")
                return
            return

        with self._lock:
            if self.engine.paused or self._emergency_latched or not self._normal_queue:
                return
            key = self._normal_queue.popleft()
            pending = self._pending.get(key)
        if pending is None:
            return
        if pending.command.is_expired(now_ms):
            observation = self._feedback(
                pending.command,
                ObservationStatus.TIMEOUT,
                now_ms,
                code="deadline_expired",
                message="command deadline has expired before action start",
            )
            self._complete(key, observation, "timeout")
            return
        budget_error = self._budget_error(pending.command, pending.action, now_ms)
        if budget_error is not None:
            observation = self._feedback(
                pending.command,
                ObservationStatus.TIMEOUT,
                now_ms,
                code=budget_error,
                message="motion cannot finish before action start deadline",
            )
            self._complete(key, observation, "timeout")
            return
        with self._lock:
            if self._emergency_latched:
                return
            pending.started_at_ms = now_ms
            self._active_key = key
        if pending.action is None:
            raise RuntimeError("motion pending command has no SimulationAction")
        self.engine.submit_action(pending.action)
        with self._lock:
            self.executed_motion_count += 1
        self._log("started", pending.command, None)

    def after_tick(self, now_ms: int) -> None:
        completion = self.engine.take_action_completion()
        if completion is None:
            return
        key = (completion.action.task_id, completion.action.seq)
        with self._lock:
            pending = self._pending.get(key)
        if pending is None:
            return
        frame = self.engine.snapshot()
        self.update_frame(frame)
        if self._active_timeout_code(pending, now_ms) is not None:
            observation = self._feedback_for_action(
                pending.command,
                ObservationStatus.TIMEOUT,
                now_ms,
                code="action_timeout",
                message="simulation action completed outside its timeout budget",
                frame=frame,
            )
            self._complete(key, observation, "timeout")
            return
        if completion.status == "blocked":
            observation = self._feedback_for_action(
                pending.command,
                ObservationStatus.BLOCKED,
                now_ms,
                code=completion.reason,
                message="simulation movement stopped before an obstacle or world boundary",
                extra=self._blocked_completion_extra(pending.command, completion, frame),
                frame=frame,
            )
            self._complete(key, observation, "blocked")
            return
        observation = self._feedback_for_action(
            pending.command,
            ObservationStatus.SUCCESS,
            now_ms,
            extra=self._completion_extra(pending.command, completion, frame),
            frame=frame,
        )
        self._complete(key, observation, "completed")

    def cancel_all(self, *, code: str = "simulation_reset", now_ms: int | None = None) -> None:
        """Complete all pending callbacks before an operator reset or runtime stop."""

        now = self._now() if now_ms is None else now_ms
        self.engine.cancel_action()
        frame = self.engine.snapshot()
        self.update_frame(frame)
        with self._lock:
            keys = list(self._pending)
            self._normal_queue.clear()
            self._emergency_queue.clear()
            self._cancel_task_ids.clear()
            self._active_key = None
            self._emergency_latched = False
        for key in keys:
            with self._lock:
                pending = self._pending.get(key)
            if pending is None:
                continue
            observation = self._feedback_for_action(
                pending.command,
                ObservationStatus.REJECTED,
                now,
                code=code,
                message="pending simulation action was cancelled by operator control",
                frame=frame,
            )
            self._complete(key, observation, "cancelled")

    def cancel_task(self, task_id: str) -> bool:
        """Request cancellation for one task; Emergency is resolved first on the next tick."""

        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        with self._lock:
            matched = any(key[0] == task_id for key in self._pending)
            self._cancel_task_ids.add(task_id)
            self._cancelled_tasks[task_id] = None
            self._cancelled_tasks.move_to_end(task_id)
            while len(self._cancelled_tasks) > self._cache_size:
                self._cancelled_tasks.popitem(last=False)
        return matched

    def _process_operator_cancellation(self, now_ms: int) -> None:
        with self._lock:
            if not self._cancel_task_ids:
                return
            if self._emergency_latched:
                self._cancel_task_ids.clear()
                return
            if self._emergency_queue:
                return
            task_ids = set(self._cancel_task_ids)
            self._cancel_task_ids.clear()
            keys = [
                key
                for key, pending in self._pending.items()
                if key[0] in task_ids and pending.command.tool != "emergency_stop"
            ]
            active_key = self._active_key
            active_pending = self._pending.get(active_key) if active_key in keys else None
        if active_pending is not None and active_key is not None:
            self.engine.cancel_action()
        frame = self.engine.snapshot()
        self.update_frame(frame)
        with self._lock:
            cancelled = set(keys)
            self._normal_queue = deque(key for key in self._normal_queue if key not in cancelled)
        for key in keys:
            with self._lock:
                pending = self._pending.get(key)
            if pending is None:
                continue
            observation = self._feedback_for_action(
                pending.command,
                ObservationStatus.REJECTED,
                now_ms,
                code="operator_cancelled",
                message="command cancelled by operator",
                frame=frame,
            )
            self._complete(key, observation, "operator_cancelled")

    def _process_emergency(self, now_ms: int) -> None:
        with self._lock:
            keys = list(self._emergency_queue)
            if not keys:
                return
            self._emergency_queue.clear()
            active_key = self._active_key
            queued_keys = list(self._normal_queue)
            self._normal_queue.clear()
            self._emergency_latched = True
            active_pending = self._pending.get(active_key) if active_key is not None else None
            emergency_pending = [self._pending.get(key) for key in keys]
        active = self.engine.cancel_action(emergency_stopped=True)
        frame = self.engine.snapshot()
        self.update_frame(frame)
        if active is not None and active_pending is not None and active_key is not None:
            observation = self._feedback_for_action(
                active_pending.command,
                ObservationStatus.EMERGENCY_STOP,
                now_ms,
                code="emergency_cancelled",
                message="active simulation action cancelled by emergency stop",
                frame=frame,
            )
            self._complete(active_key, observation, "emergency_cancelled")
        for key in queued_keys:
            with self._lock:
                pending = self._pending.get(key)
            if pending is None:
                continue
            observation = self._feedback_for_action(
                pending.command,
                ObservationStatus.REJECTED,
                now_ms,
                code="emergency_stopped",
                message="motion was queued behind an emergency stop",
                frame=frame,
            )
            self._complete(key, observation, "safety_rejected")
        for pending in emergency_pending:
            if pending is None:
                continue
            observation = self._feedback_for_action(
                pending.command,
                ObservationStatus.EMERGENCY_STOP,
                now_ms,
                extra={"emergency_stopped": True, "reason": pending.command.params["reason"]},
                frame=frame,
            )
            self._complete((pending.command.task_id, pending.command.seq), observation, "completed")

    def _expire_queued(self, now_ms: int) -> None:
        expired: list[CacheKey] = []
        with self._lock:
            for key in tuple(self._normal_queue):
                pending = self._pending.get(key)
                if pending is not None and pending.command.is_expired(now_ms):
                    expired.append(key)
            if expired:
                self._normal_queue = deque(key for key in self._normal_queue if key not in set(expired))
        for key in expired:
            with self._lock:
                pending = self._pending.get(key)
            if pending is None:
                continue
            observation = self._feedback(
                pending.command,
                ObservationStatus.TIMEOUT,
                now_ms,
                code="deadline_expired",
                message="command deadline expired while queued",
            )
            self._complete(key, observation, "timeout")

    def _complete(self, key: CacheKey, observation: ObservationMessage, event: str) -> None:
        with self._lock:
            pending = self._pending.pop(key, None)
            if pending is None:
                return
            if self._active_key == key:
                self._active_key = None
            self._remember(pending.command, observation, pending.fingerprint)
            self._log(event, pending.command, observation)
            callbacks = list(pending.callbacks)
        for callback in callbacks:
            self._emit(callback, observation)

    def _remember(
        self,
        command: CommandMessage,
        observation: ObservationMessage,
        fingerprint: str,
        *,
        update_latest: bool = True,
    ) -> None:
        if update_latest:
            latest = self._latest_seq.get(command.task_id)
            if latest is None or command.seq > latest:
                self._latest_seq[command.task_id] = command.seq
            self._last_task_id = command.task_id
            self._last_seq = command.seq
            self._latest_state = self._latest_state.model_copy(
                update={"last_task_id": command.task_id, "last_seq": command.seq}
            )
        key = (command.task_id, command.seq)
        self._completed[key] = _CachedResult(fingerprint, observation.model_copy(deep=True))
        self._completed.move_to_end(key)
        while len(self._completed) > self._cache_size:
            self._completed.popitem(last=False)

    def _query_observation(self, command: CommandMessage, received_at: int) -> ObservationMessage:
        state = self._latest_state.model_dump(mode="json")
        if command.tool == "get_robot_state":
            return self._feedback(
                command,
                ObservationStatus.SUCCESS,
                received_at,
                extra={"state": state},
            )
        if command.tool == "inspect_semantic_world":
            frame = self._latest_frame
            result = query_semantic_world(
                self.engine.world_config,
                x_m=frame.robot.x_m,
                y_m=frame.robot.y_m,
                yaw_deg=frame.robot.yaw_deg,
                kind=command.params.get("kind"),
                color=command.params.get("color"),
                label=command.params.get("label"),
                max_results=int(command.params.get("max_results", 8)),
            )
            return self._feedback(
                command,
                ObservationStatus.SUCCESS,
                received_at,
                extra=result.model_dump(mode="json"),
            )
        return self._feedback(
            command,
            ObservationStatus.SUCCESS,
            received_at,
            extra={
                "front_distance_cm": self._latest_state.front_distance_cm,
                "left_distance_cm": self._latest_state.left_distance_cm,
                "right_distance_cm": self._latest_state.right_distance_cm,
            },
        )

    def _completion_extra(
        self,
        command: CommandMessage,
        completion: SimulationActionCompletion,
        frame: SimulationFrame,
    ) -> dict[str, Any]:
        state = self._state_from_frame(frame).model_copy(
            update={"last_task_id": command.task_id, "last_seq": command.seq}
        ).model_dump(mode="json")
        if command.tool == "move_robot":
            return {"moved_distance_m": completion.moved_amount, "state": state}
        return {"turned_angle_deg": completion.moved_amount, "state": state}

    def _blocked_completion_extra(
        self,
        command: CommandMessage,
        completion: SimulationActionCompletion,
        frame: SimulationFrame,
    ) -> dict[str, Any]:
        state = self._state_from_frame(frame).model_copy(
            update={"last_task_id": command.task_id, "last_seq": command.seq}
        ).model_dump(mode="json")
        return {
            "reason": completion.reason,
            "requested_distance_m": completion.action.amount,
            "moved_distance_m": completion.moved_amount,
            "remaining_distance_m": completion.remaining_amount,
            "state": state,
        }

    def _feedback_for_action(
        self,
        command: CommandMessage,
        status: ObservationStatus,
        received_at: int,
        *,
        code: str | None = None,
        message: str | None = None,
        extra: Mapping[str, Any] | None = None,
        frame: SimulationFrame,
    ) -> ObservationMessage:
        state = self._state_from_frame(frame).model_copy(
            update={"last_task_id": command.task_id, "last_seq": command.seq}
        )
        details = state.model_dump(mode="json")
        if extra:
            details.update(dict(extra))
        return ObservationMessage(
            version=1,
            task_id=command.task_id,
            seq=command.seq,
            status=status,
            observation=details,
            error_code=code,
            error_message=message,
            received_at_ms=received_at,
        )

    def _feedback(
        self,
        command: CommandMessage,
        status: ObservationStatus,
        received_at: int,
        *,
        code: str | None = None,
        message: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> ObservationMessage:
        details = self._latest_state.model_dump(mode="json")
        if extra:
            details.update(dict(extra))
        return ObservationMessage(
            version=1,
            task_id=command.task_id,
            seq=command.seq,
            status=status,
            observation=details,
            error_code=code,
            error_message=message,
            received_at_ms=received_at,
        )

    def _schema_rejection(self, payload: Any, error: Exception, received_at: int) -> ObservationMessage:
        raw = payload
        if isinstance(payload, (bytes, bytearray, str)):
            try:
                raw = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                raw = payload
        task_id, seq = self._fallback_correlation(raw)
        message = str(error).replace("\n", " ")[:256]
        code = "dangerous_parameter" if self._looks_like_dangerous_payload(raw) else "schema_validation_error"
        observation = ObservationMessage(
            version=1,
            task_id=task_id,
            seq=seq,
            status=ObservationStatus.REJECTED,
            observation=self._latest_state.model_dump(mode="json"),
            error_code=code,
            error_message=message,
            received_at_ms=received_at,
        )
        with self._lock:
            self.event_log.append(
                {
                    "event": "safety_rejected" if code == "dangerous_parameter" else "schema_rejected",
                    "task_id": task_id,
                    "seq": seq,
                    "tool": raw.get("tool") if isinstance(raw, Mapping) else None,
                    "status": observation.status.value,
                    "error_code": code,
                    "received_at_ms": received_at,
                }
            )
        return observation

    def _action_from_command(self, command: CommandMessage) -> SimulationAction:
        if command.tool == "move_robot":
            return SimulationAction(
                command.task_id,
                command.seq,
                "move_robot",
                float(command.params["distance_m"]),
                float(command.params["speed_mps"]),
            )
        if command.tool == "turn_robot":
            return SimulationAction(
                command.task_id,
                command.seq,
                "turn_robot",
                float(command.params["angle_deg"]),
                float(command.params["angular_speed_dps"]),
            )
        raise ValueError(f"tool {command.tool} is not a motion action")

    def _budget_error(
        self,
        command: CommandMessage,
        action: SimulationAction | None,
        now_ms: int,
    ) -> str | None:
        if action is None:
            return None
        duration_ms = max(1, math.ceil(abs(action.amount) / action.rate_per_second * 1_000))
        action_timeout = command.params.get("timeout_ms")
        if action_timeout is not None and duration_ms > int(action_timeout):
            return "action_timeout_budget"
        if duration_ms > max(0, command.sent_at_ms + command.deadline_ms - now_ms):
            return "deadline_budget_exceeded"
        return None

    def _active_timeout_code(self, pending: _PendingCommand, now_ms: int) -> str | None:
        if pending.command.is_expired(now_ms):
            return "deadline_expired"
        timeout_ms = pending.command.params.get("timeout_ms")
        if timeout_ms is not None and pending.started_at_ms is not None:
            if now_ms >= pending.started_at_ms + int(timeout_ms):
                return "action_timeout"
        return None

    def _active_would_miss_deadline(self, pending: _PendingCommand, now_ms: int) -> bool:
        if pending.action is None:
            return False
        progress = 0.0
        with self._lock:
            active = self._latest_frame.active_command
            if active is not None and (active.task_id, active.seq) == self._active_key:
                progress = active.progress
        remaining_ms = math.ceil(
            abs(pending.action.amount) * max(0.0, 1.0 - progress)
            / pending.action.rate_per_second
            * 1_000
        )
        return now_ms + remaining_ms > pending.command.sent_at_ms + pending.command.deadline_ms

    @staticmethod
    def _status_from_safety(status: SafetyStatus) -> ObservationStatus:
        if status is SafetyStatus.BLOCKED:
            return ObservationStatus.BLOCKED
        if status is SafetyStatus.TIMEOUT:
            return ObservationStatus.TIMEOUT
        return ObservationStatus.REJECTED

    def _state_from_frame(self, frame: SimulationFrame) -> RobotState:
        return RobotState(
            x_m=frame.robot.x_m,
            y_m=frame.robot.y_m,
            yaw_deg=frame.robot.yaw_deg,
            gait=frame.robot.gait,
            front_distance_cm=frame.sensors.front_distance_cm,
            left_distance_cm=frame.sensors.left_distance_cm,
            right_distance_cm=frame.sensors.right_distance_cm,
            emergency_stopped=frame.robot.emergency_stopped,
            last_task_id=self._last_task_id,
            last_seq=self._last_seq,
        )

    def _log(
        self,
        event: str,
        command: CommandMessage,
        observation: ObservationMessage | None,
    ) -> None:
        self.event_log.append(
            {
                "event": event,
                "task_id": command.task_id,
                "seq": command.seq,
                "tool": command.tool,
                "status": observation.status.value if observation is not None else None,
                "error_code": observation.error_code if observation is not None else None,
                "received_at_ms": observation.received_at_ms if observation is not None else None,
            }
        )

    def _now(self) -> int:
        value = self._clock_ms()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError("clock must return a non-negative integer")
        return value

    @staticmethod
    def _emit(callback: ObservationCallback, observation: ObservationMessage) -> None:
        try:
            callback(observation.model_copy(deep=True))
        except Exception:
            # The MQTT service logs publish failures; a callback consumer must not
            # break the simulation tick owner.
            return

    @staticmethod
    def _fallback_correlation(payload: Any) -> tuple[str, int]:
        if isinstance(payload, Mapping):
            task_id = payload.get("task_id")
            seq = payload.get("seq")
            safe_task_id = task_id[:64] if isinstance(task_id, str) and task_id.strip() else "invalid-command"
            safe_seq = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1 else 1
            return safe_task_id, safe_seq
        return "invalid-command", 1

    @staticmethod
    def _looks_like_dangerous_payload(payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return False
        tool = payload.get("tool")
        params = payload.get("params")
        if not isinstance(params, Mapping):
            return False
        if tool == "move_robot":
            limits = {"distance_m": (-2.0, 2.0), "speed_mps": (0.05, 0.5)}
        elif tool == "turn_robot":
            limits = {"angle_deg": (-180.0, 180.0), "angular_speed_dps": (5.0, 180.0)}
        else:
            return False
        for field, (lower, upper) in limits.items():
            if field not in params:
                continue
            value = params[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return True
            number = float(value)
            if not math.isfinite(number) or not lower <= number <= upper:
                return True
        return False


__all__ = ["SimulationAdapter"]
