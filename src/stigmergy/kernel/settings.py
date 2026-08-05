"""Backend selection — the one dispatch every model-calling subsystem goes through.

Modules never read the environment at import time: `resolve_backend()` is called by
`kernel.llm.build_processor`, at call time.

All this holds is the backend name and its validation — deliberately, so no module needs to
import a wider configuration object just to learn which backend to build.
"""
import os

_VALID_BACKENDS = ("openai", "fake", "fake-flawed")


def resolve_backend() -> str:
    """Read + validate CLEAN_LLM once, at call time (never at import). Returns one of
    'openai' | 'fake' | 'fake-flawed'.

    Single source of truth for backend selection across every agent built on `kernel.llm`: an
    unknown value raises here so a typo fails fast instead of silently falling through to the real
    OpenAI path. It stays in this dependency-light module so anything can read the backend without
    pulling in pydantic-ai."""
    backend = os.environ.get("CLEAN_LLM", "openai").lower()
    if backend not in _VALID_BACKENDS:
        raise RuntimeError(f"invalid CLEAN_LLM: {backend!r} (use 'openai', 'fake' or 'fake-flawed')")
    return backend
