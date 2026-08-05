"""`slack_submissions` — the mapping that turns Slack into a push channel for ask-back, plus the
dedup key the 🧠 gesture needs.

**This table lives inside `stigmergy.slack`.** Its vocabulary (`team_id`, `channel_id`,
`slack_user_id`) is Slack's own naming, not the server's, and a Slack-shaped table sitting a layer
BELOW the package that owns it is exactly the kind of drift
`tests/test_architecture.py::test_no_slack_identifiers_below_the_slack_package` pins shut. The one
thing that placement has to protect — `stigmergy.capture`'s `startup_ddl_lock` (`CREATE INDEX IF NOT
EXISTS` is not atomic against a concurrent creator) — is reached through exactly one door, this
module's: `stigmergy.slack` imports `stigmergy.capture.schema` directly, and ONLY for
`startup_ddl_lock` plus the state constants re-exported below. That single edge is pinned by
`tests/test_architecture.py::test_slack_store_imports_only_capture_schema`.

**Two-phase reserve-then-fill, not insert-then-check, is what holds under real concurrency** (a
redelivered `reaction_added`, or a fast remove-then-re-add) rather than merely under a
single-threaded test: `reserve()` is the ONLY write that can race, and it races against Postgres's
own UNIQUE index on `(team_id, channel_id, thread_ts, slack_user_id)` — the same "let the database
serialize it" posture `capture.queue`'s claim statement uses. A caller that loses the race gets
`None` back and must not call `BrainService.submit()` at all, which is what keeps a redelivered
event from ever producing a second `capture_queue` row (not merely a second mapping row for the
same one).

**The dedup key is per THREAD.** What gets submitted is the thread, not the message: two 🧠
reactions by the SAME person on two DIFFERENT messages of ONE thread are one capture (both
reservations collide on the same key), while two DIFFERENT people reacting to the same thread
still produce two captures (attribution differs, and that is deliberate). `message_ts` stays a
stored column — it is the gesture's own provenance — but is not part of the UNIQUE key.

**Every read here is read-only against `capture_queue`**: the poller must not claim, lease or
mutate a queue row. `find_thread_submissions` and `due_for_report` both join to it with a plain
`SELECT`; nothing in this module calls `capture.queue.finish`, `.dispose`, `.claim_next` or
`.record_reply` — those stay the worker's and the service layer's, respectively.

**The join is RAW SQL, not a call into `stigmergy.capture.queue`.** `_FIND_THREAD` and
`_DUE_FOR_REPORT` below name `capture_queue`'s columns by hand (`q.status`, `q.reply`, `q.report`,
`q.result_ref`) — this module never imports `stigmergy.capture.queue` at all (the pinned-edge tests
in `tests/test_architecture.py` hold the import list to `.schema` alone). That means this coupling
is invisible to every import-level check: a column renamed on `capture_queue`'s side would not
fail an import, only the poller's next real query.
`tests/test_architecture.py::test_slack_store_sql_column_names_exist_on_capture_queue` pins the
column names these two queries reference against `capture.schema`'s own DDL, so that rename
breaks a test with a message instead.
"""
import logging

from stigmergy.capture import schema as capture_schema
from stigmergy.capture.schema import ensure_capture_schema, startup_ddl_lock

log = logging.getLogger(__name__)

# The six terminal/parked states the poller reports on — every terminal and parked state the
# queue's vocabulary contains, `failed` included. `queued`/`claimed` are deliberately absent: an
# ordinary in-flight row produces no Slack traffic.
REPORTABLE_STATUSES = (capture_schema.FILED, capture_schema.NEEDS_INPUT, capture_schema.TRIAGE,
                      capture_schema.REJECTED, capture_schema.RESOLVED, capture_schema.FAILED)

