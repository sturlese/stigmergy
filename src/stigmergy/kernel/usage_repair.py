"""Usage extraction fallback for unsupported provider detail fields."""
import dataclasses

_REPAIR_MARKER = "_stigmergy_usage_repair_installed"


def ensure_usage_extraction_repaired() -> bool:
    """Install the idempotent extraction fallback."""
    from pydantic_ai.usage import RequestUsage

    if getattr(RequestUsage.extract, _REPAIR_MARKER, False):
        return False

    original = RequestUsage.extract

    def _extract(cls, data, *, provider: str, provider_url: str, provider_fallback: str,
                 api_flavor: str = "default", details: dict | None = None):
        result = original(data, provider=provider, provider_url=provider_url,
                          provider_fallback=provider_fallback, api_flavor=api_flavor,
                          details=details)
        if _any_token_counted(result):
            return result
        repaired = _repair(cls, data, provider=provider, provider_url=provider_url,
                           provider_fallback=provider_fallback, api_flavor=api_flavor,
                           details=details)
        return result if repaired is None else repaired

    setattr(_extract, _REPAIR_MARKER, True)
    RequestUsage.extract = classmethod(_extract)
    return True


def _token_field_names(usage_class) -> tuple:
    return tuple(field.name for field in dataclasses.fields(usage_class)
                 if field.name != "details")


def _any_token_counted(result) -> bool:
    return any(getattr(result, name, 0) for name in _token_field_names(type(result)))


def _repair(usage_class, data, *, provider: str, provider_url: str, provider_fallback: str,
            api_flavor: str, details: dict | None):
    """Keep declared counts and preserve other provider fields as details."""
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
                unknown = {k: v for k, v in values.items() if k not in declared}
                return usage_class(**counts, details={**unknown, **(details or {})})
            except Exception:  # noqa: BLE001, S112 — mirrors the loop this repairs
                continue
    except Exception:  # noqa: BLE001 — the repair must never be worse than not repairing
        return None
    return None
