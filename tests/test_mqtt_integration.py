from __future__ import annotations

import json
import socket
import threading
import time
import uuid

import paho.mqtt.client as mqtt
import paho.mqtt.publish as mqtt_publish
import paho.mqtt.subscribe as mqtt_subscribe
import pytest
from fastapi.testclient import TestClient

from embodied_agent import (
    AgentGraph,
    CommandMessage,
    DeviceSimulator,
    FakePlanner,
    MqttConfig,
    MqttConnectionError,
    MqttDeviceService,
    MqttRequestClient,
    MqttResponseTimeout,
    MqttTopics,
    ObservationMessage,
    ObservationStatus,
    RobotState,
    ToolExecutor,
)
from embodied_agent.simulation import SimulationAdapter, SimulationEngine, obstacle_world_config
from embodied_agent.simulation.runtime import SimulationRuntime, create_app, create_bridge_app


def broker_available(host: str = "127.0.0.1", port: int = 1883) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


MQTT_AVAILABLE = broker_available()


def make_command(task_id: str, seq: int, tool: str, params: dict) -> CommandMessage:
    return CommandMessage(
        version=1,
        task_id=task_id,
        seq=seq,
        tool=tool,
        params=params,
        deadline_ms=10_000,
        sent_at_ms=int(time.time() * 1000),
    )


def wait_for_finished_task(client: TestClient, *, timeout_s: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get("/api/tasks/current")
        if response.status_code == 200:
            snapshot = response.json()
            if snapshot["status"] == "finished":
                return snapshot
        time.sleep(0.02)
    raise AssertionError("mission task did not finish before the integration timeout")


def wait_for_finished_fault(client: TestClient, *, timeout_s: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get("/api/faults/current")
        if response.status_code == 200:
            snapshot = response.json()
            if snapshot["status"] == "finished":
                return snapshot
        time.sleep(0.02)
    raise AssertionError("fault run did not finish before the integration timeout")


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_real_broker_round_trip_and_qos1_idempotency():
    suffix = uuid.uuid4().hex[:8]
    topics = MqttTopics(f"dog-{suffix}")
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=2.0)
    simulator = DeviceSimulator(
        device_id=topics.device_id,
        initial_state=RobotState(front_distance_cm=100.0),
        obstacle_on_first_move=False,
    )
    command = make_command(
        f"mqtt-idempotent-{suffix}",
        1,
        "move_robot",
        {"distance_m": 1.0, "speed_mps": 0.2},
    )

    with MqttDeviceService(simulator, config, topics, client_id=f"device-{suffix}"):
        with MqttRequestClient(config, topics, client_id=f"agent-{suffix}") as client:
            first = client.execute(command)
            second = client.execute(command)

    assert first.status is ObservationStatus.SUCCESS
    assert second.model_dump(mode="json") == first.model_dump(mode="json")
    assert simulator.state.x_m == 1.0
    assert simulator.executed_command_count == 1


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_uncorrelated_observation_is_ignored_until_timeout():
    suffix = uuid.uuid4().hex[:8]
    topics = MqttTopics(f"dog-{suffix}")
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=0.3)
    command = make_command(f"expected-{suffix}", 7, "get_robot_state", {})
    wrong = ObservationMessage(
        version=1,
        task_id=f"wrong-{suffix}",
        seq=7,
        status=ObservationStatus.SUCCESS,
        observation={},
        received_at_ms=int(time.time() * 1000),
    )

    def publish_wrong_observation() -> None:
        time.sleep(0.05)
        mqtt_publish.single(
            topics.status,
            payload=wrong.model_dump_json(),
            qos=1,
            hostname=config.host,
            port=config.port,
        )

    with MqttRequestClient(config, topics, client_id=f"agent-{suffix}") as client:
        publisher = threading.Thread(target=publish_wrong_observation, daemon=True)
        publisher.start()
        with pytest.raises(MqttResponseTimeout):
            client.execute(command)
        publisher.join(timeout=1.0)


