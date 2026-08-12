"""Tokens to dollars, and the refusal that fires before a single item is claimed.

`pricing.py` exists because a report answering "what did filing this cost?" with `$0.00` because
nobody did the multiplication is worse than one that says nothing: a silent zero reads as free, and
free is the one direction nobody audits. So the table refuses an id it cannot price rather than
answering zero, and this file is what proves the refusal fires, that it fires only on ids nobody
priced, and that **the line it tells the operator to set actually works** — a refusal naming a fix
that does not fix it is a refusal that costs a day.

Everything here is arithmetic and environment. No model, no network, no queue.

**Why the override does the load-bearing arithmetic.** The seeded table prices a cache read at the
input rate for the two UNVERIFIED rows (deliberately — see `pricing.PRICES`), which makes the
inclusive-versus-exclusive question arithmetically invisible on those numbers: a test written
against them alone would pass identically whether the cached tokens were subtracted from the prompt
total or added beside it. So the cases that pin that behaviour configure a genuinely cheaper cached
rate through `$STIGMERGY_LIBRARIAN_PRICING` — the same door the module tells an operator to use, and
the only way to make the two conventions produce different dollars.

**The shape itself is under test here, twice over (ADR 036).** `PRICES` rows and a CURRENT override
are four figures — `(input, cached input, cache write, output)` — and a LEGACY three-figure override
is still read, with the missing cache-write figure normalized to the input rate: today's semantics
for an operator's existing variable, kept so widening the shape is optional rather than forced. Both
shapes are exercised below, and `require_priced` is what proves they converge on one convention.
"""
import json
import logging
import re

import pytest

from stigmergy.librarian import pricing
from stigmergy.librarian.errors import LibrarianConfigError

# An id nothing prices, in a shape a provider-prefixed model really takes.
UNPRICED = "openai:gpt-9"


def _example_json(message: str) -> str:
    """The `STIGMERGY_LIBRARIAN_PRICING='...'` value a refusal printed, pulled back out of it.

    Extracted rather than retyped, because retyping it would make these tests pass while the
    sentence an operator reads had drifted — which is the whole failure mode "a message containing
    a command is an executable promise" exists to catch. Anchored on the module's own variable
    name so a rename breaks this instead of silently matching nothing.
    """
    match = re.search(rf"{re.escape(pricing.PRICING_ENV)}='(.+?)'", message)
    assert match, f"the refusal printed no {pricing.PRICING_ENV} example to run:\n{message}"
    return match.group(1)


# ── the refusal, and the promise it makes ──────────────────────────────────────────────────────
def test_require_priced_refuses_an_unpriced_id_naming_it_the_variable_and_the_date():
    """Three facts, because a refusal is only actionable with all three: WHICH id (the operator
    typed one of several), WHERE to correct it, and HOW OLD the arithmetic behind the table is —
    somebody reading a surprising number needs to know the figures were last set by a human on a
    date, not computed today."""
    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.require_priced(UNPRICED)

    message = str(exc_info.value)
    assert UNPRICED in message
    assert pricing.PRICING_ENV in message
    assert pricing.AS_OF in message
    # ...and what this environment CAN price, so the operator can see a working spelling beside
    # their broken one rather than guessing at the format.
    for known in pricing.PRICES:
        assert known in message


def test_the_json_the_refusal_prints_is_json_that_makes_the_same_call_pass(monkeypatch):
    """**The executable promise.** The refusal hands the operator a line to export; this sets
    exactly that line, byte for byte as printed, and calls the function that refused. If the
    example ever drifts from what `_override` can parse — a stray brace, the wrong bracket, the
    figure count changing — this goes red instead of an operator's afternoon."""
    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.require_priced(UNPRICED)
    printed = _example_json(str(exc_info.value))

    monkeypatch.setenv(pricing.PRICING_ENV, printed)

    assert pricing.require_priced(UNPRICED) == (2.0, 2.0, 2.0, 12.0)
    assert UNPRICED in pricing.priced_models()
    # and the figure it now computes is the one those rates say, not a zero that survived
    assert pricing.compute_cost_usd(UNPRICED, input_tokens=1_000_000) == 2.0


