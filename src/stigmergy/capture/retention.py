"""Retention: captured material is deleted 30 days after the row that carried it went terminal.

The row is stripped, never deleted: keeping material forever accumulates other people's raw
writing with no policy, and deleting the ROW would destroy the record that it happened. Only
TERMINAL rows are eligible — a parked row is never purged out from under the question it is
waiting on.

The evidence blob is untouched: it has its own lifecycle (the bucket's retention policy), and a
filed page must stay checkable against its material after the row is stripped. "Physically":
the UPDATE clears the live row; the dead tuple lives until autovacuum, and a hard guarantee for
a data-subject request is a `VACUUM` away — documented, not performed here.
"""
from stigmergy.capture import ops, schema

DEFAULT_RETENTION_DAYS = 30

# The `job_runs` names this module writes under. Constants rather than literals at the call site
# because the librarian's night shift asks "when did JOB_NAME last run" to decide whether today's
# purge is due — so the name the pass records and the name the scheduler reads must be one string,
# not two that happen to match.
JOB_NAME = "capture-purge"
DRY_RUN_JOB_NAME = "capture-purge-dry-run"
IMMEDIATE_JOB_NAME = "capture-purge-immediate"

# Literal, not a bind parameter: `WITHHELD_REASONS` is a fixed, importable set.
_WITHHELD_REASON_LITERALS = schema.sql_literals(schema.WITHHELD_REASONS)

# One predicate shared by the preview and the action, so a dry run can never describe a
# different set of rows than the purge that follows it. Two clauses, OR'd:
#   1. the ordinary window — any terminal status older than the retention window;
#   2. the secret/PII reconciliation — a `rejected` row whose `reason_code` is a secret/PII
#      match, REGARDLESS OF AGE: the immediate purge (`purge_secret_capture_immediately`) is a
#      separate autocommit statement from the rejection write, so a crash between them leaves a
#      secrets-rejected row holding its payload and nothing else revisits it. This clause makes
#      that gap self-healing on the next scheduled purge.
_ELIGIBLE = f"""
WHERE (payload IS NOT NULL OR hints IS NOT NULL)
  AND (
    (status = ANY(%s) AND finished_at IS NOT NULL
     AND finished_at < now() - make_interval(days => %s))
    OR (status = '{schema.REJECTED}'
        AND report ->> '{schema.REASON_CODE_KEY}' IN ({_WITHHELD_REASON_LITERALS}))
  )
"""

# `outcome` is purged here too — belt and braces for rows the terminal-clear path in `finish`
# missed (a crash between the status write and the clear, an older worker), since the
# column holds the full drafted body of every page. Deliberately NOT added to `_ELIGIBLE`'s
# guard: the guard is what makes the purge idempotent, and an already-NULL payload row must stay
# ineligible.
_PURGE = (f"UPDATE capture_queue SET payload = NULL, hints = NULL, outcome = NULL "
          f"{_ELIGIBLE} RETURNING id")
_PREVIEW = f"SELECT id FROM capture_queue {_ELIGIBLE} ORDER BY finished_at"

# NOT the same transaction as the rejection write — the connection is autocommit, so they are
# two independently committed statements; a crash between them is `purge`'s reconciliation
# clause's job, not this function's. Idempotent by the same payload/hints-not-NULL guard.
_PURGE_ONE = """
UPDATE capture_queue SET payload = NULL, hints = NULL
WHERE id = %s AND status = %s AND (payload IS NOT NULL OR hints IS NOT NULL)
RETURNING id
"""


def purge_secret_capture_immediately(conn, submission_id: int, *, reason_code: str) -> dict:
    """Purge one `rejected` row's payload/hints RIGHT NOW — its `reason_code` is a secret/PII
    match, the one rejection whose clock is not the 30-day window.

    The caller decides WHETHER the row qualifies (checked once, in `librarian.worker._finish`);
    this function only acts on the id it is given. A `job_runs` row records the purge, so "did
    it happen" is answerable from the database. Returns `{"purged", "id", "reason_code"}`;
    `purged` is False when payload/hints were already NULL — nothing left to do, not an error.
    """
    job = IMMEDIATE_JOB_NAME
    with ops.job_run(conn, job) as stats:
        with conn.cursor() as cur:
            cur.execute(_PURGE_ONE, (submission_id, schema.REJECTED))
            purged = cur.fetchone() is not None
        stats.update({"submission_id": submission_id, "reason_code": reason_code, "purged": purged})
    return {"purged": purged, "id": submission_id, "reason_code": reason_code}


def purge(conn, *, older_than_days: int = DEFAULT_RETENTION_DAYS, dry_run: bool = False) -> dict:
    """Delete the payload and hints of terminal rows older than `older_than_days` — PLUS any
    `rejected` row whose `reason_code` is a secret/PII match, regardless of age (the
    reconciliation for an immediate purge that never ran).

    Returns `{"purged": n, "ids": [...], "dry_run": bool}`. `dry_run` uses the same predicate,
    so the preview cannot disagree with the action; every run is recorded in `job_runs`.
    """
    days = max(0, int(older_than_days))
    terminal = sorted(schema.TERMINAL_STATUSES)
    job = DRY_RUN_JOB_NAME if dry_run else JOB_NAME
    with ops.job_run(conn, job) as stats:
        with conn.cursor() as cur:
            cur.execute(_PREVIEW if dry_run else _PURGE, (terminal, days))
            ids = [row[0] for row in cur.fetchall()]
        stats.update({"purged": len(ids), "older_than_days": days, "dry_run": dry_run})
    return {"purged": len(ids), "ids": ids, "dry_run": dry_run}
