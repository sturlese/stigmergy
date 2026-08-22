"""`repairs`' DDL: idempotent, its two vocabularies really are constraints, and the two migrations
that carry a deployed database from the lifecycle ADR 044 removed.

A CHECK constraint that exists in the code's tuple and not in the column is drift nobody sees
until a write fails in production, so both are asked of the real database rather than of the SQL
string that was meant to create them. The migrations are asked the same way, from the state a
deployment upgraded from the previous release is actually in — which is the only state that
reproduces them.
"""
import psycopg
import pytest

from stigmergy.repair import deletion, schema, store

OPS = [{"op": "backlink", "path": "wiki/notes/x.md", "link": "y", "note": ""}]


def _applied(conn, *, key: str, kind: str = schema.KIND_EDITS) -> int:
    return store.record_applied(conn, run_id=1, finding_ids=[1], target_paths=["wiki/notes/x.md"],
                                ops=OPS, rationale="r", content_key=key, commit="c", diff="d",
                                kind=kind)


def test_ensure_repair_schema_is_idempotent(conn):
    schema.ensure_repair_schema(conn)
    schema.ensure_repair_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM repairs")
        assert cur.fetchone()[0] == 0


def test_the_status_vocabulary_is_a_real_check_constraint(conn):
    repair_id = _applied(conn, key="k1")
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute("UPDATE repairs SET status = 'whatever' WHERE id = %s", (repair_id,))
    conn.rollback()


def test_the_kind_vocabulary_is_a_real_check_constraint(conn):
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO repairs (kind, target_paths, ops, content_key, status) "
            f"VALUES ('rewrite', '[]'::jsonb, '[]'::jsonb, 'k', '{schema.STATUS_APPLIED}')")
    conn.rollback()


def test_every_status_the_code_can_write_is_one_the_column_accepts(conn):
    """The benign twin for the two refusals above: a vocabulary that rejected a value the code
    legitimately produces would be a gate on the WRONG side, and this is the test that would have
    caught it.

    Written through the THREE WRITERS rather than by UPDATE-ing one row through the tuple, and that
    is the change ADR 044 made here: a row is written once, when the attempt is already over, and
    never transitions. A test that moved one row through every status would be exercising a
    lifecycle the code no longer has."""
    _applied(conn, key="status-applied")
    store.record_failed(conn, run_id=1, finding_ids=[], target_paths=["wiki/notes/x.md"], ops=OPS,
                        rationale="r", content_key="status-failed", error="the gates refused it")
    store.record_skipped(conn, run_id=1, finding_ids=[], reason="no kind can express this")

    assert {row["status"] for row in store.recent(conn)} == set(schema.STATUSES)


def test_every_kind_the_code_can_write_is_one_the_column_accepts(conn):
    """The benign twin of the refusal above, and the half `STATUSES` already had: a vocabulary
    that rejected a value the code legitimately produces is a gate on the WRONG side. Every kind
    in the tuple is INSERTED for real, so a fifth kind added to the code and not to the column
    fails here rather than in production the first night the pass derives one."""
    for n, kind in enumerate(schema.KINDS):
        repair_id = _applied(conn, key=f"kind-{n}", kind=kind)
        assert store.repair(conn, repair_id)["kind"] == kind


def test_a_table_that_predates_a_kind_gains_it_when_the_ddl_runs(conn):
    """OLD BEHAVIOUR: `CREATE TABLE IF NOT EXISTS` carried the kind CHECK inline, so a database
    where the table already existed NEVER widened it — the vocabulary grew in the code, every
    deployed queue kept refusing the new value, and the first repair of that kind would have
    died on an IntegrityError nobody could act on.

    The constraint is narrowed here on purpose: that is exactly the state a deployment upgraded
    from the previous release is in, and nothing else reproduces it."""
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE repairs DROP CONSTRAINT IF EXISTS {schema.KIND_CHECK_NAME}")
        cur.execute(f"ALTER TABLE repairs ADD CONSTRAINT {schema.KIND_CHECK_NAME} "
                    f"CHECK (kind IN ('{schema.KIND_EDITS}'))")

    schema.ensure_repair_schema(conn)

    repair_id = _applied(conn, key="migrated", kind=schema.KIND_ENTITY_BODY)
    assert store.repair(conn, repair_id)["kind"] == schema.KIND_ENTITY_BODY


