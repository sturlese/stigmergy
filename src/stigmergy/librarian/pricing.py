"""Tokens to dollars, for the backends that do not price themselves.

Providers report COUNTS, and a report that answered "what did filing this cost?" with `0.0`
because nobody did the multiplication would be worse than one that said nothing — a silent zero
reads as free. The table is CONFIGURATION, the same rule model ids live under: a seeded table
with an `AS_OF` stamp, and `$STIGMERGY_LIBRARIAN_PRICING` — a JSON map of the same shape —
merged over it PER ID at call time, never at import. **An unknown id is refused, loudly, at
startup** (`require_priced`, via `worker.startup_checks`), so the operator learns before a
single item is claimed rather than by reading `$0.00` off ten filed rows.

Only a backend that reports TOKENS prices itself here. A backend priced by its own provider
passes that figure straight through to `AgentRun.cost_usd` and never calls `_override` — so
"malformed is refused rather than ignored" is a promise to the token-priced backends, not a
claim that every librarian run validates the variable.
"""
import json
import math
import os

from stigmergy.librarian.errors import LibrarianConfigError

PRICING_ENV = "STIGMERGY_LIBRARIAN_PRICING"

# When a human last touched the figures below. Printed in the refusal, so somebody reading a
# surprising number knows how old the arithmetic behind it is; each row's own comment says what
# was actually verified and when.
AS_OF = "2026-08-19"

# `{model id: (input, cached input, cache write, output)}`, US dollars per MILLION tokens.
#
# Anthropic's row is verified; every non-Anthropic row deliberately sets cached input AND cache
# write equal to input — no other provider's real fractions is verified here, over-stating a
# cost is the safe direction, and only the Anthropic caching path is built, so those figures are
# unreachable elsewhere. Correct a row through `$STIGMERGY_LIBRARIAN_PRICING` for one
# deployment, or an edit here plus a new `AS_OF`.
#
# The bare `claude-sonnet-5` is deliberately ABSENT: a bare name reaches pydantic-ai as an
# OPENAI model and `worker._check_pydantic_backend` refuses it before this table is consulted —
# and `priced_models()` is printed in that refusal as the list to choose from, so every id here
# has to be one a run can actually use.
PRICES = {
    # $2 in / $12 out is what the trial was budgeted against; nothing confirmed against a
    # published price sheet. Cached input and cache write are UNVERIFIED, set equal to input.
    "openai:gpt-5.6-terra": (2.00, 2.00, 2.00, 12.00),
    # CONFIRMED PERMANENT: Anthropic's 2026-08-12 pricing notice states $2 in / $10 out for this
    # model with no expiry. Still corrected against the real bill rather than trusted outright —
    # a pricing notice is not a line item, and `AS_OF` says how recently a human checked. Cached
    # input and cache write ARE Anthropic's standing multipliers on the $2 base — 0.1x for a
    # cache read, 1.25x for a five-minute cache write — re-derive both if the base ever moves.
    "anthropic:claude-sonnet-5": (2.00, 0.20, 2.50, 10.00),
    # The Flash family's standing shape at the time of writing, NOT a confirmed 3.6 figure —
    # an order of magnitude, to correct before quoting. Cached figures UNVERIFIED, set to input.
    "google-gla:gemini-3.6-flash": (0.30, 0.30, 0.30, 2.50),
    # OpenRouter serves one id through several underlying hosts and lists the default route's
    # price; a pricier route can serve a given call, so correct these against the real bill —
    # AS_OF says when a human last read the listing. Cached input and cache write UNVERIFIED
    # (the discount varies per underlying host), set equal to input, the direction that can only
    # over-state. Both rows read from openrouter.ai/api/v1/models on the AS_OF date.
    "openrouter:z-ai/glm-5.2": (0.966, 0.966, 0.966, 3.036),
    "openrouter:deepseek/deepseek-v4-flash": (0.083, 0.083, 0.083, 0.165),
    # The drive road's vision OCR (`processing._vision_spend`), not a filing model — priced here
    # because this is the librarian's one tokens-to-dollars table. Same OpenRouter caveats as
    # the rows above; listed 2026-08-19.
    "openrouter:qwen/qwen3-vl-8b-instruct": (0.117, 0.117, 0.117, 0.455),
}

