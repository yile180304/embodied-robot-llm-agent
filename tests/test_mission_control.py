import threading

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from embodied_agent.simulation.mission import (
    FinalPayload,
    MissionTaskActiveError,
    MissionTaskCancelAccepted,
    MissionTaskCoordinator,
    MissionTaskNotActiveError,
    MissionTaskRequest,
    MissionTaskResult,
    PlanningPayload,
    RuntimeCapabilities,
    RuntimeEvent,
    UserGoalPayload,
)
from embodied_agent.simulation.run_gate import RuntimeRunGate, RuntimeRunGateBusyError
from embodied_agent.simulation.runtime import SimulationRuntime, create_app


class BlockingRunner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.worker_thread_id: int | None = None

    def __call__(self, request, task_id, emit) -> MissionTaskResult:
        self.worker_thread_id = threading.get_ident()
        emit(
            phase="planning",
            seq=1,
            payload=PlanningPayload(
                step_count=0,
                max_steps=request.max_steps,
                blocked_triggered=False,
            ),
        )
        self.started.set()
        if not self.release.wait(2.0):
            raise TimeoutError("test runner release timed out")
        result = MissionTaskResult(
            final_status="success",
            final_message="test mission completed",
            step_count=0,
        )
        emit(
            phase="final",
            seq=0,
            payload=FinalPayload(
                final_status=result.final_status,
                final_message=result.final_message,
                duration_ms=1,
                step_count=result.step_count,
            ),
        )
        return result


class CancellationAwareRunner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.finish = threading.Event()
        self.cancel_event: threading.Event | None = None

    def __call__(self, request, task_id, emit, cancellation_event) -> MissionTaskResult:
        self.cancel_event = cancellation_event
        self.started.set()
        cancellation_event.wait(2.0)
        self.finish.wait(2.0)
        return MissionTaskResult(
            final_status="success",
            final_message="runner observed cancellation",
            step_count=0,
        )


def test_runtime_event_enforces_phase_payload_seq_and_tool_contract() -> None:
    event = RuntimeEvent(
        event_id="evt-1",
        timestamp_ms=1,
        task_id="mission-1",
        seq=0,
        phase="user_goal",
        payload=UserGoalPayload(goal="读取状态", planner="fake", max_steps=8),
    )

    assert event.model_dump(mode="json")["payload"]["goal"] == "读取状态"
    with pytest.raises(ValidationError, match="requires seq=0"):
        RuntimeEvent(
            event_id="evt-2",
            timestamp_ms=2,
            task_id="mission-1",
            seq=1,
            phase="user_goal",
            payload=UserGoalPayload(goal="读取状态", planner="fake", max_steps=8),
        )
    with pytest.raises(ValidationError, match="requires PlanningPayload"):
        RuntimeEvent(
            event_id="evt-3",
            timestamp_ms=3,
            task_id="mission-1",
            seq=1,
            phase="planning",
            payload=UserGoalPayload(goal="读取状态", planner="fake", max_steps=8),
        )


def test_coordinator_runs_on_worker_retains_order_and_releases_active_slot() -> None:
    runner = BlockingRunner()
    listener_phases: list[str] = []
    coordinator = MissionTaskCoordinator(
        runner,
        event_listener=lambda event: listener_phases.append(event.phase),
    )
    main_thread_id = threading.get_ident()
    try:
        accepted = coordinator.submit(MissionTaskRequest(goal="读取状态"))
        assert accepted.status == "accepted"
        assert runner.started.wait(1.0)
        assert coordinator.active is True
        assert runner.worker_thread_id != main_thread_id

        with pytest.raises(MissionTaskActiveError, match="another mission"):
            coordinator.submit(MissionTaskRequest(goal="扫描障碍"))

        running = coordinator.current()
        assert running is not None
        assert running.status == "running"
        assert [event.phase for event in running.events] == ["user_goal", "planning"]

        runner.release.set()
        assert coordinator.wait_for_idle(1.0)
        finished = coordinator.current()
        assert finished is not None
        assert finished.status == "finished"
        assert finished.final_status == "success"
        assert [event.phase for event in finished.events] == [
            "user_goal",
            "planning",
            "final",
        ]
        assert listener_phases == ["user_goal", "planning", "final"]
    finally:
        runner.release.set()
        coordinator.shutdown()


