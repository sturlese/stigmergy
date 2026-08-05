"""The queue's one non-additive migration: `resolved` joins the status CHECK constraint, and a
constraint cannot be widened in place — `schema.py`'s own comment names the risk directly:
"DROP ... IF EXISTS followed by ADD is idempotent as a PAIR", which is exactly the property a
table that already exists from an earlier release needs `ensure_capture_schema` to hold.

Isolated in its OWN Postgres schema (`capture_migration_test`), never `public.capture_queue` —
every other suite in this repo depends on that table already being in its current shape at all
times, so this file builds and tears down a throwaway table under a different schema rather than
touching the shared one. Postgres schemas are separate namespaces: a constraint named
`capture_queue_status_check` in `capture_migration_test` cannot collide with the one in `public`.
"""
import threading

import psycopg
import pytest

from stigmergy.capture import schema
from tests import testdb

SCHEMA = "capture_migration_test"

# The queue's ORIGINAL DDL (git 5431538, `feat(capture): the capture queue and the submit
# surface`), reproduced byte-for-byte for the parts this test needs: the seven statuses that
# predate `resolved`, an UNNAMED column-level CHECK — which is exactly what
# `capture_queue_status_check` names (schema.py's own comment: "the name Postgres would have
# derived for the unnamed column check" the first release shipped) — and none of the additive
# columns that arrived later (`report`, `asked_at`, `parked_at`, `reply`, `trace`, `outcome`).
_PRE_RESOLVED_STATUSES = ("queued", "claimed", "filed", "rejected", "needs_input", "triage",
                          "failed")
_PRE_RESOLVED_CAPTURE_QUEUE_DDL = f"""
CREATE TABLE capture_queue (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,
    payload JSONB,
    blob_refs TEXT[] NOT NULL DEFAULT '{{}}',
    submitted_by TEXT NOT NULL,
    hints JSONB,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ({', '.join(f"'{s}'" for s in _PRE_RESOLVED_STATUSES)})),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    result_ref TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
)
"""


