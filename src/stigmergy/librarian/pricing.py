"""Tokens to dollars, for the backends that do not price themselves.

A harness that reports `total_cost_usd` per run needs no price table at all, which is why the
librarian went without one for as long as its only backend was that kind. Providers report COUNTS,
and a report that answered "what did
filing this cost?" with `0.0` because nobody did the multiplication would be worse than one that
said nothing — a silent zero reads as free (ADR 031 D2 records why the figure has to reach the row
at all; ADR 032 records this half; ADR 036 records the fourth column, below).

**The table is CONFIGURATION, not a constant, and the same rule model ids already live under**
(`config.py`: "model IDs are configuration, never constants — models get deprecated and a hardcoded
id is a landmine"). Prices move, promotional rates expire on a date, and a number compiled into a
release is a number nobody can correct without one. So: a seeded table with an `AS_OF` stamp, and
`$STIGMERGY_LIBRARIAN_PRICING` — a JSON map of the same shape — merged over it PER ID at call time,
never at import.

**An unknown id is refused, loudly, at startup.** `require_priced` is called by
`worker.startup_checks` for the backend that needs it, so the operator learns before a single item
is claimed rather than by reading `$0.00` off ten filed rows. A pricing table that answers "I don't
know" with zero is an instrument that lies in the one direction nobody checks.

**Who reads this module, and who never does.** Only a backend that reports TOKENS prices itself
here. A backend priced by its own provider passes that figure straight through to
`AgentRun.cost_usd` and never calls `_override`, so it is untouched by a malformed
`$STIGMERGY_LIBRARIAN_PRICING` — the retired Claude-Code backend was the one that worked that way,
and the offline double, which spends nothing, is the other. "Malformed is refused rather than
ignored", below, is therefore a promise to the token-priced backends and not a claim that every
librarian run validates that variable.
"""
import json
import math
import os

from stigmergy.librarian.errors import LibrarianConfigError

PRICING_ENV = "STIGMERGY_LIBRARIAN_PRICING"

# When the figures below were last set by a human. Printed in the refusal, so somebody reading a
# surprising number knows how old the arithmetic behind it is — and so a stale table is a visible
# fact rather than an assumption.
AS_OF = "2026-08-12"

# `{model id: (input, cached input, cache write, output)}`, US dollars per MILLION tokens.
#
# **Anthropic's row is the one this milestone verified; the other two stay deliberately
# over-stated.** `openai:gpt-5.6-terra` and `google-gla:gemini-3.6-flash` set cached input AND cache
# write equal to input — none of either provider's real fractions is verified here, and
# over-stating a cost is the safe direction for an instrument whose whole job is to stop a bill
# surprising somebody. No caching path is built for either provider today, so both figures are
# unreachable regardless of what they say. The paid trial corrects a row through
# `$STIGMERGY_LIBRARIAN_PRICING` for one deployment, or through an edit here plus a new `AS_OF` for
# all of them.
#
# The bare `claude-sonnet-5` is deliberately ABSENT, and stays absent now that the backend which
# used that spelling has retired. Adding it would make an unusable model id look configured: a bare
# name reaches pydantic-ai as an OPENAI model, and `worker._check_pydantic_backend` refuses it
# before this table is ever consulted. `priced_models()` is printed in that refusal as the list to
# choose from, so every id here has to be one a run can actually use.
PRICES = {
    # The milestone's own trial model. $2 in / $12 out is what the trial was budgeted against;
    # nothing here has been confirmed against a published price sheet, which is why this module
    # refuses to be the last word on any of it. Cached input and cache write are UNVERIFIED, set
    # equal to input — see the block comment above.
    "openai:gpt-5.6-terra": (2.00, 2.00, 2.00, 12.00),
    # CONFIRMED PERMANENT: Anthropic's 2026-08-12 pricing notice states $2 in / $10 out for this
    # model holds with no expiry — the step to $3/$15 this milestone was originally budgeted
    # against, and once carried here as an introductory rate with a 2026-08-31 cutover, does not
    # apply. Still corrected against the real bill rather than trusted outright: a pricing notice
    # is not a line item, and `AS_OF` is what says how recently a human last checked. Cached input
    # and cache write ARE Anthropic's own standing multipliers applied to the $2 base — 0.1x for a
    # cache read, 1.25x for a five-minute cache write — re-derive both if the base rate ever moves.
    "anthropic:claude-sonnet-5": (2.00, 0.20, 2.50, 10.00),
    # The Flash family's standing shape at the time of writing, NOT a confirmed 3.6 figure. Treat a
    # number computed from this row as an order of magnitude and correct it before quoting it.
    # Cached input and cache write are UNVERIFIED, set equal to input — see the block comment above.
    "google-gla:gemini-3.6-flash": (0.30, 0.30, 0.30, 2.50),
}

