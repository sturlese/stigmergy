"""`repairs`' DDL: idempotent, its two vocabularies really are constraints, and the migrations that
carry a deployed database from the lifecycle the capture-is-the-approval change removed.

A CHECK constraint that exists in the code's tuple and not in the column is drift nobody sees
until a write fails in production, so both are asked of the real database rather than of the SQL
string that was meant to create them. The migrations are asked the same way, from the state a
deployment upgraded from a previous release is actually in — which is the only state that
reproduces them.

**The vocabularies are wider than what code writes, and that gap is the point.** A deployed table
holds the elective repair loop's rows under three kinds nothing can write any more, and `ALTER
TABLE ... ADD CONSTRAINT ... CHECK` validates the rows already there. A CHECK narrowed to
`WRITABLE_KIND` would therefore abort the whole DDL sequence on every start of an upgraded
deployment — the same shape as the crash-loop the status migration below reproduces.
"""
import psycopg
import pytest

from stigmergy.repair import deletion, schema, store

OPS = [{"op": schema.DELETE_OP_NAME, "path": "wiki/notes/x.md"}]


def _applied(conn, *, commit: str = "c") -> int:
    return store.record_applied(conn, target_paths=["wiki/notes/x.md"], ops=OPS, rationale="r",
                                commit=commit, diff="d")


def _row(conn, *, kind: str, status: str, key: str = "") -> int:
    """One row written by hand, in a kind or a status the current writer cannot produce — the only
    way the retired half of either vocabulary can be exercised at all."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO repairs (kind, target_paths, ops, content_key, status) "
                    "VALUES (%s, '[]'::jsonb, '[]'::jsonb, %s, %s) RETURNING id",
                    (kind, key, status))
        return cur.fetchone()[0]


def test_ensure_repair_schema_is_idempotent(conn):
    schema.ensure_repair_schema(conn)
    schema.ensure_repair_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM repairs")
        assert cur.fetchone()[0] == 0


def test_the_status_vocabulary_is_a_real_check_constraint(conn):
    repair_id = _applied(conn)
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute("UPDATE repairs SET status = 'whatever' WHERE id = %s", (repair_id,))
    conn.rollback()


def test_the_kind_vocabulary_is_a_real_check_constraint(conn):
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO repairs (kind, target_paths, ops, content_key, status) "
            f"VALUES ('rewrite', '[]'::jsonb, '[]'::jsonb, 'k', '{schema.STATUS_APPLIED}')")
    conn.rollback()


def test_every_kind_a_deployed_row_may_carry_is_one_the_column_accepts(conn):
    """The benign twin of the refusal above, and the half that decides whether an upgrade starts at
    all: every kind in `KINDS` is INSERTED for real, so a retired kind dropped from the tuple fails
    here rather than aborting the DDL on a production database that still holds one."""
    for n, kind in enumerate(schema.KINDS):
        assert store.repair(conn, _row(conn, kind=kind, status=schema.STATUS_APPLIED,
                                       key=f"kind-{n}"))["kind"] == kind


def test_every_status_a_deployed_row_may_carry_is_one_the_column_accepts(conn):
    """The same argument for the other vocabulary. Only `applied` is written now; the other two are
    the elective loop's, and a table that refused them could not be migrated into."""
    for n, status in enumerate(schema.STATUSES):
        assert store.repair(conn, _row(conn, kind=schema.KIND_DELETE, status=status,
                                       key=f"status-{n}"))["status"] == status


def test_a_table_that_predates_a_kind_gains_it_when_the_ddl_runs(conn):
    """OLD BEHAVIOUR: `CREATE TABLE IF NOT EXISTS` carried the kind CHECK inline, so a database
    where the table already existed NEVER widened it — the vocabulary changed in the code, every
    deployed database kept refusing the value, and the first row of the new shape would have died
    on an IntegrityError nobody could act on.

    The constraint is narrowed here on purpose: that is exactly the state a deployment upgraded
    from an older release is in, and nothing else reproduces it."""
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE repairs DROP CONSTRAINT IF EXISTS {schema.KIND_CHECK_NAME}")
        cur.execute(f"ALTER TABLE repairs ADD CONSTRAINT {schema.KIND_CHECK_NAME} "
                    f"CHECK (kind IN ('{schema.RETIRED_KINDS[0]}'))")

    schema.ensure_repair_schema(conn)

    assert store.repair(conn, _applied(conn))["kind"] == schema.KIND_DELETE


