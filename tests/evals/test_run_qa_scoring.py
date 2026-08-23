"""`evals/run_qa.py`'s scorer.

Same posture as `test_eval_history.py` and `test_run_retrieval_filters.py`: the eval RUNNER has no
unit tests of its own — it is the test, at system level — but `_score`/`_aggregate` are a small
pure library, and they are where a yardstick can lie. Every defect pinned here made the instrument
report a MISS for behavior that is actually CORRECT, the dangerous direction for a number a
release gate is armed against.

The `res` fixtures mirror `AnswerService.ask`'s real return shape: `refused`, `answer_markdown`,
`citations` (dicts carrying `path`), `verdict.verdict`, and the two verdict detail lists.
"""
import pytest

from evals import run_qa


def _res(answer="", *, refused=False, cites=(), verdict="verified", retried=False,
         citation_problems=(), unverified_figures=()):
    return {"refused": refused, "answer_markdown": answer,
            "citations": [{"path": p} for p in cites],
            "verdict": {"verdict": verdict, "unverified_figures": list(unverified_figures),
                       "citation_problems": list(citation_problems)},
            "reason": "", "suppressed": False, "retried": retried}


PAGE = "sources/entities/aurora-systems/kpi-metrics-2026-42501c.md"


def _exact(expect, cites=PAGE):
    return {"id": "c", "kind": "exact", "family": "answerable",
            "expect_contains": expect, "cites": cites}


# ------------------------------------------------------------------------------- figures

@pytest.mark.parametrize("expected, answer", [
    ("1074", "In 2026-03 Aurora Systems had **1.074** active users."),        # thousands dot
    ("512000", "February ARR was 512.000 USD."),                             # thousands dot
    ("512000", "February ARR was 512k USD."),                                # magnitude suffix
    ("1,234,567", "ARR reached 1234567 EUR."),                               # grouping dropped
    ("1,234,567", "ARR reached 1.234.567 EUR."),                             # other grouping
    ("2.3x", "Our engine is 2,3x faster."),                                  # decimal comma
    ("2.3x", "It is 2.3 times faster on the 10k-stop dataset."),             # 'x' spelled out
    ("18%", "It puts forward an 18 % saving on routing costs."),             # spaced percent
])
def test_a_figure_scores_as_found_when_it_is_numerically_equivalent(expected, answer):
    """The literal `in` test made the scorer fail answers that are RIGHT — a model writing
    1.074 for 1074 was recorded as a groundedness miss. The model picks the notation; the
    question set does not, which is why these equivalences survive an English golden."""
    scored = run_qa._score(_exact(expected), _res(answer, cites=[PAGE]))
    assert scored["ok"], scored.get("miss")


@pytest.mark.parametrize("expected, answer", [
    ("1074", "Aurora Systems had 2.500 active users."),
    ("512000", "February ARR was 480.000 USD."),
    ("2.3x", "Our engine is 5x faster."),
])
def test_a_wrong_figure_still_scores_as_a_miss(expected, answer):
    """Generosity in one direction only: numeric equivalence must not become 'any number'."""
    scored = run_qa._score(_exact(expected), _res(answer, cites=[PAGE]))
    assert not scored["ok"]
    assert scored["miss"]["expected_found"] is False


def test_numeric_equivalence_never_applies_to_a_prose_expectation():
    """`routing v2` carries a digit but is not a figure. If the fallback fired on it, ANY answer
    containing a 2 would score as a hit — the scorer would stop measuring anything at all."""
    case = {"id": "roadmap-q2", "kind": "prose", "family": "answerable",
            "expect_contains": "routing v2", "cites": PAGE}
    scored = run_qa._score(case, _res("El Q2 trae mejoras de rendimiento.", cites=[PAGE]))
    assert not scored["ok"]


def test_a_literal_prose_expectation_still_matches_literally():
    case = {"id": "git-store-repo", "kind": "prose", "family": "answerable",
            "expect_contains": "stigmergy", "cites": PAGE}
    assert run_qa._score(case, _res("Vive en el repositorio stigmergy.", cites=[PAGE]))["ok"]


# -------------------------------------------------------------------------------- chains