@pytest.mark.parametrize("model", sorted(pricing.PRICES))
def test_every_id_the_table_ships_is_priced(model):
    """The benign twin: a refusal this loud must never fire on the models the repo itself names.
    Parametrized off `PRICES` rather than a retyped list, so a new row is covered the moment it is
    added and a deleted one takes its case with it."""
    rates = pricing.require_priced(model)
    assert len(rates) == 4, "a PRICES row must be the (input, cached input, cache write, output) 4-tuple"
    assert all(isinstance(rate, float) and rate > 0 for rate in rates), (
        f"{model} carries a zero or non-numeric rate, which prices real work at nothing")


def test_the_anthropic_rows_cache_figures_derive_from_its_input_rate():
    """The one row this milestone actually verified (ADR 036): Anthropic's own standing
    multipliers — 0.1x for a cache read, 1.25x for a five-minute cache write — applied to the $2
    input rate ADR 032 already prices this model at, confirmed PERMANENT by Anthropic's own
    2026-08-12 pricing notice (`pricing.PRICES`'s own comment carries the citation). Pinned so an
    edit to the base rate cannot silently leave the cache figures stale beside it.

    **Two checks, not one, because they catch different mistakes.** The literal tuple pins TODAY's
    figures against a regression that moves the whole row together — a base rate that silently
    reverts to the $3 step this ADR explicitly says does not apply, carried through proportionally
    to `cached`/`write`, would keep the ratios below intact and would NOT be caught by them alone
    (confirmed by mutation: a `(3.00, 0.30, 3.75, 10.00)` row passes the ratio assertions below
    unchanged). The ratio assertions pin the ARITHMETIC — 0.1x / 1.25x of whatever the input rate
    is — which is the sentence a maintainer re-deriving both figures after a legitimate future base
    move actually needs, and which the literal tuple alone would not restate.
    """
    assert pricing.PRICES["anthropic:claude-sonnet-5"] == (2.00, 0.20, 2.50, 10.00), (
        "today's ADR-036 figures — $2.00 input (confirmed permanent), $0.20 cached (0.1x), $2.50 "
        "cache write (1.25x), $10.00 output. An intentional edit updates this literal pin; a drift "
        "that leaves it stale is exactly what this assertion exists to catch")
    input_rate, cached_rate, write_rate, _output_rate = pricing.PRICES["anthropic:claude-sonnet-5"]
    assert cached_rate == round(input_rate * 0.1, 6)
    assert write_rate == round(input_rate * 1.25, 6)


def test_a_bare_model_spelling_is_deliberately_absent_from_the_table():
    """`claude-sonnet-5` (no prefix) is not a model id any backend can run: the one that took bare
    names retired, and pydantic-ai reads a bare name as an OPENAI model, so
    `worker._check_pydantic_backend` refuses one before this table is consulted. A row here would
    make an unusable id look configured — and `priced_models()` is printed in that very refusal as
    the list to choose from, so every id here has to be one a run can actually use."""
    assert "claude-sonnet-5" not in pricing.PRICES
    with pytest.raises(LibrarianConfigError):
        pricing.require_priced("claude-sonnet-5")


def test_an_empty_or_whitespace_model_is_refused_rather_than_priced_as_something():
    """The shape a half-written `$STIGMERGY_LIBRARIAN_MODEL` takes. `""` must not resolve through
    some default row — an unnamed model priced at somebody else's rates is worse than no figure."""
    for spelling in ("", "   ", None):
        with pytest.raises(LibrarianConfigError):
            pricing.require_priced(spelling)


def test_a_priced_id_is_found_however_it_was_spaced():
    """Its benign twin: `require_priced` strips, so a trailing newline out of a shell export does
    not read as an unpriced model. A refusal that fired on whitespace would be the tool blaming the
    operator for the tool's own parsing."""
    assert pricing.require_priced("  openai:gpt-5.6-terra\n") == pricing.PRICES["openai:gpt-5.6-terra"]


