"""`stigmergy.slack.store` — the `slack_submissions` mapping's own primitives, direct. The
higher-level flows (`tests/slack/test_capture.py`, `test_poller.py`, `test_replies.py`) exercise
these through the Slack handlers; this file pins the primitives themselves.
"""
import pytest

from stigmergy.slack import store
from tests import testdb


def connect_or_skip():
    conn = testdb.connect_or_skip("slack_store")
    store.ensure_write_path_schema(conn)
    return conn


@pytest.fixture()
def conn():
    c = connect_or_skip()
    with c.cursor() as cur:
        cur.execute("DELETE FROM slack_submissions")
        cur.execute("DELETE FROM capture_queue")
    yield c
    c.close()


def test_ensure_write_path_schema_is_idempotent(conn):
    store.ensure_write_path_schema(conn)   # a second call must not raise
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('slack_submissions')")
        assert cur.fetchone()[0] == "slack_submissions"


def test_ensure_slack_schema_migration_collapses_pre_existing_collisions(conn):
    """The dedup key narrows from `(..., message_ts, ...)` to `(..., thread_ts, ...)` — strictly
    coarser, so rows that agreed on thread/reactor but differed on message (the ORDINARY shape
    under the old key: one person 🧠'd two messages in one thread) now collide on the new key.
    `ADD CONSTRAINT` on colliding data raises `UniqueViolation`, which `ensure_slack_schema` would
    propagate straight up through `ensure_write_path_schema` — a deploy-time crash loop in
    `stigmergy.slack.app`'s boot, identical on every restart, on exactly the state the old key
    produced. This seeds that legacy state against a REAL migration run (not a table built fresh
    from today's `_DDL`, which never exercises the migration path) and asserts it collapses
    instead of crashing."""
    with conn.cursor() as cur:
        # Reset to the legacy shape: drop today's (thread-keyed) constraint, add the OLD
        # (message-keyed) one back under a name of our own — the table's very first DDL never
        # named it either, and the migration drops ANY unique constraint it finds, by name or not.
        cur.execute(f"ALTER TABLE slack_submissions DROP CONSTRAINT {store._DEDUP_KEY_NAME}")
        cur.execute(
            "ALTER TABLE slack_submissions ADD CONSTRAINT slack_submissions_legacy_dedup_key "
            "UNIQUE (team_id, channel_id, message_ts, slack_user_id)")
        # Two rows, same thread and reactor, different messages — legal under the OLD key,
        # colliding under the new one. One carries a real submission_id (the row the collapse
        # must PREFER to keep over a bare reservation that never produced a capture).
        cur.execute(
            "INSERT INTO capture_queue (kind, submitted_by) VALUES ('raw', 'ana@example.com') "
            "RETURNING id")
        submission_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts,"
            " slack_user_id, submitted_by, submission_id) VALUES"
            " ('T1', 'C1', '1.1', '9.1', 'U1', 'ana@example.com', NULL)")
        cur.execute(
            "INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts,"
            " slack_user_id, submitted_by, submission_id) VALUES"
            " ('T1', 'C1', '1.2', '9.1', 'U1', 'ana@example.com', %s)", (submission_id,))

    store.ensure_slack_schema(conn)   # must NOT raise UniqueViolation

    with conn.cursor() as cur:
        cur.execute(
            "SELECT submission_id FROM slack_submissions WHERE team_id = 'T1' AND channel_id ="
            " 'C1' AND thread_ts = '9.1' AND slack_user_id = 'U1'")
        rows = cur.fetchall()
    assert len(rows) == 1                       # collapsed to exactly one row per new key
    assert rows[0][0] == submission_id           # the row WITH a submission_id survived
    with conn.cursor() as cur:
        cur.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'slack_submissions'::regclass"
            " AND contype = 'u'")
        assert [r[0] for r in cur.fetchall()] == [store._DEDUP_KEY_NAME]


