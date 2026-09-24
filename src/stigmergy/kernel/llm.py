"""Approved OpenRouter model construction for every model-backed runtime path."""

from __future__ import annotations

import contextlib
import os

from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings

LIBRARIAN_MODEL = "openrouter:deepseek/deepseek-v4.1-flash"
# Answers share the librarian model, and so its reasoning, token ceiling, and provider routing.
ANSWER_MODEL = LIBRARIAN_MODEL
LIBRARIAN_REASONING_LEVEL = "medium"
LIBRARIAN_MAX_TOKENS = 40960
LIBRARIAN_TEMPERATURE = 0
OCR_MODEL = "openrouter:qwen/qwen3-vl-8b-instruct"
APPROVED_MODELS = frozenset(
    {ANSWER_MODEL, LIBRARIAN_MODEL, OCR_MODEL}
)

OPENROUTER_PROVIDER_POLICY = {
    "allow_fallbacks": True,
    "require_parameters": True,
    "data_collection": "deny",
    "zdr": True,
}

# Prefer the fastest privacy-compatible host. Native structured output is required below, so
# OpenRouter automatically excludes providers that cannot honor the librarian's schema.
LIBRARIAN_PROVIDER_ROUTING = {
    "sort": "throughput",
}
def provider_policy(model_name: str) -> dict:
    """The OpenRouter provider policy one approved model is requested with."""
    policy = dict(OPENROUTER_PROVIDER_POLICY)
    if model_name == LIBRARIAN_MODEL:
        policy.update(LIBRARIAN_PROVIDER_ROUTING)
    return policy

_MODEL_OVERRIDE = None


@contextlib.contextmanager
def model_override(model):
    """Temporarily inject a pydantic-ai model object for controlled tests."""
    global _MODEL_OVERRIDE
    previous = _MODEL_OVERRIDE
    _MODEL_OVERRIDE = model
    try:
        yield
    finally:
        _MODEL_OVERRIDE = previous


def build_model(model_name: str = ANSWER_MODEL):
    """Construct an approved model with the mandatory OpenRouter privacy policy."""
    if _MODEL_OVERRIDE is not None:
        return _MODEL_OVERRIDE, None
    if model_name not in APPROVED_MODELS:
        raise RuntimeError(f"model is not approved for Stigmergy: {model_name!r}")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is required")

    from pydantic_ai.providers.openrouter import OpenRouterProvider

    from stigmergy.kernel.usage_repair import ensure_usage_extraction_repaired

    ensure_usage_extraction_repaired()
    model_settings = OpenRouterModelSettings(
        openrouter_provider=provider_policy(model_name)
    )
    if model_name == LIBRARIAN_MODEL:
        model_settings["max_tokens"] = LIBRARIAN_MAX_TOKENS
        model_settings["openrouter_reasoning"] = {
            "effort": LIBRARIAN_REASONING_LEVEL,
            "exclude": True,
        }
    if model_name == LIBRARIAN_MODEL:
        model_settings["temperature"] = LIBRARIAN_TEMPERATURE
    model_kwargs = {}
    if model_name == LIBRARIAN_MODEL:
        # OpenRouter exposes native JSON Schema for DeepSeek V4.1 Flash. The installed
        # pydantic-ai catalogue predates the model and otherwise falls back to tool output.
        model_kwargs["profile"] = {
            "supports_json_schema_output": True,
            "supports_json_object_output": True,
        }
    model = OpenRouterModel(
        model_name.removeprefix("openrouter:"),
        provider=OpenRouterProvider(api_key=key),
        settings=model_settings,
        **model_kwargs,
    )
    return model, model_settings