# Re-exported so the rest of `stigmergy.slack` (`replies.py`, `poller.py`, `capture.py`,
# `doorbell.py`) never has to import `stigmergy.capture` itself to ask "is this row waiting on the
# submitter's answer", to build its filed/needs_input render, to bound a provenance hint before
# `normalize_hints` would reject it outright, or to ask whether a status means "the secrets/PII
# gate has not looked at this yet" — the one permitted edge into `stigmergy.capture` is THIS
# module's, and only into `.schema`.
FILED = capture_schema.FILED
NEEDS_INPUT = capture_schema.NEEDS_INPUT
MAX_HINT_CHARS = capture_schema.MAX_HINT_CHARS
withheld_reason = capture_schema.withheld_reason


def is_awaiting_reply(status: str) -> bool:
    return status == capture_schema.NEEDS_INPUT

# The dedup key's name is explicit and stable (`slack_submissions_dedup_key`) rather than left to
# Postgres's auto-generated one, for the same reason `capture.schema._STATUS_CHECK_NAME` is: a
# migration that has to find and drop the OLD constraint needs a name it can rely on for the NEW
# one, and an auto-derived name changes the moment the column list does.
_DEDUP_KEY_NAME = "slack_submissions_dedup_key"
# The exact column list, in the exact order `pg_get_constraintdef` renders a UNIQUE constraint's
# definition in (verified empirically: `UNIQUE (a, b, c)`, comma-space-joined, no surprises across
# the Postgres versions this repo targets) — named once so the DDL, the migration's skip check
# (`_DEDUP_KEY_DEF` below) and any future reader agree on what "today's column list" means.
_DEDUP_KEY_COLUMNS = ("team_id", "channel_id", "thread_ts", "slack_user_id")
_DEDUP_KEY_DEF = f"UNIQUE ({', '.join(_DEDUP_KEY_COLUMNS)})"

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