_TOKENS_PER_UNIT = 1_000_000

# The two shapes `_override` accepts, keyed by row length — the LEGACY one predates the
# cache-write column and is read with the write rate normalized to the input rate.
# `require_priced` always hands back the second shape, whichever one configured it.
_LEGACY_POSITIONS = ("input", "cached input", "output")
_CURRENT_POSITIONS = ("input", "cached input", "cache write", "output")


def _rate(value, *, model: str, position: str) -> float:
    """One figure of one row, as a NON-NEGATIVE, FINITE float — or a refusal naming both ends of
    where it went wrong.

    `json.loads` accepts `NaN` and `Infinity` as literals, so the check is on the parsed float:
    a `NaN` rate multiplies into a `NaN` cost, which cannot even be stored in the queue row's
    `jsonb` column. A negative rate would subtract dollars from a run, and an instrument that
    can be made to under-report by editing one variable is not an instrument.
    """
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise LibrarianConfigError(
            f"${PRICING_ENV} entry for {model!r} carries something that is not a number in the "
            f"{position} position; it must be [input, cached input, cache write, output] in "
            f"dollars per million tokens (a legacy three-figure [input, cached input, output] row "
            f"is also accepted)") from None
    if not math.isfinite(rate):
        raise LibrarianConfigError(
            f"${PRICING_ENV} entry for {model!r} has a {position} rate of {value!r}, which is not a "
            f"finite number. JSON admits NaN and Infinity as literals; a cost computed from one is "
            f"not a dollar figure and cannot even be stored on the row it would be reported on")
    if rate < 0:
        raise LibrarianConfigError(
            f"${PRICING_ENV} entry for {model!r} has a negative {position} rate ({rate}), which "
            f"would subtract dollars from a run's cost")
    return rate


def _override() -> dict:
    """`$STIGMERGY_LIBRARIAN_PRICING`, parsed — `{}` when unset.

    Read at CALL time, never at import, and MERGED over `PRICES` per id: correcting one model's
    price must not mean retyping the others. Malformed is refused rather than ignored — a
    variable somebody set and the process quietly dropped teaches them the tool lies. Both a
    4-figure row and a legacy 3-figure one are accepted; the legacy shape has no cache-write
    figure, so it is read with the write rate equal to the input rate.
    """
    raw = os.environ.get(PRICING_ENV)
    if not (raw or "").strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as ex:
        raise LibrarianConfigError(
            f"${PRICING_ENV} is not valid JSON ({ex.__class__.__name__}). It is a map of model id "
            f"to four dollars-per-million-token figures, for example: "
            f'{PRICING_ENV}=\'{{"openai:gpt-5.6-terra": [2.0, 2.0, 2.0, 12.0]}}\' '
            f"(input, cached input, cache write, output) — a legacy three-figure "
            f"[input, cached input, output] row is also accepted") from None
    if not isinstance(parsed, dict):
        raise LibrarianConfigError(
            f"${PRICING_ENV} must be a JSON OBJECT mapping a model id to [input, cached input, "
            f"cache write, output] dollars per million tokens, not a {type(parsed).__name__}")
    prices = {}
    for model, row in parsed.items():
        if not isinstance(row, (list, tuple)) or len(row) not in (3, 4):
            raise LibrarianConfigError(
                f"${PRICING_ENV} entry for {model!r} is not three or four numbers — it must be "
                f"[input, cached input, cache write, output] in dollars per million tokens (a "
                f"legacy three-figure [input, cached input, output] row is also accepted)")
        positions = _LEGACY_POSITIONS if len(row) == 3 else _CURRENT_POSITIONS
        rates = tuple(_rate(value, model=str(model), position=name)
                      for value, name in zip(row, positions, strict=True))
        if len(rates) == 3:
            # LEGACY shape: no cache-write figure at all, normalized to the input rate.
            rates = (rates[0], rates[1], rates[0], rates[2])
        # A zero OUTPUT rate is refused where zero input/cached/write are not: output tokens are
        # what a model is FOR and nobody gives them away — a `0.0` here is a typo or a
        # placeholder, and it would under-price every filing silently, in the direction nobody
        # audits. Keyed on position 3 unconditionally: by this point `rates` is always the
        # normalized 4-tuple, whichever shape it arrived in.
        if rates[3] == 0:
            raise LibrarianConfigError(
                f"${PRICING_ENV} entry for {model!r} prices output tokens at $0.00, which no "
                f"provider does — that is a placeholder or a typo, and it would under-report every "
                f"run on this model. If this model genuinely costs nothing, it does not belong in "
                f"a table whose job is to stop a bill surprising somebody")
        prices[str(model)] = rates
    return prices


