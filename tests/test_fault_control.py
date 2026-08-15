import threading

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from embodied_agent.schemas import CommandMessage
from embodied_agent.simulation import (
    FaultCoordinator,
    FaultEvidence,
    FaultPrepareEvidencePayload,
    FaultPublishedEvidencePayload,
    FaultRunRequest,
    FaultServiceDisconnectedEvidencePayload,
    FaultRunSnapshot,
    MAX_FAULT_EVIDENCE,
    RuntimeRunGate,
)
from embodied_agent.simulation.runtime import SimulationRuntime, create_app, create_bridge_app
from embodied_agent.simulation.mission import RuntimeCapabilities


class PassingFaultRunner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, request, run_id, emit):
        emit(stage="prepare", payload=FaultPrepareEvidencePayload(scenario=request.scenario))
        self.started.set()
        self.release.wait(1.0)
        return "passed", "test fault completed"


def bridge_capabilities() -> RuntimeCapabilities:
    return RuntimeCapabilities(
        mode="bridge",
        device_id="dog01",
        mqtt_endpoint="127.0.0.1:1883",
        model_configured=False,
        fault_injection=True,
    )


def test_fault_contracts_reject_unknown_scenarios_and_mismatched_evidence() -> None:
    with pytest.raises(ValidationError):
        FaultRunRequest(scenario="random_loss")
    with pytest.raises(ValidationError):
        FaultRunRequest(scenario="response_timeout", extra=True)

    with pytest.raises(ValidationError, match="requires FaultPrepareEvidencePayload"):
        FaultEvidence(
            event_id="fault-evt-1",
            timestamp_ms=1,
            stage="prepare",
            payload=FaultPublishedEvidencePayload(
                topic="robot/dog01/cmd",
                qos=1,
                command=CommandMessage(
                    version=1,
                    task_id="fault-task",
                    seq=1,
                    tool="get_robot_state",
                    params={},
                    deadline_ms=1000,
                    sent_at_ms=1,
                ),
                published=True,
            ),
        )
    with pytest.raises(ValidationError):
        FaultEvidence(
            event_id="fault-evt-2",
            timestamp_ms=1,
            stage="service_disconnected",
            payload=FaultServiceDisconnectedEvidencePayload(
                device_id="dog01",
                last_will=True,
            ),
        )


def test_fault_coordinator_api_tracks_current_releases_gate_and_keeps_snapshot() -> None:
    runner = PassingFaultRunner()
    cleanup_calls: list[tuple[str, str]] = []
    gate = RuntimeRunGate()
    coordinator = FaultCoordinator(
        runner,
        cleanup_handler=lambda request, run_id: cleanup_calls.append((request.scenario, run_id)),
        run_gate=gate,
        id_factory=lambda prefix: f"{prefix}-test",
    )
    try:
        with TestClient(
            create_app(
                SimulationRuntime(tick_ms=10_000),
                fault_coordinator=coordinator,
                runtime_capabilities=bridge_capabilities(),
                run_gate=gate,
            )
        ) as client:
            accepted = client.post("/api/faults", json={"scenario": "duplicate_delivery"})
            assert accepted.status_code == 202
            assert runner.started.wait(1.0)
            running = client.get("/api/faults/current")
            assert running.status_code == 200
            assert running.json()["status"] == "running"

            conflict_lease = gate.active
            assert conflict_lease is not None
            conflict = client.post("/api/faults", json={"scenario": "out_of_order"})
            assert conflict.status_code == 409
            assert conflict.json()["error_code"] == "runtime_busy"

            runner.release.set()
            assert coordinator.wait_for_idle(1.0)
            finished = client.get("/api/faults/current")

        assert finished.status_code == 200
        snapshot = FaultRunSnapshot.model_validate(finished.json())
        assert snapshot.status == "finished"
        assert snapshot.result == "passed"
        assert snapshot.summary == "test fault completed"
        assert [e.stage for e in snapshot.evidence] == ["prepare", "final"]
        assert cleanup_calls == [("duplicate_delivery", "fault-test")]
        assert gate.busy is False
    finally:
        runner.release.set()
        coordinator.shutdown()