def test_cites_accepts_a_chain_and_any_page_in_it_counts_as_the_citation():
    """An entity-shaped answer may legitimately cite ANY of the several source documents that
    back the same claim (`qa_golden.json`'s `aurora-dossier-count`, four documents, one chain).
    Pinning one of them scored the others as uncited."""
    first = "sources/entities/aurora-systems/meeting-notes-2026-02-10-a59d7b.md"
    second = "sources/entities/aurora-systems/kpi-metrics-2026-42501c.md"
    case = {"id": "aurora-dossier-summary", "kind": "prose", "family": "entity-shaped",
            "expect_contains": "SSO", "cites": [first, second]}

    assert run_qa._score(case, _res("Pendiente el SSO.", cites=[first]))["ok"]
    assert run_qa._score(case, _res("Pendiente el SSO.", cites=[second]))["ok"]


def test_a_citation_outside_the_chain_is_still_a_miss():
    case = {"id": "x", "kind": "prose", "family": "entity-shaped", "expect_contains": "SSO",
            "cites": ["sources/entities/aurora-systems/meeting-notes-2026-02-10-a59d7b.md"]}
    scored = run_qa._score(case, _res("Pendiente el SSO.", cites=["sources/units/product/roadmap-2026-9e7390.md"]))
    assert not scored["ok"]
    assert scored["miss"]["cited_expected_page"] is False


def test_cites_as_a_plain_string_still_works():
    """Most golden cases keep the scalar spelling, and it must keep working."""
    assert run_qa._score(_exact("1074"), _res("Fueron 1074.", cites=[PAGE]))["ok"]


# ------------------------------------------------------------------------- false premise

REFUTE = {"id": "benchmark-5x-premise", "kind": "refute", "family": "false-premise",
          "refutes": "5x", "expect_contains": "2.3x",
          "cites": "sources/units/product/benchmark-study-15f21e.md"}
BENCH = "sources/units/product/benchmark-study-15f21e.md"


def test_a_false_premise_question_passes_when_the_brain_refuses():
    """Unchanged: refusing a mis-premised question was, and stays, correct behavior."""
    assert run_qa._score(REFUTE, _res(refused=True))["ok"]


def test_a_false_premise_question_passes_when_the_brain_REFUTES_with_a_cited_figure():
    """Correcting the premise is BETTER than refusing, and the scorer used to record it as a
    miss — the instrument punished the best available behavior."""
    answer = "The benchmark does not say 5x: it measures **2.3x** faster on the 10k-stop dataset."
    assert run_qa._score(REFUTE, _res(answer, cites=[BENCH]))["ok"]


def test_a_refutation_is_numerically_equivalent_too():
    answer = "El estudio reporta 2,3x, no 5x."
    assert run_qa._score(REFUTE, _res(answer, cites=[BENCH]))["ok"]


def test_swallowing_the_false_premise_is_still_a_miss():
    """The failure mode the case exists to catch: answering AS IF 5x were true."""
    answer = "A 5x de velocidad el consumo de memoria se mantiene dentro del 10%."
    assert not run_qa._score(REFUTE, _res(answer, cites=[BENCH]))["ok"]


def test_an_uncited_refutation_is_a_miss():
    assert not run_qa._score(REFUTE, _res("En realidad son 2.3x.", cites=[]))["ok"]


def test_a_refutation_whose_verdict_failed_is_a_miss():
    answer = "El benchmark mide 2.3x, no 5x."
    assert not run_qa._score(REFUTE, _res(answer, cites=[BENCH], verdict="failed"))["ok"]


# ----------------------------------------------------------- mixed-entity disambiguation

DISAMB = {"id": "northwind-mrr", "kind": "disambiguate", "family": "mixed-entity",
          "expect_contains": "23500", "cites": "wiki/notes/Vantage June 2026 Investor Update.md"}
UPDATE = "wiki/notes/Vantage June 2026 Investor Update.md"


def test_a_mixed_entity_question_passes_when_the_brain_refuses():
    """The honest refusal is a first-class pass — accepting disambiguation widens what counts,
    it never narrows it."""
    assert run_qa._score(DISAMB, _res(refused=True))["ok"]


