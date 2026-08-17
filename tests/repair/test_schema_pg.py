"""`repair_proposals`' DDL: idempotent, and its two vocabularies really are constraints.

A CHECK constraint that exists in the code's tuple and not in the column is drift nobody sees
until a write fails in production, so both are asked of the real database rather than of the SQL
string that was meant to create them.
"""
import psycopg
import pytest

from stigmergy.repair import schema, store


def test_ensure_repair_schema_is_idempotent(conn):
    schema.ensure_repair_schema(conn)
    schema.ensure_repair_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM repair_proposals")
        assert cur.fetchone()[0] == 0


def test_the_status_vocabulary_is_a_real_check_constraint(conn):
    proposal_id = store.insert_proposal(
        conn, run_id=1, finding_ids=[1], target_paths=["wiki/notes/x.md"],
        ops=[{"op": "backlink", "path": "wiki/notes/x.md", "link": "y", "note": ""}],
        rationale="r", content_key="k1")
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute("UPDATE repair_proposals SET status = 'whatever' WHERE id = %s",
                    (proposal_id,))
    conn.rollback()


def test_the_kind_vocabulary_is_a_real_check_constraint(conn):
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO repair_proposals (kind, target_paths, ops, content_key) "
            "VALUES ('rewrite', '[]'::jsonb, '[]'::jsonb, 'k')")
    conn.rollback()


def test_every_status_the_code_can_write_is_one_the_column_accepts(conn):
    """The benign twin for the two refusals above: a vocabulary that rejected a value the code
    legitimately produces would be a gate on the WRONG side, and this is the test that would have
    caught it — every status in the tuple is written for real."""
    proposal_id = store.insert_proposal(
        conn, run_id=1, finding_ids=[], target_paths=["wiki/notes/x.md"],
        ops=[{"op": "backlink", "path": "wiki/notes/x.md", "link": "y", "note": ""}],
        rationale="r", content_key="k2")
    for status in schema.STATUSES:
        with conn.cursor() as cur:
            cur.execute("UPDATE repair_proposals SET status = %s WHERE id = %s",
                        (status, proposal_id))
        assert store.proposal(conn, proposal_id)["status"] == status


def test_only_one_pending_proposal_per_content_key(conn):
    """The UNIQUE index, asked of the database. Two propose runs deriving the same repair must not
    put the same question in front of a steward twice."""
    args = {"run_id": 1, "finding_ids": [], "target_paths": ["wiki/notes/x.md"],
            "ops": [{"op": "backlink", "path": "wiki/notes/x.md", "link": "y", "note": ""}],
            "rationale": "r", "content_key": "same-key"}
    store.insert_proposal(conn, **args)
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.insert_proposal(conn, **args)
    conn.rollback()


def test_a_rejected_key_may_be_proposed_again_because_the_index_is_pending_only(conn):
    """The benign twin, and the design decision it protects: the dismissal memory is enforced in
    `proposer.py`, which SKIPS a known key, not by the database, which would REFUSE it. Deciding
    to re-propose after a rejection is a human's to make, and it must not arrive as an
    IntegrityError nobody can act on."""
    args = {"run_id": 1, "finding_ids": [], "target_paths": ["wiki/notes/x.md"],
            "ops": [{"op": "backlink", "path": "wiki/notes/x.md", "link": "y", "note": ""}],
            "rationale": "r", "content_key": "declined-once"}
    first = store.insert_proposal(conn, **args)
    store.mark_decided(conn, first, status=schema.STATUS_REJECTED, decided_by="s", notes="no")
    assert store.insert_proposal(conn, **args) != first


# ── content_key: what identifies a proposal ───────────────────────────────────────────────────
def test_the_content_key_is_what_a_proposal_would_do_not_the_order_it_says_it_in():
    ops_one = [{"op": "backlink", "path": "a.md", "link": "B", "note": ""},
               {"op": "backlink", "path": "b.md", "link": "A", "note": ""}]
    assert schema.content_key(ops_one) == schema.content_key(list(reversed(ops_one)))


def test_a_reworded_note_is_the_same_question_and_keys_the_same():
    """The dismissal memory's whole value: a steward who declined "add this callout to this page"
    must not meet the same proposal tomorrow with the sentence rephrased."""
    base = [{"op": "contradiction", "path": "a.md", "link": "B", "note": "these disagree"}]
    reworded = [{"op": "contradiction", "path": "a.md", "link": "B", "note": "they contradict"}]
    assert schema.content_key(base) == schema.content_key(reworded)


def test_a_different_page_or_link_or_kind_is_a_different_question():
    base = [{"op": "backlink", "path": "a.md", "link": "B", "note": ""}]
    for changed in ({"op": "backlink", "path": "z.md", "link": "B", "note": ""},
                    {"op": "backlink", "path": "a.md", "link": "Z", "note": ""},
                    {"op": "overlap", "path": "a.md", "link": "B", "note": "n"}):
        assert schema.content_key(base) != schema.content_key([changed])


def test_declared_edits_translates_the_stored_op_into_the_edit_validators_vocabulary():
    """One translation, used by BOTH validations — the propose-time one and the apply-time one —
    so the two cannot come to judge different things."""
    ops = [{"op": "overlap", "path": "wiki/notes/x.md", "link": "Y", "note": "same ground"}]
    assert schema.declared_edits(ops) == [
        {"kind": "overlap", "path": "wiki/notes/x.md", "link": "Y", "note": "same ground"}]


def test_target_paths_are_deduplicated_and_sorted():
    ops = [{"op": "backlink", "path": "b.md", "link": "A", "note": ""},
           {"op": "overlap", "path": "b.md", "link": "C", "note": "n"},
           {"op": "backlink", "path": "a.md", "link": "B", "note": ""}]
    assert schema.target_paths(ops) == ["a.md", "b.md"]