def test_cancel_is_idempotent_emits_cancelled_final_and_releases_gate() -> None:
    runner = CancellationAwareRunner()
    gate = RuntimeRunGate()
    cancel_calls: list[str] = []
    coordinator = MissionTaskCoordinator(
        runner,
        run_gate=gate,
        cancel_handler=cancel_calls.append,
    )
    try:
        accepted = coordinator.submit(MissionTaskRequest(goal="执行长动作"))
        assert runner.started.wait(1.0)

        first = coordinator.cancel_current()
        second = coordinator.cancel_current()
        assert isinstance(first, MissionTaskCancelAccepted)
        assert second == first
        assert runner.cancel_event is not None and runner.cancel_event.is_set()
        assert cancel_calls == [accepted.task_id]
        runner.finish.set()
        assert coordinator.wait_for_idle(1.0)

        snapshot = coordinator.current()
        assert snapshot is not None
        assert snapshot.task_id == accepted.task_id
        assert snapshot.status == "finished"
        assert snapshot.final_status == "cancelled"
        finals = [event for event in snapshot.events if event.phase == "final"]
        assert len(finals) == 1
        assert finals[0].payload.final_status == "cancelled"
        assert coordinator.active is False
        assert gate.busy is False

        next_run = coordinator.submit(MissionTaskRequest(goal="下一任务"))
        assert next_run.status == "accepted"
        coordinator.cancel_current()
        assert coordinator.wait_for_idle(1.0)
    finally:
        coordinator.shutdown()


def test_cancel_without_active_task_is_explicitly_rejected() -> None:
    coordinator = MissionTaskCoordinator(BlockingRunner())
    try:
        with pytest.raises(MissionTaskNotActiveError):
            coordinator.cancel_current()
    finally:
        coordinator.shutdown()


def test_runtime_run_gate_rejects_cross_kind_run_atomically() -> None:
    gate = RuntimeRunGate()
    fault_lease = gate.acquire("fault", "fault-1")
    coordinator = MissionTaskCoordinator(BlockingRunner(), run_gate=gate)
    try:
        with pytest.raises(RuntimeRunGateBusyError) as exc_info:
            coordinator.submit(MissionTaskRequest(goal="读取状态"))
        assert exc_info.value.active == fault_lease
        assert coordinator.active is False
        gate.release(fault_lease)
        accepted = coordinator.submit(MissionTaskRequest(goal="读取状态"))
        assert accepted.status == "accepted"
        coordinator.cancel_current()
    finally:
        coordinator.shutdown()


def test_preview_task_api_is_explicitly_unavailable() -> None:
    with TestClient(create_app(SimulationRuntime(tick_ms=10_000))) as client:
        capabilities = client.get("/api/runtime/capabilities")
        current = client.get("/api/tasks/current")
        response = client.post(
            "/api/tasks",
            json={"goal": "读取状态", "planner": "fake", "max_steps": 8},
        )

    assert capabilities.status_code == 200
    assert capabilities.json() == {
        "version": 1,
        "mode": "preview",
        "device_id": None,
        "mqtt_endpoint": None,
        "fake_planner": True,
        "model_configured": False,
        "fault_injection": False,
    }
    assert current.status_code == 204
    assert response.status_code == 503
    assert response.json()["error_code"] == "bridge_unavailable"


