from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from embodied_agent import (
    AgentStep,
    ModelConfig,
    ObservationMessage,
    ObservationStatus,
    OpenAICompatiblePlanner,
    PlannerContext,
    ProviderConfigurationError,
    ProviderResponseError,
    ToolCall,
)


def test_model_config_requires_explicit_environment_values():
    with pytest.raises(ProviderConfigurationError) as error:
        ModelConfig.from_env({})
    assert "EMBODIED_AGENT_API_KEY" in str(error.value)


def test_model_config_can_load_project_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "EMBODIED_AGENT_API_KEY=dotenv-key\n"
        "EMBODIED_AGENT_MODEL=dotenv-model\n"
        "EMBODIED_AGENT_BASE_URL=http://127.0.0.1:9999/v1\n",
        encoding="utf-8",
    )
    for name in (
        "EMBODIED_AGENT_API_KEY",
        "EMBODIED_AGENT_MODEL",
        "EMBODIED_AGENT_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    config = ModelConfig.from_env()
    assert config.api_key == "dotenv-key"
    assert config.model == "dotenv-model"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EMBODIED_AGENT_TEMPERATURE", "hot"),
        ("EMBODIED_AGENT_TEMPERATURE", "2.1"),
        ("EMBODIED_AGENT_TIMEOUT_S", "0"),
        ("EMBODIED_AGENT_TIMEOUT_S", "forever"),
        ("EMBODIED_AGENT_BASE_URL", "provider.example/v1"),
    ],
)
def test_model_config_rejects_invalid_values_without_echoing_secrets(name, value):
    environ = {
        "EMBODIED_AGENT_API_KEY": "secret-do-not-echo",
        "EMBODIED_AGENT_MODEL": "test-model",
        "EMBODIED_AGENT_BASE_URL": "https://provider.example/v1",
        name: value,
    }
    with pytest.raises(ProviderConfigurationError) as error:
        ModelConfig.from_env(environ)
    message = str(error.value)
    assert name in message
    assert "secret-do-not-echo" not in message
    assert "https://provider.example/v1" not in message


def test_model_config_repr_hides_key_and_endpoint():
    config = ModelConfig(
        api_key="secret-do-not-log",
        model="test-model",
        base_url="https://private-provider.example/v1",
    )
    rendered = repr(config)
    assert "secret-do-not-log" not in rendered
    assert "private-provider" not in rendered


def test_provider_invocation_error_is_sanitized():
    class BoundModel:
        def invoke(self, messages):
            raise RuntimeError("Authorization: Bearer secret at https://private-provider.example/v1")

    class RawModel:
        def bind_tools(self, schemas):
            return BoundModel()

    planner = OpenAICompatiblePlanner(
        ModelConfig(
            api_key="secret",
            model="test-model",
            base_url="https://private-provider.example/v1",
        ),
        model_factory=lambda **kwargs: RawModel(),
    )
    context = PlannerContext(
        goal="读取状态",
        task_id="model-error",
        next_seq=1,
        step_count=0,
        max_steps=4,
        last_observation=None,
        known_state=None,
        steps=(),
    )
    with pytest.raises(ProviderResponseError) as error:
        planner.plan(context)
    assert str(error.value) == "model invocation failed: RuntimeError"


def test_native_tool_call_is_converted_to_planner_decision_without_execution():
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "move_robot",
                "args": {"distance_m": 1.0, "speed_mps": 0.2},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )
    decision = OpenAICompatiblePlanner._parse_response(response)
    assert decision.kind == "tool_call"
    assert decision.tool_call is not None
    assert decision.tool_call.name == "move_robot"


def test_provider_context_includes_prior_tool_history_and_completion_rule():
    observation = ObservationMessage(
        version=1,
        task_id="model-history",
        seq=1,
        status=ObservationStatus.SUCCESS,
        observation={"x_m": 0.0, "y_m": 0.0, "yaw_deg": 0.0},
        received_at_ms=1,
    )
    step = AgentStep(
        seq=1,
        planner_message="model selected a registered tool",
        tool_call=ToolCall(name="get_robot_state", arguments={}),
        command=None,
        observation=observation,
        published=True,
        started_at_ms=0,
        finished_at_ms=1,
        latency_ms=1,
    )
    context = PlannerContext(
        goal="读取机器人状态并告诉我当前位姿",
        task_id="model-history",
        next_seq=2,
        step_count=1,
        max_steps=4,
        last_observation=observation,
        known_state=None,
        steps=(step,),
    )

    message = OpenAICompatiblePlanner._context_message(context)

    assert '"tool":"get_robot_state"' in message
    assert '"status":"success"' in message
    assert "Do not repeat a successful read-only call" in message


def test_model_code_block_is_rejected_and_never_executed():
    response = AIMessage(content="```python\nprint('should never execute')\n```")
    with pytest.raises(ProviderResponseError, match="code-like"):
        OpenAICompatiblePlanner._parse_response(response)


def test_model_json_body_is_rejected_instead_of_treated_as_a_tool_call_or_final():
    response = AIMessage(
        content='{"name":"get_robot_state","arguments":{}}',
    )
    with pytest.raises(ProviderResponseError, match="JSON text"):
        OpenAICompatiblePlanner._parse_response(response)


def test_multiple_native_tool_calls_are_rejected():
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "get_robot_state", "args": {}, "id": "1", "type": "tool_call"},
            {"name": "scan_obstacles", "args": {}, "id": "2", "type": "tool_call"},
        ],
    )
    with pytest.raises(ProviderResponseError):
        OpenAICompatiblePlanner._parse_response(response)
