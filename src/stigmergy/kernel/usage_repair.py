"""A shim for one pydantic-ai bug: token counts silently extracted as ZERO.

**This is not defensive programming.** It repairs a specific, reproduced defect in the pinned
`pydantic-ai==2.13.0`, and it is written to disappear the moment the framework fixes itself.

## The bug

`pydantic_ai.usage.RequestUsage.extract` asks genai-prices for the token counts and then builds
its own dataclass from whatever came back:

    _model_ref, extracted_usage = provider_obj.extract_usage(data, api_flavor=api_flavor)
    return cls(**{k: v for k, v in extracted_usage.__dict__.items() if v is not None}, details=details)

genai-prices' own `Usage` carries fields `RequestUsage` does not. For **any OpenAI model that
reports `output_tokens_details.reasoning_tokens`** — which a reasoning model does on every
response, including when the count is `0` — the extraction returns `output_reasoning_tokens`, and
the construction raises:

    TypeError: RequestUsage.__init__() got an unexpected keyword argument
    'output_reasoning_tokens'. Did you mean 'output_audio_tokens'?

That `TypeError` is raised INSIDE the method's own `try`, whose `except Exception: pass` exists to
move on to the next provider candidate. All three candidates fail identically, so the method falls
through to `return cls(details=details)` — a `RequestUsage` with every token count at zero. No
warning, no log line, no exception: a real, paid response reports as if it had cost nothing.

Measured, not theorised: a real OpenAI Responses payload `{"input_tokens": 9, ..., "output_tokens":
5, "output_tokens_details": {"reasoning_tokens": 0}}` extracts as `{'input_tokens': 9,
'cache_read_tokens': 0, 'output_reasoning_tokens': 0, 'output_tokens': 5}` and lands as
`RequestUsage(input_tokens=0, output_tokens=0, details={'reasoning_tokens': 0})`. **The raw counts
are not recoverable afterwards** — `provider_details` keeps only the finish reason and a timestamp
— which is why this has to be repaired at the extraction seam rather than downstream.

## Why the kernel owns it, and why it is worth owning

Every pydantic-ai consumer in this process shares that one classmethod: the librarian's meeting
backend prices a filing from it, `views` and the `gardener` sweep account with it, and `ask` writes
its token counters into `audit_log.result.usage` from it. One patch site, applied by whichever of
them runs first.

The cost of NOT repairing it is asymmetric in the direction this codebase cares about most: a zero
that reads as free. `librarian.pricing` exists precisely so a model's spend cannot be reported as
`$0.00`, and it refuses an unpriced model at startup for that reason — and then the framework hands
it zero tokens and the whole seam computes `$0.0000` for a real run, which is the exact failure it
was built to prevent, arrived at from below. A paid trial found it on the first real measurement.

## What this does, and what it refuses to do

`ensure_usage_extraction_repaired()` wraps the classmethod ONCE, idempotently, at CALL time (no
import-time side effects, and nothing here runs in a keyless process that never builds an agent).
The wrapper **defers to the original**: it calls it first and returns its answer untouched whenever
it extracted any token count at all. Only when every count came back zero does it redo the same
provider-fallback loop, constructing with the kwargs FILTERED to the fields `RequestUsage` actually
declares and stashing the ones it does not (`output_reasoning_tokens`) into `details`, so nothing
the provider reported is dropped in silence. If the repair finds nothing either, the original's own
answer is returned unchanged — this can improve a figure, never worsen one.

Deferring to the original is what makes the shim self-retiring: on a version where the framework
constructs its usage correctly, our branch is never reached, and the only trace of it is this file.

## Removing it

The removal condition is a pydantic-ai pin bump whose changelog fixes the kwarg mismatch (upstream
either filters the unknown fields or grows the field). At that point `ensure_usage_extraction_repaired`
becomes a no-op wrapper worth deleting, along with its two call sites. It is deliberately NOT
version-gated: a `< 2.14` check would make the shim's own correctness depend on a second thing
being true, and the defer-to-original design already produces the right answer on any version.
"""
import dataclasses

# The attribute stamped on the installed wrapper, so a second call — or a second module reaching
# for the same repair — is a no-op rather than a wrapper around a wrapper.
_REPAIR_MARKER = "_stigmergy_usage_repair_installed"


def ensure_usage_extraction_repaired() -> bool:
    """Install the extraction repair if it is not already installed. Returns whether it installed.

    Idempotent and safe to call on every agent construction: the check is one `getattr` on a bound
    method. Imports live inside the function because this module is reached from the librarian's
    lazily-imported backend, where loading an agent framework at module scope is banned outright
    (`tests/test_architecture.py`), and because a kernel module must cost nothing to import.
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
        # Every count is zero. That is either a genuinely empty response — in which case the
        # repair finds the same zeros and nothing changes — or the swallowed `TypeError` above.
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
    """Did the original extraction recover anything at all?

    Any non-zero count means the framework's own path worked, and the answer is its own. Asking
    about EVERY count rather than only input/output is the conservative direction: a response that
    reported only cache reads still proves the extraction succeeded, and re-running the loop for it
    would be work with no possible gain.
    """
    return any(getattr(result, name, 0) for name in _token_field_names(type(result)))


def _repair(usage_class, data, *, provider: str, provider_url: str, provider_fallback: str,
            api_flavor: str, details: dict | None):
    """The framework's own provider-fallback loop, with the construction that does not raise.

    The candidate list and the lookup mirror `RequestUsage.extract` exactly — a repair that
    consulted a different provider would answer a different question than the one that failed.

    Two deliberate differences. The construction FILTERS to the declared fields and puts the rest in
    `details`, which is the whole fix. And a candidate that yields only zeros does not end the loop:
    the caller only reaches this function because the original already returned zeros, so trying the
    next candidate can only add information (in practice every candidate reads the same payload and
    agrees, so this cannot quietly change which provider's numbers win).

    `None` means the repair recovered nothing, and the caller keeps the original's answer. Every
    exception is swallowed for the same reason the framework swallows its own — a broken snapshot
    lookup must not turn a successful, already-paid model call into a failed item over telemetry.
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
                # view, and ours is whatever the provider object happened to carry.
                unknown = {k: v for k, v in values.items() if k not in declared}
                return usage_class(**counts, details={**unknown, **(details or {})})
            except Exception:  # noqa: BLE001, S112 — mirrors the loop this repairs
                continue
    except Exception:  # noqa: BLE001 — the repair must never be worse than not repairing
        return None
    return None