# ── the ORDINARY case — BOTH colliding rows carry a real submission_id ─────────────────────────
def test_ensure_slack_schema_migration_prefers_the_open_capture_when_both_rows_are_real(conn):
    """The case the module's own comment calls ordinary, and the test above does not reach:
    two live `capture_queue` rows, both mapped, colliding on the new thread-scoped key.
    `submission_id IS NOT NULL` cannot break the tie (both have one) — the survivor must be the
    one still awaiting a reply (`needs_input`), and the loser's id must be named in a WARNING,
    not silently dropped."""
    notices = []
    conn.add_notice_handler(lambda diag: notices.append(diag.message_primary or ""))
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE slack_submissions DROP CONSTRAINT {store._DEDUP_KEY_NAME}")
        cur.execute(
            "ALTER TABLE slack_submissions ADD CONSTRAINT slack_submissions_legacy_dedup_key "
            "UNIQUE (team_id, channel_id, message_ts, slack_user_id)")

        # The OPEN capture: still waiting on the submitter's answer.
        cur.execute(
            "INSERT INTO capture_queue (kind, submitted_by, status) VALUES "
            "('raw', 'ana@example.com', 'needs_input') RETURNING id")
        open_submission_id = cur.fetchone()[0]
        # The RESOLVED capture: already filed, nothing left to answer.
        cur.execute(
            "INSERT INTO capture_queue (kind, submitted_by, status) VALUES "
            "('raw', 'ana@example.com', 'filed') RETURNING id")
        filed_submission_id = cur.fetchone()[0]

        # Two Slack mappings, same thread and reactor (colliding on the new key), each pointing at
        # a REAL, DIFFERENT capture — the shape the old `submission_id IS NOT NULL` tiebreak could
        # not distinguish at all.
        cur.execute(
            "INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts,"
            " slack_user_id, submitted_by, submission_id) VALUES"
            " ('T2', 'C2', '1.1', '9.2', 'U1', 'ana@example.com', %s)", (filed_submission_id,))
        cur.execute(
            "INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts,"
            " slack_user_id, submitted_by, submission_id) VALUES"
            " ('T2', 'C2', '1.2', '9.2', 'U1', 'ana@example.com', %s)", (open_submission_id,))

    store.ensure_slack_schema(conn)   # runs the migration

    with conn.cursor() as cur:
        cur.execute(
            "SELECT submission_id FROM slack_submissions WHERE team_id = 'T2' AND channel_id ="
            " 'C2' AND thread_ts = '9.2' AND slack_user_id = 'U1'")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == open_submission_id, "the row still awaiting a reply must survive"

    # The loser is named in a WARNING, before the delete — not a silent data loss.
    assert any(str(filed_submission_id) in n for n in notices), notices
    assert any("submission_id" in n.lower() for n in notices), notices


# ── the trace must reach the APPLICATION's own logs ────────────────────────────────────────────
def test_dedup_key_migration_logs_the_collapse_plan_before_running(conn, caplog):
    """The migration's `RAISE WARNING` reaches only the Postgres server log — `psycopg`
    discards a notice by default when no `add_notice_handler` is registered, and the real startup
    connection (`stigmergy.slack.app`) never registers one; only this test file's OWN fixture does,
    for the test above. `_log_pending_dedup_collapse` must put the same information — the count and
    the lost `submission_id`s — through the module's application logger instead, computed
    READ-ONLY before the migration runs."""
    # BOTH rows must carry a REAL, DIFFERENT submission_id — a bare reservation that never
    # produced a capture (`submission_id IS NULL`) loses nothing worth logging, and the collapse
    # plan's own rank puts it last regardless (see `test_ensure_slack_schema_migration_collapses_
    # pre_existing_collisions`, which does not exercise a genuine loss at all).
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE slack_submissions DROP CONSTRAINT {store._DEDUP_KEY_NAME}")
        cur.execute(
            "ALTER TABLE slack_submissions ADD CONSTRAINT slack_submissions_legacy_dedup_key "
            "UNIQUE (team_id, channel_id, message_ts, slack_user_id)")
        cur.execute(
            "INSERT INTO capture_queue (kind, submitted_by, status) VALUES "
            "('raw', 'ana@example.com', 'needs_input') RETURNING id")
        surviving_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO capture_queue (kind, submitted_by, status) VALUES "
            "('raw', 'ana@example.com', 'filed') RETURNING id")
        lost_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts,"
            " slack_user_id, submitted_by, submission_id) VALUES"
            " ('T4', 'C4', '1.1', '9.4', 'U1', 'ana@example.com', %s)", (lost_id,))
        cur.execute(
            "INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts,"
            " slack_user_id, submitted_by, submission_id) VALUES"
            " ('T4', 'C4', '1.2', '9.4', 'U1', 'ana@example.com', %s)", (surviving_id,))

    with caplog.at_level("WARNING", logger="stigmergy.slack.store"):
        store.ensure_slack_schema(conn)

    warnings = [r.getMessage() for r in caplog.records if r.name == "stigmergy.slack.store"]
    assert any("dedup-key migration" in w and str(lost_id) in w for w in warnings), warnings
    # the SURVIVING id must not be reported as lost
    assert not any(str(surviving_id) in w for w in warnings if "dedup-key migration" in w), warnings