def test_unavailable_broker_does_not_report_success():
    config = MqttConfig(
        host="127.0.0.1",
        port=65_530,
        connect_timeout_s=0.2,
        response_timeout_s=0.2,
    )
    client = MqttRequestClient(config, MqttTopics("dog-unavailable"))
    with pytest.raises(MqttConnectionError):
        client.start()


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_langgraph_replanning_runs_over_real_mqtt_broker():
    suffix = uuid.uuid4().hex[:8]
    topics = MqttTopics(f"dog-agent-{suffix}")
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=2.0)
    simulator = DeviceSimulator(device_id=topics.device_id)

    with MqttDeviceService(simulator, config, topics, client_id=f"device-agent-{suffix}"):
        with MqttRequestClient(config, topics, client_id=f"agent-graph-{suffix}") as client:
            graph = AgentGraph(FakePlanner(), ToolExecutor(client))
            result = graph.run(
                "前进 2 米，如果遇到障碍就从更宽的一侧绕开",
                task_id=f"mqtt-agent-{suffix}",
                max_steps=8,
                response_timeout_s=2.0,
            )

    assert result.final_status == "success"
    assert [step.tool_call.name for step in result.steps] == [
        "move_robot",
        "scan_obstacles",
        "turn_robot",
        "move_robot",
        "turn_robot",
        "move_robot",
    ]
    assert result.steps[0].observation.status is ObservationStatus.BLOCKED
    assert simulator.state.x_m == 2.0
    assert simulator.state.y_m == 0.95


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_device_service_publishes_business_heartbeat():
    suffix = uuid.uuid4().hex[:8]
    topics = MqttTopics(f"dog-heartbeat-{suffix}")
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=2.0)
    simulator = DeviceSimulator(device_id=topics.device_id)

    with MqttDeviceService(
        simulator,
        config,
        topics,
        client_id=f"device-heartbeat-{suffix}",
        heartbeat_interval_s=0.05,
    ):
        message = mqtt_subscribe.simple(
            topics.telemetry,
            qos=0,
            hostname=config.host,
            port=config.port,
            retained=False,
        )

    payload = json.loads(message.payload)
    assert payload["device_id"] == topics.device_id
    assert payload["state"]["front_distance_cm"] == 18.0


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_device_service_configures_last_will_offline_event():
    suffix = uuid.uuid4().hex[:8]
    topics = MqttTopics(f"dog-will-{suffix}")
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=2.0)
    simulator = DeviceSimulator(device_id=topics.device_id)
    service = MqttDeviceService(
        simulator,
        config,
        topics,
        client_id=f"device-will-{suffix}",
        heartbeat_interval_s=60.0,
    )

    received = threading.Event()
    payload_holder: dict[str, object] = {}
    observer = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"observer-will-{suffix}",
    )

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe(topics.event, qos=1)

    def on_message(client, userdata, message):
        payload_holder.update(json.loads(message.payload))
        received.set()

    observer.on_connect = on_connect
    observer.on_message = on_message
    observer.connect(config.host, config.port, config.keepalive_s)
    observer.loop_start()
    try:
        time.sleep(0.05)
        service.start()
        service._client._sock_close()
        assert received.wait(2.0)
    finally:
        service.stop()
        observer.disconnect()
        observer.loop_stop()

    assert payload_holder["device_id"] == topics.device_id
    assert payload_holder["event"] == "device_offline"


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_agentgraph_drives_continuous_simulation_over_real_broker():
    suffix = uuid.uuid4().hex[:8]
    topics = MqttTopics(f"dog-simulation-{suffix}")
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=6.0)
    engine = SimulationEngine(demo_mode=False)
    adapter = SimulationAdapter(engine, device_id=topics.device_id)
    runtime = SimulationRuntime(engine, adapter=adapter)
    service = MqttDeviceService(
        adapter,
        config,
        topics,
        client_id=f"device-simulation-{suffix}",
        heartbeat_interval_s=0.2,
    )

    with TestClient(create_app(runtime, device_service=service)) as http:
        with MqttRequestClient(config, topics, client_id=f"agent-simulation-{suffix}") as client:
            result = AgentGraph(FakePlanner(), ToolExecutor(client)).run(
                "前进 0.2 米",
                task_id=f"simulation-agent-{suffix}",
                max_steps=3,
                deadline_ms=5_000,
                response_timeout_s=6.0,
            )
            command = result.steps[0].command
            replay = client.execute(command, timeout_s=2.0)

        frame = http.get("/api/simulation/snapshot").json()

    assert result.final_status == "success"
    assert result.steps[0].published is True
    assert result.steps[0].observation.status is ObservationStatus.SUCCESS
    assert replay.model_dump(mode="json") == result.steps[0].observation.model_dump(mode="json")
    assert frame["robot"]["x_m"] == pytest.approx(0.2)
    assert frame["active_command"] is None
    assert adapter.executed_motion_count == 1


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_agentgraph_detours_around_spatial_obstacle_over_real_broker():
    suffix = uuid.uuid4().hex[:8]
    topics = MqttTopics(f"dog-detour-{suffix}")
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=8.0)
    engine = SimulationEngine(
        demo_mode=False,
        world_config=obstacle_world_config("left"),
    )
    adapter = SimulationAdapter(engine, device_id=topics.device_id)
    runtime = SimulationRuntime(engine, adapter=adapter)
    service = MqttDeviceService(
        adapter,
        config,
        topics,
        client_id=f"device-detour-{suffix}",
        heartbeat_interval_s=0.2,
    )

    with TestClient(create_app(runtime, device_service=service)) as http:
        with MqttRequestClient(config, topics, client_id=f"agent-detour-{suffix}") as client:
            planner = FakePlanner(linear_speed_mps=0.5, turn_speed_dps=180.0)
            result = AgentGraph(planner, ToolExecutor(client)).run(
                "前进 2 米，如果遇到障碍就从更宽的一侧绕开",
                task_id=f"simulation-detour-{suffix}",
                max_steps=8,
                deadline_ms=15_000,
                response_timeout_s=8.0,
            )
        frame = http.get("/api/simulation/snapshot").json()

    assert result.final_status == "success"
    assert [step.tool_call.name for step in result.steps] == [
        "move_robot",
        "scan_obstacles",
        "turn_robot",
        "move_robot",
        "turn_robot",
        "move_robot",
    ]
    assert result.steps[0].observation.status is ObservationStatus.BLOCKED
    assert result.steps[0].observation.error_code == "front_obstacle"
    assert result.steps[2].tool_call.arguments["angle_deg"] == 90.0
    assert result.steps[5].tool_call.arguments["distance_m"] == pytest.approx(1.65)
    assert all(step.published for step in result.steps)
    assert frame["robot"]["x_m"] == pytest.approx(2.0)
    assert frame["robot"]["y_m"] == pytest.approx(1.2)
    assert frame["robot"]["yaw_deg"] == pytest.approx(0.0)
    assert adapter.executed_motion_count == 5


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_bridge_task_api_runs_fake_mission_over_real_broker():
    suffix = uuid.uuid4().hex[:8]
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=4.0)
    app = create_bridge_app(config, device_id=f"dog-task-{suffix}")

    with TestClient(app) as http:
        capabilities = http.get("/api/runtime/capabilities")
        accepted = http.post(
            "/api/tasks",
            json={"goal": "读取状态", "planner": "fake", "max_steps": 8},
        )
        finished = wait_for_finished_task(http)
        frame = http.get("/api/simulation/snapshot").json()

    assert capabilities.status_code == 200
    assert capabilities.json()["mode"] == "bridge"
    assert capabilities.json()["device_id"] == f"dog-task-{suffix}"
    assert accepted.status_code == 202
    assert [event["phase"] for event in finished["events"]] == [
        "user_goal",
        "planning",
        "tool_call",
        "published",
        "observation",
        "planning",
        "final",
    ]
    published = next(event for event in finished["events"] if event["phase"] == "published")
    assert published["payload"]["topic"] == f"robot/dog-task-{suffix}/cmd"
    assert published["payload"]["qos"] == 1
    assert published["payload"]["published"] is True
    assert finished["final_status"] == "success"
    assert frame["active_command"] is None


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_bridge_semantic_mission_confirms_targets_over_real_broker():
    suffix = uuid.uuid4().hex[:8]
    device_id = f"dog-semantic-{suffix}"
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=16.0)
    app = create_bridge_app(config, device_id=device_id)

    with TestClient(app) as http:
        accepted = http.post(
            "/api/tasks",
            json={
                "goal": "找到红色瓶子并前往蓝色目标区",
                "planner": "fake",
                "max_steps": 16,
            },
        )
        assert accepted.status_code == 202
        task_id = accepted.json()["task_id"]
        finished = wait_for_finished_task(http, timeout_s=40.0)
        frame = http.get("/api/simulation/snapshot").json()
        replay = http.get("/api/replays/current")

    assert finished["final_status"] == "success"
    assert replay.status_code == 200
    replay_payload = replay.json()
    assert replay_payload["run"]["kind"] == "mission"
    assert replay_payload["run"]["snapshot"]["task_id"] == task_id
    assert replay_payload["frame_capture"]["truncated"] is False
    assert replay_payload["frames"][0]["reason"] == "start"
    assert replay_payload["frames"][-1]["reason"] == "terminal"
    tool_events = [event for event in finished["events"] if event["phase"] == "tool_call"]
    published_events = [event for event in finished["events"] if event["phase"] == "published"]
    observation_events = [event for event in finished["events"] if event["phase"] == "observation"]

    tools = [event["tool"] for event in tool_events]
    assert tools.count("inspect_semantic_world") >= 6
    assert "turn_robot" in tools
    assert "move_robot" in tools
    assert len(published_events) == len(observation_events) == len(tool_events)

    for published, observation in zip(published_events, observation_events, strict=True):
        command = published["payload"]["command"]
        response = observation["payload"]["observation"]
        assert published["payload"]["topic"] == f"robot/{device_id}/cmd"
        assert published["payload"]["qos"] == 1
        assert published["payload"]["published"] is True
        assert command["task_id"] == response["task_id"] == task_id
        assert command["seq"] == response["seq"] == published["seq"] == observation["seq"]

    confirmed_objects = [
        item
        for event in observation_events
        for item in event["payload"]["observation"].get("observation", {}).get("objects", [])
        if item["within_interaction_radius"] is True
    ]
    assert [(item["kind"], item["color"]) for item in confirmed_objects] == [
        ("bottle", "red"),
        ("goal_zone", "blue"),
    ]
    semantic_results = [
        event["payload"]["observation"]["observation"]
        for event in observation_events
        if "objects" in event["payload"]["observation"].get("observation", {})
    ]
    assert all(result["source"] == "simulation_ground_truth" for result in semantic_results)
    assert frame["robot"]["x_m"] == pytest.approx(2.858, abs=0.01)
    assert frame["robot"]["y_m"] == pytest.approx(2.299, abs=0.01)
    assert frame["robot"]["emergency_stopped"] is False
    assert frame["active_command"] is None


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_bridge_cancel_stops_active_mission_without_latching_emergency():
    suffix = uuid.uuid4().hex[:8]
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=6.0)
    app = create_bridge_app(config, device_id=f"dog-cancel-{suffix}")

    with TestClient(app) as http:
        accepted = http.post(
            "/api/tasks",
            json={"goal": "前进 1 米", "planner": "fake", "max_steps": 8},
        )
        assert accepted.status_code == 202

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            moving_frame = http.get("/api/simulation/snapshot").json()
            if moving_frame["active_command"] is not None:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("bridge mission never became active")

        cancelled = http.post("/api/tasks/current/cancel")
        finished = wait_for_finished_task(http)
        stopped_frame = http.get("/api/simulation/snapshot").json()
        time.sleep(0.1)
        stable_frame = http.get("/api/simulation/snapshot").json()

        next_accepted = http.post(
            "/api/tasks",
            json={"goal": "读取状态", "planner": "fake", "max_steps": 8},
        )
        assert next_accepted.status_code == 202
        next_finished = wait_for_finished_task(http)

    assert cancelled.status_code == 200
    assert cancelled.json() == {
        "version": 1,
        "task_id": accepted.json()["task_id"],
        "status": "cancel_requested",
    }
    assert finished["final_status"] == "cancelled"
    final_events = [event for event in finished["events"] if event["phase"] == "final"]
    assert len(final_events) == 1
    cancelled_observations = [
        event
        for event in finished["events"]
        if event["phase"] == "observation"
        and event["payload"]["observation"]["error_code"] == "operator_cancelled"
    ]
    assert len(cancelled_observations) == 1
    assert cancelled_observations[0]["payload"]["observation"]["status"] == "rejected"
    assert 0.0 <= stopped_frame["robot"]["x_m"] < 1.0
    assert stable_frame["robot"]["x_m"] == pytest.approx(stopped_frame["robot"]["x_m"])
    assert stopped_frame["active_command"] is None
    assert stopped_frame["robot"]["emergency_stopped"] is False
    assert next_finished["final_status"] == "success"


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_device_disconnect_fault_recovers_service_and_next_mission():
    suffix = uuid.uuid4().hex[:8]
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=0.3)
    app = create_bridge_app(config, device_id=f"dog-fault-disconnect-{suffix}")

    with TestClient(app) as http:
        accepted = http.post("/api/faults", json={"scenario": "device_disconnect"})
        assert accepted.status_code == 202
        finished = wait_for_finished_fault(http)
        assert app.state.device_service.connected is True

        next_accepted = http.post(
            "/api/tasks",
            json={"goal": "读取状态", "planner": "fake", "max_steps": 8},
        )
        assert next_accepted.status_code == 202
        next_finished = wait_for_finished_task(http)

    assert finished["result"] == "passed"
    assert [item["stage"] for item in finished["evidence"]] == [
        "prepare",
        "service_disconnected",
        "published",
        "timeout",
        "service_restored",
        "published",
        "observation",
        "final",
    ]
    disconnected = finished["evidence"][1]["payload"]
    assert disconnected == {
        "device_id": f"dog-fault-disconnect-{suffix}",
        "connected": False,
        "last_will": False,
    }
    restored_observation = finished["evidence"][-2]["payload"]["observation"]
    assert restored_observation["status"] == "success"
    assert next_finished["final_status"] == "success"


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_response_timeout_fault_drops_once_and_next_mission_recovers():
    suffix = uuid.uuid4().hex[:8]
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=0.3)
    app = create_bridge_app(config, device_id=f"dog-fault-timeout-{suffix}")

    with TestClient(app) as http:
        accepted = http.post("/api/faults", json={"scenario": "response_timeout"})
        assert accepted.status_code == 202
        finished = wait_for_finished_fault(http)
        replay = http.get("/api/replays/current")

        next_accepted = http.post(
            "/api/tasks",
            json={"goal": "读取状态", "planner": "fake", "max_steps": 8},
        )
        assert next_accepted.status_code == 202
        next_finished = wait_for_finished_task(http)

    stages = [item["stage"] for item in finished["evidence"]]
    assert finished["result"] == "passed"
    assert stages == ["prepare", "published", "timeout", "final"]
    assert stages.count("published") == 1
    assert "observation" not in stages
    assert replay.status_code == 200
    assert replay.json()["run"]["kind"] == "fault"
    assert replay.json()["run"]["snapshot"]["run_id"] == finished["run_id"]
    assert next_finished["final_status"] == "success"


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_duplicate_delivery_fault_replays_without_second_motion():
    suffix = uuid.uuid4().hex[:8]
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=2.0)
    app = create_bridge_app(config, device_id=f"dog-fault-duplicate-{suffix}")

    with TestClient(app) as http:
        before_frame = http.get("/api/simulation/snapshot").json()
        before_count = app.state.simulation.adapter.executed_motion_count
        accepted = http.post("/api/faults", json={"scenario": "duplicate_delivery"})
        assert accepted.status_code == 202
        finished = wait_for_finished_fault(http)
        after_frame = http.get("/api/simulation/snapshot").json()
        after_count = app.state.simulation.adapter.executed_motion_count

    assert finished["result"] == "passed"
    assert [item["stage"] for item in finished["evidence"]] == [
        "prepare",
        "published",
        "observation",
        "published",
        "observation",
        "final",
    ]
    published = [item for item in finished["evidence"] if item["stage"] == "published"]
    observations = [item for item in finished["evidence"] if item["stage"] == "observation"]
    assert published[0]["payload"]["command"] == published[1]["payload"]["command"]
    assert observations[0]["payload"]["observation"] == observations[1]["payload"]["observation"]
    assert observations[0]["payload"]["replayed"] is False
    assert observations[1]["payload"]["replayed"] is True
    assert after_frame["robot"]["yaw_deg"] == pytest.approx(
        before_frame["robot"]["yaw_deg"] + 30.0
    )
    assert after_count == before_count + 1


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_out_of_order_fault_rejects_stale_sequence_without_motion():
    suffix = uuid.uuid4().hex[:8]
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=2.0)
    app = create_bridge_app(config, device_id=f"dog-fault-order-{suffix}")

    with TestClient(app) as http:
        before_frame = http.get("/api/simulation/snapshot").json()
        before_count = app.state.simulation.adapter.executed_motion_count
        accepted = http.post("/api/faults", json={"scenario": "out_of_order"})
        assert accepted.status_code == 202
        finished = wait_for_finished_fault(http)
        after_frame = http.get("/api/simulation/snapshot").json()
        after_count = app.state.simulation.adapter.executed_motion_count

    assert finished["result"] == "passed"
    assert [item["stage"] for item in finished["evidence"]] == [
        "prepare",
        "published",
        "observation",
        "published",
        "observation",
        "final",
    ]
    observations = [item["payload"]["observation"] for item in finished["evidence"] if item["stage"] == "observation"]
    assert observations[0]["seq"] == 2
    assert observations[0]["status"] == "success"
    assert observations[1]["seq"] == 1
    assert observations[1]["status"] == "rejected"
    assert observations[1]["error_code"] == "stale_sequence"
    assert after_frame["robot"] == before_frame["robot"]
    assert after_count == before_count


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_real_broker_pause_freezes_pose_but_deadline_still_expires():
    suffix = uuid.uuid4().hex[:8]
    topics = MqttTopics(f"dog-pause-deadline-{suffix}")
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=4.0)
    engine = SimulationEngine(demo_mode=False)
    adapter = SimulationAdapter(engine, device_id=topics.device_id)
    runtime = SimulationRuntime(engine, adapter=adapter)
    service = MqttDeviceService(
        adapter,
        config,
        topics,
        client_id=f"device-pause-deadline-{suffix}",
        heartbeat_interval_s=0.2,
    )
    command = make_command(
        f"pause-deadline-{suffix}",
        1,
        "move_robot",
        {"distance_m": 0.5, "speed_mps": 0.5},
    ).model_copy(update={"deadline_ms": 1_500})
    result: list[ObservationMessage] = []

    with TestClient(create_app(runtime, device_service=service)) as http:
        with MqttRequestClient(config, topics, client_id=f"agent-pause-{suffix}") as client:
            worker = threading.Thread(
                target=lambda: result.append(client.execute(command, timeout_s=4.0)),
                daemon=True,
            )
            worker.start()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if http.get("/api/simulation/snapshot").json()["active_command"] is not None:
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("pause/deadline move never became active")
            paused = http.post("/api/simulation/pause")
            frozen = http.get("/api/simulation/snapshot").json()
            time.sleep(1.8)
            worker.join(timeout=2.0)
            expired = http.get("/api/simulation/snapshot").json()

    assert paused.status_code == 200
    assert len(result) == 1
    assert result[0].status is ObservationStatus.TIMEOUT
    assert result[0].error_code == "deadline_expired"
    assert expired["robot"]["x_m"] == pytest.approx(frozen["robot"]["x_m"])
    assert expired["active_command"] is None


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_all_faults_then_reset_and_obstacle_detour_regression():
    suffix = uuid.uuid4().hex[:8]
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=16.0)
    app = create_bridge_app(config, device_id=f"dog-fault-regression-{suffix}")
    scenarios = [
        "device_disconnect",
        "response_timeout",
        "duplicate_delivery",
        "out_of_order",
    ]

    with TestClient(app) as http:
        fault_results = []
        for scenario in scenarios:
            accepted = http.post("/api/faults", json={"scenario": scenario})
            assert accepted.status_code == 202
            fault_results.append(wait_for_finished_fault(http, timeout_s=12.0))

        reset = http.post("/api/simulation/reset")
        assert reset.status_code == 200
        accepted = http.post(
            "/api/tasks",
            json={
                "goal": "前进 2 米，如果遇到障碍就从更宽的一侧绕开",
                "planner": "fake",
                "max_steps": 8,
            },
        )
        assert accepted.status_code == 202
        finished = wait_for_finished_task(http, timeout_s=32.0)
        frame = http.get("/api/simulation/snapshot").json()

    assert [item["result"] for item in fault_results] == ["passed"] * 4
    assert finished["final_status"] == "success"
    assert frame["robot"]["x_m"] == pytest.approx(2.0)
    assert frame["robot"]["y_m"] == pytest.approx(1.2)
    assert frame["robot"]["yaw_deg"] == pytest.approx(0.0)
    assert frame["active_command"] is None


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_real_broker_emergency_stop_completes_active_motion_immediately():
    suffix = uuid.uuid4().hex[:8]
    topics = MqttTopics(f"dog-emergency-{suffix}")
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=6.0)
    engine = SimulationEngine(demo_mode=False)
    adapter = SimulationAdapter(engine, device_id=topics.device_id)
    runtime = SimulationRuntime(engine, adapter=adapter)
    service = MqttDeviceService(
        adapter,
        config,
        topics,
        client_id=f"device-emergency-{suffix}",
        heartbeat_interval_s=0.2,
    )
    move = make_command(
        f"active-{suffix}",
        1,
        "move_robot",
        {"distance_m": 2.0, "speed_mps": 0.5},
    )
    stop = make_command(
        f"stop-{suffix}",
        1,
        "emergency_stop",
        {"reason": "integration test"},
    )
    active_result: list[ObservationMessage] = []

    with TestClient(create_app(runtime, device_service=service)) as http:
        with MqttRequestClient(config, topics, client_id=f"agent-active-{suffix}") as active_client:
            with MqttRequestClient(config, topics, client_id=f"agent-stop-{suffix}") as stop_client:
                worker = threading.Thread(
                    target=lambda: active_result.append(active_client.execute(move, timeout_s=6.0)),
                    daemon=True,
                )
                worker.start()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    frame = http.get("/api/simulation/snapshot").json()
                    if frame["active_command"] is not None:
                        break
                    time.sleep(0.02)
                else:
                    raise AssertionError("move command never became active")

                started_stop = time.monotonic()
                stop_result = stop_client.execute(stop, timeout_s=2.0)
                worker.join(timeout=2.0)
                stop_latency_s = time.monotonic() - started_stop
                final = http.get("/api/simulation/snapshot").json()

    assert stop_result.status is ObservationStatus.EMERGENCY_STOP
    assert len(active_result) == 1
    assert active_result[0].status is ObservationStatus.EMERGENCY_STOP
    assert active_result[0].error_code == "emergency_cancelled"
    assert stop_latency_s < 1.0
    assert final["robot"]["emergency_stopped"] is True
    assert final["active_command"] is None