# ── the override: merged per id, at call time, and refused when malformed ──────────────────────
def test_the_override_corrects_one_model_without_retyping_the_others(monkeypatch):
    """Merged PER ID, which is the property with a cost attached: replacing the table wholesale
    would mean an operator correcting one expiring price silently un-prices the other two, and
    discovers it at the next startup rather than at the edit."""
    monkeypatch.setenv(pricing.PRICING_ENV,
                       json.dumps({"openai:gpt-5.6-terra": [1.0, 0.25, 1.5, 5.0]}))

    assert pricing.require_priced("openai:gpt-5.6-terra") == (1.0, 0.25, 1.5, 5.0)
    for untouched in pricing.PRICES:
        if untouched != "openai:gpt-5.6-terra":
            assert pricing.require_priced(untouched) == pricing.PRICES[untouched]


def test_the_override_is_read_at_call_time_and_not_at_import(monkeypatch):
    """The package's standing rule, and it is what lets a deployment correct a price without a
    release. Proven by changing the variable between two calls in one process — an import-time read
    would answer the first value twice."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [1.0, 1.0, 1.0, 1.0]}))
    assert pricing.require_priced(UNPRICED) == (1.0, 1.0, 1.0, 1.0)

    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [9.0, 9.0, 9.0, 9.0]}))
    assert pricing.require_priced(UNPRICED) == (9.0, 9.0, 9.0, 9.0)


def test_an_unset_or_blank_override_is_simply_no_override(monkeypatch):
    """An exported-but-empty variable is the shape a half-written env file produces, and it must
    mean "nothing configured" rather than "a malformed configuration" — the whole table would
    otherwise refuse on a blank line."""
    monkeypatch.delenv(pricing.PRICING_ENV, raising=False)
    assert pricing.priced_models() == sorted(pricing.PRICES)
    monkeypatch.setenv(pricing.PRICING_ENV, "   ")
    assert pricing.priced_models() == sorted(pricing.PRICES)


@pytest.mark.parametrize("raw, why", [
    ("{not json", "malformed JSON"),
    ('["openai:gpt-5.6-terra", 2.0]', "a JSON array rather than an object"),
    ('"openai:gpt-5.6-terra"', "a bare JSON string"),
    ('{"openai:gpt-9": [2.0, 12.0]}', "two figures — neither the legacy three nor the current four"),
    ('{"openai:gpt-9": [2.0, 2.0, 2.0, 12.0, 1.0]}', "five figures — one more than the current shape"),
    ('{"openai:gpt-9": 2.0}', "a scalar instead of a row"),
    ('{"openai:gpt-9": ["2.0", "cheap", "12.0"]}', "a legacy three-figure row carrying a word"),
    ('{"openai:gpt-9": [2.0, null, 12.0]}', "a legacy three-figure row carrying a null"),
    ('{"openai:gpt-9": [2.0, 2.0, "free", 12.0]}', "a current four-figure row carrying a word"),
])
def test_a_malformed_override_is_refused_rather_than_ignored(monkeypatch, raw, why):
    """Refused, never dropped. A variable somebody set and the process quietly ignored is the one
    outcome that teaches them the tool lies — and here the lie would be a column of prices computed
    from figures they thought they had corrected."""
    monkeypatch.setenv(pricing.PRICING_ENV, raw)
    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.require_priced("openai:gpt-5.6-terra")
    assert pricing.PRICING_ENV in str(exc_info.value), (
        f"the refusal for {why} does not name the variable it is about")


def test_the_malformed_refusal_also_prints_a_line_that_works(monkeypatch):
    """The same executable promise one refusal over: the parse failure prints an example too, and
    an example that does not parse would be a tool refusing a value while demonstrating the same
    mistake."""
    monkeypatch.setenv(pricing.PRICING_ENV, "{not json")
    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.priced_models()

    monkeypatch.setenv(pricing.PRICING_ENV, _example_json(str(exc_info.value)))
    assert "openai:gpt-5.6-terra" in pricing.priced_models()


@pytest.mark.parametrize("bad_row", [
    [0.3, 2.5],                              # two figures — too few for either shape
    [0.3, 0.3, 2.5, 10.0, 1.0],               # five figures — one too many for the current shape
])
def test_a_malformed_row_refusal_names_the_model_it_could_not_read(monkeypatch, bad_row):
    """Which entry, out of however many the operator pasted. "One of your rows is wrong" over a
    ten-model map is a refusal that costs more than it saves — proven on a row that is too SHORT
    and one that is too LONG, since the current shape accepts two different lengths (three or
    four) and both edges of that acceptance need their own miss."""
    monkeypatch.setenv(pricing.PRICING_ENV,
                       json.dumps({"openai:gpt-5.6-terra": [2.0, 2.0, 2.0, 12.0],
                                   "google-gla:gemini-3.6-flash": bad_row}))
    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.priced_models()
    assert "google-gla:gemini-3.6-flash" in str(exc_info.value)


# ── the individual rates: what `float()` would have accepted and a dollar figure must not ──────
@pytest.mark.parametrize("literal, position", [
    ("NaN", "input"),
    ("Infinity", "input"),
    ("-Infinity", "input"),
    ("NaN", "cached input"),
    ("NaN", "cache write"),
    ("Infinity", "output"),
])
def test_a_non_finite_rate_is_refused_naming_the_model_and_the_position(monkeypatch, literal,
                                                                       position):
    """**`json.loads` accepts `NaN`, `Infinity` and `-Infinity` as bare literals** — so a row can
    arrive already holding a float that is not a number, and `float()` coerces it happily. The
    consequence is not a wrong number, it is an unstorable one: a `NaN` rate multiplies into a `NaN`
    cost, which lands in `report.cost_usd`, which is serialized into the queue row's `jsonb` column
    — where `NaN` is not valid JSON at all. One malformed variable would turn a successful filing
    into a write that cannot be stored.

    Driven through the REAL `json.loads` on a real environment string, never a hand-built `float`
    object: the whole point is that the JSON parser lets these through, and a test that constructed
    `float("nan")` in Python would prove nothing about the door they actually come in by. Exercised
    on the CURRENT four-figure shape, `cache write` included, since that is the position ADR 036 added.
    """
    row = {"input": [literal, "2.0", "2.0", "12.0"],
           "cached input": ["2.0", literal, "2.0", "12.0"],
           "cache write": ["2.0", "2.0", literal, "12.0"],
           "output": ["2.0", "2.0", "2.0", literal]}[position]
    monkeypatch.setenv(pricing.PRICING_ENV, f'{{"{UNPRICED}": [{", ".join(row)}]}}')

    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.require_priced(UNPRICED)

    message = str(exc_info.value)
    assert UNPRICED in message                 # which row
    assert position in message                 # ...and which of its four figures
    assert "finite" in message                 # ...and why it is refused rather than coerced


@pytest.mark.parametrize("position, row", [
    ("input", [-2.0, 2.0, 2.0, 12.0]),
    ("cached input", [2.0, -0.5, 2.0, 12.0]),
    ("cache write", [2.0, 2.0, -1.5, 12.0]),
    ("output", [2.0, 2.0, 2.0, -12.0]),
])
def test_a_negative_rate_is_refused_naming_the_model_and_the_position(monkeypatch, position, row):
    """A negative rate would SUBTRACT dollars from a run, and an instrument that can be made to
    under-report by editing one environment variable is not an instrument. Exercised on the
    CURRENT four-figure shape, `cache write` included."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: row}))

    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.require_priced(UNPRICED)

    message = str(exc_info.value)
    assert UNPRICED in message and position in message
    assert "negative" in message


