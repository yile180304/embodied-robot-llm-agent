from __future__ import annotations

import json

from embodied_agent.cli import main
from embodied_agent.metrics import latency_summary, percentile


def test_latency_summary_contains_required_percentiles_and_samples():
    summary = latency_summary([1, 2, 3, 4, 5], environment="test")
    assert summary["sample_count"] == 5
    assert summary["p50"] == 3.0
    assert summary["p95"] == 4.8
    assert summary["max"] == 5.0
    assert len(summary["samples_ms"]) == 5
    assert percentile([1, 3], 50) == 2.0


def test_offline_cli_demo_can_write_transcript(tmp_path):
    output = tmp_path / "transcript.json"
    code = main(["demo", "前进 1 米", "--output", str(output)])
    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["final_status"] == "success"
    assert payload["steps"][0]["tool_call"]["name"] == "move_robot"


def test_provider_check_without_credentials_is_explicit(capsys, monkeypatch, tmp_path):
    for name in (
        "EMBODIED_AGENT_API_KEY",
        "EMBODIED_AGENT_MODEL",
        "EMBODIED_AGENT_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    code = main(["provider-check", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert output["configured"] is False


def test_model_demo_requires_explicit_broker(capsys, monkeypatch):
    monkeypatch.setenv("EMBODIED_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("EMBODIED_AGENT_MODEL", "test-model")
    monkeypatch.setenv("EMBODIED_AGENT_BASE_URL", "http://127.0.0.1:9999/v1")
    # Configuration is checked before the network call; the command then
    # refuses to route model actions through the in-process direct transport.
    code = main(["model-demo", "前进 1 米"])
    assert code == 2
    assert "--broker" in capsys.readouterr().err


def test_simulation_cli_prints_local_url_and_runs_uvicorn(capsys, monkeypatch):
    calls = []

    monkeypatch.setattr("embodied_agent.cli.create_app", lambda: object())
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: calls.append((app, host, port)))

    code = main(["simulation", "--host", "127.0.0.1", "--port", "8123"])

    assert code == 0
    assert calls == [(calls[0][0], "127.0.0.1", 8123)]
    assert "http://127.0.0.1:8123" in capsys.readouterr().out


def test_simulation_cli_bridge_uses_mqtt_runtime(capsys, monkeypatch):
    calls = []
    app = object()

    monkeypatch.setattr(
        "embodied_agent.cli.create_bridge_app",
        lambda config, device_id: calls.append((config, device_id)) or app,
    )
    monkeypatch.setattr("uvicorn.run", lambda value, host, port: calls.append((value, host, port)))

    code = main(
        [
            "simulation",
            "--bridge",
            "--host",
            "127.0.0.1",
            "--port",
            "8124",
            "--mqtt-port",
            "1884",
            "--device-id",
            "dog-sim",
        ]
    )

    assert code == 0
    assert calls[0][0].port == 1884
    assert calls[0][1] == "dog-sim"
    assert calls[1] == (app, "127.0.0.1", 8124)
    output = capsys.readouterr().out
    assert "mode=MQTT bridge" in output
    assert "robot/dog-sim/cmd" in output


def test_simulation_demo_parser_defaults_to_local_bridge():
    from embodied_agent.cli import build_parser

    args = build_parser().parse_args(["simulation-demo", "前进 1 米"])
    assert args.command == "simulation-demo"
    assert args.mqtt_host == "127.0.0.1"
    assert args.mqtt_port == 1883
    assert args.device_id == "dog01"
    assert args.max_steps == 8