def test_dedup_key_migration_logs_nothing_when_no_collapse_is_pending(conn, caplog):
    """The benign twin: a database already on today's constraint (every fresh one, and every boot
    after the one that actually migrates) must not log a collapse warning at all — the pre-check
    has to agree with the migration's own `IF NOT EXISTS` about when there is nothing to do."""
    with caplog.at_level("WARNING", logger="stigmergy.slack.store"):
        store.ensure_slack_schema(conn)   # conn is already on today's schema

    warnings = [r for r in caplog.records
               if r.name == "stigmergy.slack.store" and "dedup-key migration" in r.getMessage()]
    assert warnings == []


# ── NULLS FIRST under DESC inverted the tiebreak's own intent ──────────────────────────────────
def test_dedup_key_migration_prefers_needs_input_over_an_orphaned_submission_id(conn):
    """A mapping row whose `submission_id` points at NO `capture_queue` row at all (there is no
    foreign key on this column — see the module docstring) has `q.status` come back NULL through
    the LEFT JOIN. Postgres's default null-ordering is NULLS FIRST under `DESC`, so plain
    `(q.status = 'needs_input') DESC` used to rank that NULL AHEAD of an actual `TRUE` — the
    orphaned row would win the tiebreak over a row genuinely awaiting a reply, the exact opposite
    of what the rule says it is doing. `(...) IS TRUE DESC` is what makes the real `needs_input`
    row survive instead."""
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE slack_submissions DROP CONSTRAINT {store._DEDUP_KEY_NAME}")
        cur.execute(
            "ALTER TABLE slack_submissions ADD CONSTRAINT slack_submissions_legacy_dedup_key "
            "UNIQUE (team_id, channel_id, message_ts, slack_user_id)")

        # The OPEN capture: still waiting on the submitter's answer.
        cur.execute(
            "INSERT INTO capture_queue (kind, submitted_by, status) VALUES "
            "('raw', 'ana@example.com', 'needs_input') RETURNING id")
        open_submission_id = cur.fetchone()[0]
        # An id that names NO capture_queue row at all — orphaned, not merely a different status.
        cur.execute("SELECT COALESCE(MAX(id), 0) + 1000000 FROM capture_queue")
        orphaned_submission_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts,"
            " slack_user_id, submitted_by, submission_id) VALUES"
            " ('T5', 'C5', '1.1', '9.5', 'U1', 'ana@example.com', %s)",
            (orphaned_submission_id,))
        cur.execute(
            "INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts,"
            " slack_user_id, submitted_by, submission_id) VALUES"
            " ('T5', 'C5', '1.2', '9.5', 'U1', 'ana@example.com', %s)", (open_submission_id,))

    store.ensure_slack_schema(conn)   # runs the migration

    with conn.cursor() as cur:
        cur.execute(
            "SELECT submission_id FROM slack_submissions WHERE team_id = 'T5' AND channel_id ="
            " 'C5' AND thread_ts = '9.5' AND slack_user_id = 'U1'")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == open_submission_id, (
        "the row still genuinely awaiting a reply must survive over the orphaned one")


def test_reserve_is_exactly_once_for_the_same_dedup_key(conn):
    first = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.1",
                          thread_ts="1.1", slack_user_id="U1", submitted_by="ana@example.com")
    second = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.1",
                           thread_ts="1.1", slack_user_id="U1",
                           submitted_by="ana@example.com")
    assert first is not None
    assert second is None


def test_reserve_dedups_by_thread_not_by_message(conn):
    """The dedup grain is the THREAD, not the message. Two 🧠 reactions by the SAME person on two
    DIFFERENT messages of one thread must collide on the same key — what gets submitted is the
    thread, so the key names what is submitted."""
    first = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.1",
                          thread_ts="9.1", slack_user_id="U1", submitted_by="ana@example.com")
    second = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.2",
                           thread_ts="9.1", slack_user_id="U1", submitted_by="ana@example.com")
    assert first is not None
    assert second is None


