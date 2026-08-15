"""Optional OpenAI-compatible Function Calling planner.

This module is deliberately only a parser/adapter: model text is never
executed.  A provider is enabled only when its endpoint, model, and API key
are supplied through environment variables or explicit configuration.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

from .agent_graph import PlannerContext, PlannerDecision
from .tool_registry import ToolCall, ToolRegistry


class ProviderConfigurationError(ValueError):
    pass


class ProviderResponseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelConfig:
    api_key: str = field(repr=False)
    model: str
    base_url: str = field(repr=False)
    temperature: float = 0.0
    timeout_s: float = 30.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ModelConfig":
        if environ is None:
            load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
            env: Mapping[str, str] = os.environ
        else:
            env = environ
        api_key = env.get("EMBODIED_AGENT_API_KEY", "").strip()
        model = env.get("EMBODIED_AGENT_MODEL", "").strip()
        base_url = env.get("EMBODIED_AGENT_BASE_URL", "").strip()
        missing = [name for name, value in (
            ("EMBODIED_AGENT_API_KEY", api_key),
            ("EMBODIED_AGENT_MODEL", model),
            ("EMBODIED_AGENT_BASE_URL", base_url),
        ) if not value]
        if missing:
            raise ProviderConfigurationError(
                "missing model configuration: " + ", ".join(missing)
            )
        temperature = cls._number(
            env.get("EMBODIED_AGENT_TEMPERATURE", "0"),
            name="EMBODIED_AGENT_TEMPERATURE",
            minimum=0.0,
            maximum=2.0,
        )
        timeout_s = cls._number(
            env.get("EMBODIED_AGENT_TIMEOUT_S", "30"),
            name="EMBODIED_AGENT_TIMEOUT_S",
            minimum=0.001,
            maximum=300.0,
        )
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ProviderConfigurationError(
                "invalid model configuration: EMBODIED_AGENT_BASE_URL must be an absolute http(s) URL"
            )
        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            temperature=temperature,
            timeout_s=timeout_s,
        )

    @staticmethod
    def _number(raw: str, *, name: str, minimum: float, maximum: float) -> float:
        try:
            value = float(raw.strip())
        except (AttributeError, ValueError) as exc:
            raise ProviderConfigurationError(
                f"invalid model configuration: {name} must be a number"
            ) from exc
        if not minimum <= value <= maximum:
            raise ProviderConfigurationError(
                f"invalid model configuration: {name} must be between {minimum:g} and {maximum:g}"
            )
        return value


class ChatModelLike(Protocol):
    def invoke(self, messages: list[Any]) -> Any: ...


class OpenAICompatiblePlanner:
    """Planner backed by native tool calls from an OpenAI-compatible API."""

    def __init__(
        self,
        model_config: ModelConfig,
        *,
        registry: ToolRegistry | None = None,
        model_factory: Callable[..., ChatOpenAI] = ChatOpenAI,
        system_prompt: str | None = None,
    ) -> None:
        self.config = model_config
        self.registry = registry or ToolRegistry()
        self.system_prompt = system_prompt or (
            "You are a high-level robot task planner operating in bounded one-step turns. "
            "Use only the declared tools and choose at most one tool per turn. "
            "Read prior_steps and last_observation before deciding. "
            "When the latest successful observation satisfies the user's goal, return a concise plain-text "
            "completion and do not repeat the same read-only tool. "
            "Call another tool only when the goal still requires another physical action or observation. "
            "Never output or execute Python, shell, C, PWM, motor-current, or PID code. "
            "Return a native tool call when an action is needed; return plain text only when the task is complete."
        )
        raw_model = model_factory(
            model=self.config.model,
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            temperature=self.config.temperature,
            timeout=self.config.timeout_s,
        )
        self._model = raw_model.bind_tools(self.registry.function_schemas())

    def plan(self, context: PlannerContext) -> PlannerDecision:
        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=self._context_message(context)),
        ]
        try:
            response = self._model.invoke(messages)
        except Exception as exc:
            raise ProviderResponseError(f"model invocation failed: {type(exc).__name__}") from exc
        return self._parse_response(response)

    @staticmethod
    def _context_message(context: PlannerContext) -> str:
        observation = (
            context.last_observation.model_dump_json()
            if context.last_observation is not None
            else "none"
        )
        prior_steps = [
            {
                "seq": step.seq,
                "tool": step.tool_call.name,
                "arguments": step.tool_call.arguments,
                "status": step.observation.status.value,
                "error_code": step.observation.error_code,
            }
            for step in context.steps[-12:]
        ]
        return (
            f"Goal: {context.goal}\n"
            f"task_id: {context.task_id}\n"
            f"step: {context.step_count}/{context.max_steps}\n"
            f"prior_steps: {json.dumps(prior_steps, ensure_ascii=False, separators=(',', ':'))}\n"
            f"last_observation: {observation}\n"
            "Choose exactly one next action: emit one native high-level tool call, or state that the goal is complete. "
            "Do not repeat a successful read-only call when its observation already answers the goal."
        )

    @staticmethod
    def _parse_response(response: Any) -> PlannerDecision:
        if not isinstance(response, AIMessage):
            # Accept compatible message doubles in tests without accepting code.
            tool_calls = getattr(response, "tool_calls", None)
            content = getattr(response, "content", "")
        else:
            tool_calls = response.tool_calls
            content = response.content
        if tool_calls:
            if len(tool_calls) != 1:
                raise ProviderResponseError("expected exactly one tool call per planner step")
            call = tool_calls[0]
            name = call.get("name")
            args = call.get("args", {})
            if not isinstance(name, str) or not isinstance(args, dict):
                raise ProviderResponseError("model returned malformed native tool call")
            return PlannerDecision.call(name, args, "model selected a registered tool")
        if isinstance(content, list):
            text = " ".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content)
        else:
            text = str(content)
        if not text.strip():
            raise ProviderResponseError("model returned neither a tool call nor completion text")
        normalized = text.strip()
        if "```" in normalized or "python" in normalized.lower() or "shell" in normalized.lower():
            raise ProviderResponseError(
                "model returned code-like text instead of a native Tool Call"
            )
        if normalized[:1] in {"{", "["}:
            try:
                json.loads(normalized)
            except json.JSONDecodeError:
                pass
            else:
                raise ProviderResponseError(
                    "model returned JSON text instead of a native Tool Call"
                )
        return PlannerDecision.finish("success", normalized[:512])


__all__ = [
    "ModelConfig",
    "OpenAICompatiblePlanner",
    "ProviderConfigurationError",
    "ProviderResponseError",
]