def test_fault_journal_overflow_fails_without_silent_drop() -> None:
    def overflowing_runner(request, run_id, emit):
        for _ in range(MAX_FAULT_EVIDENCE):
            emit(stage="prepare", payload=FaultPrepareEvidencePayload(scenario=request.scenario))
        return "passed", "should not be reached"

    coordinator = FaultCoordinator(overflowing_runner)
    try:
        coordinator.submit(FaultRunRequest(scenario="response_timeout"))
        assert coordinator.wait_for_idle(1.0)
        snapshot = coordinator.current()
        assert snapshot is not None
        assert snapshot.status == "finished"
        assert snapshot.result == "failed"
        assert len(snapshot.evidence) == MAX_FAULT_EVIDENCE
        assert snapshot.evidence[-1].stage == "final"
        assert "overflow" in (snapshot.summary or "")
        assert coordinator.run_gate.busy is False
    finally:
        coordinator.shutdown()


def test_fault_cleanup_failure_marks_run_failed_and_releases_gate() -> None:
    def fail_cleanup(request, run_id) -> None:
        raise RuntimeError("restore failed")

    coordinator = FaultCoordinator(
        lambda request, run_id, emit: ("passed", "runner completed"),
        cleanup_handler=fail_cleanup,
    )
    try:
        coordinator.submit(FaultRunRequest(scenario="device_disconnect"))
        assert coordinator.wait_for_idle(1.0)
        snapshot = coordinator.current()
        assert snapshot is not None
        assert snapshot.result == "failed"
        assert snapshot.summary == "fault cleanup failed: RuntimeError: restore failed"
        assert snapshot.evidence[-1].payload.result == "failed"
        assert coordinator.run_gate.busy is False
    finally:
        coordinator.shutdown()


def test_fault_preview_and_empty_current_are_explicit() -> None:
    with TestClient(create_app(SimulationRuntime(tick_ms=10_000))) as client:
        submit = client.post("/api/faults", json={"scenario": "device_disconnect"})
        current = client.get("/api/faults/current")

    assert submit.status_code == 503
    assert submit.json()["error_code"] == "bridge_unavailable"
    assert current.status_code == 503
    assert current.json()["error_code"] == "bridge_unavailable"


def test_fault_current_is_empty_until_a_run_is_accepted() -> None:
    coordinator = FaultCoordinator(lambda request, run_id, emit: ("passed", "done"))
    try:
        with TestClient(
            create_app(
                SimulationRuntime(tick_ms=10_000),
                fault_coordinator=coordinator,
                runtime_capabilities=bridge_capabilities(),
            )
        ) as client:
            response = client.get("/api/faults/current")
            invalid = client.post("/api/faults", json={"scenario": "random_loss"})
        assert response.status_code == 204
        assert invalid.status_code == 400
        assert invalid.json()["error_code"] == "invalid_input"
    finally:
        coordinator.shutdown()


def test_fault_api_rejects_when_a_mission_owns_the_shared_gate() -> None:
    gate = RuntimeRunGate()
    lease = gate.acquire("mission", "mission-live")
    coordinator = FaultCoordinator(
        lambda request, run_id, emit: ("passed", "done"),
        run_gate=gate,
    )
    try:
        with TestClient(
            create_app(
                SimulationRuntime(tick_ms=10_000),
                fault_coordinator=coordinator,
                runtime_capabilities=bridge_capabilities(),
                run_gate=gate,
            )
        ) as client:
            response = client.post("/api/faults", json={"scenario": "out_of_order"})
        assert response.status_code == 409
        assert response.json()["error_code"] == "runtime_busy"
        assert coordinator.current() is None
    finally:
        gate.release(lease)
        coordinator.shutdown()


def test_bridge_app_mounts_fault_capability_and_coordinator() -> None:
    app = create_bridge_app(device_id="bridge-fault-test")
    assert app.state.runtime_capabilities.fault_injection is True
    assert app.state.fault_coordinator is not None
    app.state.fault_coordinator.shutdown()
    app.state.mission_coordinator.shutdown()