def test_reserve_treats_a_different_reactor_as_a_different_key(conn):
    """Two DIFFERENT people reacting to the SAME thread still produce two captures — attribution
    differs, and that is deliberate."""
    first = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.1",
                          thread_ts="1.1", slack_user_id="U1", submitted_by="ana@example.com")
    second = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.2",
                           thread_ts="1.1", slack_user_id="U2", submitted_by="steward@example.com")
    assert first is not None and second is not None
    assert first != second


def test_release_reservation_frees_the_key_for_a_genuine_retry(conn):
    reservation_id = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.1",
                                   thread_ts="1.1", slack_user_id="U1",
                                   submitted_by="ana@example.com")
    store.release_reservation(conn, reservation_id)
    retried = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.1",
                            thread_ts="1.1", slack_user_id="U1", submitted_by="ana@example.com")
    assert retried is not None


def test_release_reservation_never_touches_an_already_attached_row(conn):
    """A reservation that already produced a real submission must not be releasable — only a
    failed attempt (submission_id still NULL) may be undone."""
    reservation_id = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.1",
                                   thread_ts="1.1", slack_user_id="U1",
                                   submitted_by="ana@example.com")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO capture_queue (kind, submitted_by) VALUES ('raw', 'ana@example.com')"
                   " RETURNING id")
        submission_id = cur.fetchone()[0]
    store.attach_submission(conn, reservation_id, submission_id)

    store.release_reservation(conn, reservation_id)   # must be a no-op

    duplicate = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.1",
                              thread_ts="1.1", slack_user_id="U1",
                              submitted_by="ana@example.com")
    assert duplicate is None   # the key is still taken


def test_find_thread_submissions_is_scoped_to_team_channel_and_thread(conn):
    reservation_id = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.1",
                                   thread_ts="1.1", slack_user_id="U1",
                                   submitted_by="ana@example.com")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO capture_queue (kind, submitted_by, status)"
                   " VALUES ('raw', 'ana@example.com', 'needs_input') RETURNING id")
        submission_id = cur.fetchone()[0]
    store.attach_submission(conn, reservation_id, submission_id)

    found = store.find_thread_submissions(conn, team_id="T1", channel_id="C1",
                                          thread_ts="1.1")
    assert found == [{"submission_id": submission_id, "submitted_by": "ana@example.com",
                      "status": "needs_input", "reply": None}]

    assert store.find_thread_submissions(conn, team_id="T1", channel_id="C2",
                                         thread_ts="1.1") == []
    assert store.find_thread_submissions(conn, team_id="T2", channel_id="C1",
                                         thread_ts="1.1") == []


def test_find_thread_submissions_returns_every_row_in_the_thread_newest_first(conn):
    """A thread may legally hold more than one capture — the UNIQUE key is per (thread, reactor),
    not per-message — so two DIFFERENT reactors in one thread each reserve their own row. Both
    must come back, in order, so the caller can pick the right one rather than being handed only
    whichever is newest."""
    older = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.1",
                          thread_ts="9.1", slack_user_id="U1", submitted_by="ana@example.com")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO capture_queue (kind, submitted_by, status)"
                   " VALUES ('raw', 'ana@example.com', 'needs_input') RETURNING id")
        older_submission_id = cur.fetchone()[0]
    store.attach_submission(conn, older, older_submission_id)

    newer = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.2",
                          thread_ts="9.1", slack_user_id="U2",
                          submitted_by="steward@example.com")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO capture_queue (kind, submitted_by, status)"
                   " VALUES ('raw', 'steward@example.com', 'queued') RETURNING id")
        newer_submission_id = cur.fetchone()[0]
    store.attach_submission(conn, newer, newer_submission_id)

    found = store.find_thread_submissions(conn, team_id="T1", channel_id="C1",
                                          thread_ts="9.1")
    assert [f["submission_id"] for f in found] == [newer_submission_id, older_submission_id]


