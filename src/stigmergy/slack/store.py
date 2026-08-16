"""`slack_submissions` — the mapping that turns Slack into a push channel for ask-back, plus the
dedup key the 🧠 gesture needs, and the steward doorbell's delivery bookkeeping.

This module is `stigmergy.slack`'s ONLY door into `stigmergy.capture`, and only into `.schema`.
The dedup key is per (thread, reactor): a redelivered or re-added reaction collides on the UNIQUE
key, `reserve()` returns `None`, and the caller must then NOT call `BrainService.submit()` — that
is what keeps a redelivery from producing a second `capture_queue` row.

`_FIND_THREAD` and `_DUE_FOR_REPORT` name `capture_queue`'s columns in raw SQL, so a rename there
breaks no import — only the poller's next query. Every read of `capture_queue` here is a plain
read-only `SELECT`; nothing in this module claims, leases or mutates a queue row.
"""
import logging

from stigmergy.capture import schema as capture_schema
from stigmergy.capture.schema import ensure_capture_schema, startup_ddl_lock

log = logging.getLogger(__name__)

# Every terminal and parked state, `failed` included. `queued`/`claimed` are deliberately absent:
# an ordinary in-flight row produces no Slack traffic.
REPORTABLE_STATUSES = (capture_schema.FILED, capture_schema.NEEDS_INPUT, capture_schema.TRIAGE,
                      capture_schema.REJECTED, capture_schema.RESOLVED, capture_schema.FAILED)

# Re-exported so the rest of `stigmergy.slack` never has to import `stigmergy.capture` itself.
FILED = capture_schema.FILED
NEEDS_INPUT = capture_schema.NEEDS_INPUT
MAX_HINT_CHARS = capture_schema.MAX_HINT_CHARS
withheld_reason = capture_schema.withheld_reason


def is_awaiting_reply(status: str) -> bool:
    return status == capture_schema.NEEDS_INPUT

# Explicit and stable, not Postgres's auto-generated name: a migration has to find and drop the
# constraint by a name that does not change when the column list does.
_DEDUP_KEY_NAME = "slack_submissions_dedup_key"
# The column list in the exact order `pg_get_constraintdef` renders `UNIQUE (a, b, c)` — the DDL
# and the migration's skip check must agree on what today's definition string is.
_DEDUP_KEY_COLUMNS = ("team_id", "channel_id", "thread_ts", "slack_user_id")
_DEDUP_KEY_COLUMN_LIST = ", ".join(_DEDUP_KEY_COLUMNS)
_DEDUP_KEY_DEF = f"UNIQUE ({_DEDUP_KEY_COLUMN_LIST})"
# The same columns qualified for `_COLLAPSE_PLAN_SELECT`'s `slack_submissions s` alias.
_DEDUP_KEY_COLUMN_LIST_S = ", ".join(f"s.{column}" for column in _DEDUP_KEY_COLUMNS)

_DDL = f"""
CREATE TABLE IF NOT EXISTS slack_submissions (
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
    CONSTRAINT {_DEDUP_KEY_NAME} {_DEDUP_KEY_DEF}
)
"""
_INDEX_THREAD = """
CREATE INDEX IF NOT EXISTS slack_submissions_thread_idx
    ON slack_submissions (team_id, channel_id, thread_ts)
"""
_INDEX_SUBMISSION = """
CREATE INDEX IF NOT EXISTS slack_submissions_submission_idx
    ON slack_submissions (submission_id)
"""

# Moves a database off the message-scoped dedup key onto today's thread-scoped one. The DROP/ADD
# pair must stay in ONE `DO` block: under autocommit it would otherwise be two transactions, and
# `IF NOT EXISTS` is a check, not a lock.
#
# The skip check compares the constraint's DEFINITION, not just its name: a constraint named
# `_DEDUP_KEY_NAME` but still carrying the old columns would pass a name-only test and never be
# migrated, leaving that database's dedup grain per-message forever with every test green.
#
# The new key is strictly coarser, so pre-existing rows can collide on it; `ADD CONSTRAINT` would
# then raise `UniqueViolation` up through `ensure_write_path_schema` and `stigmergy.slack.app`
# could not boot. Colliding rows are collapsed before the ADD, and the loser is irreversibly
# unmapped from Slack — hence the tiebreak (a `needs_input` row, then the most recent) and the
# logged trace of every lost `submission_id`.
#
# Shared fragment: this condition is asked both by the `DO` block below and by the Python
# pre-check, so "already migrated" cannot mean two different things to the two callers.
_DEDUP_KEY_UP_TO_DATE = f"""
SELECT 1 FROM pg_constraint c
WHERE c.conrelid = 'slack_submissions'::regclass
  AND c.conname = '{_DEDUP_KEY_NAME}'
  AND pg_get_constraintdef(c.oid) = '{_DEDUP_KEY_DEF}'
""".strip()

