"""`repair.store`: the one write, and the reads every surface draws from.

The store decides nothing and authorizes nothing, and it holds no state machine either — a row is
written once, when the removal has already landed, and never transitions. So what is worth pinning
is exactly what a caller cannot see for itself: that a row comes back the way it went in, and that
the reads a console draws a part-to-whole from count the WHOLE table rather than a page of it.

**Rows this version cannot write are still read.** A deployed database holds the elective repair
loop's rows under three retired kinds and two statuses nothing records any more, so the reads are
exercised against those too — through raw SQL, deliberately: there is no writer left to make one,
and a test that could only construct what the current writer produces would go green the day the
console stopped rendering the history this table was kept for.
"""
from stigmergy.repair import schema, store

OPS = [{"op": schema.DELETE_OP_NAME, "path": "wiki/notes/Superseded Memo.md"},
       {"op": schema.SCRUB_OP_NAME, "path": "wiki/notes/Keeps A Link.md",
        "expected_before_hash": "0" * 64, "planned_after": "---\ntype: note\n---\n\n# Keeps\n"}]

DIFF = ("--- a/wiki/notes/Keeps A Link.md\n+++ b/wiki/notes/Keeps A Link.md\n@@ -3,6 +3,7 @@\n"
        "-related: [\"[[Superseded Memo]]\"]\n+related: []\n")


def _applied(conn, *, commit="cafebabe", rationale="the memo was superseded in January") -> int:
    return store.record_applied(
        conn, target_paths=schema.target_paths(OPS), ops=OPS, rationale=rationale,
        commit=commit, diff=DIFF, model_id="m")


def _legacy(conn, *, kind: str, status: str, key: str = "", error: str = "",
            reason: str = "") -> int:
    """One row of the ELECTIVE repair loop, written the only way it still can be — by hand. Its
    writer is gone, and pinning the reads against a row this version could produce would prove
    nothing about the history the table is kept for."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repairs (kind, status, target_paths, ops, rationale, content_key, "
            "finding_ids, error, reason) VALUES (%s, %s, '[\"wiki/notes/x.md\"]'::jsonb, "
            "'[{\"op\": \"backlink\", \"path\": \"wiki/notes/x.md\", \"link\": \"y\"}]'::jsonb, "
            "'r', %s, '[7]'::jsonb, %s, %s) RETURNING id",
            (kind, status, key, error, reason))
        return cur.fetchone()[0]


# ── the round trip ─────────────────────────────────────────────────────────────────────────────
def test_a_removal_round_trips_every_field_it_was_given(conn):
    """Including the DIFF, which is the one field this table exists to carry: nobody read the prose
    the sweep wrote before it was pushed, so a row that lost it would be a change nobody will ever
    read. The capture that asked carries the same reading and is purged with the retention window;
    this row is not."""
    row = store.repair(conn, _applied(conn))

    assert row["kind"] == schema.KIND_DELETE
    assert row["target_paths"] == ["wiki/notes/Keeps A Link.md",
                                   "wiki/notes/Superseded Memo.md"]
    assert row["ops"] == OPS
    assert row["rationale"] == "the memo was superseded in January"
    assert row["model_id"] == "m"
    assert row["status"] == schema.STATUS_APPLIED
    assert row["applied_commit"] == "cafebabe"
    assert row["diff"] == DIFF
    # The elective loop's columns, present and empty: this row answered no finding and remembers
    # nothing, because a person decided it.
    assert (row["finding_ids"], row["finding_subjects"]) == ([], [])
    assert (row["content_key"], row["reason"], row["error"]) == ("", "", "")


def test_an_unknown_id_is_none_rather_than_an_exception(conn):
    assert store.repair(conn, 999_999) is None


def test_two_removals_of_the_same_pages_both_record(conn):
    """The content key was the elective loop's memory — its whole problem was deriving the same
    repair twice — and a removal carries none. Two rows keyed on nothing must not collide on the
    partial unique index that is still on the column for the rows that have one, and a person who
    asks twice is entitled to two records."""
    first, second = _applied(conn, commit="aaa"), _applied(conn, commit="bbb")

    assert first != second
    assert store.counts_by_status(conn)[schema.STATUS_APPLIED] == 2


# ── the reads a surface draws from ─────────────────────────────────────────────────────────────
def test_counts_by_status_covers_every_status_over_the_whole_table(conn):
    """Every declared status present with zero included, and counted over ALL rows — not a page: a
    surface drawing a part-to-whole from a bounded page would understate history the moment the
    page fills, which is the defect this aggregate exists to replace. The two statuses nothing
    writes any more are still counted, because a deployed database still holds them."""
    assert store.counts_by_status(conn) == {s: 0 for s in schema.STATUSES}

    _applied(conn)
    _applied(conn, commit="two")
    _legacy(conn, kind="edits", status=schema.STATUS_FAILED, key="c", error="the gates refused it")
    _legacy(conn, kind="entity-body", status=schema.STATUS_SKIPPED, reason="the model declined")

    assert store.counts_by_status(conn) == {schema.STATUS_APPLIED: 2, schema.STATUS_FAILED: 1,
                                            schema.STATUS_SKIPPED: 1}


def test_a_retired_kinds_row_is_read_back_whole(conn):
    """The reason this table was kept rather than dropped. A row nothing can write any more still
    comes back with its kind, its status and the sentence that refused it — the console labels the
    kind retired and renders the rest."""
    row = store.repair(conn, _legacy(conn, kind="entity-alias", status=schema.STATUS_FAILED,
                                     key="k", error="the gates refused this repair"))

    assert row["kind"] == "entity-alias"
    assert row["kind"] in schema.RETIRED_KINDS
    assert row["status"] == schema.STATUS_FAILED
    assert row["error"] == "the gates refused this repair"
    assert row["content_key"] == "k"
    assert row["finding_ids"] == [7]


def test_recent_is_newest_first_and_bounded(conn):
    """Newest first and never oldest: this table only grows, and what a reader wants from it is
    what left the corpus most recently."""
    first = _applied(conn, commit="1")
    second = _applied(conn, commit="2")
    third = _applied(conn, commit="3")

    assert [r["id"] for r in store.recent(conn)] == [third, second, first]
    assert [r["id"] for r in store.recent(conn, limit=2)] == [third, second]


def test_recent_can_be_asked_for_one_outcome(conn):
    """The console lists everything and filters by outcome in one query, rather than a second
    SELECT that could come to disagree about the ordering."""
    applied = _applied(conn)
    _legacy(conn, kind="edits", status=schema.STATUS_FAILED, key="b", error="refused")

    assert [r["id"] for r in store.recent(conn, status=schema.STATUS_APPLIED)] == [applied]
    assert [r["status"] for r in store.recent(conn, status=schema.STATUS_FAILED)] == [
        schema.STATUS_FAILED]