def test_a_zero_output_rate_is_refused_because_no_provider_gives_output_away(monkeypatch):
    """Zero OUTPUT is a typo or a placeholder, and it would price every filing on that model at a
    fraction of its real cost — silently, in the direction nobody audits. Refused where a zero input,
    cached-input or cache-write rate is not, and the asymmetry is the argument: a cache operation
    really can be free somewhere and a promotional input rate is at least conceivable, but output
    tokens are what a model is FOR."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 2.0, 2.0, 0.0]}))

    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.require_priced(UNPRICED)

    assert UNPRICED in str(exc_info.value)
    assert "$0.00" in str(exc_info.value)


def test_a_zero_output_rate_is_refused_on_a_legacy_row_too(monkeypatch):
    """**The output position moves when a row is normalized, and the check has to move with it.**
    A legacy three-figure row's output sits at index 2 on the wire and index 3 after `_override`
    pads it with a normalized cache-write figure — this pins that the zero-output refusal reads the
    NORMALIZED position, not the wire one, so a legacy row's real output figure is never mistaken
    for the write rate it just gained."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 2.0, 0.0]}))

    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.require_priced(UNPRICED)

    assert UNPRICED in str(exc_info.value)
    assert "$0.00" in str(exc_info.value)


@pytest.mark.parametrize("row", [
    [0.0, 2.0, 2.0, 12.0],
    [2.0, 0.0, 2.0, 12.0],
    [2.0, 2.0, 0.0, 12.0],
])
def test_a_zero_input_cached_or_write_rate_is_accepted_which_is_the_asymmetrys_other_half(
        monkeypatch, row):
    """The specificity half of the rule above: a free cache read, a free cache write, and a
    promotional input rate are real configurations, and a check that refused every zero would make
    this table unable to describe them."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: row}))
    assert pricing.require_priced(UNPRICED) == tuple(row)


def test_an_ordinary_corrected_row_passes_every_one_of_those_checks(monkeypatch):
    """**The benign twin for the whole `_rate` family.** A lot of ways to bounce an operator who is
    doing exactly what the module told them to do, so the shape the docs hand them — a genuinely
    cheaper cached rate and its own write rate, which is the whole reason the override exists — is
    asserted to sail through and to be the figures that come back out."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.2, 2.5, 12.0]}))

    assert pricing.require_priced(UNPRICED) == (2.0, 0.2, 2.5, 12.0)
    assert pricing.compute_cost_usd(UNPRICED, input_tokens=1_000_000, cached_input_tokens=0,
                                    output_tokens=1_000_000) == 14.0