_TOKENS_PER_UNIT = 1_000_000

# The two shapes `_override` accepts, keyed by row length — the LEGACY one predates the cache-write
# column (ADR 036) and is read with the write rate normalized to the input rate, exactly the
# semantics `compute_cost_usd` billed a write at before this column existed. `require_priced`
# always hands back the second shape, whichever one configured it.
_LEGACY_POSITIONS = ("input", "cached input", "output")
_CURRENT_POSITIONS = ("input", "cached input", "cache write", "output")


def _rate(value, *, model: str, position: str) -> float:
    """One figure of one row, as a NON-NEGATIVE, FINITE float — or a refusal naming both ends of
    where it went wrong.

    **`json.loads` accepts `NaN`, `Infinity` and `-Infinity` as literals**, so a row can arrive
    already holding a float that is not a number, and `float()` will coerce it happily. That is why
    the check is on the parsed float rather than on the text: a `NaN` rate multiplies into a `NaN`
    cost, which lands in `report.cost_usd`, which is serialized into the queue row's `jsonb` column
    — where `NaN` is not valid JSON at all. One malformed environment variable would turn a
    successful filing into a write that cannot be stored.

    A negative rate is refused for a plainer reason: it would subtract dollars from a run, and an
    instrument that can be made to under-report by editing one variable is not an instrument.
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

    Read at CALL time, never at import (this package's standing rule), and MERGED over `PRICES` per
    id rather than replacing it: correcting one model's price must not mean retyping the other two.
    Malformed is refused rather than ignored, for the reason `config.resolved_timeout_s` refuses a
    malformed timeout — a variable somebody set and the process quietly dropped is the one outcome
    that teaches them the tool lies.

    **Both a 4-figure row and a legacy 3-figure one are accepted.**
    `[input, cached input, cache write, output]` is the shape `require_priced` always returns.
    `[input, cached input, output]` — every row this variable could hold before the cache-write
    column existed (ADR 036) — is still read, with the cache write rate taken equal to the input
    rate: today's documented semantics, kept so an operator's existing variable is not broken by
    this change. An operator widens to four figures on their own schedule, not this module's.
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
            # LEGACY shape: no cache-write figure at all, normalized to the input rate — see the
            # docstring above.
            rates = (rates[0], rates[1], rates[0], rates[2])
        # A zero OUTPUT rate is refused where zero input, cached-input and cache-write are not: a
        # cached read (or an unusually cheap write) really can be free somewhere, and an input rate
        # of zero is at least conceivable under a promotion, but output tokens are what a model is
        # FOR and nobody gives them away. A `0.0` here is a typo or a placeholder, and it would
        # price every filing on that model at a fraction of its real cost — silently, and in the
        # direction nobody audits. Keyed on position 3 unconditionally: by this point `rates` is
        # always the normalized 4-tuple, whichever shape it arrived in.
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

    Called by `worker.startup_checks` for a backend that must compute its own cost, so an unpriced
    model is one line before the first claim instead of a column of `$0.00` nobody questions.

    **Always the normalized 4-tuple, whichever shape priced it.** A `PRICES` row is written as one
    already; an override row may be four figures or a legacy three (see `_override`), and either
    way what comes back here is the same shape — so every caller downstream of this function reads
    one convention regardless of which shape configured it.
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

    **`input_tokens` is INCLUSIVE, and that is a fact about the framework rather than a convention
    this module invented.** `pydantic_ai.usage.UsageBase` documents its buckets as an inclusive
    parent with children: `input_tokens` is the WHOLE prompt, and `cache_read_tokens` /
    `cache_write_tokens` are the parts of it served from, or written to, the cache. OpenAI reports
    natively that way; Anthropic does not, and pydantic-ai NORMALIZES it — `models/anthropic.py`'s
    `_map_usage` folds `cache_read_input_tokens` and `cache_creation_input_tokens` INTO
    `input_tokens` before the usage object is built. A raw Anthropic
    `{input: 600, cache_read: 400, cache_write: 50}` therefore arrives here as
    `input_tokens=1050, cached_input_tokens=400, cache_write_tokens=50`.

    So the fresh count is one subtraction, with no provider branch and no inference from magnitudes.
    An earlier version guessed the convention by comparing the counts, which is a heuristic that
    silently doubles a bill the first time a provider's numbers land the other way round. Floored at
    zero, so no arithmetic here can bill a negative number of tokens whatever a provider reports.

    **Cache writes are billed at their OWN rate — the fourth element of a `PRICES` row (ADR 036).**
    A legacy three-figure override has no such figure and is normalized by `_override` to the input
    rate instead: the same approximation this module used everywhere, for every id, before this
    column existed.
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