def test_a_mixed_entity_question_passes_when_the_brain_DISAMBIGUATES_with_a_cited_figure():
    """A measured answer's exact shape: 'no own figure for X' plus sibling Y's real figure,
    correctly attributed and cited, verdict verified — the refusal-only scorer counted it a
    honesty miss, punishing the better behavior."""
    answer = ("No hay una cifra de MRR propio de Northwind Group en la base; el MRR "
              "contratado de Vantage (participada) era de 23.500 EUR a cierre de junio.")
    assert run_qa._score(DISAMB, _res(answer, cites=[UPDATE]))["ok"]


def test_an_uncited_disambiguation_is_a_miss():
    answer = "Northwind no tiene MRR propio; el de Vantage era 23.500 EUR."
    assert not run_qa._score(DISAMB, _res(answer, cites=[]))["ok"]


def test_a_disambiguation_with_the_wrong_figure_is_a_miss():
    answer = "Northwind no tiene MRR propio; el de Vantage era 99.000 EUR."
    assert not run_qa._score(DISAMB, _res(answer, cites=[UPDATE]))["ok"]


def test_a_disambiguation_whose_verdict_failed_is_a_miss():
    answer = "El MRR contratado de Vantage era 23.500 EUR."
    assert not run_qa._score(DISAMB, _res(answer, cites=[UPDATE], verdict="failed"))["ok"]


# --------------------------------------------------------------- the honesty denominator

def _golden(questions):
    return {"questions": questions}


def test_refute_cases_leave_the_honesty_denominator_and_get_their_own_axis():
    """Honesty means REFUSAL RATE over genuinely unanswerable questions. A mis-premised question
    is answerable — correctly, by contradiction — so keeping it in that denominator while
    accepting a non-refusal would quietly redefine the metric the release gate is armed against.
    It gets a third axis instead, and groundedness (which measures plain answering) does not
    absorb it either."""
    results = [
        {"id": "a", "family": "answerable", "kind": "exact", "ok": True},
        {"id": "r", "family": "absent-fact", "kind": "refusal", "ok": True},
        {"id": "f1", "family": "false-premise", "kind": "refute", "ok": True},
        {"id": "f2", "family": "false-premise", "kind": "refute", "ok": False},
    ]
    report = run_qa._aggregate(_golden([]), results, "gpt-5.6-terra")

    assert report["counts"] == {"answerable": 1, "unanswerable": 1, "corrective": 2}
    assert report["honesty"] == 1.0
    assert report["groundedness"] == 1.0
    assert report["refutation"] == 0.5


def test_disambiguate_cases_join_the_refutation_axis_and_leave_honesty_alone():
    """Aggregated: a failed disambiguation moves REFUTATION, never honesty — the honesty gate
    keeps meaning "refusal rate over the genuinely unanswerable"."""
    results = [
        {"id": "r", "family": "absent-fact", "kind": "refusal", "ok": True},
        {"id": "d1", "family": "mixed-entity", "kind": "disambiguate", "ok": True},
        {"id": "d2", "family": "mixed-entity", "kind": "disambiguate", "ok": False},
        {"id": "f1", "family": "false-premise", "kind": "refute", "ok": True},
    ]
    report = run_qa._aggregate(_golden([]), results, "gpt-5.6-terra")

    assert report["counts"] == {"answerable": 0, "unanswerable": 1, "corrective": 3}
    assert report["honesty"] == 1.0
    assert report["refutation"] == pytest.approx(2 / 3)


def test_the_rendered_report_shows_the_refutation_axis():
    results = [{"id": "f", "family": "false-premise", "kind": "refute", "ok": True}]
    rendered = run_qa._render(run_qa._aggregate(_golden([]), results, "m"))
    assert "refutation" in rendered


# ------------------------------------------------------------- the shipped golden set

def test_the_three_false_premise_cases_carry_expected_refutation_fields():
    """The values come from the frozen corpus, never invented here."""
    import json

    golden = json.loads((run_qa.ROOT / "evals" / "qa_golden.json").read_text(encoding="utf-8"))
    cases = [q for q in golden["questions"] if q["family"] == "false-premise"]

    assert len(cases) == 3
    for c in cases:
        assert c["kind"] == "refute"
        assert c["refutes"] and c["expect_contains"] and c["cites"]


def test_the_golden_set_size_is_pinned():
    """Pinned so the instrument cannot be quietly resized between runs — a score is only
    comparable against another score over the same question set."""
    import json

    golden = json.loads((run_qa.ROOT / "evals" / "qa_golden.json").read_text(encoding="utf-8"))
    assert len(golden["questions"]) == 26