def test_the_kind_check_still_admits_the_rows_the_elective_loop_left(conn):
    """The DDL run against a table that already HOLDS a retired kind, which is the state every
    deployed database is in. `ADD CONSTRAINT ... CHECK` validates existing rows, so a `KINDS`
    narrowed to what code writes would abort here — with the whole sequence inside one advisory
    lock, on every process start."""
    legacy = _row(conn, kind=schema.RETIRED_KINDS[0], status=schema.STATUS_APPLIED, key="legacy")
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE repairs DROP CONSTRAINT IF EXISTS {schema.KIND_CHECK_NAME}")

    schema.ensure_repair_schema(conn)

    assert store.repair(conn, legacy)["kind"] == schema.RETIRED_KINDS[0]


def test_a_row_that_was_waiting_on_a_person_becomes_a_skip_when_the_ddl_runs(conn):
    """The other half of the same migration problem, and the one the capture-is-the-approval change
    introduced: a database upgraded from an older release carries rows whose status is `pending`,
    `approved` or `rejected`, and the CHECK names none of them. A row that was WAITING on somebody
    was never applied and never refused — it is a change that did not happen, which is what
    `skipped` means, and the reason says so rather than leaving a status a reader has to know the
    history of.

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
                    "RETURNING id", (schema.RETIRED_KINDS[0],))
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
                    "RETURNING id", (schema.RETIRED_KINDS[0],))
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
    applied_id = _applied(conn)

    schema.ensure_repair_schema(conn)

    assert store.repair(conn, applied_id)["status"] == schema.STATUS_APPLIED
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM pg_constraint
                        WHERE conrelid = 'repairs'::regclass AND contype = 'c'""")
        assert cur.fetchone()[0] == 2, "a repeat run left a duplicate or a missing CHECK"


def test_the_content_key_index_still_guards_the_rows_that_have_one(conn):
    """The elective loop's memory, kept for the rows that hold it. Removals carry no key, so this
    index constrains nothing new — but two legacy rows under one key were never possible, and a
    dropped index would silently let a repaired-history import create them."""
    _row(conn, kind=schema.RETIRED_KINDS[0], status=schema.STATUS_APPLIED, key="same")
    with conn.cursor() as cur, pytest.raises(psycopg.errors.UniqueViolation):
        cur.execute("INSERT INTO repairs (kind, target_paths, ops, content_key, status) "
                    "VALUES (%s, '[]'::jsonb, '[]'::jsonb, 'same', %s)",
                    (schema.RETIRED_KINDS[0], schema.STATUS_APPLIED))
    conn.rollback()


def test_the_index_the_old_lifecycle_needed_is_gone_by_name(conn):
    """The dropped index, asked of the database. Left in place it would go on enforcing uniqueness
    over rows whose status nothing can create — harmless, and a reader finding it would reasonably
    conclude this table still has a lifecycle."""
    with conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'repairs'")
        names = {r[0] for r in cur.fetchall()}

    assert "repair_proposals_pending_key_idx" not in names
    assert "repairs_content_key_idx" in names


# ── the stored op shapes, and the paths a row names ───────────────────────────────────────────
def test_the_two_op_shapes_are_named_and_do_not_overlap():
    """The shapes, pinned where they are declared. Two readers reshape an op — the console's
    cleaner and the applier — and a reader that assumed the deletion's shape for a scrub would drop
    the planned bytes, which are the model's prose and the only thing on that op worth reading."""
    assert schema.DELETE_OP_FIELDS[0] == schema.SCRUB_OP_FIELDS[0] == schema.OP_KIND_KEY
    assert set(schema.DELETE_OP_FIELDS) & set(schema.SCRUB_OP_FIELDS) == {"op", "path"}
    assert "planned_after" not in schema.DELETE_OP_FIELDS, (
        "a removal names a page and stores no bytes — there is nothing left to write")
    assert set(schema.DELETE_OP_NAMES) == {schema.DELETE_OP_NAME, schema.SCRUB_OP_NAME}


def test_target_paths_are_deduplicated_and_sorted():
    ops = [{"op": deletion.OP_SCRUB, "path": "b.md", "expected_before_hash": "h",
            "planned_after": "bytes"},
           {"op": deletion.OP_DELETE, "path": "a.md"},
           {"op": deletion.OP_SCRUB, "path": "b.md", "expected_before_hash": "h",
            "planned_after": "bytes"}]
    assert schema.target_paths(ops) == ["a.md", "b.md"]
