"""LLM backend construction — the ONE fake/real dispatch for every agent in the package.

`build_model()`: model + settings from CLEAN_MODEL / CLEAN_REASONING_EFFORT, read at call time.
`build_processor()`: the validated backend picks the caller's fake or a real Agent; tool
registration stays with the caller via the `tools` hook. Imports nothing from the package except
settings, so any agent module can use it without inheriting the converter/tooling stack.
"""
import os
from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent

from stigmergy.kernel.settings import resolve_backend

DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_REASONING_EFFORT = "medium"
_VALID_EFFORTS = ("minimal", "low", "medium", "high")


def build_model(model_name: str | None = None):
    """Model + settings for the agents. Without an explicit name, CLEAN_MODEL is resolved HERE,
    at call time, never at import — this is the single place that reads it.

    Two forms of CLEAN_MODEL: a bare name ("gpt-5.6-terra") means the OpenAI Responses API with
    an EXPLICIT reasoning effort (never the API's implicit default; requires OPENAI_API_KEY); a
    provider-prefixed pydantic-ai string ("anthropic:claude-sonnet-4-5") is resolved by
    pydantic-ai, whose provider reads its own env key.
    """
    from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
    from pydantic_ai.providers.openai import OpenAIProvider

    from stigmergy.kernel.usage_repair import ensure_usage_extraction_repaired

    # The pinned pydantic-ai reports zero tokens for OpenAI reasoning models — a zero that reads
    # as free, the one direction a cost figure must never lie in. Every agent-construction site
    # installs the repair. Idempotent — see `kernel.usage_repair`.
    ensure_usage_extraction_repaired()

    model_name = model_name or os.environ.get("CLEAN_MODEL", DEFAULT_MODEL)
    if ":" in model_name:
        return model_name, None
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required (set it in the environment / .env)")
    effort = os.environ.get("CLEAN_REASONING_EFFORT", DEFAULT_REASONING_EFFORT)
    if effort not in _VALID_EFFORTS:
        raise RuntimeError(f"invalid CLEAN_REASONING_EFFORT: {effort!r} (use one of {_VALID_EFFORTS})")
    model = OpenAIResponsesModel(model_name, provider=OpenAIProvider(api_key=key))
    return model, OpenAIResponsesModelSettings(openai_reasoning_effort=effort)


def build_processor(output_type, instructions: str, *, fake: Callable[[bool], Any],
                    deps_type: type | None = None,
                    tools: Callable[[Agent], None] | None = None,
                    model_name: str | None = None):
    """The CLEAN_LLM dispatch shared by every agent builder in the package.

    `fake(flawed)` constructs the caller's offline backend — resolve_backend has already
    validated the value, so the only decision left is fake vs fake-flawed. On the real path the
    caller's `tools` hook registers its @agent.tool functions on the constructed Agent.
    `model_name` lets a caller outside the CLEAN_MODEL convention name its own model setting;
    omitted, CLEAN_MODEL applies.
    """
    backend = resolve_backend()
    if backend != "openai":
        return fake(backend == "fake-flawed")
    model, settings = build_model(model_name)
    kwargs = {"deps_type": deps_type} if deps_type is not None else {}
    agent = Agent(model, output_type=output_type, instructions=instructions,
                  model_settings=settings, **kwargs)
    if tools is not None:
        tools(agent)
    return agent
