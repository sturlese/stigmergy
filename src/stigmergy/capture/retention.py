"""Retention: captured material is deleted 30 days after the row that carried it reached a
terminal state.

What goes and what stays is the whole design. `payload` and `hints` — the raw writing, the
suggested placement, the frontmatter a document declared about itself — are set to NULL in place.
`id`, `submitted_by`, `status`, `created_at`/`claimed_at`/`finished_at`, `attempts` and
`result_ref` survive, so the trace and the capture->page latency measurement stay intact and a
purged submission is still readable as history. Keeping the material forever accumulates other
people's raw writing with no policy; deleting the ROW would destroy the record that it ever
happened.

Only TERMINAL rows are eligible (`filed`, `rejected`, `resolved`, `failed`). A `needs_input` or
`triage` row is parked awaiting a human and is never purged out from under the question it is
waiting on, no matter how old — the human has not answered yet.

**`resolved` sits on the ORDINARY window like every other terminal status**, and it does so by
reading `schema.TERMINAL_STATUSES` rather than by naming three states here. That is the intended
answer, not a shortcut: a steward handled the material by hand, the row is done in exactly the
sense retention means, and inventing a longer window for it would keep somebody's raw writing
around because a human rather than an agent closed the row.

**The evidence blob is untouched.** It has its own lifecycle — the bucket's retention policy, set
by the operator — because a filed page must stay checkable against the material it came from
after the queue row has been stripped.

**Honest about "physically".** The UPDATE removes the value from the live row immediately; the
previous row version lives on as a dead tuple until autovacuum reclaims it, as with any Postgres
delete. For an operator who needs a hard guarantee (a data-subject request), that is a `VACUUM`
away and is documented in the reference doc rather than performed here — a purge that took an
exclusive-ish maintenance action on every run would be a surprising thing for a cron to do.
"""
from stigmergy.capture import ops, schema

DEFAULT_RETENTION_DAYS = 30

# The WITHHELD_REASONS reconciliation clause, literal (same convention
# `capture.queue._WITHHELD_REASON_LITERALS` already takes) rather than a third bind parameter —
# `WITHHELD_REASONS` is a fixed, importable set, not something a caller ever varies.
_WITHHELD_REASON_LITERALS = ", ".join(f"'{r}'" for r in sorted(schema.WITHHELD_REASONS))

# One predicate, written once and shared by the preview and the action, so a dry run can never
# describe a different set of rows than the purge that follows it.
#
# Two eligibility clauses, OR'd:
#   1. the ordinary window — any terminal status, older than the retention window;
#   2. the WITHHELD_REASONS reconciliation — a `rejected` row whose `reason_code` is a
#      secret/PII match is eligible REGARDLESS OF AGE. `librarian.worker._finish` already purges
#      such a row's payload/hints immediately (`purge_secret_capture_immediately`, below) — but
#      that immediate purge is a SEPARATE statement, not the same transaction as the rejection
#      write (`index.store.connect` opens autocommit — see that immediate-purge function's own
#      docstring). A crash between the two statements leaves a secrets-rejected row holding its
#      payload at rest, and nothing else revisits it: the ordinary clause above only reaches rows
#      OLDER than the window, and the immediate purge does not run twice on its own. This clause
#      is what makes that gap self-healing — the very next scheduled purge closes it — without
#      requiring the immediate purge and the rejection write to share a transaction.
_ELIGIBLE = f"""
WHERE (payload IS NOT NULL OR hints IS NOT NULL)
  AND (
    (status = ANY(%s) AND finished_at IS NOT NULL
     AND finished_at < now() - make_interval(days => %s))
    OR (status = '{schema.REJECTED}'
        AND report ->> '{schema.REASON_CODE_KEY}' IN ({_WITHHELD_REASON_LITERALS}))
  )
"""

# `outcome` is purged here too. `finish` and `dispose` both clear it on every terminal
# transition, so on a well-behaved row it is already NULL by the time this runs — which is
# exactly why it belongs here too: retention is the belt-and-braces layer for the rows those two
# paths missed (a crash between the status write and the clear, a row written by an older worker),
# and the column holds the full drafted body of every page a distillation produced. Adding it to
# `_ELIGIBLE`'s guard was deliberately NOT done: the guard is what makes the purge idempotent, and
# a row whose payload is already NULL must stay ineligible.
_PURGE = (f"UPDATE capture_queue SET payload = NULL, hints = NULL, outcome = NULL "
          f"{_ELIGIBLE} RETURNING id")
