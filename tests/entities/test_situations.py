"""`entities.situations` — which parked rows are an identity decision (pure, over a plain dict —
module docstring: "`classify(row)` takes a plain dict"). No database: `list_pending_situations`/
`get_situation`/`require_situation` are exercised against real Postgres in `test_full_circle_pg.py`
and by `entities.cli`'s own tests; this file is the pure-function layer underneath all of them.
"""
import pytest

from stigmergy.capture import schema
from stigmergy.entities import situations
from stigmergy.entities.errors import EntityError


def _row(*, status=schema.TRIAGE, report=None) -> dict:
    return {"status": status, "report": report or {}}


# ── classify: the current, coded vocabulary ───────────────────────────────────────────────────────
def test_classify_reads_the_declared_situation_code():
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY})
    assert situations.classify(row) == schema.SITUATION_UNRESOLVED_ENTITY


def test_classify_is_empty_for_a_row_not_parked_in_triage():
    row = _row(status=schema.QUEUED, report={schema.SITUATION_KEY:
                                             schema.SITUATION_UNRESOLVED_ENTITY})
    assert situations.classify(row) == ""


def test_classify_is_empty_for_an_unrecognized_situation_code():
    row = _row(report={schema.SITUATION_KEY: "some-future-situation-this-code-does-not-know"})
    assert situations.classify(row) == ""


def test_classify_handles_a_row_with_no_report_at_all():
    assert situations.classify({"status": schema.TRIAGE}) == ""
    assert situations.classify({}) == ""
    assert situations.classify(None) == ""


# ── classify: the legacy fallback (the shape that predates the coded vocabulary) ─────────────────
def test_classify_falls_back_to_the_legacy_which_entity_is_prefix():
    """The legacy hint shape predates `schema.SITUATION_KEY` — `report.triage_entity`'s prefix."""
    row = _row(report={"open_question": "which entity is this material about? Candidates: ..."})
    assert situations.classify(row) == schema.SITUATION_UNRESOLVED_ENTITY


def test_classify_falls_back_to_the_legacy_where_does_prefix():
    """The other legacy shape: `open_question: "where does a person page belong?"`."""
    row = _row(report={"open_question": "where does a person page belong?"})
    assert situations.classify(row) == schema.SITUATION_UNSUPPORTED_TYPE


def test_classify_prefers_the_coded_situation_over_the_legacy_prefix_when_both_are_present():
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNSUPPORTED_TYPE,
                      "open_question": "which entity is this about?"})
    assert situations.classify(row) == schema.SITUATION_UNSUPPORTED_TYPE


def test_classify_is_case_and_whitespace_tolerant_on_the_legacy_prefix():
    row = _row(report={"open_question": "  WHICH ENTITY is this about?"})
    assert situations.classify(row) == schema.SITUATION_UNRESOLVED_ENTITY


def test_classify_does_not_match_an_unrelated_question():
    row = _row(report={"open_question": "please clarify the date range"})
    assert situations.classify(row) == ""


# ── subject_of: the fact BESIDE the sentence ──────────────────────────────────────────────────────
def test_subject_of_unresolved_entity_is_the_name():
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                      schema.SITUATION_NAME_KEY: "Acme Corp"})
    assert situations.subject_of(row) == "Acme Corp"


def test_subject_of_unsupported_type_is_the_judged_type():
    row = _row(report={schema.SITUATION_KEY: schema.SITUATION_UNSUPPORTED_TYPE,
                      schema.SITUATION_TYPE_KEY: "person"})
    assert situations.subject_of(row) == "person"


def test_subject_of_a_legacy_row_is_honestly_empty():
    """A legacy row carries no `entity_name`/`judged_type` at all — answered honestly rather than
    parsed back out of a sentence (module docstring)."""
    row = _row(report={"open_question": "where does a person page belong?"})
    assert situations.subject_of(row) == ""


# ── require_situation: the write guard (three distinct refusals) ─────────────────────────────────
class _FakeConn:
    """Not a mock of an interface — `require_situation`/`get_situation` take `conn` only to hand it
    to `queue.get_submission_trace`, which this stubs at the module level below instead. Kept for
    readability at the call site; carries no behaviour of its own."""


def test_require_situation_refuses_a_nonexistent_row(monkeypatch):
    monkeypatch.setattr(situations.queue, "get_submission_trace", lambda conn, sid: None)
    with pytest.raises(EntityError, match="does not exist"):
        situations.require_situation(_FakeConn(), 999, action="approve")


def test_require_situation_refuses_a_row_not_parked_in_triage(monkeypatch):
    monkeypatch.setattr(situations.queue, "get_submission_trace",
                       lambda conn, sid: {"status": schema.CLAIMED, "report": {}})
    with pytest.raises(EntityError, match="claimed"):
        situations.require_situation(_FakeConn(), 41, action="approve")


def test_require_situation_refuses_a_triage_row_that_is_not_an_identity_situation(monkeypatch):
    monkeypatch.setattr(situations.queue, "get_submission_trace",
                       lambda conn, sid: {"status": schema.TRIAGE, "report": {}})
    with pytest.raises(EntityError, match="not an entity situation"):
        situations.require_situation(_FakeConn(), 41, action="approve")


def test_require_situation_returns_the_row_when_it_really_is_a_pending_situation(monkeypatch):
    """The benign twin of the three refusals above."""
    monkeypatch.setattr(situations.queue, "get_submission_trace",
                       lambda conn, sid: {"status": schema.TRIAGE, "id": 41,
                                          "report": {schema.SITUATION_KEY:
                                                    schema.SITUATION_UNRESOLVED_ENTITY,
                                                    schema.SITUATION_NAME_KEY: "Acme"}})
    row = situations.require_situation(_FakeConn(), 41, action="approve")
    assert row["situation"] == schema.SITUATION_UNRESOLVED_ENTITY
    assert row["subject"] == "Acme"