def test_a_row_that_was_waiting_on_a_person_becomes_a_skip_when_the_ddl_runs(conn):
    """The other half of the same migration problem, and the one ADR 044 introduced: a database
    upgraded from the previous release carries rows whose status is `pending`, `approved` or
    `rejected`, and the new CHECK names none of them. A row that was WAITING on somebody was never
    applied and never refused — it is a repair that did not happen, which is what `skipped` means,
    and the reason says so rather than leaving a status a reader has to know the history of.

    The ORDER is what this test really pins, and `schema._ALL_DDL` calls it load-bearing: the
    status migration runs BEFORE the CHECK swap. Reversed, the swap would refuse the very rows the
    migration exists to fix.

    It drops the constraint first, which is NOT the state a real upgrade starts from — the twin
    below is the one that starts from the real state, and it is the one that caught this.
    """
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE repairs DROP CONSTRAINT IF EXISTS {schema.STATUS_CHECK_NAME}")
        cur.execute("INSERT INTO repairs (kind, target_paths, ops, content_key, status) "
                    "VALUES (%s, '[]'::jsonb, '[]'::jsonb, 'was-waiting', 'pending') "
                    "RETURNING id", (schema.KIND_EDITS,))
        waiting_id = cur.fetchone()[0]

    schema.ensure_repair_schema(conn)

    row = store.repair(conn, waiting_id)
    assert row["status"] == schema.STATUS_SKIPPED
    assert "waiting on a person" in row["reason"]
    assert row["content_key"] == "was-waiting", "the row is migrated, never rewritten"


def test_the_migration_runs_against_the_constraint_the_old_release_actually_left(conn):
    """**Found on the deployment, and this suite had the blind spot written into it.**

    OLD BEHAVIOUR: the whole DDL sequence aborted with `CheckViolation` on any database carrying a
    row whose status was retired. `ALTER TABLE ... RENAME` brings the table's CONSTRAINTS with it,
    so an upgraded database still has `repair_proposals_status_check` — which allows
    `pending|approved|rejected|applied|failed` and does NOT allow `skipped`. The migration's own
    `UPDATE ... SET status = 'skipped'` therefore violated the constraint that was still standing,
    before the swap that would have replaced it ever ran. The server exited 2 on every start with
    "cannot read the index (CheckViolation)", and the app crash-looped.

    The test above passes because it DROPS the constraint first — it constructs the one state in
    which the migration works and asserts that. This one starts where a real upgrade starts: the
    old constraint in place, a retired row under it.

    The fix is one statement of ordering: the legacy constraint is dropped BEFORE the migration
    writes a value it does not permit, and the swap adds the new one after. That is three steps,
    not two, and it cannot be expressed as one atomic drop-and-add around an UPDATE."""
    with conn.cursor() as cur:
        # Exactly what `ALTER TABLE repair_proposals RENAME TO repairs` leaves behind.
        cur.execute(f"ALTER TABLE repairs DROP CONSTRAINT IF EXISTS {schema.STATUS_CHECK_NAME}")
        cur.execute("ALTER TABLE repairs DROP CONSTRAINT IF EXISTS repair_proposals_status_check")
        cur.execute("ALTER TABLE repairs ADD CONSTRAINT repair_proposals_status_check "
                    "CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'failed'))")
        cur.execute("INSERT INTO repairs (kind, target_paths, ops, content_key, status) "
                    "VALUES (%s, '[]'::jsonb, '[]'::jsonb, 'was-rejected', 'rejected') "
                    "RETURNING id", (schema.KIND_EDITS,))
        rejected_id = cur.fetchone()[0]

    schema.ensure_repair_schema(conn)          # red before the fix: CheckViolation

    assert store.repair(conn, rejected_id)["status"] == schema.STATUS_SKIPPED
    with conn.cursor() as cur:
        cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                        WHERE conrelid = 'repairs'::regclass AND contype = 'c'""")
        defs = " ".join(r[0] for r in cur.fetchall())
    assert schema.STATUS_SKIPPED in defs, "the new vocabulary never got installed"
    assert "'pending'" not in defs, "the legacy constraint survived the swap"


def test_the_ddl_is_still_idempotent_on_a_database_that_has_already_migrated(conn):
    """The benign twin of the fix: dropping the legacy constraint before the migration must not
    make a second run do anything. Every deployed process runs this DDL at every start."""
    schema.ensure_repair_schema(conn)
    applied_id = _applied(conn, key="already-here")

    schema.ensure_repair_schema(conn)

    assert store.repair(conn, applied_id)["status"] == schema.STATUS_APPLIED
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM pg_constraint
                        WHERE conrelid = 'repairs'::regclass AND contype = 'c'""")
        assert cur.fetchone()[0] == 2, "a repeat run left a duplicate or a missing CHECK"