_PREVIEW = f"SELECT id FROM capture_queue {_ELIGIBLE} ORDER BY finished_at"

# ── the secret/PII clock, which is not the ordinary one ────────────────────────────────────────
# A rejection whose REASON is a secret or PII match is the one case where 30 days is the wrong
# window entirely: the system has already declared this material unsafe to keep sitting around,
# and the ordinary retention job runs once a night. `--older-than-days 0` COULD purge it on the
# next scheduled run, but "the next run" is still hours away for a row rejected minutes after
# midnight — this purges it from the one seam every `process_item` result already goes through
# (`librarian.worker._finish`), immediately, rather than waiting for a second, separately-
# scheduled path.
#
# **Honest about atomicity:** this is NOT the same transaction the rejection is written in.
# `index.store.connect` opens every connection `autocommit=True` (a reader must never sit
# idle-in-transaction holding a lock a concurrent rebuild could block behind forever — see that
# function's own docstring), so `queue.finish`'s status write and this function's UPDATE are two
# separate, independently committed statements over the same autocommit connection. A crash
# between them is possible, and is NOT this function's job to prevent — `purge`'s own
# WITHHELD_REASONS clause above is the reconciler for exactly that gap, run nightly regardless.
#
# Idempotent by the same guard `_ELIGIBLE` uses (`payload IS NOT NULL OR hints IS NOT NULL`), so a
# redelivered or re-run finish can call this again for free.
_PURGE_ONE = """
UPDATE capture_queue SET payload = NULL, hints = NULL
WHERE id = %s AND status = %s AND (payload IS NOT NULL OR hints IS NOT NULL)
RETURNING id
"""


def purge_secret_capture_immediately(conn, submission_id: int, *, reason_code: str) -> dict:
    """Purge one `rejected` row's payload/hints RIGHT NOW, because its `reason_code` is a secret or
    PII match (`schema.WITHHELD_REASONS`) — the one rejection reason whose clock is not the
    ordinary 30-day window.

    The caller decides WHETHER this row qualifies (`result.report.get(schema.REASON_CODE_KEY) in
    schema.WITHHELD_REASONS`, checked once, in `librarian.worker._finish` — the same seam every
    other terminal outcome already goes through); this function only acts, and only on the id it is
    given. A `job_runs` row records that the purge happened — the same instrument the scheduled
    retention job writes, so "did this secret/PII capture get its payload purged" is answerable
    from the database, not from trusting that this code path ran.

    Returns `{"purged": bool, "id": submission_id, "reason_code": reason_code}`. `purged` is False
    when the row's payload/hints were already NULL (already purged, or never had one) — not an
    error, just nothing left to do.
    """
    job = "capture-purge-immediate"
    with ops.job_run(conn, job) as stats:
        with conn.cursor() as cur:
            cur.execute(_PURGE_ONE, (submission_id, schema.REJECTED))
            purged = cur.fetchone() is not None
        stats.update({"submission_id": submission_id, "reason_code": reason_code, "purged": purged})
    return {"purged": purged, "id": submission_id, "reason_code": reason_code}


def purge(conn, *, older_than_days: int = DEFAULT_RETENTION_DAYS, dry_run: bool = False) -> dict:
    """Delete the payload and hints of terminal rows older than `older_than_days` — PLUS any
    `rejected` row whose `reason_code` is in `schema.WITHHELD_REASONS`,
    REGARDLESS of age: the self-healing reconciliation for a row whose
    `purge_secret_capture_immediately` call never ran (a crash between the rejection write and
    that separate, non-atomic statement — see that function's own docstring).

    Returns `{"purged": n, "ids": [...], "dry_run": bool}`. `dry_run` lists exactly what would go
    without touching it — the same predicate, so the preview cannot disagree with the action.
    Every run (dry or not) is recorded in `job_runs`; a purge is a scheduled job like any other
    and "did the retention job run last night" must be answerable from the database.
    """
    days = max(0, int(older_than_days))
    terminal = sorted(schema.TERMINAL_STATUSES)
    job = "capture-purge-dry-run" if dry_run else "capture-purge"
    with ops.job_run(conn, job) as stats:
        with conn.cursor() as cur:
            cur.execute(_PREVIEW if dry_run else _PURGE, (terminal, days))
            ids = [row[0] for row in cur.fetchall()]
        stats.update({"purged": len(ids), "older_than_days": days, "dry_run": dry_run})
    return {"purged": len(ids), "ids": ids, "dry_run": dry_run}