def test_an_integer_or_string_row_is_coerced_rather_than_refused(monkeypatch):
    """JSON numbers arrive as `int` when they have no decimal point, and an operator pasting a
    quoted figure is making a formatting mistake rather than a pricing one. Both coerce — the
    refusals above are for values that are not numbers at all, not for spellings."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2, "0.2", "1.5", 12]}))
    assert pricing.require_priced(UNPRICED) == (2.0, 0.2, 1.5, 12.0)


def test_priced_models_lists_the_table_and_the_override_together_sorted(monkeypatch):
    """Sorted and de-duplicated, so the refusal above always lists them the same way — two runs of
    the same misconfiguration must not print two different sentences."""
    monkeypatch.setenv(pricing.PRICING_ENV,
                       json.dumps({UNPRICED: [1.0, 1.0, 1.0, 1.0],
                                   "openai:gpt-5.6-terra": [1.0, 1.0, 1.0, 1.0]}))
    listed = pricing.priced_models()
    assert listed == sorted(listed)
    assert listed == sorted(set(pricing.PRICES) | {UNPRICED})


# ── require_priced: one normalized shape however a row was written ─────────────────────────────
def test_require_priced_normalizes_a_legacy_row_to_the_same_shape_as_a_current_one(monkeypatch):
    """Whichever shape configured a price, `require_priced` hands back one convention: a 4-tuple,
    always — so `compute_cost_usd`, or anything else downstream of it, never has to ask which shape
    an operator's override happened to use."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.5, 12.0]}))
    legacy = pricing.require_priced(UNPRICED)
    assert legacy == (2.0, 0.5, 2.0, 12.0)
    assert len(legacy) == 4

    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.5, 9.0, 12.0]}))
    current = pricing.require_priced(UNPRICED)
    assert current == (2.0, 0.5, 9.0, 12.0)
    assert len(current) == 4


def test_a_seeded_prices_row_is_already_the_normalized_shape(monkeypatch):
    """The other half: a `PRICES` row needs no normalization at all, because it is written as a
    4-tuple already — `require_priced` must not, say, silently drop or reorder it on the way out."""
    monkeypatch.delenv(pricing.PRICING_ENV, raising=False)
    assert pricing.require_priced("anthropic:claude-sonnet-5") == \
        pricing.PRICES["anthropic:claude-sonnet-5"]