# Migrates a database whose dedup key is still the message-scoped
# `(team_id, channel_id, message_ts, slack_user_id)` onto today's thread-scoped
# `(team_id, channel_id, thread_ts, slack_user_id)`. A non-additive change, so it needs the same
# "one statement, one transaction" discipline `capture.schema._CAPTURE_QUEUE_STATUS_CHECK` uses
# for exactly this class of problem (a constraint that cannot be widened in place). See that
# constant's own comment for the full argument: autocommit means a DROP/ADD pair is TWO
# transactions unless wrapped in one `DO` block, and `IF NOT EXISTS` is a check, not a lock.
#
# Skips entirely once `_DEDUP_KEY_NAME` already exists with today's column list — which is every
# fresh database, since `_DDL` above creates it that way; this only does work against a table that
# still carries the message_ts-keyed constraint under whatever name Postgres gave it (the earliest
# DDL for this table never named it).
#
# **The skip check compares the constraint's DEFINITION, not just its NAME** — the same defect
# class `_CAPTURE_QUEUE_STATUS_CHECK` guards against for a CHECK constraint
# (`pg_get_constraintdef(c.oid) LIKE ...`). A constraint NAMED `{_DEDUP_KEY_NAME}` but still
# carrying the OLD (message_ts-keyed) columns — a hand-run migration, a partial fix, a restore from
# a renamed dump — matches a name-only test and is never migrated: the 🧠 dedup grain then stays
# per-message forever on that database, silently, with every test green (every test here builds the
# table fresh from `_DDL`, which never exercises "right name, wrong columns" at all). Requiring
# `pg_get_constraintdef(c.oid) = '{_DEDUP_KEY_DEF}'` — the exact definition string — is what makes
# a same-named constraint over the wrong columns distinguishable from the real thing.
#
# **Narrowing the key can COLLIDE existing rows, and that must not crash startup.** The old key
# carried `message_ts`; today's carries `thread_ts` instead — strictly coarser, so any pair of
# pre-existing rows that agreed on thread/reactor but differed on message (the ordinary case: one
# person 🧠'd two messages in one thread) collides on the NEW key. `ADD CONSTRAINT` on data
# violating it raises `UniqueViolation`, which `ensure_slack_schema` propagates, which
# `ensure_write_path_schema` propagates, which means `stigmergy.slack.app` cannot boot — a
# deploy-time crash loop, identical on every restart, with no code path that recovers on its own.
# Collapsed here, before the ADD.
#
# **The BLAST RADIUS of that collapse is the harder half.** In the ordinary case BOTH colliding
# rows carry a real `submission_id` pointing at a live `capture_queue` row — the
# `submission_id IS NOT NULL` preference does not distinguish them at all, and the row that loses
# is silently and irreversibly unmapped from Slack: `due_for_report` never reports that submission
# to the thread again (not filed, not rejected, not needs_input), `find_thread_submissions` cannot
# return it, and a `needs_input` question already asked on it can never be answered from Slack — at
# deploy time, against production data. So the tiebreak among rows that BOTH carry a
# `submission_id` is "which one is still most likely waiting on a reply": prefer a LEFT-JOINed
# `capture_queue` row whose status is `{capture_schema.NEEDS_INPUT}` (the one state
# `is_awaiting_reply` names as actually open) over any other status, then the most RECENT by
# `created_at` (an older capture has had more time to progress through its lifecycle and is less
# likely to still be the one a person is about to reply to). And every collapse that loses a row
# with a real `submission_id` is named — the count and the lost id(s) — BEFORE the delete: a
# migration that changes production data without a trace is the failure mode this whole entry
# exists to close, not just the crash.
#
# **That trace has to reach the APPLICATION log, not only the Postgres server log.** `RAISE
# WARNING` below is a real Postgres notice, but nothing in `src/` registers `add_notice_handler`
# on the connection this runs on — only this module's OWN test fixture does — so on a deployed
# system the notice is discarded at the client library before anything with "log" in its name sees
# it. The trace an operator can actually find is `_log_pending_dedup_collapse`, which reads the
# identical collapse plan in PYTHON, read-only, BEFORE this block runs, and writes it through this
# module's own logger — see that function for the full argument. `RAISE WARNING` is a secondary
# signal (an interactive `psql` session still sees it), not the primary one.
#
# This exact condition — is the constraint ALREADY today's thread-scoped one — is asked from TWO
# places: the migration's own `IF NOT EXISTS` below, and the Python pre-check
# (`_log_pending_dedup_collapse`) that has to skip just as cleanly on every fresh database and
# every boot after the one that actually migrates. One fragment, so "already migrated" cannot mean
# two different things to the two callers.
_DEDUP_KEY_UP_TO_DATE = f"""
SELECT 1 FROM pg_constraint c
WHERE c.conrelid = 'slack_submissions'::regclass
  AND c.conname = '{_DEDUP_KEY_NAME}'
  AND pg_get_constraintdef(c.oid) = '{_DEDUP_KEY_DEF}'
""".strip()

# The row-ranking rule shared by the Python pre-check (below) and the migration's own temp-table
# plan (`_DEDUP_KEY_MIGRATION`) — ONE fragment, so what "the row that survives a collision" means
# can never drift between the two.
#
# **`(...) IS TRUE DESC`, not bare `(...) DESC`.** Postgres's default null-ordering is NULLS LAST
# for `ASC` and **NULLS FIRST for `DESC`** — so a mapping row whose `submission_id` points at a
# MISSING `capture_queue` row (an orphaned reference; `q.status` is NULL through the LEFT JOIN)
# would sort AHEAD of a genuine `needs_input` row under plain `DESC` (NULL outranks TRUE there),
# the exact opposite of the rule's own stated intent ("prefer the row still awaiting a reply").
# `IS TRUE` maps NULL and FALSE onto the same rank, so only an actual `needs_input` row can win
# this tiebreak.
_COLLAPSE_PLAN_SELECT = f"""
SELECT s.id, s.submission_id, ROW_NUMBER() OVER (
    PARTITION BY s.team_id, s.channel_id, s.thread_ts, s.slack_user_id
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
            UNIQUE (team_id, channel_id, thread_ts, slack_user_id);
    END IF;
END $$
"""