# The row-ranking rule shared by the Python pre-check and the migration's temp-table plan, so what
# "the row that survives a collision" means cannot drift between the two.
#
# `(...) IS TRUE DESC`, not bare `(...) DESC`: Postgres orders NULLS FIRST under `DESC`, so an
# orphaned mapping row (NULL `q.status` through the LEFT JOIN) would outrank a genuine
# `needs_input` row. `IS TRUE` ranks NULL and FALSE alike.
_COLLAPSE_PLAN_SELECT = f"""
SELECT s.id, s.submission_id, ROW_NUMBER() OVER (
    PARTITION BY {_DEDUP_KEY_COLUMN_LIST_S}
    ORDER BY (s.submission_id IS NOT NULL) DESC,
             (q.status = '{capture_schema.NEEDS_INPUT}') IS TRUE DESC,
             s.created_at DESC, s.id DESC
) AS rn
FROM slack_submissions s
LEFT JOIN capture_queue q ON q.id = s.submission_id
""".strip()

_DEDUP_KEY_MIGRATION = f"""
DO $$
DECLARE
    drop_stmt text;
    lost_ids bigint[];
    lost_count integer;
BEGIN
    IF NOT EXISTS (
        {_DEDUP_KEY_UP_TO_DATE}
    ) THEN
        SELECT string_agg(format('ALTER TABLE slack_submissions DROP CONSTRAINT %I', c.conname),
                          '; ')
        INTO drop_stmt
        FROM pg_constraint c
        WHERE c.conrelid = 'slack_submissions'::regclass AND c.contype = 'u';
        IF drop_stmt IS NOT NULL THEN
            EXECUTE drop_stmt;
        END IF;

        CREATE TEMP TABLE _dedup_collapse_plan ON COMMIT DROP AS
        {_COLLAPSE_PLAN_SELECT};

        SELECT array_agg(submission_id ORDER BY submission_id), count(*)
        INTO lost_ids, lost_count
        FROM _dedup_collapse_plan
        WHERE rn > 1 AND submission_id IS NOT NULL;

        -- A secondary signal (it reaches an interactive `psql` session, or any future notice
        -- handler) — not the trace a deployment relies on. That is `_log_pending_dedup_collapse`,
        -- which runs in Python, before this block, and writes through the application's own
        -- logger.
        IF lost_count > 0 THEN
            RAISE WARNING 'slack_submissions dedup-key migration (thread-scoped key): % row(s) '
                'with a live submission_id lost their Slack mapping — submission_id(s): %',
                lost_count, lost_ids;
        END IF;

        DELETE FROM slack_submissions
        WHERE id IN (SELECT id FROM _dedup_collapse_plan WHERE rn > 1);

        ALTER TABLE slack_submissions
            ADD CONSTRAINT {_DEDUP_KEY_NAME}
            {_DEDUP_KEY_DEF};
    END IF;
END $$
"""

# One row per (item, steward) the doorbell has ever DMed, carrying the state it was notified at:
# one notification per pair, re-sent only when that state differs from the item's current one.
_STEWARD_NOTIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS steward_notifications (
    id BIGSERIAL PRIMARY KEY,
    item_kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    steward_email TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '',
    notified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT steward_notifications_item_key UNIQUE (item_kind, item_id, steward_email)
)
"""

_ALL_DDL = (_DDL, _DEDUP_KEY_MIGRATION, _INDEX_THREAD, _INDEX_SUBMISSION,
           _STEWARD_NOTIFICATIONS_DDL)


def _log_pending_dedup_collapse(cur) -> None:
    """Log what `_DEDUP_KEY_MIGRATION` is about to delete, through the APPLICATION's logger: its
    `RAISE WARNING` reaches nothing, since no connection in `src/` registers a psycopg notice
    handler. Read-only, and a no-op on any database the migration would skip."""
    cur.execute(f"SELECT NOT EXISTS ({_DEDUP_KEY_UP_TO_DATE})")
    (migration_pending,) = cur.fetchone()
    if not migration_pending:
        return
    cur.execute(
        f"SELECT collapse_plan.id, collapse_plan.submission_id "
        f"FROM ({_COLLAPSE_PLAN_SELECT}) collapse_plan "
        f"WHERE collapse_plan.rn > 1 AND collapse_plan.submission_id IS NOT NULL "
        f"ORDER BY collapse_plan.submission_id")
    lost_ids = [row[1] for row in cur.fetchall()]
    if lost_ids:
        log.warning(
            "slack_submissions dedup-key migration (thread-scoped key) is about to collapse "
            "%d row(s) with a live submission_id, losing their Slack mapping — submission_id(s): "
            "%s", len(lost_ids), lost_ids)


def ensure_slack_schema(conn) -> None:
    """Idempotent DDL behind the shared startup-DDL advisory lock — `CREATE INDEX IF NOT EXISTS`
    is not atomic against a concurrent creator. The pre-check must stay between `_DDL` (it needs
    the table to exist) and `_DEDUP_KEY_MIGRATION` (it must observe the collapse before it runs).
    """
    with startup_ddl_lock(conn) as cur:
        cur.execute(_DDL)
        _log_pending_dedup_collapse(cur)
        for statement in _ALL_DDL[1:]:
            cur.execute(statement)


def ensure_write_path_schema(conn) -> None:
    """The capture-queue tables and this module's own, in one call — so `stigmergy.slack.app`
    never has to import `stigmergy.capture` itself."""
    ensure_capture_schema(conn)
    ensure_slack_schema(conn)


_RESERVE = f"""
INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts, slack_user_id,
                               submitted_by)
