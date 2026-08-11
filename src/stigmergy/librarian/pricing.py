"""Tokens to dollars, for the backends that do not price themselves.

The Claude Agent SDK reports `total_cost_usd` per run, so `agent.SdkAgent` has never had to know
what a token costs. Every other provider reports COUNTS, and a report that answered "what did
filing this cost?" with `0.0` because nobody did the multiplication would be worse than one that
said nothing — a silent zero reads as free (ADR 031 D2 records why the figure has to reach the row
at all; ADR 032 records this half).

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
here. The `sdk` backend is priced by its own SDK, never calls `_override`, and is therefore
untouched by a malformed `$STIGMERGY_LIBRARIAN_PRICING` — so "malformed is refused rather than
ignored", below, is a promise to the token-priced backends and not a claim that every librarian run
validates that variable.
"""
import json
import math
import os

from stigmergy.librarian.errors import LibrarianConfigError

PRICING_ENV = "STIGMERGY_LIBRARIAN_PRICING"

# When the figures below were last set by a human. Printed in the refusal, so somebody reading a
# surprising number knows how old the arithmetic behind it is — and so a stale table is a visible
# fact rather than an assumption.
AS_OF = "2026-08-11"

# `{model id: (input, cached input, output)}`, US dollars per MILLION tokens.
#
# **Cached input is set equal to input for every id here, and that is deliberate rather than
# researched.** Most providers bill a cache read at a fraction of an ordinary input token; none of
# those fractions is verified for the ids below, and over-stating a cost is the safe direction for
# an instrument whose whole job is to stop a bill surprising somebody. It also makes the
# inclusive-versus-exclusive question `compute_cost_usd` documents arithmetically irrelevant today.
# The paid trial corrects both halves — through `$STIGMERGY_LIBRARIAN_PRICING` for one deployment,
# or through an edit here plus a new `AS_OF` for all of them.
#
# A three-figure row cannot price a cache WRITE separately, so `compute_cost_usd` bills one at the
# input rate — the single figure in this seam that errs DOWNWARD (Anthropic charges 1.25x base
# input to write a cache entry). Stated here as well as there, because this is the table somebody
# edits when a number looks wrong.
#
# The bare `claude-sonnet-5` is deliberately ABSENT: that spelling is the `sdk` backend's, and that
# backend is priced by its own SDK. An entry for it would be a second, drifting answer to a question
# already answered upstream.
PRICES = {
    # The milestone's own trial model. $2 in / $12 out is what the trial was budgeted against;
    # nothing here has been confirmed against a published price sheet, which is why this module
    # refuses to be the last word on any of it.
    "openai:gpt-5.6-terra": (2.00, 2.00, 12.00),
    # INTRODUCTORY pricing, and it expires: $2/$10 holds until 2026-08-31, after which the standing
    # rate is $3 in / $15 out. Whoever passes that date edits this line and `AS_OF` with it — the
    # expiry is the entire reason this milestone exists, so it must not be discovered by a bill.
    "anthropic:claude-sonnet-5": (2.00, 2.00, 10.00),
    # The Flash family's standing shape at the time of writing, NOT a confirmed 3.6 figure. Treat a
    # number computed from this row as an order of magnitude and correct it before quoting it.
    "google-gla:gemini-3.6-flash": (0.30, 0.30, 2.50),
}

_TOKENS_PER_UNIT = 1_000_000


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
            f"{position} position; it must be [input, cached input, output] in dollars per million "
            f"tokens") from None
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
    """
    raw = os.environ.get(PRICING_ENV)
    if not (raw or "").strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as ex:
        raise LibrarianConfigError(
            f"${PRICING_ENV} is not valid JSON ({ex.__class__.__name__}). It is a map of model id "
            f"to three dollars-per-million-token figures, for example: "
            f'{PRICING_ENV}=\'{{"openai:gpt-5.6-terra": [2.0, 2.0, 12.0]}}\' '
            f"(input, cached input, output)") from None
    if not isinstance(parsed, dict):
        raise LibrarianConfigError(
            f"${PRICING_ENV} must be a JSON OBJECT mapping a model id to [input, cached input, "
            f"output] dollars per million tokens, not a {type(parsed).__name__}")
    prices = {}
    for model, row in parsed.items():
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise LibrarianConfigError(
                f"${PRICING_ENV} entry for {model!r} is not three numbers — it must be "
                f"[input, cached input, output] in dollars per million tokens")
        rates = tuple(_rate(value, model=str(model), position=name)
                      for value, name in zip(row, ("input", "cached input", "output"), strict=True))
        # A zero OUTPUT rate is refused where zero input and zero cached-input are not: a cached
        # read really can be free somewhere, and an input rate of zero is at least conceivable
        # under a promotion, but output tokens are what a model is FOR and nobody gives them away.
        # A `0.0` here is a typo or a placeholder, and it would price every filing on that model at
        # a fraction of its real cost — silently, and in the direction nobody audits.
        if rates[2] == 0:
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
    """The `(input, cached input, output)` rates for `model`, or a loud refusal.

    Called by `worker.startup_checks` for a backend that must compute its own cost, so an unpriced
    model is one line before the first claim instead of a column of `$0.00` nobody questions.
    """
    name = (model or "").strip()
    rates = {**PRICES, **_override()}.get(name)
    if rates is None:
        raise LibrarianConfigError(
            f"no price is configured for the model {name!r}, so this run could only report $0.00 "
            f"for work that costs money. Either add it to ${PRICING_ENV} — "
            f'{PRICING_ENV}=\'{{"{name}": [2.0, 2.0, 12.0]}}\', the three figures being dollars '
            f"per million tokens for input, cached input and output — or add a row to "
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

    **Cache WRITES are billed at the input rate, which UNDER-bills Anthropic by 20 %.** Anthropic
    charges 1.25x base input to write a cache entry; a three-figure row cannot express that, and the
    gap is stated rather than hidden because it is the one place this table errs downward — every
    other approximation here over-states. A fourth element on a `PRICES` row (and on the override,
    which cannot express it either) is the follow-up that closes it.
    """
    rate_in, rate_cached, rate_out = require_priced(model)
    cached = max(int(cached_input_tokens or 0), 0)
    written = max(int(cache_write_tokens or 0), 0)
    fresh = max(max(int(input_tokens or 0), 0) - cached - written, 0)
    dollars = (fresh * rate_in + cached * rate_cached + written * rate_in
               + max(int(output_tokens or 0), 0) * rate_out) / _TOKENS_PER_UNIT
    # Six decimals, the same rounding `processing._stamp_cost` applies to the item's own sum: a
    # float tail on a dollar figure reads as precision nobody has.
    return round(dollars, 6)