# One row per (item, steward) the doorbell has ever DMed, carrying the STATE it was notified at —
# "one notification per (item, steward), re-sent only on a state change" is enforced by comparing
# this row's `state` against the item's current one, the same "read the last-known value, compare,
# only act on a difference" shape `slack_submissions.last_status`/`due_for_report` already use for
# the fast-lane push channel. Kept in THIS module (not `stigmergy.server.review`) for the same reason
# `slack_submissions` itself lives here: "which (item, steward) pair has already been told" is
# Slack's own delivery bookkeeping, not a fact the capture queue needs to know about itself.
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
    """Read the SAME collapse plan `_DEDUP_KEY_MIGRATION`'s `DO` block is about to apply, BEFORE it
    runs, and put what it will do into the APPLICATION's own logger — not just a Postgres `RAISE
    WARNING`, which reaches the Postgres server log and nothing else. `psycopg`'s
    `Connection.add_notice_handler` is how a caller would receive that notice, and it is registered
    NOWHERE in `src/` — only in this module's own test fixture — so on the real startup connection
    the notice is silently discarded (psycopg3's documented default for a connection with no
    handler). Without this function the migration changes production data leaving a trace only
    where no human and no log aggregation ever looks.

    Read-only (the exact SELECT `_DEDUP_KEY_MIGRATION`'s temp table is built from —
    `_COLLAPSE_PLAN_SELECT`, shared rather than re-derived) and safe to call unconditionally: it
    first asks whether the migration would even run — `_DEDUP_KEY_UP_TO_DATE`, the same condition
    the `DO` block's own `IF NOT EXISTS` checks — and returns immediately when it would not, which
    is every fresh database and every boot after the one that actually migrates.
    """
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
    """Idempotent DDL, behind the SAME startup-DDL advisory lock `capture.ensure_capture_schema`
    and `server.audit.ensure_audit_table` use — `CREATE INDEX IF NOT EXISTS` is not atomic against
    a concurrent creator, and that applies exactly as much to a fresh database's FIRST
    `stigmergy-slack` boot racing the `app`/`worker` processes as it does to theirs.

    `_log_pending_dedup_collapse` runs between `_DDL` (which the check itself depends on —
    `'slack_submissions'::regclass` needs the table to exist first) and `_DEDUP_KEY_MIGRATION`
    (whose DROP/DELETE/ADD this call only OBSERVES, never performs) — so the application log
    carries what is about to happen BEFORE it happens, not a hope that something downstream
    noticed.
    """
    with startup_ddl_lock(conn) as cur:
        cur.execute(_DDL)
        _log_pending_dedup_collapse(cur)
        for statement in _ALL_DDL[1:]:
            cur.execute(statement)


def ensure_write_path_schema(conn) -> None:
    """Both the capture-queue tables AND this module's own table, in one call —
    `stigmergy.slack.app` calls this ONE function at startup instead of importing `stigmergy.capture`
    itself."""
    ensure_capture_schema(conn)
    ensure_slack_schema(conn)


_RESERVE = """
INSERT INTO slack_submissions (team_id, channel_id, message_ts, thread_ts, slack_user_id,
                               submitted_by)
VALUES (%(team_id)s, %(channel_id)s, %(message_ts)s, %(thread_ts)s, %(slack_user_id)s,
        %(submitted_by)s)
ON CONFLICT (team_id, channel_id, thread_ts, slack_user_id) DO NOTHING
RETURNING id
"""


def reserve(conn, *, team_id: str, channel_id: str, message_ts: str, thread_ts: str,
           slack_user_id: str, submitted_by: str) -> int | None:
    """Claim the dedup key for one (thread, reactor) pair. Returns the reservation's id on
    success, or `None` when the key already exists — a redelivered event, a remove-then-re-add, or
    the SAME person reacting to a second message in a thread they already reserved — in which case
    the caller must NOT call `BrainService.submit()` at all: exactly one `capture_queue` row per
    (thread, reactor), never a second one that a later cleanup has to notice and discard."""
    with conn.cursor() as cur:
        cur.execute(_RESERVE, {
            "team_id": team_id, "channel_id": channel_id, "message_ts": message_ts,
            "thread_ts": thread_ts, "slack_user_id": slack_user_id, "submitted_by": submitted_by,
        })
        row = cur.fetchone()
    return row[0] if row else None