def test_one_repair_per_content_key_WHATEVER_its_outcome(conn):
    """The UNIQUE index, asked of the database, and its predicate is the change ADR 044 made: it
    used to be `WHERE status = 'pending'`, so a decided row freed its key for a second question.
    Nobody is asked now, so the key is the whole of the memory — an applied repair and a refused
    one both hold theirs forever, and a second pass deriving either meets the index rather than
    pushing a second commit for an edit already in the corpus.

    A FAILED row against an APPLIED one, deliberately: same key, different status, and the old
    partial index would have let this through."""
    _applied(conn, key="same-key")

    with pytest.raises(store.ContentKeyTaken):
        store.record_failed(conn, run_id=1, finding_ids=[], target_paths=["wiki/notes/x.md"],
                            ops=OPS, rationale="r", content_key="same-key",
                            error="the gates refused it")


def test_the_index_the_old_lifecycle_needed_is_gone_by_name(conn):
    """The dropped index, asked of the database. Left in place it would go on enforcing uniqueness
    over rows whose status nothing can create — harmless, and a reader finding it would reasonably
    conclude this table still has a lifecycle."""
    with conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'repairs'")
        names = {r[0] for r in cur.fetchall()}

    assert "repair_proposals_pending_key_idx" not in names
    assert "repairs_content_key_idx" in names


# ── content_key: what identifies a repair ─────────────────────────────────────────────────────
def test_the_content_key_is_what_a_repair_would_do_not_the_order_it_says_it_in():
    ops_one = [{"op": "backlink", "path": "a.md", "link": "B", "note": ""},
               {"op": "backlink", "path": "b.md", "link": "A", "note": ""}]
    assert schema.content_key(ops_one) == schema.content_key(list(reversed(ops_one)))


def test_a_reworded_note_is_the_same_question_and_keys_the_same():
    """The memory's whole value: a callout this loop already added to a page must not be added a
    second time tomorrow with the sentence rephrased — which under ADR 044 is not a repeated
    question, it is a repeated commit."""
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
    """One translation, used by BOTH validations — the derive-time one and the apply-time one, run
    against two different trees — so the two cannot come to judge different things."""
    ops = [{"op": "overlap", "path": "wiki/notes/x.md", "link": "Y", "note": "same ground"}]
    assert schema.declared_edits(ops) == [
        {"kind": "overlap", "path": "wiki/notes/x.md", "link": "Y", "note": "same ground"}]


def test_target_paths_are_deduplicated_and_sorted():
    ops = [{"op": "backlink", "path": "b.md", "link": "A", "note": ""},
           {"op": "overlap", "path": "b.md", "link": "C", "note": "n"},
           {"op": "backlink", "path": "a.md", "link": "B", "note": ""}]
    assert schema.target_paths(ops) == ["a.md", "b.md"]


# ── the second kind's key and its stored shape ────────────────────────────────────────────────
def test_a_redrafted_body_is_the_same_question():
    """The memory's whole value, for the kind whose op is PROSE. A page whose body this loop has
    already written must not be rewritten tomorrow with the prose rearranged — the body is excluded
    from the key for exactly the reason a callout's `note` is, and the reason is stronger here
    because the model can rephrase indefinitely and nobody is reading the difference."""
    first = [{"op": schema.KIND_ENTITY_BODY, "path": "wiki/entities/X.md",
              "body_markdown": "## What / Who\n\nOne reading.\n", "role": ""}]
    second = [{"op": schema.KIND_ENTITY_BODY, "path": "wiki/entities/X.md",
               "body_markdown": "## What / Who\n\nA different reading entirely.\n",
               "role": "a role this time"}]

    assert (schema.content_key(first, kind=schema.KIND_ENTITY_BODY)
            == schema.content_key(second, kind=schema.KIND_ENTITY_BODY))