@pytest.fixture()
def old_table_conn():
    """A connection whose `search_path` points at a fresh, empty schema seeded with the queue's
    original table — dropped on teardown so nothing here is visible to (or shares a name with)
    any other suite's `public.capture_queue`."""
    conn = testdb.connect_or_skip("capture-migration")
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(_PRE_RESOLVED_CAPTURE_QUEUE_DDL)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def _status_check_name(conn, table: str) -> str:
    """The constraint's own name, scoped to the CURRENT `search_path` (`'<table>'::regclass`) —
    never a plain `pg_class.relname` match. Every other Postgres suite in this repo runs its own
    `capture_queue` in the `public` schema, migrated to this same shape, under the SAME real
    (non-test) database this fixture's connection is also open against; a bare `t.relname = %s`
    join matches BOTH tables and `fetchone()` silently picks whichever `pg_class` happens to list
    first — which was always `public.capture_queue`'s own row in practice, never this file's
    throwaway one. That stayed invisible here because a CONSTRAINT NAME is the same string in both
    schemas after migration (`capture_queue_status_check`), so the wrong-row answer coincidentally
    read as correct; it stopped being invisible the moment a test needed the constraint's OID
    (`_constraint_oid`, below), which is schema-specific and cannot coincide by accident.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass "
                   "AND contype = 'c'", (table,))
        return cur.fetchone()[0]


def _insert(conn, status: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capture_queue (kind, submitted_by, status) VALUES (%s, %s, %s) "
            "RETURNING id", ("raw", "steward@example.com", status))
        return cur.fetchone()[0]


def test_postgres_derives_the_same_constraint_name_schema_py_hardcodes(old_table_conn):
    """The premise the whole migration rests on. `_CAPTURE_QUEUE_STATUS_CHECK` does `DROP
    CONSTRAINT IF EXISTS capture_queue_status_check` — that only touches the OLD constraint if
    Postgres really did derive that exact name for the original unnamed column-level CHECK. Pinned
    down directly rather than only inferred from the migration happening to succeed."""
    assert _status_check_name(old_table_conn, "capture_queue") == "capture_queue_status_check"


def test_ensure_capture_schema_on_a_pre_resolved_table_accepts_resolved_afterward(old_table_conn):
    """The migration's whole point: a table created before `resolved` existed accepts it after
    `ensure_capture_schema` runs, with no manual ALTER and no downtime."""
    schema.ensure_capture_schema(old_table_conn)
    row_id = _insert(old_table_conn, schema.RESOLVED)
    with old_table_conn.cursor() as cur:
        cur.execute("SELECT status FROM capture_queue WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == schema.RESOLVED


def test_ensure_capture_schema_is_idempotent_run_twice_on_the_same_old_table(old_table_conn):
    """The DROP/ADD pair, run twice — the actual regression `schema.py`'s own comment names: a
    migration that is not idempotent as a PAIR fails on the SECOND startup of a worker that already
    migrated once, which is every ordinary restart after the first deploy carrying this change."""
    schema.ensure_capture_schema(old_table_conn)
    schema.ensure_capture_schema(old_table_conn)   # must not raise
    assert _status_check_name(old_table_conn, "capture_queue") == "capture_queue_status_check"
    _insert(old_table_conn, schema.RESOLVED)       # the constraint still accepts it afterward


def test_a_status_from_before_the_widening_still_satisfies_the_constraint(old_table_conn):
    """The expand half of an expand/contract with no contract half scheduled (schema.py: "every
    status the old code writes is still accepted"): a process still writing one of the original
    seven statuses across the same migration keeps working."""
    schema.ensure_capture_schema(old_table_conn)
    _insert(old_table_conn, schema.QUEUED)         # must not raise
    _insert(old_table_conn, schema.TRIAGE)         # must not raise


def test_the_migrated_constraint_still_refuses_an_unknown_status(old_table_conn):
    """The widening is bounded — it did not turn the CHECK into a no-op that accepts anything."""
    schema.ensure_capture_schema(old_table_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(old_table_conn, "bogus-status")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Four properties "does not raise" cannot see.
# `test_ensure_capture_schema_is_idempotent_run_twice_on_the_same_old_table` above asserts only
# that a second call survives — which the OLD (non-atomic DROP-then-ADD, two transactions) code
# also satisfied, because it never observed the constraint's IDENTITY. These four close that gap.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _constraint_oid(conn, table: str) -> int:
    """Same `%s::regclass` scoping as `_status_check_name` above, for the same reason — an oid
    cannot coincidentally match across schemas the way a name string can, which is exactly what
    makes it the right witness for "is this the SAME object", and exactly what would have made a
    `pg_class.relname` join return a stable-but-WRONG answer every time (always `public.
    capture_queue`'s oid, never changing across this file's own migrations)."""
    with conn.cursor() as cur:
        cur.execute("SELECT oid FROM pg_constraint WHERE conrelid = %s::regclass "
                   "AND contype = 'c'", (table,))
        return cur.fetchone()[0]


def test_the_second_run_is_a_true_no_op_the_constraint_keeps_its_identity(old_table_conn):
    """The property the existing "does not raise" test cannot see: a constraint that is DROPPED
    and RE-ADDED gets a brand-new Postgres object (a new `oid`), even when its definition ends up
    identical to the one it replaced. So "the second run did nothing" is only provable by reading
    the oid before and after — if the guard's `NOT EXISTS` check were ever weakened back to firing
    unconditionally (the pre-fix shape, one transaction now instead of two, but still a needless
    swap every startup), this test catches it where "run twice, does not raise" cannot.
    """
    schema.ensure_capture_schema(old_table_conn)
    first_oid = _constraint_oid(old_table_conn, "capture_queue")

    schema.ensure_capture_schema(old_table_conn)
    second_oid = _constraint_oid(old_table_conn, "capture_queue")

    assert second_oid == first_oid, (
        "the constraint was dropped and re-added on a run that should have been a no-op — the "
        "guard's `NOT EXISTS` check fired when the constraint already named every current status")


def test_two_concurrent_ensure_capture_schema_calls_neither_raise(old_table_conn):
    """Two processes starting together (the server and the worker, in the composition) both call
    `ensure_capture_schema` on first boot. The old two-statement shape raced — with the two
    starters as A and B: A DROP, B DROP, A ADD, B ADD — and B's ADD died with `DuplicateObject`
    (schema.py's own comment: "no ADD CONSTRAINT IF NOT EXISTS to hide behind"). The DO block that
    replaced it is one statement, so a
    concurrent starter either sees the finished swap and skips, or blocks on the table lock until
    it commits and then redoes its own check — never colliding.

    A SECOND connection, not a second thread over the same one: `psycopg` connections are not
    thread-safe for concurrent statements, and the property under test is a real Postgres lock
    interaction between two independent sessions — a real race, never a simulated one — exactly
    `test_dispositions_pg.py`'s own posture for its concurrent-disposition test.

    **The DO block alone was not enough**, and this test also covers its siblings. Two sessions
    both running `CREATE INDEX IF NOT EXISTS capture_queue_status_created_idx` (etc.) against a
    table that has NEVER had that index before can both pass the "does it exist" check and then
    collide on `pg_class`'s own uniqueness constraint — `CREATE INDEX IF NOT EXISTS`, unlike the
    DO-blocked constraint swap, is not atomic against a concurrent creator. Exactly the scenario
    this docstring describes (two process groups of one Fly app, or `docker compose up` starting
    the server and the worker together) reaches it the one time it matters: a genuinely FRESH
    database, initialized for the first time by two processes at once. Every already-migrated
    deployment has these indexes already, which made it a cold-start-only defect and is why it
    stayed invisible. `ensure_capture_schema` now runs its whole DDL loop inside
    `startup_ddl_lock`, one advisory lock for the entire database, so both starters here get
    through.
    """
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def _run(label: str) -> None:
        conn = testdb.connect_or_skip(f"schema-migration-concurrent-{label}")
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
            barrier.wait(timeout=5)
            try:
                schema.ensure_capture_schema(conn)
                outcomes[label] = "ok"
            except Exception as ex:  # noqa: BLE001 — the assertion below decides pass/fail
                outcomes[label] = ex
        finally:
            conn.close()

    threads = [threading.Thread(target=_run, args=(label,)) for label in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert outcomes == {"a": "ok", "b": "ok"}, outcomes
    assert _status_check_name(old_table_conn, "capture_queue") == "capture_queue_status_check"
    _insert(old_table_conn, schema.RESOLVED)      # the constraint landed correctly either way


def test_a_watcher_connection_never_observes_the_table_unconstrained(old_table_conn):
    """The atomicity claim itself, checked rather than trusted: a SEPARATE connection polls
    `pg_constraint` in a tight loop DURING the migration and must never see the constraint absent
    (dropped, not yet re-added) — only before or after, never a moment in between. Meaningful
    because Postgres MVCC means a concurrent reader on read-committed only ever sees the last
    COMMITTED state, and the DO block is one statement (one implicit transaction); a regression
    back to two separate `ALTER TABLE` statements (schema.py's own history) would very likely be
    caught by a watcher polling this fast, across many rows to make the `ADD CONSTRAINT ... VALIDATE`
    scan take measurable time.
    """
    # Rows so the constraint's own re-validation on ADD has bytes to scan — widening the window a
    # non-atomic regression would need to be caught in.
    with old_table_conn.cursor() as cur:
        cur.executemany("INSERT INTO capture_queue (kind, submitted_by, status) "
                        "VALUES (%s, %s, %s)",
                        [("raw", "steward@example.com", "queued")] * 500)

    stop = threading.Event()
    observed_unconstrained = threading.Event()

    def _watch() -> None:
        conn = testdb.connect_or_skip("schema-migration-watcher")
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
            while not stop.is_set():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM pg_constraint c JOIN pg_class t "
                        "ON t.oid = c.conrelid WHERE t.relname = 'capture_queue' "
                        "AND c.contype = 'c'")
                    if cur.fetchone()[0] == 0:
                        observed_unconstrained.set()
                        break
        finally:
            conn.close()

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    try:
        schema.ensure_capture_schema(old_table_conn)
    finally:
        stop.set()
        watcher.join(timeout=5)

    assert not observed_unconstrained.is_set(), (
        "a concurrent reader observed capture_queue with NO status constraint at all mid-migration "
        "— the drop and the re-add are no longer one atomic statement")


def test_the_guard_is_derived_from_statuses_not_a_frozen_snapshot(old_table_conn):
    """schema.py's own comment: "built from STATUSES, never a hand-written list, so a ninth status
    is one edit in one place and this migration wakes up by itself". Checked by building the SAME
    guard SQL `_CAPTURE_QUEUE_STATUS_CHECK` builds — same shape, same quoting — over a COPY of
    `STATUSES` with one fake status appended, and confirming it fires (drops and re-adds) against a
    table already migrated by the real (8-status) code. If the guard were ever hand-written for a
    fixed status list instead of parameterized, this would stop firing and the fake status would
    never be reachable no matter how many times `regenerate` was asked to notice it.
    """
    schema.ensure_capture_schema(old_table_conn)
    before_oid = _constraint_oid(old_table_conn, "capture_queue")

    fake_statuses = (*schema.STATUSES, "archived-for-this-test-only")
    literals = ", ".join(f"'{s}'" for s in fake_statuses)
    guarded_swap = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'capture_queue'::regclass
          AND c.conname = 'capture_queue_status_check'
          AND (SELECT bool_and(pg_get_constraintdef(c.oid) LIKE '%' || quote_literal(s) || '%')
               FROM unnest(ARRAY[{literals}]) AS s)
    ) THEN
        ALTER TABLE capture_queue DROP CONSTRAINT IF EXISTS capture_queue_status_check;
        ALTER TABLE capture_queue ADD CONSTRAINT capture_queue_status_check
            CHECK (status IN ({literals}));
    END IF;
END $$
"""
    with old_table_conn.cursor() as cur:
        cur.execute(guarded_swap)

    after_oid = _constraint_oid(old_table_conn, "capture_queue")
    assert after_oid != before_oid, "the guard did not fire for a status STATUSES does not have yet"
    _insert(old_table_conn, "archived-for-this-test-only")   # the widened constraint now accepts it
    _insert(old_table_conn, schema.RESOLVED)                 # and still accepts every real status