def attach_submission(conn, reservation_id: int, submission_id: int) -> None:
    """Record which `capture_queue` row a reservation produced, once `BrainService.submit()` has
    actually succeeded."""
    with conn.cursor() as cur:
        cur.execute("UPDATE slack_submissions SET submission_id = %s WHERE id = %s",
                    (submission_id, reservation_id))


def release_reservation(conn, reservation_id: int) -> None:
    """Undo a reservation whose `BrainService.submit()` call failed — without this, a genuine retry
    of the SAME event would find the dedup key already taken and be silently treated as a duplicate
    of a submission that was never actually made."""
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
    """EVERY Slack-originated submission mapped to this thread, newest first — plural, because a
    thread may legally hold more than one capture (the UNIQUE key is `(team_id, channel_id,
    thread_ts, slack_user_id)`: two DIFFERENT people reacting in the SAME thread each reserve their
    own key). `[]` for an ordinary thread with no capture in it — the common case, and every
    ordinary conversation Slack ever carries.

    **Plural is the whole point.** A query returning only the newest row in the thread carries two
    defects at once: it compares the CURRENT replier against whichever row happens to be newest, so
    an older capture's genuinely-open `needs_input` question is silently dropped once a newer,
    unrelated capture lands in the same thread; and "not `needs_input`" reads as "already answered"
    even for a row that has NEVER been asked anything (`queued`, right after the capture ack).
    `stigmergy.slack.replies` closes both one layer up, by looking at ALL of this thread's rows for
    the RESOLVED replier's own email (never assumed from the newest row) and consulting `q.reply`
    (set only once a `needs_input` question was actually answered) rather than inferring "answered"
    from status alone."""
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
    """Every Slack-originated submission whose `capture_queue` state has moved into one the poller
    must announce, and has not already been reported at THAT status. Read-only: a plain `SELECT`
    joined to `capture_queue`, never a claim (see the module docstring above)."""
    with conn.cursor() as cur:
        cur.execute(_DUE_FOR_REPORT, {"statuses": list(REPORTABLE_STATUSES)})
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def mark_reported(conn, reservation_id: int, status: str) -> None:
    """Record that `status` has been announced for this submission, so the next poll pass does not
    re-post the same news — one message per state change."""
    with conn.cursor() as cur:
        cur.execute("UPDATE slack_submissions SET last_status = %s WHERE id = %s",
                    (status, reservation_id))


# ── the steward doorbell's own delivery bookkeeping ─────────────────────────────────────────────
def last_notified_state(conn, *, item_kind: str, item_id: str, steward_email: str) -> str | None:
    """The state this (item, steward) pair was last DMed at, or `None` if it has never been
    notified — `doorbell.poll_once` compares this against the item's CURRENT state signature and
    sends only on a genuine difference (including "never notified", which always differs)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM steward_notifications "
            "WHERE item_kind = %s AND item_id = %s AND steward_email = %s",
            (item_kind, item_id, steward_email))
        row = cur.fetchone()
    return row[0] if row else None


def mark_notified(conn, *, item_kind: str, item_id: str, steward_email: str, state: str) -> None:
    """Record that this (item, steward) pair has now been DMed at `state` — called ONLY after the
    Slack send actually succeeded (the same `send, then mark` order `poller.poll_once`/
    `mark_reported` already use), so a post that fails leaves nothing recorded and the next poll
    pass retries it rather than silently treating a failed send as delivered."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO steward_notifications (item_kind, item_id, steward_email, state) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (item_kind, item_id, steward_email) "
            "DO UPDATE SET state = EXCLUDED.state, notified_at = now()",
            (item_kind, item_id, steward_email, state))
