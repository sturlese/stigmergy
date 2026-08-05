"""Golden-set loading and Recall@k scoring — the pure half of evals/run_retrieval.py."""
import json
from pathlib import Path

import pytest

from stigmergy.index import golden, search

GOLDEN_V0 = str(Path(__file__).resolve().parents[2] / "evals" / "retrieval_golden.json")


def test_the_golden_set_is_a_real_instrument_not_a_stub():
    """A floor, not an exact count: the set grows with every observed miss, and a handful of
    questions would measure nothing. Every entry must carry both a question and an expectation —
    one without the other scores silently and forever."""
    questions = golden.load_golden(GOLDEN_V0)
    assert len(questions) >= 15
    for q in questions:
        assert q["q"] and q["expect"]


def test_load_golden_accepts_qa_golden_shape(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"questions": [
        {"id": "x", "q": "pregunta", "expect": {"pages": ["a", "b"]}}]}))
    (item,) = golden.load_golden(str(p))
    assert item == {"id": "x", "q": "pregunta", "expect": ["a", "b"], "filters": {}}


def test_load_golden_carries_a_questions_filters_through(tmp_path):
    """The golden set can ask a FILTERED question — `filters` is the carrier field the runner
    hands to `search.search_arms(filters=...)`."""
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"questions": [
        {"id": "x", "q": "pregunta", "expect": {"pages": ["a"]},
         "filters": {"entity": "aurora-systems"}}]}))
    (item,) = golden.load_golden(str(p))
    assert item["filters"] == {"entity": "aurora-systems"}


def test_load_golden_defaults_filters_to_empty_when_absent(tmp_path):
    """An unfiltered question stays unfiltered: `{}` is what `_filter_clause` reads as
    'no filters', so the control half of the set is byte-identically unchanged."""
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"questions": [{"q": "pregunta", "expect": {"pages": ["a"]}}]}))
    (item,) = golden.load_golden(str(p))
    assert item["filters"] == {}


def test_evaluate_passes_each_questions_filters_to_the_ranking_fn():
    """A filtered question must REACH the search call filtered. `evaluate` used to call the
    ranking fn with the query alone, which left the golden run structurally blind to `filters=`
    no matter what the JSON said."""
    questions = [{"id": "q1", "q": "one", "expect": ["a"], "filters": {"entity": "aurora-systems"}},
                 {"id": "q2", "q": "two", "expect": ["b"], "filters": {}}]
    seen = []

    def arms(q, filters):
        seen.append((q, filters))
        return {"fts": [], "vec": [], "rrf": [], "final": []}

    golden.evaluate(questions, arms, k=5)
    assert seen == [("one", {"entity": "aurora-systems"}), ("two", {})]


def test_retrieval_golden_v0_asks_at_least_ten_entity_filtered_questions():
    """The shipped set exercises the filter: every question whose SUBJECT is a named entity
    carries `filters.entity`, so breaking the membership clause in `index/search.py` moves this
    run's result."""
    questions = golden.load_golden(GOLDEN_V0)
    filtered = [q for q in questions if q["filters"]]
    assert len(filtered) >= 10
    assert all("entity" in q["filters"] for q in filtered)


def test_retrieval_golden_v0_only_filters_on_legal_columns():
    """A typo'd column is a ValueError deep inside `_filter_clause` at run time, minutes into a
    keyed measurement — caught here instead, keyless."""
    for q in golden.load_golden(GOLDEN_V0):
        assert set(q["filters"]) <= set(search.FILTER_COLUMNS), q["id"]


def test_load_golden_rejects_empty_expectations(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"questions": [{"q": "sin respuesta", "expect": {"pages": []}}]}))
    with pytest.raises(ValueError):
        golden.load_golden(str(p))


def test_recall_and_hit_at_k():
    assert golden.recall_at_k(["a", "b"], ["a", "x", "y"], 3) == 0.5
    assert golden.recall_at_k(["a"], ["x", "y", "a"], 3) == 1.0
    assert golden.recall_at_k(["a"], ["x", "y", "a"], 2) == 0.0
    assert golden.hit_at_k(["a", "b"], ["b"], 5) is True
    assert golden.hit_at_k(["a"], [], 5) is False


def test_evaluate_reports_per_arm_numbers_and_misses():
    questions = [{"id": "q1", "q": "one", "expect": ["a"]},
                 {"id": "q2", "q": "two", "expect": ["b"]}]

    def arms(_q, _filters):
        return {"fts": ["x"], "vec": ["a", "b"], "rrf": ["a"], "final": ["b", "a"]}

    report = golden.evaluate(questions, arms, k=2)
    assert report["summary"]["fts"] == {"recall_at_k": 0.0, "hit_at_k": 0.0}
    assert report["summary"]["vec"] == {"recall_at_k": 1.0, "hit_at_k": 1.0}
    assert report["summary"]["rrf"] == {"recall_at_k": 0.5, "hit_at_k": 0.5}
    assert report["summary"]["final"] == {"recall_at_k": 1.0, "hit_at_k": 1.0}
    rendered = golden.render_report(report)
    assert "MISS [fts,rrf] two" in rendered
    assert "fts" in rendered and "R@2" in rendered


# --- chain-equivalent scoring: surfacing a part IS surfacing the document ---------------------

def test_recall_credits_a_chain_member_when_the_base_is_expected():
    """rank's top-k collapses a chain to its best-scoring member; WHICH member that is must not
    decide a hit — the golden always expects the base id."""
    assert golden.recall_at_k(["meeting-transcript"], ["meeting-transcript-p3"], 5) == 1.0
    assert golden.hit_at_k(["meeting-transcript"], ["meeting-transcript-p3"], 5)
    assert golden.recall_at_k(["drive:R1"], ["drive:R1#p2"], 5) == 1.0


def test_chain_equivalence_never_credits_a_different_document():
    assert golden.recall_at_k(["meeting-transcript"], ["other-page"], 5) == 0.0
    assert not golden.hit_at_k(["meeting-transcript"], ["other-page-p2"], 5)
