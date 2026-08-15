import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from embodied_agent.mqtt_transport import MqttConfig, MqttConnectionError
from embodied_agent.simulation import SimulationEngine, obstacle_world_config
from embodied_agent.simulation.mission import (
    FinalPayload,
    MissionTaskCoordinator,
    MissionTaskResult,
    PlanningPayload,
    RuntimeCapabilities,
    RuntimeEvent,
    UserGoalPayload,
)
from embodied_agent.simulation.runtime import (
    LatestFrameHub,
    RuntimeEventHub,
    SimulationRuntime,
    create_app,
    create_bridge_app,
)


def test_snapshot_and_operator_controls() -> None:
    runtime = SimulationRuntime(SimulationEngine(), tick_ms=10_000)
    with TestClient(create_app(runtime)) as client:
        initial = client.get("/api/simulation/snapshot").json()
        paused = client.post("/api/simulation/pause").json()
        resumed = client.post("/api/simulation/resume").json()
        reset = client.post("/api/simulation/reset").json()

    assert initial["type"] == "simulation.frame"
    assert paused["revision"] > initial["revision"]
    assert resumed["revision"] > paused["revision"]
    assert reset["revision"] > resumed["revision"]
    assert reset["sim_time_ms"] == 0
    assert reset["robot"]["x_m"] == 0.0
    assert reset["active_command"]["seq"] == 1


def test_world_endpoint_returns_the_engine_world_config() -> None:
    config = obstacle_world_config("right")
    runtime = SimulationRuntime(SimulationEngine(demo_mode=False, world_config=config), tick_ms=10_000)

    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/simulation/world")

    assert response.status_code == 200
    assert response.json() == config.model_dump(mode="json")


def test_websocket_sends_snapshot_before_live_frames() -> None:
    runtime = SimulationRuntime(SimulationEngine(), tick_ms=20)
    with TestClient(create_app(runtime)) as client:
        with client.websocket_connect("/ws/events") as websocket:
            first = websocket.receive_json()
            second = websocket.receive_json()

    assert first["type"] == "simulation.frame"
    assert second["revision"] > first["revision"]
    assert second["sim_time_ms"] >= first["sim_time_ms"]


def test_reconnect_starts_from_latest_authoritative_snapshot() -> None:
    runtime = SimulationRuntime(SimulationEngine(), tick_ms=20)
    with TestClient(create_app(runtime)) as client:
        with client.websocket_connect("/ws/events") as websocket:
            websocket.receive_json()
            live = websocket.receive_json()
        latest = client.get("/api/simulation/snapshot").json()
        with client.websocket_connect("/ws/events") as websocket:
            reconnected = websocket.receive_json()

    assert latest["revision"] >= live["revision"]
    assert reconnected["revision"] >= latest["revision"]
    assert reconnected["robot"] == latest["robot"] or reconnected["revision"] > latest["revision"]


def test_latest_frame_hub_replaces_stale_queued_frame() -> None:
    async def scenario() -> tuple[int, int]:
        hub = LatestFrameHub()
        engine = SimulationEngine()
        async with hub.subscribe() as queue:
            first = engine.advance()
            second = engine.advance()
            hub.publish(first)
            hub.publish(second)
            retained = queue.get_nowait()
            return retained.revision, queue.qsize()

    revision, remaining = asyncio.run(scenario())
    assert revision == 2
    assert remaining == 0


def test_runtime_event_hub_preserves_fifo_from_worker_thread() -> None:
    async def scenario() -> list[str]:
        hub = RuntimeEventHub()
        events = [
            RuntimeEvent(
                event_id="evt-goal",
                timestamp_ms=1,
                task_id="mission-fifo",
                seq=0,
                phase="user_goal",
                payload=UserGoalPayload(goal="读取状态", planner="fake", max_steps=8),
            ),
            RuntimeEvent(
                event_id="evt-final",
                timestamp_ms=2,
                task_id="mission-fifo",
                seq=0,
                phase="final",
                payload=FinalPayload(
                    final_status="success",
                    final_message="done",
                    duration_ms=1,
                    step_count=0,
                ),
            ),
        ]
        async with hub.subscribe() as queue:
            worker = threading.Thread(target=lambda: [hub.publish(event) for event in events])
            worker.start()
            worker.join()
            return [(await queue.get()).event_id, (await queue.get()).event_id]

    assert asyncio.run(scenario()) == ["evt-goal", "evt-final"]