def test_a_body_draft_for_a_different_page_is_a_different_question():
    """The benign twin: the key must still SEPARATE pages, or one written body would silence every
    entity page in the corpus."""
    here = [{"op": schema.KIND_ENTITY_BODY, "path": "wiki/entities/X.md", "body_markdown": "b",
             "role": ""}]
    there = [{"op": schema.KIND_ENTITY_BODY, "path": "wiki/entities/Y.md", "body_markdown": "b",
              "role": ""}]

    assert (schema.content_key(here, kind=schema.KIND_ENTITY_BODY)
            != schema.content_key(there, kind=schema.KIND_ENTITY_BODY))


def test_the_two_kinds_are_two_questions_about_the_same_page():
    """`kind` is hashed into the key, so an additive edit and a body draft naming one page cannot
    suppress each other — they are different changes to the same page."""
    ops = [{"op": "backlink", "path": "wiki/entities/X.md", "link": "Y", "note": ""}]
    assert (schema.content_key(ops, kind=schema.KIND_EDITS)
            != schema.content_key(ops, kind=schema.KIND_ENTITY_BODY))


def test_each_kinds_stored_op_shape_is_named_and_they_do_not_overlap():
    """The shapes, pinned where they are declared. Two readers reshape an op — the console's
    cleaner and the applier — and a reader that assumed the additive shape for a body draft
    rendered an empty cell where the draft should have been. That mattered when a steward read the
    draft before deciding; under ADR 044 the console IS the reading, and it happens after the
    commit."""
    assert schema.EDIT_OP_FIELDS[0] == schema.ENTITY_BODY_OP_FIELDS[0] == schema.OP_KIND_KEY
    assert schema.DELETE_OP_FIELDS[0] == schema.SCRUB_OP_FIELDS[0] == schema.OP_KIND_KEY
    assert set(schema.EDIT_OP_FIELDS) & set(schema.ENTITY_BODY_OP_FIELDS) == {"op", "path"}
    assert set(schema.EDIT_OP_FIELDS) & set(schema.SCRUB_OP_FIELDS) == {"op", "path"}
    assert "body_markdown" not in schema.EDIT_OP_FIELDS
    assert "link" not in schema.ENTITY_BODY_OP_FIELDS
    assert "planned_after" not in schema.DELETE_OP_FIELDS, (
        "a removal names a page and stores no bytes — there is nothing left to write")


# ── the third kind's key: the question is WHICH PAGES GO ──────────────────────────────────────
def _delete_ops(*paths, scrubbed=()):
    return ([{"op": deletion.OP_DELETE, "path": p} for p in paths]
            + [{"op": deletion.OP_SCRUB, "path": p, "expected_before_hash": "h",
                "planned_after": "bytes"} for p in scrubbed])


def test_a_deletion_is_the_same_question_however_the_corpus_has_moved_around_it():
    """OLD BEHAVIOUR: `content_key` hashed every op, so the SCRUB set was part of a deletion's
    identity — and the scrub set is a fact about the rest of the corpus, not about the question.
    A deletion this loop had already settled would have been derived again the moment somebody
    added a link to the doomed page, under a key that had silently changed."""
    before = _delete_ops("wiki/notes/Doomed.md", scrubbed=["wiki/notes/A.md"])
    after = _delete_ops("wiki/notes/Doomed.md", scrubbed=["wiki/notes/A.md", "wiki/notes/B.md"])

    assert (schema.content_key(before, kind=schema.KIND_DELETE)
            == schema.content_key(after, kind=schema.KIND_DELETE))


def test_deleting_a_different_page_is_a_different_question():
    """The benign twin: the key still separates deletion sets, or one settled sweep would silence
    every deletion in the corpus."""
    assert (schema.content_key(_delete_ops("wiki/notes/A.md"), kind=schema.KIND_DELETE)
            != schema.content_key(_delete_ops("wiki/notes/B.md"), kind=schema.KIND_DELETE))
    assert (schema.content_key(_delete_ops("wiki/notes/A.md"), kind=schema.KIND_DELETE)
            != schema.content_key(_delete_ops("wiki/notes/A.md", "wiki/notes/B.md"),
                                  kind=schema.KIND_DELETE))
