"""`answer/numbers.py` — the two debts numeric matching carried, and the fixes that paid them.

Pure and keyless. The first block pins the `x`-multiplier repair with the MEASURED witness that
found it (`benchmark-speed`: a correct page-backed figure refused); the second pins the one-sided
anti-laundering tightening (`$2M` no longer verifies against a bare `2`); the third pins that the
older generosities — the ones `test_verify.py` has always asserted — did not move.
"""
import pytest

from stigmergy.answer.numbers import claimed, number_pool, unverified_figures

# ── the x multiplier: a REAL false refusal, measured against a live corpus, not theorized ──────


def test_the_benchmark_speed_witness_a_decimal_comma_draft_now_verifies():
    """The page says 2.3x; the model's draft wrote 2,3 — a decimal comma, which a model may
    reach for whatever language the question was asked in. The token regex used to read
    `2.3x` as a bare 2, so no overlap existed and the strict gate withheld a correct answer — the
    one measured groundedness cost of the old tokenizer. Both spellings pool as 2.3 now."""
    assert unverified_figures("The engine is 2,3 times faster.", "the engine is 2.3x faster") == []


def test_a_multiplier_answer_verifies_against_a_plain_evidence_figure():
    assert unverified_figures("It is 2.3x faster.", "speedup measured: 2.3 over baseline") == []


def test_a_wrong_multiplier_is_still_flagged():
    assert unverified_figures("It is 5x faster.", "the engine is 2.3x faster") == ["5x"]


def test_x_is_a_dimension_never_a_magnitude():
    """`2.3x` means 2.3 (times) — it must not scale like k/m/bn do."""
    assert number_pool("2.3x") == {"v:2.3"}
    assert claimed("2.3", "x", None) == {"v:2.3"}


def test_a_single_digit_with_a_multiplier_is_a_figure_not_a_list_marker():
    assert unverified_figures("The system is 5x faster.", "no figures here") == ["5x"]


@pytest.mark.parametrize("text", ["4x4", "0x2F", "x2"])
def test_embedded_x_forms_still_tokenize_to_nothing(text):
    assert number_pool(text) == set()


# ── magnitude/percent laundering, closed one-sided: `$2M` used to verify against a bare `2` ────


def test_a_magnitude_claim_no_longer_verifies_against_its_bare_mantissa():
    """The laundering witness: an answer's $2M and an evidence-side bare 2 used to share the
    mantissa interpretation, so the dimensioned claim shipped unbacked."""
    assert unverified_figures("Revenue was $2M.", "we ran 2 experiments") == ["2M"]


def test_a_magnitude_claim_verifies_against_the_same_magnitude():
    assert unverified_figures("Revenue was $2M.", "revenue: 2M EUR") == []


def test_a_magnitude_claim_verifies_against_its_expanded_value():
    assert unverified_figures("Revenue was $2M.", "revenue reached 2,000,000 EUR") == []


def test_a_percent_claim_no_longer_verifies_against_a_bare_number():
    assert unverified_figures("Margin was 40%.", "we hired 40 people") == ["40%"]


def test_a_percent_claim_verifies_against_a_percent_even_spaced():
    assert unverified_figures("Margin was 40%.", "margin held at 40 % this quarter") == []


def test_a_bare_answer_figure_still_verifies_against_a_suffixed_evidence_token():
    """The generous direction survives: prose writes magnitudes out ('2,3 millones') where no
    tokenizer reaches, so the EVIDENCE side keeps pooling both readings."""
    assert unverified_figures("El ARR fue de 512.000 USD.", "ARR: 512k usd") == []


# ── the older generosities did not move (test_verify.py's own assertions, re-pinned here) ──────


def test_the_original_generosity_case_is_byte_identical():
    ev = "ARR was 1,200,000 EUR in Q1 2026 (about 40 %)."
    assert unverified_figures("Revenue reached 1.2M, up 40%, in 2026.", ev) == []
    assert unverified_figures("Margin was 77%.", ev) == ["77%"]
    assert unverified_figures("the 3 initiatives", "no digits") == []


def test_named_accepted_residual_suffixed_answer_vs_spelled_out_evidence_refuses():
    """A named residual, accepted with eyes open: the strict answer-side claim cannot trace
    `$2M` to prose evidence that spells the magnitude out ('2 millones'), where the bare mantissa
    used to launder it through. The agent is instructed to quote figures as the page states them,
    so the honest future fix (if this ever bites a real answer) is EVIDENCE-side word-magnitude
    parsing — never a wider answer-side claim."""
    assert unverified_figures("Revenue was $2M.", "ingresos de 2 millones de euros") == ["2M"]
    assert unverified_figures("Margin was 40%.", "el margen fue del 40 por ciento") == ["40%"]
