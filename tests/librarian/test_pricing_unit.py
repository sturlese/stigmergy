"""Tokens to dollars, and the refusal that fires before a single item is claimed.

`pricing.py` exists because a report answering "what did filing this cost?" with `$0.00` because
nobody did the multiplication is worse than one that says nothing: a silent zero reads as free, and
free is the one direction nobody audits. So the table refuses an id it cannot price rather than
answering zero, and this file is what proves the refusal fires, that it fires only on ids nobody
priced, and that **the line it tells the operator to set actually works** — a refusal naming a fix
that does not fix it is a refusal that costs a day.

Everything here is arithmetic and environment. No model, no network, no queue.

**Why the override does the load-bearing arithmetic.** The seeded table prices a cache read at the
input rate for every id (deliberately — see `pricing.PRICES`), which makes the whole
inclusive-versus-exclusive question arithmetically invisible on the shipped numbers: a test written
against them alone would pass identically whether the cached tokens were subtracted from the prompt
total or added beside it. So the cases that pin that behaviour configure a genuinely cheaper cached
rate through `$STIGMERGY_LIBRARIAN_PRICING` — the same door the module tells an operator to use,
and the only way to make the two conventions produce different dollars.
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
    example ever drifts from what `_override` can parse — a stray brace, the wrong bracket, three
    figures becoming two — this goes red instead of an operator's afternoon."""
    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.require_priced(UNPRICED)
    printed = _example_json(str(exc_info.value))

    monkeypatch.setenv(pricing.PRICING_ENV, printed)

    assert pricing.require_priced(UNPRICED) == (2.0, 2.0, 12.0)
    assert UNPRICED in pricing.priced_models()
    # and the figure it now computes is the one those rates say, not a zero that survived
    assert pricing.compute_cost_usd(UNPRICED, input_tokens=1_000_000) == 2.0


@pytest.mark.parametrize("model", sorted(pricing.PRICES))
def test_every_id_the_table_ships_is_priced(model):
    """The benign twin: a refusal this loud must never fire on the models the repo itself names.
    Parametrized off `PRICES` rather than a retyped list, so a new row is covered the moment it is
    added and a deleted one takes its case with it."""
    rates = pricing.require_priced(model)
    assert len(rates) == 3
    assert all(isinstance(rate, float) and rate > 0 for rate in rates), (
        f"{model} carries a zero or non-numeric rate, which prices real work at nothing")


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
                       json.dumps({"openai:gpt-5.6-terra": [1.0, 0.25, 5.0]}))

    assert pricing.require_priced("openai:gpt-5.6-terra") == (1.0, 0.25, 5.0)
    for untouched in pricing.PRICES:
        if untouched != "openai:gpt-5.6-terra":
            assert pricing.require_priced(untouched) == pricing.PRICES[untouched]


def test_the_override_is_read_at_call_time_and_not_at_import(monkeypatch):
    """The package's standing rule, and it is what lets a deployment correct a price without a
    release. Proven by changing the variable between two calls in one process — an import-time read
    would answer the first value twice."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [1.0, 1.0, 1.0]}))
    assert pricing.require_priced(UNPRICED) == (1.0, 1.0, 1.0)

    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [9.0, 9.0, 9.0]}))
    assert pricing.require_priced(UNPRICED) == (9.0, 9.0, 9.0)


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
    ('{"openai:gpt-9": [2.0, 12.0]}', "two figures where three are required"),
    ('{"openai:gpt-9": [2.0, 2.0, 12.0, 1.0]}', "four figures"),
    ('{"openai:gpt-9": 2.0}', "a scalar instead of a row"),
    ('{"openai:gpt-9": ["2.0", "cheap", "12.0"]}', "a row carrying a word"),
    ('{"openai:gpt-9": [2.0, null, 12.0]}', "a row carrying a null"),
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


def test_a_malformed_row_refusal_names_the_model_it_could_not_read(monkeypatch):
    """Which entry, out of however many the operator pasted. "One of your rows is wrong" over a
    ten-model map is a refusal that costs more than it saves."""
    monkeypatch.setenv(pricing.PRICING_ENV,
                       json.dumps({"openai:gpt-5.6-terra": [2.0, 2.0, 12.0],
                                   "google-gla:gemini-3.6-flash": [0.3, 2.5]}))
    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.priced_models()
    assert "google-gla:gemini-3.6-flash" in str(exc_info.value)


# ── the individual rates: what `float()` would have accepted and a dollar figure must not ──────
@pytest.mark.parametrize("literal, position", [
    ("NaN", "input"),
    ("Infinity", "input"),
    ("-Infinity", "input"),
    ("NaN", "cached input"),
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
    `float("nan")` in Python would prove nothing about the door they actually come in by.
    """
    row = {"input": [literal, "2.0", "12.0"], "cached input": ["2.0", literal, "12.0"],
           "output": ["2.0", "2.0", literal]}[position]
    monkeypatch.setenv(pricing.PRICING_ENV, f'{{"{UNPRICED}": [{", ".join(row)}]}}')

    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.require_priced(UNPRICED)

    message = str(exc_info.value)
    assert UNPRICED in message                 # which row
    assert position in message                 # ...and which of its three figures
    assert "finite" in message                 # ...and why it is refused rather than coerced


