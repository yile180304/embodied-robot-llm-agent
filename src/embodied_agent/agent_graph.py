"""Bounded LangGraph Action-Observation loop for the embodied robot MVP."""

from __future__ import annotations

import time
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from .mqtt_transport import MqttConnectionError, MqttResponseTimeout, MqttTransportError
from .schemas import CommandMessage, ObservationMessage, ObservationStatus, RobotState
from .tool_registry import ToolCall, ToolExecutionRecord, ToolExecutor


FinalStatus = Literal[
    "success",
    "rejected",
    "timeout",
    "emergency_stop",
    "cancelled",
    "step_limit",
    "planner_error",
    "transport_error",
]


class PlannerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["tool_call", "final"]
    message: StrictStr = Field(..., min_length=1, max_length=512)
    tool_call: ToolCall | None = None
    final_status: FinalStatus | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "PlannerDecision":
        if self.kind == "tool_call" and self.tool_call is None:
            raise ValueError("tool_call decision requires a ToolCall")
        if self.kind == "final" and self.final_status is None:
            raise ValueError("final decision requires final_status")
        return self

    @classmethod
    def call(cls, name: str, arguments: dict[str, Any], message: str) -> "PlannerDecision":
        return cls(
            kind="tool_call",
            message=message,
            tool_call=ToolCall(name=name, arguments=arguments),
        )

    @classmethod
    def finish(cls, status: FinalStatus, message: str) -> "PlannerDecision":
        return cls(kind="final", final_status=status, message=message)


class AgentStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: StrictInt = Field(..., ge=1)
    planner_message: StrictStr
    tool_call: ToolCall
    command: CommandMessage | None
    observation: ObservationMessage
    published: bool
    started_at_ms: StrictInt = Field(..., ge=0)
    finished_at_ms: StrictInt = Field(..., ge=0)
    latency_ms: StrictInt = Field(..., ge=0)


class AgentRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: StrictStr
    goal: StrictStr
    final_status: FinalStatus
    final_message: StrictStr
    steps: list[AgentStep]
    final_observation: ObservationMessage | None = None
    started_at_ms: StrictInt = Field(..., ge=0)
    finished_at_ms: StrictInt = Field(..., ge=0)
    duration_ms: StrictInt = Field(..., ge=0)


@dataclass(frozen=True)
class PlannerContext:
    goal: str
    task_id: str
    next_seq: int
    step_count: int
    max_steps: int
    last_observation: ObservationMessage | None
    known_state: RobotState | None
    steps: tuple[AgentStep, ...]


class Planner(Protocol):
    def plan(self, context: PlannerContext) -> PlannerDecision: ...


class AgentGraphObserver(Protocol):
    def on_planning(self, context: PlannerContext, *, blocked_triggered: bool) -> None: ...

    def on_tool_call(self, context: PlannerContext, decision: PlannerDecision) -> None: ...

    def on_observation(self, step: AgentStep) -> None: ...

    def on_replanning(self, step: AgentStep, *, next_seq: int) -> None: ...

    def on_final(self, result: AgentRunResult) -> None: ...


class AgentGraphState(TypedDict):
    goal: str
    task_id: str
    next_seq: int
    step_count: int
    max_steps: int
    deadline_ms: int
    response_timeout_s: float | None
    decision: PlannerDecision | None
    last_observation: ObservationMessage | None
    known_state: RobotState | None
    steps: list[AgentStep]
    final_status: FinalStatus | None
    final_message: str | None
    started_at_ms: int
    cancellation_event: threading.Event | None