# ------------------------------------------------------------------------ date equivalence
def test_an_iso_expectation_matches_the_long_form_either_way_round():
    """`aurora-timeline-q1`'s measured miss: right page, right date, verdict verified — the
    yardstick just couldn't read a long-form spelling of the day as 2026-02-10. Both orders
    are accepted because English writes both."""
    case = {"expect_contains": "2026-02-10"}
    assert run_qa._expectation_met(case, "The agreed date was 10 February 2026.")
    assert run_qa._expectation_met(case, "The agreed date was February 10, 2026.")


def test_an_iso_expectation_still_matches_itself_and_numeric_forms():
    case = {"expect_contains": "2026-02-10"}
    assert run_qa._expectation_met(case, "the deadline is 2026-02-10")
    assert run_qa._expectation_met(case, "it closes on 10/02/2026")
    assert run_qa._expectation_met(case, "it closes on 10-2-2026")


def test_a_wrong_date_is_still_a_miss_the_benign_twin_of_equivalence():
    """Equivalence must not decay into leniency: the SAME month in a different day/year stays a
    miss, and a bare year mention proves nothing."""
    case = {"expect_contains": "2026-02-10"}
    assert not run_qa._expectation_met(case, "11 February 2026")
    assert not run_qa._expectation_met(case, "in February 2026")
    assert not run_qa._expectation_met(case, "10 February 2025")


def test_a_yearless_date_matches_when_no_year_contradicts():
    """`premise-ledgerly-api`'s measured miss: a correct, cited, verified correction wrote that
    it was agreed "to check with the vendor on 12 August" — prose leaves the year contextual —
    and the full-form-only matcher failed the right answer."""
    case = {"expect_contains": "2026-08-12"}
    assert run_qa._expectation_met(case, "it was agreed to check with the vendor on 12 August")
    assert run_qa._expectation_met(case, "the call is on August 12, with the vendor")


def test_a_yearless_match_still_refuses_a_spelled_out_wrong_year():
    """The lookahead that keeps equivalence from decaying into leniency: yearless acceptance
    must not let a year-BEARING wrong date in through the side door."""
    case = {"expect_contains": "2026-08-12"}
    assert not run_qa._expectation_met(case, "the call was on 12 August 2025")
    assert not run_qa._expectation_met(case, "the call was on August 12, 2025")
    assert not run_qa._expectation_met(case, "on 13 August")


# --------------------------------------------------- the retry tax rides along on `ok` questions
# THE defect this instrumentation fixes: `retried`/`citation_problems`/`unverified_figures` used to
# be recorded only inside the `miss` block, and nearly every retried ask still ends `verified` — so
# a report that kept these fields only for misses could not see the retry tax at all, because it is
# paid almost entirely by questions this scorer calls `ok`.
def test_an_ok_question_still_carries_retried_and_verifier_findings():
    res = _res("Fueron 1074.", cites=[PAGE], retried=True,
               citation_problems=["citation quote not found in p.md: 'x'"],
               unverified_figures=["9.9M"])
    scored = run_qa._score(_exact("1074"), res)
    assert scored["ok"] is True
    assert scored["retried"] is True
    assert scored["citation_problems"] == ["citation quote not found in p.md: 'x'"]
    assert scored["unverified_figures"] == ["9.9M"]
    assert "miss" not in scored          # an `ok` result carries no miss block at all


def test_a_non_retried_ok_question_defaults_retried_false_and_carries_no_findings():
    """The benign twin: an ordinary, non-retried `ok` question must not spuriously report a retry
    or phantom findings."""
    scored = run_qa._score(_exact("1074"), _res("Fueron 1074.", cites=[PAGE]))
    assert scored["ok"] is True
    assert scored["retried"] is False
    assert scored["citation_problems"] == []
    assert scored["unverified_figures"] == []


def test_a_missed_question_also_carries_retried_and_findings_same_as_before():
    """The regression guard for the OLD coverage: these three fields must still be there on a MISS
    too, not only relocated to the `ok` path — `_score` always sets them, unconditionally, before
    branching on `ok`."""
    res = _res("Fueron 2500.", cites=[PAGE], retried=True, citation_problems=["ghost citation"])
    scored = run_qa._score(_exact("1074"), res)
    assert scored["ok"] is False
    assert scored["retried"] is True
    assert scored["citation_problems"] == ["ghost citation"]
    assert "miss" in scored