@pytest.mark.parametrize("position, row", [
    ("input", [-2.0, 2.0, 12.0]),
    ("cached input", [2.0, -0.5, 12.0]),
    ("output", [2.0, 2.0, -12.0]),
])
def test_a_negative_rate_is_refused_naming_the_model_and_the_position(monkeypatch, position, row):
    """A negative rate would SUBTRACT dollars from a run, and an instrument that can be made to
    under-report by editing one environment variable is not an instrument."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: row}))

    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.require_priced(UNPRICED)

    message = str(exc_info.value)
    assert UNPRICED in message and position in message
    assert "negative" in message


def test_a_zero_output_rate_is_refused_because_no_provider_gives_output_away(monkeypatch):
    """Zero OUTPUT is a typo or a placeholder, and it would price every filing on that model at a
    fraction of its real cost — silently, in the direction nobody audits. Refused where a zero input
    or cached-input rate is not, and the asymmetry is the argument: a cache read really can be free
    somewhere and a promotional input rate is at least conceivable, but output tokens are what a
    model is FOR."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 2.0, 0.0]}))

    with pytest.raises(LibrarianConfigError) as exc_info:
        pricing.require_priced(UNPRICED)

    assert UNPRICED in str(exc_info.value)
    assert "$0.00" in str(exc_info.value)


@pytest.mark.parametrize("row", [[0.0, 2.0, 12.0], [2.0, 0.0, 12.0]])
def test_a_zero_input_or_cached_rate_is_accepted_which_is_the_asymmetrys_other_half(monkeypatch,
                                                                                    row):
    """The specificity half of the rule above: a free cache read and a promotional input rate are
    real configurations, and a check that refused every zero would make this table unable to
    describe them."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: row}))
    assert pricing.require_priced(UNPRICED) == tuple(row)


def test_an_ordinary_corrected_row_passes_every_one_of_those_checks(monkeypatch):
    """**The benign twin for the whole `_rate` family.** Five refusals over one three-element list
    is a lot of ways to bounce an operator who is doing exactly what the module told them to do, so
    the shape the docs hand them — a genuinely cheaper cached rate, which is the whole reason the
    override exists — is asserted to sail through and to be the figures that come back out."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.2, 12.0]}))

    assert pricing.require_priced(UNPRICED) == (2.0, 0.2, 12.0)
    assert pricing.compute_cost_usd(UNPRICED, input_tokens=1_000_000, cached_input_tokens=0,
                                    output_tokens=1_000_000) == 14.0