def test_due_for_report_and_mark_reported_track_status_changes(conn):
    reservation_id = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.1",
                                   thread_ts="1.1", slack_user_id="U1",
                                   submitted_by="ana@example.com")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO capture_queue (kind, submitted_by, status)"
                   " VALUES ('raw', 'ana@example.com', 'queued') RETURNING id")
        submission_id = cur.fetchone()[0]
    store.attach_submission(conn, reservation_id, submission_id)

    assert store.due_for_report(conn) == []   # 'queued' is not reportable

    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET status = 'triage' WHERE id = %s", (submission_id,))
    due = store.due_for_report(conn)
    assert len(due) == 1 and due[0]["status"] == "triage"

    store.mark_reported(conn, reservation_id, "triage")
    assert store.due_for_report(conn) == []   # already reported at this status

    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET status = 'resolved' WHERE id = %s", (submission_id,))
    due = store.due_for_report(conn)
    assert len(due) == 1 and due[0]["status"] == "resolved"   # a FURTHER change reports again


def test_is_awaiting_reply():
    assert store.is_awaiting_reply("needs_input") is True
    assert store.is_awaiting_reply("queued") is False


def test_dedup_key_migration_upgrades_a_legacy_message_keyed_constraint(conn):
    """A table already carrying the OLD message_ts-keyed UNIQUE constraint (under whatever name
    Postgres auto-generated for it) must end up with the new thread_ts-keyed one after
    `ensure_slack_schema` runs — the migration `store._DEDUP_KEY_MIGRATION` performs, proven here
    against a hand-built legacy shape rather than only against a fresh database (which would never
    exercise the DROP/ADD branch at all)."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS slack_submissions")
        cur.execute("""
            CREATE TABLE slack_submissions (
                id BIGSERIAL PRIMARY KEY,
                team_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_ts TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                slack_user_id TEXT NOT NULL,
                submitted_by TEXT NOT NULL DEFAULT '',
                submission_id BIGINT,
                last_status TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (team_id, channel_id, message_ts, slack_user_id)
            )
        """)
        # the legacy shape: same message, same thread, same user twice would have collided on
        # message_ts alone; a different message in the same thread would NOT have.
        cur.execute("INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts,"
                   " slack_user_id) VALUES ('T1', 'C1', '1.1', '9.1', 'U1')")

    store.ensure_slack_schema(conn)   # runs the migration

    with conn.cursor() as cur:
        cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'slack_submissions'"
                   "::regclass AND contype = 'u'")
        names = {row[0] for row in cur.fetchall()}
    assert names == {store._DEDUP_KEY_NAME}

    # and the new key is actually enforced: a second message in the same thread by the same user
    # now collides, which it would not have under the old constraint.
    second = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.2",
                           thread_ts="9.1", slack_user_id="U1", submitted_by="ana@example.com")
    assert second is None


def test_dedup_key_migration_still_runs_when_the_name_is_right_but_the_columns_are_not(conn):
    """The skip check used to compare only the constraint's NAME (`c.conname =
    _DEDUP_KEY_NAME`), never its columns. A constraint left NAMED `slack_submissions_dedup_key`
    but still keyed on the OLD `message_ts` column — a hand-run migration, a partial fix, a
    restore from a renamed legacy dump — matched that name-only test and was silently never
    migrated: the dedup grain stayed per-message forever, with every other test green (they all
    build the table fresh from `_DDL`, which never produces this "right name, wrong columns"
    shape). This seeds exactly that shape and proves the migration still runs."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS slack_submissions")
        cur.execute(f"""
            CREATE TABLE slack_submissions (
                id BIGSERIAL PRIMARY KEY,
                team_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_ts TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                slack_user_id TEXT NOT NULL,
                submitted_by TEXT NOT NULL DEFAULT '',
                submission_id BIGINT,
                last_status TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT {store._DEDUP_KEY_NAME} UNIQUE (team_id, channel_id, message_ts,
                                                           slack_user_id)
            )
        """)
        cur.execute("INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts,"
                   " slack_user_id) VALUES ('T1', 'C1', '1.1', '9.1', 'U1')")

    store.ensure_slack_schema(conn)   # must still run the migration despite the matching name

    with conn.cursor() as cur:
        cur.execute("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
                   " WHERE conrelid = 'slack_submissions'::regclass AND contype = 'u'")
        constraints = cur.fetchall()
    assert constraints == [(store._DEDUP_KEY_NAME, store._DEDUP_KEY_DEF)]

    # and the new (thread-scoped) key is actually enforced, not the stale message-scoped one.
    second = store.reserve(conn, team_id="T1", channel_id="C1", message_ts="1.2",
                           thread_ts="9.1", slack_user_id="U1", submitted_by="ana@example.com")
    assert second is None
