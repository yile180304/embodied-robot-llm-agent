"""HTTP and WebSocket runtime for the deterministic simulation engine."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..mqtt_device_service import MqttDeviceService
from ..model_provider import ModelConfig, ProviderConfigurationError
from ..mqtt_transport import MqttConfig, MqttTopics, MqttTransportError
from ..schemas import ObservationMessage
from .adapter import SimulationAdapter
from .engine import DEFAULT_TICK_MS, SimulationEngine
from .faults import (
    FaultCoordinator,
    FaultRunAccepted,
    FaultRunRequest,
    FaultRunSnapshot,
    MqttFaultRunner,
)
from .mission import (
    MissionTaskAccepted,
    MissionTaskActiveError,
    MissionTaskCancelAccepted,
    MissionTaskCoordinator,
    MissionTaskRequest,
    MissionTaskNotActiveError,
    MissionTaskSnapshot,
    RuntimeApiError,
    RuntimeCapabilities,
    RuntimeEvent,
)
from .models import SimulationFrame
from .mission_runner import SimulationMissionRunner, execute_operator_emergency_stop
from .replay import ReplayEvidenceInvalid, ReplayRecorder
from .run_gate import RuntimeRunGate, RuntimeRunGateBusyError
from .world import WorldConfig, obstacle_world_config


LOGGER = logging.getLogger(__name__)


@dataclass
class LatestFrameHub:
    """Fan out frames without allowing slow clients to build a backlog."""

    _subscribers: set[asyncio.Queue[SimulationFrame]] = field(default_factory=set)

    def publish(self, frame: SimulationFrame) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[SimulationFrame]]:
        queue: asyncio.Queue[SimulationFrame] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


@dataclass(eq=False)
class _RuntimeEventSubscription:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[RuntimeEvent]
    active: bool = True


@dataclass
class RuntimeEventHub:
    """Fan out task events from worker threads without coalescing them."""

    _subscribers: set[_RuntimeEventSubscription] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def publish(self, event: RuntimeEvent) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscription in subscribers:
            try:
                subscription.loop.call_soon_threadsafe(self._enqueue, subscription, event)
            except RuntimeError:
                self._remove(subscription)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[RuntimeEvent]]:
        subscription = _RuntimeEventSubscription(
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(maxsize=128),
        )
        with self._lock:
            self._subscribers.add(subscription)
        try:
            yield subscription.queue
        finally:
            self._remove(subscription)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def _enqueue(self, subscription: _RuntimeEventSubscription, event: RuntimeEvent) -> None:
        if not subscription.active:
            return
        if subscription.queue.full():
            LOGGER.error(
                "runtime event subscriber exceeded FIFO capacity event_id=%s",
                event.event_id,
            )
            self._remove(subscription)
            return
        subscription.queue.put_nowait(event)

    def _remove(self, subscription: _RuntimeEventSubscription) -> None:
        with self._lock:
            subscription.active = False
            self._subscribers.discard(subscription)


class SimulationRuntime:
    """Own the one engine tick loop and expose operator-safe controls."""

    def __init__(
        self,
        engine: SimulationEngine | None = None,
        *,
        tick_ms: int = DEFAULT_TICK_MS,
        adapter: SimulationAdapter | None = None,
    ) -> None:
        if isinstance(tick_ms, bool) or not isinstance(tick_ms, int) or tick_ms <= 0:
            raise ValueError("tick_ms must be a positive integer")
        self.engine = engine or SimulationEngine()
        if adapter is not None and adapter.engine is not self.engine:
            raise ValueError("SimulationAdapter must own the runtime engine")
        self.tick_ms = tick_ms
        self.adapter = adapter
        self.hub = LatestFrameHub()
        self.event_hub = RuntimeEventHub()
        self._frame_listeners: list[Callable[[SimulationFrame], None]] = []
        self._tick_task: asyncio.Task[None] | None = None

    def add_frame_listener(self, listener: Callable[[SimulationFrame], None]) -> None:
        if listener not in self._frame_listeners:
            self._frame_listeners.append(listener)

    async def start(self) -> None:
        if self._tick_task is not None:
            return
        self._tick_task = asyncio.create_task(self._run_ticks(), name="simulation-tick-loop")
        LOGGER.info("simulation runtime started tick_ms=%s", self.tick_ms)

    async def stop(self) -> None:
        if self._tick_task is None:
            return
        self._tick_task.cancel()
        try:
            await self._tick_task
        except asyncio.CancelledError:
            pass
        self._tick_task = None
        LOGGER.info("simulation runtime stopped")

    def pause(self) -> SimulationFrame:
        frame = self.engine.pause()
        if self.adapter is not None:
            self.adapter.update_frame(frame)
        self._publish_frame(frame)
        LOGGER.info("simulation paused revision=%s", frame.revision)
        return frame

    def resume(self) -> SimulationFrame:
        frame = self.engine.resume()
        if self.adapter is not None:
            self.adapter.update_frame(frame)
        self._publish_frame(frame)
        LOGGER.info("simulation resumed revision=%s", frame.revision)
        return frame

    def reset(self) -> SimulationFrame:
        if self.adapter is not None:
            self.adapter.cancel_all(code="simulation_reset")
        frame = self.engine.reset()
        if self.adapter is not None:
            self.adapter.update_frame(frame)
        self._publish_frame(frame)
        LOGGER.info("simulation reset revision=%s", frame.revision)
        return frame

    async def _run_ticks(self) -> None:
        interval_s = self.tick_ms / 1_000
        while True:
            await asyncio.sleep(interval_s)
            previous_revision = self.engine.snapshot().revision
            frame = self.tick_once()
            if not self.engine.paused or frame.revision != previous_revision:
                self._publish_frame(frame)

    def tick_once(self, *, now_ms: int | None = None) -> SimulationFrame:
        """Advance one authoritative tick; tests may supply a deterministic clock."""

        current_ms = int(time.time() * 1_000) if now_ms is None else now_ms
        if self.adapter is not None:
            self.adapter.before_tick(current_ms)
        frame = self.engine.advance(self.tick_ms)
        if self.adapter is not None:
            self.adapter.update_frame(frame)
            self.adapter.after_tick(current_ms)
            frame = self.engine.snapshot()
            self.adapter.update_frame(frame)
        return frame

    def _publish_frame(self, frame: SimulationFrame) -> None:
        self.hub.publish(frame)
        for listener in tuple(self._frame_listeners):
            try:
                listener(frame)
            except Exception:
                LOGGER.exception("simulation frame listener failed revision=%s", frame.revision)


def _web_dist_path(web_dist: Path | None) -> Path:
    resolved = web_dist or Path(__file__).resolve().parents[3] / "web" / "dist"
    if not resolved.is_dir() or not (resolved / "index.html").is_file():
        raise RuntimeError(f"web build output is missing: {resolved}")
    return resolved


def create_app(
    runtime: SimulationRuntime | None = None,
    *,
    web_dist: Path | None = None,
    device_service: MqttDeviceService | None = None,
    mission_coordinator: MissionTaskCoordinator | None = None,
    fault_coordinator: FaultCoordinator | None = None,
    runtime_capabilities: RuntimeCapabilities | None = None,
    emergency_stop_handler: Callable[[], ObservationMessage] | None = None,
    run_gate: RuntimeRunGate | None = None,
    replay_recorder: ReplayRecorder | None = None,
) -> FastAPI:
    simulation = runtime or SimulationRuntime()
    static_root = _web_dist_path(web_dist)
    capabilities = runtime_capabilities or RuntimeCapabilities(
        mode="preview",
        model_configured=False,
    )
    shared_run_gate = (
        run_gate
        or (mission_coordinator.run_gate if mission_coordinator is not None else None)
        or (fault_coordinator.run_gate if fault_coordinator is not None else None)
        or RuntimeRunGate()
    )
    if mission_coordinator is not None and mission_coordinator.run_gate is not shared_run_gate:
        raise ValueError("MissionTaskCoordinator must use the shared RuntimeRunGate")
    if fault_coordinator is not None and fault_coordinator.run_gate is not shared_run_gate:
        raise ValueError("FaultCoordinator must use the shared RuntimeRunGate")
    recorder = replay_recorder
    if recorder is None and (mission_coordinator is not None or fault_coordinator is not None):
        recorder = ReplayRecorder(simulation.engine.world_config, capabilities)
    if mission_coordinator is not None:
        mission_coordinator.add_event_listener(simulation.event_hub.publish)
    if recorder is not None:
        simulation.add_frame_listener(recorder.on_frame)
        if mission_coordinator is not None:
            mission_coordinator.add_event_listener(recorder.on_mission_event)
            mission_coordinator.add_completion_listener(recorder.complete_mission)
        if fault_coordinator is not None:
            fault_coordinator.add_run_listener(recorder.on_fault_started)
            fault_coordinator.add_completion_listener(recorder.complete_fault)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await simulation.start()
        try:
            if device_service is not None:
                await asyncio.to_thread(device_service.start)
                LOGGER.info(
                    "simulation bridge connected device_id=%s command_topic=%s",
                    device_service.backend.device_id,
                    device_service.topics.command,
                )
            yield
        finally:
            if device_service is not None:
                await asyncio.to_thread(device_service.stop)
            if mission_coordinator is not None:
                await asyncio.to_thread(mission_coordinator.shutdown)
            if fault_coordinator is not None:
                await asyncio.to_thread(fault_coordinator.shutdown)
            await simulation.stop()

    app = FastAPI(title="Quadruped Simulation Runtime", version="1.0", lifespan=lifespan)
    app.state.simulation = simulation
    app.state.device_service = device_service
    app.state.mission_coordinator = mission_coordinator
    app.state.fault_coordinator = fault_coordinator
    app.state.runtime_capabilities = capabilities
    app.state.emergency_stop_handler = emergency_stop_handler
    app.state.runtime_run_gate = shared_run_gate
    app.state.replay_recorder = recorder

    def api_error(status_code: int, code: str, message: str) -> JSONResponse:
        payload = RuntimeApiError(error_code=code, error_message=message)
        return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        detail = str(errors[0].get("msg", "request validation failed")) if errors else "request validation failed"
        return api_error(status.HTTP_400_BAD_REQUEST, "invalid_input", detail[:256])

    @app.get("/api/simulation/snapshot", response_model=SimulationFrame)
    async def snapshot() -> SimulationFrame:
        return simulation.engine.snapshot()

    @app.get("/api/simulation/world", response_model=WorldConfig)
    async def world() -> WorldConfig:
        return simulation.engine.world_config

    @app.post("/api/simulation/reset", response_model=SimulationFrame)
    async def reset() -> SimulationFrame:
        return simulation.reset()

    @app.post("/api/simulation/pause", response_model=SimulationFrame)
    async def pause() -> SimulationFrame:
        return simulation.pause()

    @app.post("/api/simulation/resume", response_model=SimulationFrame)
    async def resume() -> SimulationFrame:
        return simulation.resume()

    @app.post("/api/simulation/emergency-stop", response_model=ObservationMessage)
    async def emergency_stop() -> ObservationMessage | JSONResponse:
        if capabilities.mode != "bridge" or emergency_stop_handler is None:
            return api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "bridge_unavailable",
                "emergency stop requires bridge mode",
            )
        try:
            return await asyncio.to_thread(emergency_stop_handler)
        except MqttTransportError as exc:
            return api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "mqtt_transport_error",
                str(exc)[:256],
            )

    @app.get("/api/runtime/capabilities", response_model=RuntimeCapabilities)
    async def runtime_capability_snapshot() -> RuntimeCapabilities:
        return capabilities

    @app.post(
        "/api/tasks",
        response_model=MissionTaskAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_task(request: MissionTaskRequest) -> MissionTaskAccepted | JSONResponse:
        if capabilities.mode != "bridge" or mission_coordinator is None:
            return api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "bridge_unavailable",
                "mission tasks require bridge mode",
            )
        if request.planner == "model" and not capabilities.model_configured:
            return api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "provider_unconfigured",
                "model provider is not configured",
            )
        try:
            return mission_coordinator.submit(request)
        except MissionTaskActiveError:
            return api_error(
                status.HTTP_409_CONFLICT,
                "task_active",
                "another mission task is running",
            )
        except RuntimeRunGateBusyError:
            return api_error(
                status.HTTP_409_CONFLICT,
                "runtime_busy",
                "another mission or fault run is active",
            )

    @app.get("/api/tasks/current")
    async def current_task() -> Response:
        if mission_coordinator is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        snapshot: MissionTaskSnapshot | None = mission_coordinator.current()
        if snapshot is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return JSONResponse(content=snapshot.model_dump(mode="json"))

    @app.post(
        "/api/tasks/current/cancel",
        response_model=MissionTaskCancelAccepted,
    )
    async def cancel_current_task() -> MissionTaskCancelAccepted | JSONResponse:
        if capabilities.mode != "bridge" or mission_coordinator is None:
            return api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "bridge_unavailable",
                "mission cancellation requires bridge mode",
            )
        try:
            return mission_coordinator.cancel_current()
        except MissionTaskNotActiveError:
            return api_error(
                status.HTTP_409_CONFLICT,
                "task_not_active",
                "no mission task is active",
            )

    @app.post(
        "/api/faults",
        response_model=FaultRunAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_fault(request: FaultRunRequest) -> FaultRunAccepted | JSONResponse:
        if (
            capabilities.mode != "bridge"
            or not capabilities.fault_injection
            or fault_coordinator is None
        ):
            return api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "bridge_unavailable",
                "fault injection requires bridge mode",
            )
        try:
            return fault_coordinator.submit(request)
        except RuntimeRunGateBusyError:
            return api_error(
                status.HTTP_409_CONFLICT,
                "runtime_busy",
                "another mission or fault run is active",
            )

    @app.get("/api/faults/current")
    async def current_fault() -> Response:
        if (
            capabilities.mode != "bridge"
            or not capabilities.fault_injection
            or fault_coordinator is None
        ):
            return api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "bridge_unavailable",
                "fault injection requires bridge mode",
            )
        snapshot: FaultRunSnapshot | None = fault_coordinator.current()
        if snapshot is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return JSONResponse(content=snapshot.model_dump(mode="json"))

    @app.get("/api/replays/current")
    async def current_replay() -> Response:
        if recorder is None:
            return api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "bridge_unavailable",
                "replay export requires a runtime with Mission or Fault recording",
            )
        try:
            bundle = recorder.current_bundle()
        except ReplayEvidenceInvalid as exc:
            code = "evidence_active" if recorder.state == "recording" else "evidence_invalid"
            return api_error(status.HTTP_409_CONFLICT, code, str(exc)[:256])
        if bundle is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return JSONResponse(content=bundle.model_dump(mode="json"))

    @app.websocket("/ws/events")
    async def events(websocket: WebSocket) -> None:
        await websocket.accept()
        LOGGER.info("simulation websocket connected")
        try:
            async with (
                simulation.hub.subscribe() as frame_queue,
                simulation.event_hub.subscribe() as event_queue,
            ):
                await websocket.send_json(simulation.engine.snapshot().model_dump(mode="json"))
                frame_task = asyncio.create_task(frame_queue.get())
                event_task = asyncio.create_task(event_queue.get())
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            {frame_task, event_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if event_task in done:
                            event = event_task.result()
                            await websocket.send_json(event.model_dump(mode="json"))
                            event_task = asyncio.create_task(event_queue.get())
                        if frame_task in done:
                            frame = frame_task.result()
                            await websocket.send_json(frame.model_dump(mode="json"))
                            frame_task = asyncio.create_task(frame_queue.get())
                finally:
                    frame_task.cancel()
                    event_task.cancel()
                    await asyncio.gather(frame_task, event_task, return_exceptions=True)
        except WebSocketDisconnect:
            LOGGER.info("simulation websocket disconnected")
        except Exception:
            LOGGER.exception("simulation websocket failed")

    app.mount("/", StaticFiles(directory=static_root, html=True), name="web")
    return app


def create_bridge_app(
    config: MqttConfig | None = None,
    *,
    device_id: str = "dog01",
    web_dist: Path | None = None,
) -> FastAPI:
    """Create an idle simulation world driven only by MQTT commands."""

    engine = SimulationEngine(demo_mode=False, world_config=obstacle_world_config("left"))
    adapter = SimulationAdapter(engine, device_id=device_id)
    runtime = SimulationRuntime(engine, adapter=adapter)
    mqtt_config = config or MqttConfig()
    topics = MqttTopics(device_id)
    service = MqttDeviceService(adapter, mqtt_config, topics)
    try:
        model_config = ModelConfig.from_env()
    except ProviderConfigurationError:
        model_config = None
    run_gate = RuntimeRunGate()
    fault_runner = MqttFaultRunner(service, mqtt_config, topics)
    fault_coordinator = FaultCoordinator(
        fault_runner,
        cleanup_handler=fault_runner.cleanup,
        run_gate=run_gate,
    )
    coordinator = MissionTaskCoordinator(
        SimulationMissionRunner(mqtt_config, topics, model_config=model_config),
        run_gate=run_gate,
        cancel_handler=adapter.cancel_task,
    )
    capabilities = RuntimeCapabilities(
        mode="bridge",
        device_id=device_id,
        mqtt_endpoint=f"{mqtt_config.host}:{mqtt_config.port}",
        model_configured=model_config is not None,
        fault_injection=True,
    )
    return create_app(
        runtime,
        web_dist=web_dist,
        device_service=service,
        mission_coordinator=coordinator,
        fault_coordinator=fault_coordinator,
        runtime_capabilities=capabilities,
        emergency_stop_handler=lambda: execute_operator_emergency_stop(mqtt_config, topics),
        run_gate=run_gate,
    )


__all__ = [
    "LatestFrameHub",
    "RuntimeEventHub",
    "SimulationRuntime",
    "create_app",
    "create_bridge_app",
]