def test_one_override_object_may_mix_a_legacy_row_and_a_current_row(monkeypatch):
    """**The edge `_override` actually has to resolve per ROW, not per call.** An operator widening
    one model's line to four figures has no reason to touch a sibling model's line on the same
    schedule, so `$STIGMERGY_LIBRARIAN_PRICING` has to let a legacy three-figure row and a current
    four-figure row coexist in the SAME JSON object — `_LEGACY_POSITIONS`/`_CURRENT_POSITIONS` are
    chosen inside the per-row loop in `_override`, from that row's own `len()`, so one row's shape
    can never leak into how its neighbour is read.

    Both parse, and each keeps its OWN write-rate semantics: the legacy row's write is normalized to
    ITS input rate (2.0), the current row's write is its own configured figure (3.0) — distinct from
    its input rate (1.0) on purpose, so a bug that normalized every row regardless of shape would
    make the current row's write-cost assertion below fail.
    """
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({
        "openai:gpt-9": [2.0, 0.5, 12.0],              # legacy: no write figure of its own
        "google-gla:gemini-9": [1.0, 0.1, 3.0, 6.0],    # current: its own write figure
    }))

    legacy = pricing.require_priced("openai:gpt-9")
    current = pricing.require_priced("google-gla:gemini-9")

    assert legacy == (2.0, 0.5, 2.0, 12.0)              # write normalized to the input rate
    assert current == (1.0, 0.1, 3.0, 6.0)              # write read verbatim, not normalized

    legacy_write_cost = pricing.compute_cost_usd("openai:gpt-9", input_tokens=1000,
                                                  cache_write_tokens=1000)
    current_write_cost = pricing.compute_cost_usd("google-gla:gemini-9", input_tokens=1000,
                                                   cache_write_tokens=1000)
    assert legacy_write_cost == round(1000 * 2.0 / 1_000_000, 6)   # billed at ITS input rate
    assert current_write_cost == round(1000 * 3.0 / 1_000_000, 6)  # billed at ITS OWN write rate
    assert legacy_write_cost != current_write_cost


# ── the arithmetic ─────────────────────────────────────────────────────────────────────────────
def test_a_million_in_and_a_million_out_costs_exactly_the_two_rates():
    """The rates are dollars per MILLION tokens, so a million of each is the table's own two
    figures added — the one case where the arithmetic can be checked by reading the row. `[0]` is
    input and `[3]` is output in the current `(input, cached input, cache write, output)` shape."""
    assert pricing.compute_cost_usd("openai:gpt-5.6-terra", input_tokens=1_000_000,
                                    cached_input_tokens=0, output_tokens=1_000_000) == 14.0
    assert pricing.PRICES["openai:gpt-5.6-terra"][0] + pricing.PRICES["openai:gpt-5.6-terra"][3] == 14.0