VALUES (%(team_id)s, %(channel_id)s, %(message_ts)s, %(thread_ts)s, %(slack_user_id)s,
        %(submitted_by)s)
ON CONFLICT ({_DEDUP_KEY_COLUMN_LIST}) DO NOTHING
RETURNING id
"""


def reserve(conn, *, team_id: str, channel_id: str, message_ts: str, thread_ts: str,
           slack_user_id: str, submitted_by: str) -> int | None:
    """Claim the dedup key for one (thread, reactor) pair. Returns the reservation's id, or `None`
    when the key is already taken (a redelivery, a remove-then-re-add, or a second message in an
    already-reserved thread) — a `None` caller must NOT call `BrainService.submit()`."""
    with conn.cursor() as cur:
        cur.execute(_RESERVE, {
            "team_id": team_id, "channel_id": channel_id, "message_ts": message_ts,
            "thread_ts": thread_ts, "slack_user_id": slack_user_id, "submitted_by": submitted_by,
        })
        row = cur.fetchone()
    return row[0] if row else None


def attach_submission(conn, reservation_id: int, submission_id: int) -> None:
    """Record which `capture_queue` row a reservation produced, once `submit()` has succeeded."""
    with conn.cursor() as cur:
        cur.execute("UPDATE slack_submissions SET submission_id = %s WHERE id = %s",
                    (submission_id, reservation_id))


def release_reservation(conn, reservation_id: int) -> None:
    """Undo a reservation whose `submit()` failed — otherwise a genuine retry of the same event
    finds the dedup key taken and is discarded as a duplicate of a submission never made."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM slack_submissions WHERE id = %s AND submission_id IS NULL",
                    (reservation_id,))


_FIND_THREAD = """
SELECT s.submission_id, s.submitted_by, q.status, q.reply
FROM slack_submissions s
JOIN capture_queue q ON q.id = s.submission_id
WHERE s.team_id = %(team_id)s AND s.channel_id = %(channel_id)s AND s.thread_ts = %(thread_ts)s
  AND s.submission_id IS NOT NULL
ORDER BY s.created_at DESC
"""


def find_thread_submissions(conn, *, team_id: str, channel_id: str, thread_ts: str) -> list[dict]:
    """EVERY Slack-originated submission mapped to this thread, newest first; `[]` for an ordinary
    thread. Plural is load-bearing: two different people reacting in one thread each reserve their
    own key, and `replies` must scan all rows rather than judge the thread by its newest one."""
    with conn.cursor() as cur:
        cur.execute(_FIND_THREAD, {"team_id": team_id, "channel_id": channel_id,
                                   "thread_ts": thread_ts})
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


_DUE_FOR_REPORT = """
SELECT s.id, s.channel_id, s.thread_ts, s.slack_user_id, q.id AS submission_id, q.status,
       q.report, q.result_ref
FROM slack_submissions s
JOIN capture_queue q ON q.id = s.submission_id
WHERE s.submission_id IS NOT NULL
  AND q.status = ANY(%(statuses)s)
  AND q.status IS DISTINCT FROM NULLIF(s.last_status, '')
ORDER BY s.id
"""


def due_for_report(conn) -> list[dict]:
    """Every Slack-originated submission now in a reportable state that has not already been
    reported at THAT status. Read-only: a plain `SELECT`, never a claim."""
    with conn.cursor() as cur:
        cur.execute(_DUE_FOR_REPORT, {"statuses": list(REPORTABLE_STATUSES)})
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def mark_reported(conn, reservation_id: int, status: str) -> None:
    """Record that `status` has been announced, so the next poll pass does not re-post it."""
    with conn.cursor() as cur:
        cur.execute("UPDATE slack_submissions SET last_status = %s WHERE id = %s",
                    (status, reservation_id))


# ── the steward doorbell's own delivery bookkeeping ─────────────────────────────────────────────
def last_notified_state(conn, *, item_kind: str, item_id: str, steward_email: str) -> str | None:
    """The state this (item, steward) pair was last DMed at, or `None` if never notified — the
    doorbell sends only when this differs from the item's current state signature."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM steward_notifications "
            "WHERE item_kind = %s AND item_id = %s AND steward_email = %s",
            (item_kind, item_id, steward_email))
        row = cur.fetchone()
    return row[0] if row else None


def mark_notified(conn, *, item_kind: str, item_id: str, steward_email: str, state: str) -> None:
    """Record that this (item, steward) pair has been DMed at `state`. Call it ONLY after the send
    succeeded, so a failed post leaves nothing recorded and the next poll pass retries it."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO steward_notifications (item_kind, item_id, steward_email, state) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (item_kind, item_id, steward_email) "
            "DO UPDATE SET state = EXCLUDED.state, notified_at = now()",
            (item_kind, item_id, steward_email, state))
