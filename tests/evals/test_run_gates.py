"""`evals/run_gates.py`'s judgment — the armed bars, keyless.

The runner half (subprocesses over the two golden CLIs) is exercised by `make gates` itself;
what CI pins is the part where a release gate can lie: the bars' VALUES and the judgment logic,
including that refutation is reported but never gated.
"""
from evals import run_gates


def test_the_bars_are_the_declared_release_numbers():
    assert run_gates.BAR_RECALL == 0.80
    assert run_gates.BAR_HONESTY == 0.90
    assert run_gates.BAR_GROUNDEDNESS == 0.84      # 4.2/5, on this rig's 0..1 scale


def test_retrieval_judgment():
    ok = {"k": 5, "summary": {"final": {"recall_at_k": 0.923}}}
    low = {"k": 5, "summary": {"final": {"recall_at_k": 0.79}}}
    assert run_gates.judge_retrieval(ok) == []
    assert run_gates.judge_retrieval(low) == ["final R@5 0.790 < 0.80"]


def test_qa_judgment_gates_honesty_and_groundedness():
    ok = {"honesty": 1.0, "groundedness": 0.95, "refutation": 0.5}
    assert run_gates.judge_qa(ok) == []
    low_honesty = {"honesty": 0.86, "groundedness": 1.0, "refutation": 1.0}
    assert run_gates.judge_qa(low_honesty) == ["honesty 0.860 < 0.90"]
    low_ground = {"honesty": 1.0, "groundedness": 0.80, "refutation": 1.0}
    assert run_gates.judge_qa(low_ground) == ["groundedness 0.800 < 0.84"]


def test_refutation_is_reported_never_gated():
    """No bar in `evals/bars.py` names refutation — a gate nobody decided must not arm itself."""
    assert run_gates.judge_qa({"honesty": 1.0, "groundedness": 1.0, "refutation": 0.0}) == []


def test_both_failures_are_named_together():
    report = {"honesty": 0.5, "groundedness": 0.5, "refutation": 1.0}
    assert len(run_gates.judge_qa(report)) == 2


def test_the_adversarial_expression_names_the_armed_categories_only():
    assert run_gates.ADVERSARIAL_K == "adversarial_cat1 or adversarial_cat2 or adversarial_cat7"


# ── the CONSTRAINED noise rule ──────────────────────────────────────────────────────────────────
def test_a_single_case_miss_is_within_the_noise_band():
    report = {"honesty": 0.8667, "groundedness": 1.0, "refutation": 1.0,
              "counts": {"unanswerable": 15, "answerable": 43, "corrective": 9}}
    assert run_gates.within_noise_band(report, "qa")            # 0.90 - 1/15 = 0.833


def test_a_two_case_miss_is_a_regression_not_noise():
    report = {"honesty": 0.7333, "groundedness": 1.0, "refutation": 1.0,
              "counts": {"unanswerable": 15, "answerable": 43, "corrective": 9}}
    assert not run_gates.within_noise_band(report, "qa")


def test_retrieval_noise_band_uses_the_question_count():
    ok = {"questions": 13, "summary": {"final": {"recall_at_k": 0.769}}}   # 0.80 - 1/13 = 0.723
    bad = {"questions": 13, "summary": {"final": {"recall_at_k": 0.60}}}
    assert run_gates.within_noise_band(ok, "retrieval")
    assert not run_gates.within_noise_band(bad, "retrieval")


def test_every_failing_axis_must_be_borderline_for_the_rerun():
    """One borderline axis + one collapsed axis = a regression; the collapsed one decides."""
    report = {"honesty": 0.8667, "groundedness": 0.5, "refutation": 1.0,
              "counts": {"unanswerable": 15, "answerable": 43, "corrective": 9}}
    assert not run_gates.within_noise_band(report, "qa")
