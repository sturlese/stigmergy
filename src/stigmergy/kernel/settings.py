"""Backend selection: the one parse+validation of CLEAN_LLM. Deliberately dependency-light, so
anything can learn the backend without importing a wider configuration object.
"""
import os

_VALID_BACKENDS = ("openai", "fake", "fake-flawed")

# The provider prefix → its key variable, for every family this stack PREFLIGHTS: the
# librarian's startup check and the vision-OCR gate both read THIS map (the librarian's
# `pydantic_backend` re-exports it, and its parametrized tests run off it). It lives HERE, not
# in `kernel.llm`, so a keyless module can consult it without loading the agent framework. An
# unlisted prefix is refused nowhere — pydantic-ai supports providers this table has not heard
# of, and every check that consults it says so.
PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google-gla": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def provider_of(model: str) -> str:
    """The provider prefix of a pydantic-ai model string, or `""` for a bare name — the ONE
    spelling of the two-form convention's predicate."""
    name = (model or "").strip()
    return name.split(":", 1)[0] if ":" in name else ""


def resolve_backend() -> str:
    """Read + validate CLEAN_LLM at call time (never at import). Returns one of
    'openai' | 'fake' | 'fake-flawed'. An unknown value raises, so a typo fails fast instead of
    silently falling through to the real OpenAI path."""
    backend = os.environ.get("CLEAN_LLM", "openai").lower()
    if backend not in _VALID_BACKENDS:
        raise RuntimeError(f"invalid CLEAN_LLM: {backend!r} (use 'openai', 'fake' or 'fake-flawed')")
    return backend