def test_the_cached_part_of_a_prompt_total_is_billed_once_at_the_cached_rate(monkeypatch):
    """**The inclusive convention, made visible.** `input_tokens` is the prompt TOTAL and
    `cached_input_tokens` the part of it served from cache, so 1000 with 400 cached bills 600 fresh
    plus 400 cached — never 1000 fresh plus 400 cached, which would charge the cached tokens twice.

    A genuinely cheaper cached rate is configured on purpose (a legacy three-figure row, since no
    cache write is exercised here): with the shipped table's equal rates both readings produce the
    same dollars, so this test would pass against a double-billing implementation and prove
    nothing."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.5, 12.0]}))

    cost = pricing.compute_cost_usd(UNPRICED, input_tokens=1000, cached_input_tokens=400)

    assert cost == round((600 * 2.0 + 400 * 0.5) / 1_000_000, 6)
    assert cost != round((1000 * 2.0 + 400 * 0.5) / 1_000_000, 6), (
        "the cached tokens were billed on top of the prompt total instead of inside it")


def test_no_count_can_ever_bill_a_negative_number_of_fresh_tokens(monkeypatch):
    """The floor: a provider reporting more cached-plus-written tokens than prompt tokens must not
    produce a NEGATIVE fresh count that silently subtracts dollars from the run. The whole figure
    stays a sum of non-negative parts, whatever a provider hands back.

    **RE-PINNED, and the change of figure is the contract change.** This case used to bill 100
    fresh tokens: the old implementation inferred the convention by comparing magnitudes and, when
    the cached count exceeded the prompt total, took that as "the provider reports them beside each
    other" and skipped the subtraction. That inference is gone — `input_tokens` is DECLARED
    inclusive (pydantic-ai normalizes every provider into it), so the subtraction is unconditional
    and this shape means a provider that under-reported its own prompt total. The floor is what is
    left, and it is the property worth having: `max(…, 0)`, never a credit.
    """
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.5, 12.0]}))

    cost = pricing.compute_cost_usd(UNPRICED, input_tokens=100, cached_input_tokens=400)

    assert cost > 0                                  # the cached tokens are still billed
    assert cost == round((0 * 2.0 + 400 * 0.5) / 1_000_000, 6)
    # ...and no arrangement of counts can drive the figure below zero
    assert pricing.compute_cost_usd(UNPRICED, input_tokens=0, cached_input_tokens=999,
                                    cache_write_tokens=999) > 0


def test_the_inclusive_bucket_convention_is_the_frameworks_and_this_is_where_it_is_pinned(
        monkeypatch):
    """**The convention, pinned against the framework that defines it.**

    `pydantic_ai.usage.UsageBase` documents its counts as *inclusive parent/child buckets, not
    disjoint ones*: `input_tokens` is the WHOLE prompt and `cache_read_tokens`/`cache_write_tokens`
    are parts of it — and extraction normalizes the providers whose raw numbers report them
    separately (Anthropic, Bedrock) so the convention holds everywhere. `compute_cost_usd` therefore
    subtracts unconditionally, with no provider branch and no inference from magnitudes.

    A raw Anthropic `{input: 600, cache_read: 400, cache_write: 50}` reaches this function as
    `input_tokens=1050`, and that is the arrangement below: 600 fresh, 400 read from cache, 50
    written to it.

    **This test is the framework contract's tripwire.** If a pydantic-ai bump ever flips a bucket
    back to disjoint, nothing in production would fail — every figure would simply drop — so the
    citation lives here, in an assertion, rather than only in a docstring.

    **All four rates are distinct** (2.0 / 0.5 / 1.5 / 12.0), unlike the legacy-shape tests above:
    a transposition bug between any two positions — cached read read as the write rate, or the
    reverse — would change the total here and did not have a chance to on a row where two of the
    four figures happened to coincide.
    """
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.5, 1.5, 12.0]}))

    cost = pricing.compute_cost_usd(UNPRICED, input_tokens=1050, cached_input_tokens=400,
                                    cache_write_tokens=50, output_tokens=100)

    # 600 fresh at input · 400 read at the cached rate · 50 written at the cache-write rate · 100 output
    assert cost == round((600 * 2.0 + 400 * 0.5 + 50 * 1.5 + 100 * 12.0) / 1_000_000, 6)
    # ...and the disjoint reading, which would bill the cached and written tokens twice over
    assert cost != round((1050 * 2.0 + 400 * 0.5 + 50 * 1.5 + 100 * 12.0) / 1_000_000, 6)


def test_the_frameworks_own_documentation_still_says_the_buckets_are_inclusive():
    """The other half of the tripwire, and the one a version bump trips first: the arithmetic above
    is only correct because `UsageBase` says so. Read from the installed package, so an upgrade that
    changes the convention fails HERE — with the reason in the failure — rather than silently
    halving every recorded cost."""
    import pathlib

    import pydantic_ai

    usage_src = (pathlib.Path(pydantic_ai.__file__).parent / "usage.py").read_text(encoding="utf-8")
    assert "inclusive parent/child buckets" in usage_src, (
        "pydantic_ai.usage no longer documents inclusive buckets — `pricing.compute_cost_usd` "
        "subtracts the cached counts out of the prompt total on the strength of that sentence, so "
        "the convention has to be re-read before this bump lands")


def test_a_cache_write_is_billed_at_its_own_rate(monkeypatch):
    """**The fourth-element follow-up (ADR 036), landed.** A cache write is billed at its OWN
    rate — distinct from the input rate here on purpose, so this cannot pass by the write rate
    coincidentally equaling the input one."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.5, 9.0, 12.0]}))

    written = pricing.compute_cost_usd(UNPRICED, input_tokens=1000, cache_write_tokens=1000)

    assert written > 0
    assert written == round(1000 * 9.0 / 1_000_000, 6)
    assert written != round(1000 * 2.0 / 1_000_000, 6), (
        "a cache write was billed at the input rate rather than its own configured rate")


