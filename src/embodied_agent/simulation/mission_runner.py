"""Run Mission Tasks through the real AgentGraph, Tool Registry, and MQTT chain."""

from __future__ import annotations

import time
import threading
import uuid
from typing import Callable, cast

from ..agent_graph import (
    AgentGraph,
    AgentRunResult,
    AgentStep,
    PlannerContext,
    PlannerDecision,
)
from ..fake_planner import FakePlanner
from ..model_provider import ModelConfig, OpenAICompatiblePlanner, ProviderConfigurationError
from ..mqtt_transport import MqttConfig, MqttConnectionError, MqttRequestClient, MqttTopics, MqttTransportError
from ..schemas import CommandMessage, ObservationMessage, ObservationStatus, ToolName
from ..tool_registry import ToolCall, ToolExecutionRecord, ToolExecutor
from .mission import (
    FinalPayload,
    MissionTaskRequest,
    MissionTaskResult,
    ObservationPayload,
    PlanningPayload,
    PublishedPayload,
    ReplanningPayload,
    RuntimeEventEmitter,
    SafetyRejectedPayload,
    ToolCallPayload,
)


class RuntimeMissionObserver:
    """Map source callbacks to strict RuntimeEvent payloads as they happen."""

    def __init__(self, emit: RuntimeEventEmitter) -> None:
        self._emit = emit

    def on_planning(self, context: PlannerContext, *, blocked_triggered: bool) -> None:
        self._emit(
            phase="planning",
            seq=context.next_seq,
            payload=PlanningPayload(
                step_count=context.step_count,
                max_steps=context.max_steps,
                blocked_triggered=blocked_triggered,
            ),
        )

    def on_tool_call(self, context: PlannerContext, decision: PlannerDecision) -> None:
        if decision.tool_call is None:
            raise ValueError("tool_call observer received a decision without a Tool Call")
        self._emit(
            phase="tool_call",
            seq=context.next_seq,
            tool=self._tool_name(decision.tool_call.name),
            payload=ToolCallPayload(
                planner_message=decision.message,
                arguments=decision.tool_call.arguments,
            ),
        )

    def on_published(self, command: CommandMessage, *, topic: str, qos: int) -> None:
        self._emit(
            phase="published",
            seq=command.seq,
            tool=command.tool,
            payload=PublishedPayload(
                topic=topic,
                qos=qos,
                command=command,
                published=True,
            ),
        )

    def on_safety_rejected(self, record: ToolExecutionRecord) -> None:
        self._emit(
            phase="safety_rejected",
            seq=record.observation.seq,
            tool=self._tool_name(record.tool_call.name),
            payload=SafetyRejectedPayload(
                tool_call=record.tool_call,
                observation=record.observation,
                published=False,
            ),
        )

    def on_observation(self, step: AgentStep) -> None:
        if (
            step.command is None
            and not step.published
            and step.observation.status is ObservationStatus.REJECTED
            and step.observation.error_code != "mqtt_transport_error"
        ):
            return
        self._emit(
            phase="observation",
            seq=step.seq,
            tool=self._tool_name(step.tool_call.name),
            payload=ObservationPayload(
                observation=step.observation,
                latency_ms=step.latency_ms,
            ),
        )

    def on_replanning(self, step: AgentStep, *, next_seq: int) -> None:
        reason = step.observation.observation.get("reason")
        if not isinstance(reason, str) or not reason:
            reason = step.observation.error_code or step.observation.status.value
        self._emit(
            phase="replanning",
            seq=next_seq,
            payload=ReplanningPayload(
                reason=reason,
                last_status=step.observation.status,
                next_seq=next_seq,
            ),
        )

    def on_final(self, result: AgentRunResult) -> None:
        self._emit(
            phase="final",
            seq=0,
            payload=FinalPayload(
                final_status=result.final_status,
                final_message=result.final_message,
                duration_ms=result.duration_ms,
                step_count=len(result.steps),
            ),
        )

    @staticmethod
    def _tool_name(name: str) -> ToolName:
        return cast(ToolName, name)


class SimulationMissionRunner:
    """Create one planner/client/AgentGraph stack for each accepted Mission Task."""

    def __init__(
        self,
        config: MqttConfig,
        topics: MqttTopics,
        *,
        client_factory: Callable[..., MqttRequestClient] = MqttRequestClient,
        model_config: ModelConfig | None = None,
        model_planner_factory: Callable[[ModelConfig], OpenAICompatiblePlanner] = OpenAICompatiblePlanner,
    ) -> None:
        self.config = config
        self.topics = topics
        self._client_factory = client_factory
        self._model_config = model_config
        self._model_planner_factory = model_planner_factory

    def __call__(
        self,
        request: MissionTaskRequest,
        task_id: str,
        emit: RuntimeEventEmitter,
        cancellation_event: threading.Event | None = None,
    ) -> MissionTaskResult:
        observer = RuntimeMissionObserver(emit)
        if request.planner == "fake":
            planner = FakePlanner()
        else:
            if self._model_config is None:
                raise ProviderConfigurationError("model provider is not configured")
            planner = self._model_planner_factory(self._model_config)
        started_at = int(time.time() * 1_000)
        try:
            with self._client_factory(
                self.config,
                self.topics,
                publish_observer=observer,
            ) as client:
                result = AgentGraph(
                    planner,
                    ToolExecutor(client, observer=observer),
                    observer=observer,
                ).run(
                    request.goal,
                    task_id=task_id,
                    max_steps=request.max_steps,
                    deadline_ms=15_000,
                    response_timeout_s=self.config.response_timeout_s,
                    cancellation_event=cancellation_event,
                )
        except (MqttConnectionError, MqttTransportError) as exc:
            result_payload = MissionTaskResult(
                final_status="transport_error",
                final_message=str(exc)[:512],
                step_count=0,
            )
            emit(
                phase="final",
                seq=0,
                payload=FinalPayload(
                    final_status=result_payload.final_status,
                    final_message=result_payload.final_message,
                    duration_ms=max(0, int(time.time() * 1_000) - started_at),
                    step_count=0,
                ),
            )
            return result_payload
        return MissionTaskResult(
            final_status=result.final_status,
            final_message=result.final_message,
            step_count=len(result.steps),
        )


def execute_operator_emergency_stop(
    config: MqttConfig,
    topics: MqttTopics,
) -> ObservationMessage:
    task_id = f"operator-{uuid.uuid4().hex[:16]}"
    with MqttRequestClient(config, topics, client_id=task_id) as client:
        record = ToolExecutor(client).execute(
            ToolCall(
                name="emergency_stop",
                arguments={"reason": "operator requested emergency stop"},
            ),
            task_id=task_id,
            seq=1,
            deadline_ms=15_000,
            timeout_s=config.response_timeout_s,
        )
    return record.observation


__all__ = [
    "RuntimeMissionObserver",
    "SimulationMissionRunner",
    "execute_operator_emergency_stop",
]