def priced_models() -> list[str]:
    """Every model id this environment can price, table and override together — sorted, so the
    refusal below always lists them the same way."""
    return sorted(set(PRICES) | set(_override()))


def require_priced(model: str) -> tuple:
    """The `(input, cached input, cache write, output)` rates for `model`, or a loud refusal.

    Called by `worker.startup_checks` for a backend that must compute its own cost, so an
    unpriced model is one line before the first claim instead of a column of `$0.00` nobody
    questions. Always the normalized 4-tuple, whichever shape priced it, so every caller
    downstream reads one convention.
    """
    name = (model or "").strip()
    rates = {**PRICES, **_override()}.get(name)
    if rates is None:
        raise LibrarianConfigError(
            f"no price is configured for the model {name!r}, so this run could only report $0.00 "
            f"for work that costs money. Either add it to ${PRICING_ENV} — "
            f'{PRICING_ENV}=\'{{"{name}": [2.0, 2.0, 2.0, 12.0]}}\', the four figures being dollars '
            f"per million tokens for input, cached input, cache write and output — or add a row to "
            f"librarian/pricing.PRICES. Priced today (as of {AS_OF}): "
            f"{', '.join(priced_models())}")
    return rates


def compute_cost_usd(model: str, *, input_tokens: int = 0, cached_input_tokens: int = 0,
                     cache_write_tokens: int = 0, output_tokens: int = 0) -> float:
    """What one run cost, from its token counts. Raises `LibrarianConfigError` for an unpriced id.

    `input_tokens` is INCLUSIVE — a fact about the framework, not a convention this module
    invented: `pydantic_ai.usage.UsageBase` documents `input_tokens` as the WHOLE prompt with
    `cache_read_tokens`/`cache_write_tokens` as parts of it, and pydantic-ai normalizes
    Anthropic's raw counts into that shape (`models/anthropic.py`'s `_map_usage`). So the fresh
    count is one subtraction, with no provider branch and no inference from magnitudes — a
    magnitude heuristic silently doubles a bill the first time a provider's numbers land the
    other way round. Floored at zero, so no arithmetic here can bill a negative token count.

    Cache writes are billed at their OWN rate, the fourth element of a row; a legacy three-figure
    override is normalized by `_override` to the input rate instead.
    """
    rate_in, rate_cached, rate_write, rate_out = require_priced(model)
    cached = max(int(cached_input_tokens or 0), 0)
    written = max(int(cache_write_tokens or 0), 0)
    fresh = max(max(int(input_tokens or 0), 0) - cached - written, 0)
    dollars = (fresh * rate_in + cached * rate_cached + written * rate_write
               + max(int(output_tokens or 0), 0) * rate_out) / _TOKENS_PER_UNIT
    # Six decimals, the same rounding `processing._stamp_cost` applies to the item's own sum: a
    # float tail on a dollar figure reads as precision nobody has.
    return round(dollars, 6)
