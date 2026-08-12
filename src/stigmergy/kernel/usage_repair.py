"""A shim for a reproduced defect in the pinned `pydantic-ai==2.13.0`: token counts silently
extracted as ZERO for any OpenAI model reporting `output_tokens_details.reasoning_tokens` —
`RequestUsage.extract` raises a `TypeError` (`output_reasoning_tokens` is not a declared field)
inside its own swallowing `try` and falls through to all-zero counts, so a real, paid response
reports as free, and the raw counts are unrecoverable downstream. The wrapper defers to the
original whenever it extracted anything, so it can improve a figure, never worsen one, and
self-retires on a fixed version (deliberately not version-gated). Delete it, and its call sites,
at a pin bump whose changelog fixes the kwarg mismatch.
"""
import dataclasses

# Stamped on the installed wrapper, so a second call is a no-op rather than a wrapper around a
# wrapper.
_REPAIR_MARKER = "_stigmergy_usage_repair_installed"


def ensure_usage_extraction_repaired() -> bool:
    """Install the extraction repair if it is not already installed. Returns whether it installed.

    Idempotent and safe to call on every agent construction. Imports live inside the function:
    a kernel module must cost nothing to import, and loading an agent framework at module scope
    is banned (`tests/test_architecture.py`).
    """
    from pydantic_ai.usage import RequestUsage

    if getattr(RequestUsage.extract, _REPAIR_MARKER, False):
        return False

    original = RequestUsage.extract          # already bound to the class; call it as-is

    def _extract(cls, data, *, provider: str, provider_url: str, provider_fallback: str,
                 api_flavor: str = "default", details: dict | None = None):
        result = original(data, provider=provider, provider_url=provider_url,
                          provider_fallback=provider_fallback, api_flavor=api_flavor,
                          details=details)
        if _any_token_counted(result):
            return result
        # Every count is zero: either a genuinely empty response (the repair finds the same
        # zeros) or the swallowed TypeError.
        repaired = _repair(cls, data, provider=provider, provider_url=provider_url,
                           provider_fallback=provider_fallback, api_flavor=api_flavor,
                           details=details)
        return result if repaired is None else repaired

    setattr(_extract, _REPAIR_MARKER, True)          # stamped BEFORE it can be observed
    RequestUsage.extract = classmethod(_extract)
    return True


def _token_field_names(usage_class) -> tuple:
    """Every field of the usage dataclass that holds a COUNT — i.e. all of them but `details`."""
    return tuple(field.name for field in dataclasses.fields(usage_class)
                 if field.name != "details")


def _any_token_counted(result) -> bool:
    """Any non-zero count means the framework's own path worked, and the answer is its own.
    EVERY count is asked, not only input/output: a response reporting only cache reads still
    proves the extraction succeeded."""
    return any(getattr(result, name, 0) for name in _token_field_names(type(result)))


def _repair(usage_class, data, *, provider: str, provider_url: str, provider_fallback: str,
            api_flavor: str, details: dict | None):
    """The framework's own provider-fallback loop, with the construction that does not raise.

    The candidate list and lookup mirror `RequestUsage.extract` exactly — a repair consulting a
    different provider would answer a different question than the one that failed. Differences:
    the construction FILTERS to the declared fields and stashes the rest in `details` (the whole
    fix), and an all-zero candidate does not end the loop. `None` = recovered nothing; the caller
    keeps the original's answer. Every exception is swallowed — a broken snapshot lookup must not
    turn a successful, already-paid model call into a failed item over telemetry.
    """
    try:
        from genai_prices.data_snapshot import get_snapshot

        declared = set(_token_field_names(usage_class))
        for provider_id, provider_api_url in [(None, provider_url), (provider, None),
                                              (provider_fallback, None)]:
            try:
                provider_obj = get_snapshot().find_provider(None, provider_id, provider_api_url)
                _model_ref, extracted = provider_obj.extract_usage(data, api_flavor=api_flavor)
                values = {k: v for k, v in vars(extracted).items() if v is not None}
                counts = {k: v for k, v in values.items() if k in declared}
                if not any(counts.values()):
                    continue
                # The caller's own `details` wins a key collision: it is the framework's curated
                # view.
                unknown = {k: v for k, v in values.items() if k not in declared}
                return usage_class(**counts, details={**unknown, **(details or {})})
            except Exception:  # noqa: BLE001, S112 — mirrors the loop this repairs
                continue
    except Exception:  # noqa: BLE001 — the repair must never be worse than not repairing
        return None
    return None