@pytest.mark.mqtt_integration
@pytest.mark.skipif(not MQTT_AVAILABLE, reason="local MQTT broker is not available")
def test_bridge_emergency_stop_endpoint_cancels_active_mission_over_real_broker():
    suffix = uuid.uuid4().hex[:8]
    config = MqttConfig(connect_timeout_s=2.0, response_timeout_s=6.0)
    app = create_bridge_app(config, device_id=f"dog-stop-{suffix}")

    with TestClient(app) as http:
        accepted = http.post(
            "/api/tasks",
            json={"goal": "前进 1 米", "planner": "fake", "max_steps": 8},
        )
        assert accepted.status_code == 202

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            frame = http.get("/api/simulation/snapshot").json()
            if frame["active_command"] is not None:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("bridge mission never became active")

        emergency = http.post("/api/simulation/emergency-stop")
        finished = wait_for_finished_task(http)
        final_frame = http.get("/api/simulation/snapshot").json()

    assert emergency.status_code == 200
    assert emergency.json()["status"] == "emergency_stop"
    assert finished["final_status"] == "emergency_stop"
    emergency_observation = next(
        event
        for event in finished["events"]
        if event["phase"] == "observation"
        and event["payload"]["observation"]["status"] == "emergency_stop"
    )
    assert emergency_observation["payload"]["observation"]["error_code"] == "emergency_cancelled"
    assert final_frame["robot"]["emergency_stopped"] is True
    assert final_frame["active_command"] is None
