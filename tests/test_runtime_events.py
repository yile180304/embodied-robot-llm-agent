from __future__ import annotations

import json
from types import SimpleNamespace

import paho.mqtt.client as mqtt

from embodied_agent import (
    CommandMessage,
    DeviceSimulator,
    MqttConfig,
    MqttRequestClient,
    MqttTopics,
    ObservationMessage,
    ObservationStatus,
    ModelConfig,
    PlannerDecision,
)
from embodied_agent.simulation.mission import MissionTaskCoordinator, MissionTaskRequest
from embodied_agent.simulation.mission_runner import SimulationMissionRunner


class DirectMqttClient:
    simulator = DeviceSimulator()

    def __init__(self, config, topics, *, publish_observer) -> None:
        self.config = config
        self.topics = topics
        self.publish_observer = publish_observer

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(
        self,
        command: CommandMessage,
        *,
        timeout_s: float | None = None,
    ) -> ObservationMessage:
        self.publish_observer.on_published(
            command,
            topic=self.topics.command,
            qos=self.config.qos,
        )
        return self.simulator.process_command(command, now_ms=command.sent_at_ms + 1)


def run_direct_mission(goal: str):
    DirectMqttClient.simulator = DeviceSimulator()
    runner = SimulationMissionRunner(
        MqttConfig(response_timeout_s=1.0),
        MqttTopics("dog-runtime-events"),
        client_factory=DirectMqttClient,
    )
    coordinator = MissionTaskCoordinator(runner)
    try:
        coordinator.submit(MissionTaskRequest(goal=goal))
        assert coordinator.wait_for_idle(2.0)
        snapshot = coordinator.current()
        assert snapshot is not None
        return snapshot
    finally:
        coordinator.shutdown()


def test_source_hooks_produce_simple_mission_events_in_real_execution_order() -> None:
    snapshot = run_direct_mission("读取状态")

    assert snapshot.final_status == "success"
    assert [event.phase for event in snapshot.events] == [
        "user_goal",
        "planning",
        "tool_call",
        "published",
        "observation",
        "planning",
        "final",
    ]
    published = snapshot.events[3]
    observation = snapshot.events[4]
    assert published.seq == observation.seq == 1
    assert published.payload.published is True
    assert published.payload.qos == 1
    assert published.payload.command.task_id == snapshot.task_id
    assert observation.payload.observation.task_id == snapshot.task_id


def test_safety_rejection_has_no_published_or_duplicate_observation_event() -> None:
    snapshot = run_direct_mission("以 5 m/s 全速向前冲 10 米")
    phases = [event.phase for event in snapshot.events]

    assert snapshot.final_status == "rejected"
    assert phases == [
        "user_goal",
        "planning",
        "tool_call",
        "safety_rejected",
        "planning",
        "final",
    ]
    assert "published" not in phases
    assert "observation" not in phases
    rejected = snapshot.events[3]
    assert rejected.payload.published is False
    assert rejected.payload.observation.error_code == "dangerous_parameter"
    assert DirectMqttClient.simulator.executed_command_count == 0


def test_blocked_observation_emits_replanning_before_the_next_planning_step() -> None:
    snapshot = run_direct_mission("前进 2 米，如果遇到障碍就从更宽的一侧绕开")
    phases = [event.phase for event in snapshot.events]

    assert snapshot.final_status == "success"
    first_observation = phases.index("observation")
    replanning = phases.index("replanning")
    assert first_observation < replanning < phases.index("planning", replanning + 1)
    event = snapshot.events[replanning]
    assert event.payload.last_status is ObservationStatus.BLOCKED
    assert event.payload.next_seq == 2


def test_mqtt_publish_observer_runs_after_paho_accepts_publish() -> None:
    calls: list[str] = []

    class Recorder:
        def on_published(self, command, *, topic, qos) -> None:
            calls.append("published_event")
            assert topic == "robot/dog-publish/cmd"
            assert qos == 1

    client = MqttRequestClient(
        MqttConfig(response_timeout_s=0.2),
        MqttTopics("dog-publish"),
        publish_observer=Recorder(),
    )
    client._started = True
    client._connected.set()

    def publish(topic, payload, qos, retain):
        calls.append("paho_publish")
        raw = json.loads(payload)
        observation = ObservationMessage(
            version=1,
            task_id=raw["task_id"],
            seq=raw["seq"],
            status=ObservationStatus.SUCCESS,
            observation={"accepted": True},
            received_at_ms=raw["sent_at_ms"] + 1,
        )
        client._on_message(
            client._client,
            None,
            SimpleNamespace(payload=observation.model_dump_json().encode("utf-8")),
        )
        return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)

    client._client.publish = publish
    command = CommandMessage(
        version=1,
        task_id="publish-task",
        seq=1,
        tool="get_robot_state",
        params={},
        deadline_ms=3_000,
        sent_at_ms=1_000,
    )
    result = client.execute(command)
    calls.append("execute_returned")

    assert result.status is ObservationStatus.SUCCESS
    assert calls == ["paho_publish", "published_event", "execute_returned"]


def test_model_mission_uses_the_frozen_runtime_config_snapshot() -> None:
    captured: list[ModelConfig] = []

    class ReadStatePlanner:
        def plan(self, context):
            if context.last_observation is None:
                return PlannerDecision.call("get_robot_state", {}, "read state")
            return PlannerDecision.finish("success", "state observed")

    config = ModelConfig(
        api_key="frozen-key",
        model="frozen-model",
        base_url="https://provider.example/v1",
    )
    runner = SimulationMissionRunner(
        MqttConfig(response_timeout_s=1.0),
        MqttTopics("dog-model-snapshot"),
        client_factory=DirectMqttClient,
        model_config=config,
        model_planner_factory=lambda value: captured.append(value) or ReadStatePlanner(),
    )
    coordinator = MissionTaskCoordinator(runner)
    try:
        coordinator.submit(MissionTaskRequest(goal="读取状态", planner="model", max_steps=4))
        assert coordinator.wait_for_idle(2.0)
        snapshot = coordinator.current()
        assert snapshot is not None
        assert snapshot.final_status == "success"
        assert captured == [config]
    finally:
        coordinator.shutdown()