def test_websocket_multiplexes_snapshot_and_runtime_events() -> None:
    class EventRunner:
        def __call__(self, request, task_id, emit) -> MissionTaskResult:
            emit(
                phase="planning",
                seq=1,
                payload=PlanningPayload(
                    step_count=0,
                    max_steps=request.max_steps,
                    blocked_triggered=False,
                ),
            )
            result = MissionTaskResult(
                final_status="success",
                final_message="done",
                step_count=0,
            )
            emit(
                phase="final",
                seq=0,
                payload=FinalPayload(
                    final_status=result.final_status,
                    final_message=result.final_message,
                    duration_ms=1,
                    step_count=0,
                ),
            )
            return result

    coordinator = MissionTaskCoordinator(EventRunner())
    app = create_app(
        SimulationRuntime(SimulationEngine(), tick_ms=10_000),
        mission_coordinator=coordinator,
        runtime_capabilities=RuntimeCapabilities(
            mode="bridge",
            device_id="dog01",
            mqtt_endpoint="127.0.0.1:1883",
            model_configured=False,
        ),
    )
    with TestClient(app) as client:
        with client.websocket_connect("/ws/events") as websocket:
            first = websocket.receive_json()
            response = client.post(
                "/api/tasks",
                json={"goal": "读取状态", "planner": "fake", "max_steps": 8},
            )
            task_events = [websocket.receive_json() for _ in range(3)]

    assert first["type"] == "simulation.frame"
    assert response.status_code == 202
    assert [event["phase"] for event in task_events] == ["user_goal", "planning", "final"]
    assert all(event["type"] == "agent.step" for event in task_events)


def test_runtime_rejects_invalid_tick_size() -> None:
    for value in (0, -1, True, 1.5):
        try:
            SimulationRuntime(tick_ms=value)
        except ValueError as exc:
            assert "positive integer" in str(exc)
        else:
            raise AssertionError(f"tick_ms={value!r} was accepted")


def test_static_web_root_is_served_after_runtime_routes() -> None:
    runtime = SimulationRuntime(SimulationEngine(), tick_ms=10_000)
    with TestClient(create_app(runtime)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Quadruped Simulation Runtime" in response.text


def test_runtime_fails_explicitly_without_frontend_build(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="web build output is missing"):
        create_app(SimulationRuntime(), web_dist=tmp_path / "missing-dist")


def test_bridge_app_uses_idle_engine_and_simulation_adapter() -> None:
    app = create_bridge_app(device_id="bridge-test")
    runtime = app.state.simulation

    assert runtime.engine.demo_mode is False
    assert runtime.engine.snapshot().active_command is None
    assert runtime.adapter is not None
    assert runtime.adapter.device_id == "bridge-test"
    assert app.state.device_service.backend is runtime.adapter
    assert runtime.engine.world_config.scene_id == "indoor-lab-obstacle-left"
    assert runtime.engine.world_config.obstacles[0].id == "front-crate"


def test_device_service_is_managed_by_fastapi_lifespan() -> None:
    class FakeService:
        def __init__(self) -> None:
            self.started = 0
            self.stopped = 0
            self.backend = SimpleNamespace(device_id="lifecycle-device")
            self.topics = SimpleNamespace(command="robot/lifecycle-device/cmd")

        def start(self) -> None:
            self.started += 1

        def stop(self) -> None:
            self.stopped += 1

    service = FakeService()
    runtime = SimulationRuntime(SimulationEngine(demo_mode=False), tick_ms=10_000)

    with TestClient(create_app(runtime, device_service=service)) as client:
        assert client.get("/api/simulation/snapshot").status_code == 200
        assert service.started == 1

    assert service.stopped == 1


def test_bridge_startup_fails_explicitly_when_broker_is_unavailable() -> None:
    config = MqttConfig(
        host="127.0.0.1",
        port=65_530,
        connect_timeout_s=0.2,
        response_timeout_s=0.2,
    )
    app = create_bridge_app(config, device_id="bridge-unavailable")

    with pytest.raises(MqttConnectionError, match="connection timed out"):
        with TestClient(app):
            pass

    assert app.state.simulation._tick_task is None