class AgentGraph:
    """Compile and run a bounded LangGraph around a Planner and ToolExecutor."""

    def __init__(
        self,
        planner: Planner,
        executor: ToolExecutor,
        *,
        observer: AgentGraphObserver | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.observer = observer
        self.cancellation_event = cancellation_event
        self._graph = self._build_graph()

    def run(
        self,
        goal: str,
        *,
        task_id: str | None = None,
        max_steps: int = 8,
        deadline_ms: int = 3_000,
        response_timeout_s: float | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> AgentRunResult:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        if not goal.strip():
            raise ValueError("goal must not be blank")
        started = int(time.time() * 1000)
        initial: AgentGraphState = {
            "goal": goal.strip(),
            "task_id": task_id or f"task-{uuid.uuid4().hex[:12]}",
            "next_seq": 1,
            "step_count": 0,
            "max_steps": max_steps,
            "deadline_ms": deadline_ms,
            "response_timeout_s": response_timeout_s,
            "decision": None,
            "last_observation": None,
            "known_state": None,
            "steps": [],
            "final_status": None,
            "final_message": None,
            "started_at_ms": started,
            "cancellation_event": cancellation_event or self.cancellation_event,
        }
        final = self._graph.invoke(initial, config={"recursion_limit": max_steps * 4 + 8})
        finished = int(time.time() * 1000)
        status = final.get("final_status") or "planner_error"
        message = final.get("final_message") or "agent ended without a final message"
        steps = final.get("steps", [])
        result = AgentRunResult(
            task_id=final["task_id"],
            goal=final["goal"],
            final_status=status,
            final_message=message,
            steps=steps,
            final_observation=final.get("last_observation"),
            started_at_ms=started,
            finished_at_ms=finished,
            duration_ms=max(0, finished - started),
        )
        if self.observer is not None:
            self.observer.on_final(result)
        return result

    def _build_graph(self):
        builder = StateGraph(AgentGraphState)
        builder.add_node("planner", self._planner_node)
        builder.add_node("execute", self._execute_node)
        builder.add_node("decide", self._decide_node)
        builder.add_edge(START, "planner")
        builder.add_conditional_edges(
            "planner",
            self._route_after_planner,
            {"execute": "execute", "end": END},
        )
        builder.add_conditional_edges(
            "execute",
            self._route_after_execute,
            {"decide": "decide", "end": END},
        )
        builder.add_conditional_edges(
            "decide",
            self._route_after_decide,
            {"plan": "planner", "end": END},
        )
        return builder.compile()

    def _planner_node(self, state: AgentGraphState) -> dict[str, Any]:
        if self._is_cancelled(state):
            return {
                "final_status": "cancelled",
                "final_message": "mission cancelled by operator",
                "decision": None,
            }
        if state["step_count"] >= state["max_steps"]:
            return {
                "final_status": "step_limit",
                "final_message": f"达到最大步骤数 {state['max_steps']}，任务安全退出。",
                "decision": None,
            }
        context = PlannerContext(
            goal=state["goal"],
            task_id=state["task_id"],
            next_seq=state["next_seq"],
            step_count=state["step_count"],
            max_steps=state["max_steps"],
            last_observation=state["last_observation"],
            known_state=state["known_state"],
            steps=tuple(state["steps"]),
        )
        if self.observer is not None:
            self.observer.on_planning(
                context,
                blocked_triggered=(
                    state["last_observation"] is not None
                    and state["last_observation"].status is ObservationStatus.BLOCKED
                ),
            )
        try:
            decision = self.planner.plan(context)
        except Exception as exc:  # Provider failures become observable state, not code execution.
            return {
                "final_status": "planner_error",
                "final_message": f"planner failed: {type(exc).__name__}: {exc}"[:512],
                "decision": None,
            }
        if not isinstance(decision, PlannerDecision):
            return {
                "final_status": "planner_error",
                "final_message": "planner returned text or an invalid decision instead of a Tool Call",
                "decision": None,
            }
        if self._is_cancelled(state) and not (
            decision.kind == "final" and decision.final_status == "emergency_stop"
        ):
            return {
                "final_status": "cancelled",
                "final_message": "mission cancelled by operator",
                "decision": None,
            }
        if decision.kind == "final":
            return {
                "decision": decision,
                "final_status": decision.final_status,
                "final_message": decision.message,
            }
        if self.observer is not None:
            self.observer.on_tool_call(context, decision)
        return {"decision": decision}

    def _execute_node(self, state: AgentGraphState) -> dict[str, Any]:
        if self._is_cancelled(state):
            return {
                "final_status": "cancelled",
                "final_message": "mission cancelled by operator",
            }
        decision = state["decision"]
        if decision is None or decision.tool_call is None:
            return {
                "final_status": "planner_error",
                "final_message": "execute node received no Tool Call",
            }
        started = int(time.time() * 1000)
        try:
            record = self.executor.execute(
                decision.tool_call,
                task_id=state["task_id"],
                seq=state["next_seq"],
                deadline_ms=state["deadline_ms"],
                timeout_s=state["response_timeout_s"],
                known_state=state["known_state"],
                now_ms=started,
            )
        except MqttResponseTimeout as exc:
            record = self._transport_failure_record(
                decision.tool_call,
                state,
                started,
                ObservationStatus.TIMEOUT,
                "mqtt_response_timeout",
                str(exc),
                published=True,
            )
        except (MqttConnectionError, MqttTransportError) as exc:
            record = self._transport_failure_record(
                decision.tool_call,
                state,
                started,
                ObservationStatus.REJECTED,
                "mqtt_transport_error",
                str(exc),
                published=False,
            )
        finished = int(time.time() * 1000)
        step = AgentStep(
            seq=state["next_seq"],
            planner_message=decision.message,
            tool_call=record.tool_call,
            command=record.command,
            observation=record.observation,
            published=record.published,
            started_at_ms=started,
            finished_at_ms=finished,
            latency_ms=max(0, finished - started),
        )
        if self.observer is not None:
            self.observer.on_observation(step)
        return {
            "last_observation": record.observation,
            "known_state": self._known_state_from_observation(
                record.observation,
                state["known_state"],
            ),
            "steps": [*state["steps"], step],
            "step_count": state["step_count"] + 1,
            "next_seq": state["next_seq"] + 1,
        }

    def _decide_node(self, state: AgentGraphState) -> dict[str, Any]:
        observation = state["last_observation"]
        if observation is None:
            return {
                "final_status": "transport_error",
                "final_message": "execution produced no Observation",
            }
        if observation.status is ObservationStatus.EMERGENCY_STOP or observation.error_code in {
            "emergency_cancelled",
            "emergency_stopped",
        }:
            return {
                "final_status": "emergency_stop",
                "final_message": observation.error_message or "device entered emergency stop",
            }
        if self._is_cancelled(state) or observation.error_code == "operator_cancelled":
            return {
                "final_status": "cancelled",
                "final_message": observation.error_message or "mission cancelled by operator",
            }
        if observation.error_code == "mqtt_transport_error":
            return {
                "final_status": "transport_error",
                "final_message": observation.error_message or "MQTT transport failed",
            }
        if observation.status is ObservationStatus.TIMEOUT:
            return {
                "final_status": "timeout",
                "final_message": observation.error_message or "device response timed out",
            }
        if observation.status is ObservationStatus.BLOCKED and self.observer is not None:
            self.observer.on_replanning(state["steps"][-1], next_seq=state["next_seq"])
        return {}

    @staticmethod
    def _route_after_planner(state: AgentGraphState) -> str:
        return "end" if state.get("final_status") else "execute"

    @staticmethod
    def _route_after_execute(state: AgentGraphState) -> str:
        return "end" if state.get("final_status") else "decide"

    @staticmethod
    def _route_after_decide(state: AgentGraphState) -> str:
        return "end" if state.get("final_status") else "plan"

    @staticmethod
    def _transport_failure_record(
        tool_call: ToolCall,
        state: AgentGraphState,
        received_at_ms: int,
        status: ObservationStatus,
        code: str,
        message: str,
        *,
        published: bool,
    ) -> ToolExecutionRecord:
        observation = ObservationMessage(
            version=1,
            task_id=state["task_id"],
            seq=state["next_seq"],
            status=status,
            observation=(
                state["known_state"].model_dump(mode="json")
                if state["known_state"] is not None
                else {}
            ),
            error_code=code,
            error_message=message[:256],
            received_at_ms=received_at_ms,
        )
        return ToolExecutionRecord(tool_call, None, observation, published)

    @staticmethod
    def _known_state_from_observation(
        observation: ObservationMessage,
        current: RobotState | None,
    ) -> RobotState | None:
        raw = observation.observation.get("state", observation.observation)
        if not isinstance(raw, dict):
            return current
        base = current.model_dump(mode="json") if current is not None else RobotState().model_dump(mode="json")
        for field_name in RobotState.model_fields:
            if field_name in raw:
                base[field_name] = raw[field_name]
        try:
            return RobotState.model_validate(base)
        except Exception:
            return current

    @staticmethod
    def _is_cancelled(state: AgentGraphState) -> bool:
        event = state.get("cancellation_event")
        return event is not None and event.is_set()


def __getattr__(name: str) -> Any:
    if name == "FakePlanner":
        from .fake_planner import FakePlanner

        globals()[name] = FakePlanner
        return FakePlanner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AgentGraph",
    "AgentGraphObserver",
    "AgentRunResult",
    "AgentStep",
    "FakePlanner",
    "FinalStatus",
    "Planner",
    "PlannerContext",
    "PlannerDecision",
]