# --------------------------------------------------------------------- retry rate and seconds
def test_aggregate_computes_the_retry_rate_over_all_results():
    results = [
        {"id": "a", "family": "fam", "kind": "exact", "ok": True, "retried": True},
        {"id": "b", "family": "fam", "kind": "exact", "ok": True, "retried": False},
        {"id": "c", "family": "fam", "kind": "exact", "ok": True, "retried": False},
        {"id": "d", "family": "fam", "kind": "exact", "ok": False, "retried": False},
    ]
    report = run_qa._aggregate(_golden([]), results, "gpt-5.6-terra")
    assert report["retry_rate"] == pytest.approx(0.25)


def test_aggregate_retry_rate_is_zero_with_no_results_never_a_crash():
    report = run_qa._aggregate(_golden([]), [], "gpt-5.6-terra")
    assert report["retry_rate"] == 0.0


def test_aggregate_seconds_is_none_when_nothing_was_timed():
    """`_aggregate` is exercised directly by this whole file over results built by hand — none of
    them ever ran through `_run`'s wall clock — so `seconds` must read `None`, never `0`. A zeroed
    latency would read as "instantaneous" instead of "not measured"."""
    results = [{"id": "a", "family": "fam", "kind": "exact", "ok": True}]
    report = run_qa._aggregate(_golden([]), results, "gpt-5.6-terra")
    assert report["seconds"] is None


def test_aggregate_seconds_median_mean_max_over_timed_results():
    results = [
        {"id": "a", "family": "fam", "kind": "exact", "ok": True, "seconds": 2.0},
        {"id": "b", "family": "fam", "kind": "exact", "ok": True, "seconds": 4.0},
        {"id": "c", "family": "fam", "kind": "exact", "ok": True, "seconds": 12.0},
    ]
    report = run_qa._aggregate(_golden([]), results, "gpt-5.6-terra")
    assert report["seconds"] == {"median": 4.0, "mean": pytest.approx(6.0), "max": 12.0}


def test_render_shows_retry_rate_and_seconds_lines():
    results = [{"id": "a", "family": "fam", "kind": "exact", "ok": True, "retried": True,
               "seconds": 3.0}]
    rendered = run_qa._render(run_qa._aggregate(_golden([]), results, "gpt-5.6-terra"))
    assert "retry rate" in rendered
    assert "seconds/q" in rendered


def test_render_omits_the_seconds_line_when_nothing_was_timed():
    """The benign twin of the line above: `_render` must not crash, and must not print a
    seconds line at all, when `seconds` is `None` (the aggregate-only unit-test path, and any real
    run where every timing was somehow missing)."""
    results = [{"id": "a", "family": "fam", "kind": "exact", "ok": True, "retried": False}]
    rendered = run_qa._render(run_qa._aggregate(_golden([]), results, "gpt-5.6-terra"))
    assert "seconds/q" not in rendered


def test_render_names_only_the_actually_retried_question_ids():
    """Named, not just counted — the lead for the next prompt or matcher fix. Both a retried and a
    non-retried id are present so the negative half (the steady id must NOT appear) is proven, not
    merely assumed from a single-id fixture."""
    results = [
        {"id": "aurora-timeline-q1", "family": "fam", "kind": "exact", "ok": True,
         "retried": True, "seconds": 3.0},
        {"id": "steady-question", "family": "fam", "kind": "exact", "ok": True,
         "retried": False, "seconds": 5.0},
    ]
    rendered = run_qa._render(run_qa._aggregate(_golden([]), results, "gpt-5.6-terra"))
    retried_line = next(line for line in rendered.splitlines() if line.startswith("retried:"))
    assert "aurora-timeline-q1" in retried_line
    assert "steady-question" not in retried_line


def test_render_omits_the_retried_line_entirely_when_nothing_retried():
    results = [{"id": "a", "family": "fam", "kind": "exact", "ok": True, "retried": False}]
    rendered = run_qa._render(run_qa._aggregate(_golden([]), results, "gpt-5.6-terra"))
    assert "retried:" not in rendered
