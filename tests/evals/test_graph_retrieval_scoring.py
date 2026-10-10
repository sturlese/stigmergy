"""`evals/run_graph_retrieval.py`'s scorer: recall counts the hits, reach also the sources they cite."""
from evals import run_graph_retrieval

NOTE = "wiki/notes/Pilot results.md"
SOURCE = "sources/2026/05/30000000-0000-4000-8000-000000000001.md"
OTHER = "wiki/notes/Kickoff.md"


def test_an_expected_source_a_hit_cites_is_reached_but_not_recalled():
    case = {"id": "q", "family": "answerable", "shape": "lookup", "expect": {"pages": [SOURCE, OTHER]}}
    hits = [{"path": NOTE}, {"path": "wiki/entities/Acme.md"}]
    row = run_graph_retrieval.score_question(case, hits, [8], set(), cited={NOTE: {SOURCE}})
    assert row["recall@8"] == 0.0
    assert row["reach@8"] == 0.5
    summary = run_graph_retrieval.aggregate([row], [8])["families"]["lookup"]
    assert summary["recall@8"] == 0.0 and summary["reach@8"] == 0.5
