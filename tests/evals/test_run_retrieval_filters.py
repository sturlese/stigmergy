"""`evals/run_retrieval.py`'s ranking callback — the connected-plumbing proof.

Same posture as `test_eval_history.py`: the harness has no unit tests of its own — it IS the test,
at system level — and that holds for the RUNNER (argparse, DSN, reporting). This is not that.
`make_arm_rankings` is the seam where a golden question's declared `filters` either reach
`search.search_arms` or are silently dropped, and "silently dropped" was the actual state of the
world for a long time, survivable precisely because the only witness was a keyed, Postgres-bound
end-to-end run nobody could put in CI.
"""
import pytest

from evals import run_retrieval


class _RecordingSearch:
    """Stands in for `stigmergy.index.search`, capturing the kwargs the runner really sends."""

    def __init__(self):
        self.calls = []

    def search_arms(self, conn, query, *, embedder=None, k=5, filters=None):
        self.calls.append({"query": query, "k": k, "filters": filters, "embedder": embedder})
        return {"fts": ["p1"], "vec": ["p1"], "hits": [{"page_id": "id1"}],
                "page_ids": {"p1": "id1"}}


@pytest.fixture
def recording(monkeypatch):
    fake = _RecordingSearch()
    monkeypatch.setattr(run_retrieval, "search", fake)
    return fake


def test_a_questions_filters_reach_search_arms(recording):
    """The defect this closes: the argument was never passed at all."""
    rankings = run_retrieval.make_arm_rankings(conn=object(), embedder=object(), k=5)

    rankings("What was the ARR for Aurora Systems?", {"entity": "aurora-systems"})

    assert recording.calls[0]["filters"] == {"entity": "aurora-systems"}


def test_an_unfiltered_question_still_searches_unfiltered(recording):
    """The control half: `{}` and no-argument both have to arrive as `None`, not as an empty dict
    that `_filter_clause` might one day read as a real (and empty, and therefore
    matching-nothing) filter set."""
    rankings = run_retrieval.make_arm_rankings(conn=object(), embedder=object(), k=5)

    rankings("What does Peter Attia say about longevity?", {})
    rankings("What does Peter Attia say about longevity?")

    assert [c["filters"] for c in recording.calls] == [None, None]


def test_the_ranking_callback_reports_page_ids_per_arm(recording):
    """Unchanged contract: `golden.evaluate` scores page IDs, not paths."""
    rankings = run_retrieval.make_arm_rankings(conn=object(), embedder=object(), k=5)

    out = rankings("pregunta", {"entity": "globex"})

    assert out == {"fts": ["id1"], "vec": ["id1"], "rrf": ["id1"], "final": ["id1"]}


def test_evaluate_drives_the_callback_with_the_shipped_golden_sets_filters(recording):
    """End of the carrier chain, keylessly: load the REAL golden file, run it through the REAL
    `evaluate`, and assert the filtered questions arrive filtered at the search boundary."""
    from stigmergy.index import golden

    questions = golden.load_golden(str(run_retrieval.ROOT / "evals" / "retrieval_golden.json"))
    golden.evaluate(questions, run_retrieval.make_arm_rankings(object(), object(), 5), k=5)

    sent = [c["filters"] for c in recording.calls]
    assert len(sent) == len(questions)
    assert sum(1 for f in sent if f) >= 9
    assert {tuple(f) for f in sent if f} == {("entity",)}