def test_a_legacy_three_figure_override_still_bills_a_cache_write_at_the_input_rate(monkeypatch):
    """**The coexistence case.** An operator's existing three-figure `$STIGMERGY_LIBRARIAN_PRICING`
    has no write figure to read, so `_override` normalizes the write rate to the input rate — the
    exact number a write was billed at before this column existed. This is what keeps that
    variable, unedited, correct after this change rather than silently wrong in a new direction.
    """
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.5, 12.0]}))

    written = pricing.compute_cost_usd(UNPRICED, input_tokens=1000, cache_write_tokens=1000)
    nothing_written = pricing.compute_cost_usd(UNPRICED, input_tokens=1000)

    assert written > 0
    assert written == nothing_written, (
        "a legacy three-figure override must still bill a cache write at the input rate, so moving "
        "1000 prompt tokens into the write bucket must not change the total")


def test_zero_tokens_costs_zero_and_is_still_a_priced_answer():
    """A run that spent nothing says `0.0` — but it must still go through `require_priced`, so an
    unpriced model cannot hide behind a cheap run."""
    assert pricing.compute_cost_usd("openai:gpt-5.6-terra") == 0.0
    with pytest.raises(LibrarianConfigError):
        pricing.compute_cost_usd(UNPRICED, input_tokens=0)


def test_missing_and_none_counts_are_read_as_zero_not_as_a_crash():
    """A framework's usage object that omits a field hands `None` through `getattr`'s default. The
    figure has to survive that: a `TypeError` here would turn a priced, successful filing into a
    `failed` row over telemetry."""
    assert pricing.compute_cost_usd("openai:gpt-5.6-terra", input_tokens=None,
                                    cached_input_tokens=None, cache_write_tokens=None,
                                    output_tokens=None) == 0.0


def test_the_figure_is_rounded_to_six_places_never_a_float_tail():
    """The same rounding `processing._stamp_cost` applies to the item's own sum. A float tail on a
    dollar figure reads as precision nobody has."""
    cost = pricing.compute_cost_usd("google-gla:gemini-3.6-flash", input_tokens=7,
                                    output_tokens=13)
    assert cost == round(cost, 6)
    assert len(str(cost).partition(".")[2]) <= 6


def test_an_unpriced_model_raises_from_compute_rather_than_returning_zero():
    """The module's own thesis, at the call site that would otherwise produce the silent zero: a
    table that answers "I don't know" with `0.0` is an instrument that lies in the one direction
    nobody checks."""
    with pytest.raises(LibrarianConfigError, match=re.escape(UNPRICED)):
        pricing.compute_cost_usd(UNPRICED, input_tokens=1000, output_tokens=1000)


# ── the table is documentation as much as it is data ───────────────────────────────────────────
def test_the_as_of_stamp_is_a_real_date_somebody_set():
    """Printed in the refusal, so it has to be readable as a date rather than a placeholder. A
    stale table is meant to be a VISIBLE fact; `AS_OF` is the whole of that visibility."""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", pricing.AS_OF), (
        f"AS_OF is {pricing.AS_OF!r}, which nobody can read as a date")


def test_the_pricing_module_loads_no_agent_framework():
    """It is imported by `worker.startup_checks` on every run, including the offline ones. A table
    of numbers that dragged in a provider SDK would put a framework in the keyless path through the
    back door — the same rule `tests/test_architecture.py` holds for both frameworks by import
    site, asserted here by what the module actually needs."""
    import ast
    import pathlib

    source = pathlib.Path(pricing.__file__).read_text(encoding="utf-8")
    imported = {node.module or "" for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Import) for alias in node.names}
    assert not [mod for mod in imported
                if mod.startswith(("pydantic_ai", "openai", "anthropic"))]


def test_the_logging_module_is_not_where_a_price_gets_decided(caplog):
    """A guard against the shape this instrument could rot into: `compute_cost_usd` must be a pure
    function of its arguments and the environment, with no side channel. Nothing it does may depend
    on a logger being configured, and it emits nothing at WARNING that an operator would have to
    read to trust the figure."""
    with caplog.at_level(logging.WARNING):
        first = pricing.compute_cost_usd("openai:gpt-5.6-terra", input_tokens=1234,
                                         output_tokens=99)
        second = pricing.compute_cost_usd("openai:gpt-5.6-terra", input_tokens=1234,
                                          output_tokens=99)
    assert first == second
    assert [r for r in caplog.records if r.name.startswith("stigmergy.librarian.pricing")] == []