def test_an_integer_or_string_row_is_coerced_rather_than_refused(monkeypatch):
    """JSON numbers arrive as `int` when they have no decimal point, and an operator pasting a
    quoted figure is making a formatting mistake rather than a pricing one. Both coerce — the
    refusals above are for values that are not numbers at all, not for spellings."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2, "0.2", 12]}))
    assert pricing.require_priced(UNPRICED) == (2.0, 0.2, 12.0)


def test_priced_models_lists_the_table_and_the_override_together_sorted(monkeypatch):
    """Sorted and de-duplicated, so the refusal above always lists them the same way — two runs of
    the same misconfiguration must not print two different sentences."""
    monkeypatch.setenv(pricing.PRICING_ENV,
                       json.dumps({UNPRICED: [1.0, 1.0, 1.0],
                                   "openai:gpt-5.6-terra": [1.0, 1.0, 1.0]}))
    listed = pricing.priced_models()
    assert listed == sorted(listed)
    assert listed == sorted(set(pricing.PRICES) | {UNPRICED})


# ── the arithmetic ─────────────────────────────────────────────────────────────────────────────
def test_a_million_in_and_a_million_out_costs_exactly_the_two_rates():
    """The rates are dollars per MILLION tokens, so a million of each is the table's own two
    figures added — the one case where the arithmetic can be checked by reading the row."""
    assert pricing.compute_cost_usd("openai:gpt-5.6-terra", input_tokens=1_000_000,
                                    cached_input_tokens=0, output_tokens=1_000_000) == 14.0
    assert pricing.PRICES["openai:gpt-5.6-terra"][0] + pricing.PRICES["openai:gpt-5.6-terra"][2] == 14.0


def test_the_cached_part_of_a_prompt_total_is_billed_once_at_the_cached_rate(monkeypatch):
    """**The inclusive convention, made visible.** `input_tokens` is the prompt TOTAL and
    `cached_input_tokens` the part of it served from cache, so 1000 with 400 cached bills 600 fresh
    plus 400 cached — never 1000 fresh plus 400 cached, which would charge the cached tokens twice.

    A genuinely cheaper cached rate is configured on purpose: with the shipped table's equal rates
    both readings produce the same dollars, so this test would pass against a double-billing
    implementation and prove nothing."""
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.5, 12.0]}))

    cost = pricing.compute_cost_usd(UNPRICED, input_tokens=1000, cached_input_tokens=400)

    assert cost == round((600 * 2.0 + 400 * 0.5) / 1_000_000, 6)
    assert cost != round((1000 * 2.0 + 400 * 0.5) / 1_000_000, 6), (
        "the cached tokens were billed on top of the prompt total instead of inside it")


def test_no_count_can_ever_bill_a_negative_number_of_fresh_tokens(monkeypatch):
    """The floor: a provider reporting more cached tokens than prompt tokens must not produce a
    NEGATIVE fresh count that silently subtracts dollars from the run. The whole figure stays a sum
    of non-negative parts, whatever a provider hands back.

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
    """
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.5, 12.0]}))

    cost = pricing.compute_cost_usd(UNPRICED, input_tokens=1050, cached_input_tokens=400,
                                    cache_write_tokens=50, output_tokens=100)

    # 600 fresh at input · 400 read at the cached rate · 50 written at the input rate · 100 output
    assert cost == round((600 * 2.0 + 400 * 0.5 + 50 * 2.0 + 100 * 12.0) / 1_000_000, 6)
    # ...and the disjoint reading, which would bill the cached and written tokens twice over
    assert cost != round((1050 * 2.0 + 400 * 0.5 + 50 * 2.0 + 100 * 12.0) / 1_000_000, 6)


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


def test_a_cache_write_is_billed_rather_than_treated_as_free(monkeypatch):
    """Cache writes cost money and must not price at zero.

    They are billed at the INPUT rate, which is the one figure in this seam that errs DOWNWARD:
    Anthropic charges 1.25x base input to write a cache entry, and a three-figure row cannot express
    that. The gap is documented in `pricing.py` and is a follow-up (a fourth element per row), not a
    property to be happy about — what is pinned here is only that a write is not free, because
    "free" is the failure mode this whole module exists to prevent.
    """
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({UNPRICED: [2.0, 0.5, 12.0]}))

    written = pricing.compute_cost_usd(UNPRICED, input_tokens=1000, cache_write_tokens=1000)
    nothing_written = pricing.compute_cost_usd(UNPRICED, input_tokens=1000)

    assert written > 0
    assert written == nothing_written, (
        "a cache write is billed at the input rate today, so moving 1000 prompt tokens into the "
        "write bucket must not change the total — if this fails, the fourth-element follow-up "
        "landed and the 1.25x figure needs its own assertion")
    assert "1.25x" in pricing.compute_cost_usd.__doc__, (
        "the under-bill is documented at the function an operator would read; a fix that closes it "
        "must remove the note in the same commit")


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
                                    cached_input_tokens=None, output_tokens=None) == 0.0


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