def test_cancel_api_returns_strict_active_repeated_and_inactive_results() -> None:
    runner = CancellationAwareRunner()
    coordinator = MissionTaskCoordinator(runner)
    capabilities = RuntimeCapabilities(
        mode="bridge",
        device_id="dog01",
        mqtt_endpoint="127.0.0.1:1883",
        model_configured=False,
    )
    try:
        with TestClient(
            create_app(
                SimulationRuntime(tick_ms=10_000),
                mission_coordinator=coordinator,
                runtime_capabilities=capabilities,
            )
        ) as client:
            accepted = client.post(
                "/api/tasks",
                json={"goal": "执行长动作", "planner": "fake", "max_steps": 8},
            )
            assert runner.started.wait(1.0)
            first = client.post("/api/tasks/current/cancel")
            second = client.post("/api/tasks/current/cancel")
            runner.finish.set()
            assert coordinator.wait_for_idle(1.0)
            inactive = client.post("/api/tasks/current/cancel")

        assert accepted.status_code == 202
        assert first.status_code == 200
        assert first.json() == {
            "version": 1,
            "task_id": accepted.json()["task_id"],
            "status": "cancel_requested",
        }
        assert second.json() == first.json()
        assert inactive.status_code == 409
        assert inactive.json()["error_code"] == "task_not_active"
    finally:
        coordinator.shutdown()


@pytest.mark.parametrize(
    "payload",
    [
        {"goal": "", "planner": "fake", "max_steps": 8},
        {"goal": "读取状态", "planner": "unknown", "max_steps": 8},
        {"goal": "读取状态", "planner": "fake", "max_steps": 17},
        {"goal": "读取状态", "planner": "fake", "max_steps": 8, "extra": True},
    ],
)
def test_task_api_rejects_invalid_input_with_versioned_error(payload: dict) -> None:
    runner = BlockingRunner()
    coordinator = MissionTaskCoordinator(runner)
    capabilities = RuntimeCapabilities(
        mode="bridge",
        device_id="dog01",
        mqtt_endpoint="127.0.0.1:1883",
        model_configured=False,
    )
    try:
        with TestClient(
            create_app(
                SimulationRuntime(tick_ms=10_000),
                mission_coordinator=coordinator,
                runtime_capabilities=capabilities,
            )
        ) as client:
            response = client.post("/api/tasks", json=payload)
        assert response.status_code == 400
        assert response.json()["version"] == 1
        assert response.json()["error_code"] == "invalid_input"
    finally:
        runner.release.set()


def test_bridge_task_api_accepts_one_task_and_reports_provider_configuration() -> None:
    runner = BlockingRunner()
    coordinator = MissionTaskCoordinator(runner)
    capabilities = RuntimeCapabilities(
        mode="bridge",
        device_id="dog01",
        mqtt_endpoint="127.0.0.1:1883",
        model_configured=False,
    )
    with TestClient(
        create_app(
            SimulationRuntime(tick_ms=10_000),
            mission_coordinator=coordinator,
            runtime_capabilities=capabilities,
        )
    ) as client:
        model_response = client.post(
            "/api/tasks",
            json={"goal": "读取状态", "planner": "model", "max_steps": 8},
        )
        accepted = client.post(
            "/api/tasks",
            json={"goal": "读取状态", "planner": "fake", "max_steps": 8},
        )
        assert runner.started.wait(1.0)
        conflict = client.post(
            "/api/tasks",
            json={"goal": "扫描障碍", "planner": "fake", "max_steps": 8},
        )
        current = client.get("/api/tasks/current")
        snapshot = client.get("/api/simulation/snapshot")
        runner.release.set()
        assert coordinator.wait_for_idle(1.0)
        finished = client.get("/api/tasks/current")

    assert model_response.status_code == 503
    assert model_response.json()["error_code"] == "provider_unconfigured"
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "accepted"
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "task_active"
    assert current.status_code == 200
    assert current.json()["status"] == "running"
    assert snapshot.status_code == 200
    assert finished.json()["status"] == "finished"
    assert finished.json()["final_status"] == "success"
