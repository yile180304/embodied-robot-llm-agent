"""Command-line entry points for local phase 2-5 demonstrations."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from pathlib import Path

from .agent_graph import AgentGraph, FakePlanner
from .device_simulator import DeviceSimulator
from .metrics import latency_summary, save_json
from .model_provider import ModelConfig, OpenAICompatiblePlanner, ProviderConfigurationError
from .mqtt_device_service import MqttDeviceService
from .mqtt_transport import MqttConfig, MqttConnectionError, MqttRequestClient, MqttTopics, MqttTransportError
from .schemas import RobotState
from .simulation.runtime import create_app, create_bridge_app
from .tool_registry import ToolExecutor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Embodied Agent local MQTT demo")
    sub = parser.add_subparsers(dest="command", required=True)

    device = sub.add_parser("device", help="run the MQTT device service")
    device.add_argument("--host", default="127.0.0.1")
    device.add_argument("--port", type=int, default=1883)
    device.add_argument("--device-id", default="dog01")

    demo = sub.add_parser("demo", help="run a Fake Planner Action-Observation demo")
    demo.add_argument("goal", nargs="?", default="前进 2 米，如果遇到障碍就从更宽的一侧绕开")
    demo.add_argument("--broker", action="store_true", help="use the local MQTT broker")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=1883)
    demo.add_argument("--output", type=Path, default=None)
    demo.add_argument("--max-steps", type=int, default=8)

    measure = sub.add_parser("measure", help="measure local MQTT round-trip latency")
    measure.add_argument("--samples", type=int, default=20)
    measure.add_argument("--host", default="127.0.0.1")
    measure.add_argument("--port", type=int, default=1883)
    measure.add_argument("--output", type=Path, default=Path("reports/latency.json"))

    provider = sub.add_parser("provider-check", help="validate provider configuration without calling a model")
    provider.add_argument("--json", action="store_true")

    model_demo = sub.add_parser("model-demo", help="run the configured OpenAI-compatible planner over MQTT")
    model_demo.add_argument("goal")
    model_demo.add_argument("--broker", action="store_true", help="use the local MQTT broker")
    model_demo.add_argument("--host", default="127.0.0.1")
    model_demo.add_argument("--port", type=int, default=1883)
    model_demo.add_argument("--output", type=Path, default=None)
    model_demo.add_argument("--max-steps", type=int, default=8)

    simulation = sub.add_parser("simulation", help="run the local Three.js simulation runtime")
    simulation.add_argument("--host", default="127.0.0.1")
    simulation.add_argument("--port", type=int, default=8000)
    simulation.add_argument("--bridge", action="store_true", help="accept MQTT commands instead of running the preview program")
    simulation.add_argument("--mqtt-host", default="127.0.0.1")
    simulation.add_argument("--mqtt-port", type=int, default=1883)
    simulation.add_argument("--device-id", default="dog01")

    simulation_demo = sub.add_parser(
        "simulation-demo",
        help="run FakePlanner and AgentGraph against a bridge-mode simulation device",
    )
    simulation_demo.add_argument("goal", nargs="?", default="前进 1 米")
    simulation_demo.add_argument("--mqtt-host", default="127.0.0.1")
    simulation_demo.add_argument("--mqtt-port", type=int, default=1883)
    simulation_demo.add_argument("--device-id", default="dog01")
    simulation_demo.add_argument("--output", type=Path, default=None)
    simulation_demo.add_argument("--max-steps", type=int, default=8)

    simulation_acceptance = sub.add_parser(
        "simulation-acceptance",
        help="run the fixed local simulation Acceptance Pack registry",
    )
    simulation_acceptance.add_argument("--mqtt-host", default="127.0.0.1")
    simulation_acceptance.add_argument("--mqtt-port", type=int, default=1883)
    simulation_acceptance.add_argument("--device-id", default=None)
    simulation_acceptance.add_argument("--output", type=Path, default=None)
    simulation_acceptance.add_argument(
        "--include-real-model",
        action="store_true",
        help="explicitly allow the fixed Acceptance Pack to call the configured external model provider",
    )
    return parser


def make_mqtt_config(args: argparse.Namespace) -> MqttConfig:
    return MqttConfig(host=args.host, port=args.port, connect_timeout_s=3.0, response_timeout_s=3.0)


def run_device(args: argparse.Namespace) -> int:
    simulator = DeviceSimulator(device_id=args.device_id)
    config = make_mqtt_config(args)
    topics = MqttTopics(args.device_id)
    logging.info("starting local device service on %s:%s topic=%s", config.host, config.port, topics.command)
    service = MqttDeviceService(simulator, config, topics)
    service.start()
    try:
        print(f"device service ready: {topics.command}")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        service.stop()


def run_demo(args: argparse.Namespace) -> int:
    task_id = f"cli-{uuid.uuid4().hex[:10]}"
    simulator = DeviceSimulator()
    config = make_mqtt_config(args)
    topics = MqttTopics(simulator.device_id)
    if args.broker:
        with MqttDeviceService(simulator, config, topics) as service:
            with MqttRequestClient(config, topics) as client:
                result = AgentGraph(FakePlanner(), ToolExecutor(client)).run(
                    args.goal,
                    task_id=task_id,
                    max_steps=args.max_steps,
                )
    else:
        class DirectTransport:
            def execute(self, command, *, timeout_s=None):
                return simulator.process_command(command, now_ms=command.sent_at_ms + 1)

        result = AgentGraph(FakePlanner(), ToolExecutor(DirectTransport())).run(
            args.goal,
            task_id=task_id,
            max_steps=args.max_steps,
        )
    payload = result.model_dump(mode="json")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        save_json(payload, args.output)
    return 0 if result.final_status == "success" else 2


def run_measure(args: argparse.Namespace) -> int:
    if args.samples < 1:
        raise ValueError("--samples must be positive")
    simulator = DeviceSimulator(
        initial_state=RobotState(front_distance_cm=100.0),
        obstacle_on_first_move=False,
    )
    config = make_mqtt_config(args)
    topics = MqttTopics(simulator.device_id)
    samples: list[float] = []
    with MqttDeviceService(simulator, config, topics) as service:
        with MqttRequestClient(config, topics) as client:
            for index in range(args.samples):
                sent = int(time.time() * 1000)
                command = {
                    "version": 1,
                    "task_id": f"measure-{uuid.uuid4().hex[:10]}",
                    "seq": index + 1,
                    "tool": "get_robot_state",
                    "params": {},
                    "deadline_ms": 10_000,
                    "sent_at_ms": sent,
                }
                from .schemas import CommandMessage
                typed = CommandMessage.model_validate(command)
                started = time.perf_counter_ns()
                client.execute(typed)
                samples.append((time.perf_counter_ns() - started) / 1_000_000)
    summary = latency_summary(
        samples,
        environment="localhost Mosquitto 2.1.2 + Python DeviceSimulator; RTT publish-to-Observation",
    )
    save_json(summary, args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def run_provider_check(args: argparse.Namespace) -> int:
    try:
        config = ModelConfig.from_env()
        # Construction is intentionally omitted: this command validates only configuration.
        payload = {"configured": True, "model": config.model, "base_url": config.base_url}
        print(json.dumps(payload, ensure_ascii=False) if args.json else f"configured: {config.model} @ {config.base_url}")
        return 0
    except ProviderConfigurationError as exc:
        payload = {"configured": False, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False) if args.json else str(exc))
        return 2


def run_model_demo(args: argparse.Namespace) -> int:
    """Run a real provider only through native Function Calling messages."""

    try:
        config = ModelConfig.from_env()
    except ProviderConfigurationError as exc:
        print(json.dumps({"configured": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    if not args.broker:
        print("model-demo requires --broker so provider actions use a real MQTT transport", file=sys.stderr)
        return 2
    planner = OpenAICompatiblePlanner(config)
    task_id = f"model-{uuid.uuid4().hex[:10]}"
    simulator = DeviceSimulator()
    mqtt_config = make_mqtt_config(args)
    topics = MqttTopics(simulator.device_id)

    with MqttDeviceService(simulator, mqtt_config, topics):
        with MqttRequestClient(mqtt_config, topics) as client:
            result = AgentGraph(planner, ToolExecutor(client)).run(
                args.goal,
                task_id=task_id,
                max_steps=args.max_steps,
            )
    payload = result.model_dump(mode="json")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        save_json(payload, args.output)
    return 0 if result.final_status == "success" else 2


def run_simulation(args: argparse.Namespace) -> int:
    try:
        if args.bridge:
            mqtt_config = MqttConfig(
                host=args.mqtt_host,
                port=args.mqtt_port,
                connect_timeout_s=3.0,
                response_timeout_s=16.0,
            )
            app = create_bridge_app(mqtt_config, device_id=args.device_id)
        else:
            app = create_app()
    except RuntimeError as exc:
        print(f"simulation startup failed: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    url = f"http://{args.host}:{args.port}"
    mode = "MQTT bridge" if args.bridge else "preview"
    print(f"simulation runtime available at {url} mode={mode}")
    if args.bridge:
        topics = MqttTopics(args.device_id)
        print(
            f"simulation bridge device={args.device_id} broker={args.mqtt_host}:{args.mqtt_port} "
            f"command_topic={topics.command}"
        )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def run_simulation_demo(args: argparse.Namespace) -> int:
    task_id = f"simulation-{uuid.uuid4().hex[:10]}"
    config = MqttConfig(
        host=args.mqtt_host,
        port=args.mqtt_port,
        connect_timeout_s=3.0,
        response_timeout_s=16.0,
    )
    topics = MqttTopics(args.device_id)
    try:
        with MqttRequestClient(config, topics) as client:
            result = AgentGraph(FakePlanner(), ToolExecutor(client)).run(
                args.goal,
                task_id=task_id,
                max_steps=args.max_steps,
                deadline_ms=15_000,
                response_timeout_s=16.0,
            )
    except (MqttConnectionError, MqttTransportError) as exc:
        print(f"simulation-demo transport failed: {exc}", file=sys.stderr)
        return 2
    payload = result.model_dump(mode="json")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        save_json(payload, args.output)
    return 0 if result.final_status == "success" else 2


def run_simulation_acceptance(args: argparse.Namespace) -> int:
    from .simulation.acceptance import generate_acceptance_pack

    device_id = args.device_id or f"dog-accept-{uuid.uuid4().hex[:8]}"
    output_dir = args.output or Path("reports") / time.strftime("acceptance-pack-%Y%m%d-%H%M%S")
    config = MqttConfig(
        host=args.mqtt_host,
        port=args.mqtt_port,
        connect_timeout_s=3.0,
        response_timeout_s=16.0,
    )
    try:
        pack = generate_acceptance_pack(
            mqtt_config=config,
            device_id=device_id,
            output_dir=output_dir,
            include_real_model=args.include_real_model,
        )
    except (RuntimeError, ValueError, MqttConnectionError, MqttTransportError) as exc:
        print(f"simulation acceptance failed: {exc}", file=sys.stderr)
        return 2
    payload = {
        "output_dir": str(pack.output_dir),
        "device_id": device_id,
        "passed": pack.truthfulness.passed,
        "failed": pack.truthfulness.failed,
        "skipped": pack.truthfulness.skipped,
        "manifest": str(pack.output_dir / "manifest.json"),
        "truthfulness_json": str(pack.output_dir / "truthfulness.json"),
        "truthfulness_markdown": str(pack.output_dir / "truthfulness.md"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return pack.exit_code


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.command == "device":
        return run_device(args)
    if args.command == "demo":
        return run_demo(args)
    if args.command == "measure":
        return run_measure(args)
    if args.command == "provider-check":
        return run_provider_check(args)
    if args.command == "model-demo":
        return run_model_demo(args)
    if args.command == "simulation":
        return run_simulation(args)
    if args.command == "simulation-demo":
        return run_simulation_demo(args)
    if args.command == "simulation-acceptance":
        return run_simulation_acceptance(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
