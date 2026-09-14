"""Approved OpenRouter model construction for every model-backed runtime path."""

from __future__ import annotations

import contextlib
import os

from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
ANSWER_MODEL = "openrouter:z-ai/glm-5.2"
LIBRARIAN_MODEL = "openrouter:openai/gpt-oss-120b"
LIBRARIAN_REASONING_LEVEL = "high"
LIBRARIAN_MAX_TOKENS = 16384
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

# The librarian's native structured output is verified on Cerebras. A failed request must retry
# rather than silently route through an unverified host.
LIBRARIAN_PROVIDER_ROUTING = {
    "allow_fallbacks": False,
    "only": ["cerebras"],
}


def provider_policy(model_name: str) -> dict:
    """The OpenRouter provider policy one approved model is requested with."""
    policy = dict(OPENROUTER_PROVIDER_POLICY)
    if model_name == LIBRARIAN_MODEL:
        policy.update(LIBRARIAN_PROVIDER_ROUTING)
        policy["only"] = list(LIBRARIAN_PROVIDER_ROUTING["only"])
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
    model = OpenRouterModel(
        model_name.removeprefix("openrouter:"),
        provider=OpenRouterProvider(api_key=key),
        settings=model_settings,
    )
    return model, model_settings
