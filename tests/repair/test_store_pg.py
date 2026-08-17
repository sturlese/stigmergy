"""`repair.store`: the round trips, and the state transitions that must be races rather than
read-then-writes.

The store decides nothing and authorizes nothing, so what is worth pinning is exactly the two
things a caller cannot see for itself: that a row comes back the way it went in, and that a
transition which is no longer legal reports so instead of happening quietly.
"""
import pytest

from stigmergy.repair import schema, store

OPS = [{"op": "contradiction", "path": "wiki/notes/a.md", "link": "B", "note": "they disagree"},
       {"op": "contradiction", "path": "wiki/notes/b.md", "link": "A", "note": "they disagree"}]


def _insert(conn, *, key="k", findings=(7, 9)) -> int:
    return store.insert_proposal(
        conn, run_id=42, finding_ids=list(findings), target_paths=schema.target_paths(OPS),
        ops=OPS, rationale="because the pages disagree", content_key=key, model_id="m")


def test_a_proposal_round_trips_every_field_it_was_given(conn):
    proposal_id = _insert(conn)
    row = store.proposal(conn, proposal_id)

    assert row["run_id"] == 42
    assert row["finding_ids"] == [7, 9]
    assert row["kind"] == schema.KIND_EDITS
    assert row["target_paths"] == ["wiki/notes/a.md", "wiki/notes/b.md"]
    assert row["ops"] == OPS
    assert row["rationale"] == "because the pages disagree"
    assert row["content_key"] == "k"
    assert row["model_id"] == "m"
    assert row["status"] == schema.STATUS_PENDING
    assert (row["decided_by"], row["notes"], row["applied_commit"], row["error"]) == ("", "", "",
                                                                                      "")


def test_an_unknown_id_is_none_rather_than_an_exception(conn):
    assert store.proposal(conn, 999_999) is None


def test_pending_proposals_lists_only_what_waits_on_a_steward(conn):
    waiting = _insert(conn, key="waiting")
    decided = _insert(conn, key="decided")
    store.mark_decided(conn, decided, status=schema.STATUS_REJECTED, decided_by="s", notes="no")

    assert [row["id"] for row in store.pending_proposals(conn)] == [waiting]
    assert [row["id"] for row in store.recent_decided(conn)] == [decided]


def test_marking_decided_records_the_verdict_the_actor_and_the_reason(conn):
    proposal_id = _insert(conn)
    assert store.mark_decided(conn, proposal_id, status=schema.STATUS_REJECTED,
                              decided_by="steward@example.com", notes="already linked")
    row = store.proposal(conn, proposal_id)
    assert row["status"] == schema.STATUS_REJECTED
    assert row["decided_by"] == "steward@example.com"
    assert row["notes"] == "already linked"
    assert row["decided_at"] is not None


def test_a_second_decision_on_the_same_proposal_reports_the_race_it_lost(conn):
    """Two stewards pressing Approve in the same second must not both get a proposal to apply.
    The `WHERE status = 'pending'` in the UPDATE is what decides it, and the loser is TOLD."""
    proposal_id = _insert(conn)
    assert store.mark_decided(conn, proposal_id, status=schema.STATUS_APPROVED, decided_by="a")
    assert not store.mark_decided(conn, proposal_id, status=schema.STATUS_APPROVED,
                                  decided_by="b")
    assert store.proposal(conn, proposal_id)["decided_by"] == "a"


@pytest.mark.parametrize("status", [schema.STATUS_APPLIED, schema.STATUS_FAILED,
                                    schema.STATUS_PENDING])
def test_an_outcome_is_not_a_verdict_a_steward_can_hand_in(conn, status):
    """`applied` and `failed` are legal VALUES of the column and illegal VERDICTS, so the CHECK
    constraint cannot tell them apart from a decision — it would let a caller stamp a proposal
    `applied` with no commit behind it. Two vocabularies, and this is the only place the
    difference is enforceable."""
    proposal_id = _insert(conn)
    with pytest.raises(ValueError, match="not a verdict"):
        store.mark_decided(conn, proposal_id, status=status, decided_by="s")
    assert store.proposal(conn, proposal_id)["status"] == schema.STATUS_PENDING


@pytest.mark.parametrize("status", schema.DECIDABLE)
def test_both_real_verdicts_are_accepted(conn, status):
    """The benign twin: a guard that rejected everything would pass the test above and make the
    review lane unable to record any decision at all."""
    assert store.mark_decided(conn, _insert(conn, key=status), status=status, decided_by="s",
                              notes="because")


def test_only_an_approved_proposal_may_become_applied(conn):
    """Nothing reaches `applied` without having passed through a human — asserted against the SQL
    rather than against the caller's discipline."""
    proposal_id = _insert(conn)
    assert not store.mark_applied(conn, proposal_id, "deadbeef")
    store.mark_decided(conn, proposal_id, status=schema.STATUS_APPROVED, decided_by="s")
    assert store.mark_applied(conn, proposal_id, "deadbeef")

    row = store.proposal(conn, proposal_id)
    assert (row["status"], row["applied_commit"]) == (schema.STATUS_APPLIED, "deadbeef")


def test_a_failed_apply_keeps_the_reason_and_does_not_go_back_to_pending(conn):
    """The design decision, pinned: a failed apply stays visible as `failed` with the reason. A
    silent revert to `pending` would hide from an operator that a gate refused something a steward
    had already approved."""
    proposal_id = _insert(conn)
    store.mark_decided(conn, proposal_id, status=schema.STATUS_APPROVED, decided_by="s")
    assert store.mark_failed(conn, proposal_id, "the gates refused this repair: secrets/hit")

    row = store.proposal(conn, proposal_id)
    assert row["status"] == schema.STATUS_FAILED
    assert "secrets/hit" in row["error"]
    assert row["decided_by"] == "s", "the approval itself is history and must not be erased"


def test_known_content_keys_is_the_dismissal_memory_and_holds_every_status(conn):
    """The point of the whole column: a REJECTED key is remembered exactly as an applied one is,
    which is what makes "reviewed and declined" a durable fact rather than a shrug."""
    rejected = _insert(conn, key="rejected-key")
    store.mark_decided(conn, rejected, status=schema.STATUS_REJECTED, decided_by="s", notes="no")
    applied = _insert(conn, key="applied-key")
    store.mark_decided(conn, applied, status=schema.STATUS_APPROVED, decided_by="s")
    store.mark_applied(conn, applied, "cafe")
    _insert(conn, key="pending-key")

    assert store.known_content_keys(conn) == {"rejected-key", "applied-key", "pending-key"}
