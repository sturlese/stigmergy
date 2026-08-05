"""The operational spine: `job_runs` and `ingest_errors` writers.

Split out of `queue.py` and `retention.py` because both write it and so does the librarian
worker — one place that knows how a run is recorded, rather than three hand-rolled INSERTs that
drift. The tables' DDL lives with the rest of the write path in `schema.py`.

`job_run` is a context manager so the finished/failed bookkeeping cannot be forgotten: the row is
written when the block exits, whichever way it exits, and the exception still propagates. A
failure to WRITE the bookkeeping never fails the work it was recording (same posture as
`server.audit.AuditWriter`: observability must not take down the thing it observes).

**`job_runs.status` vocabulary — enforced by convention, not by the database: this column is
plain TEXT with no CHECK constraint, unlike `capture_queue.status`.** Three values are in use
across this codebase's callers:

- `'ok'` — the run completed; everything it computed is trustworthy end to end, including as a
  baseline for the NEXT run (a watermark, a "latest completed run" read).
- `'error'` — the run aborted before its own work could be trusted. Whatever `stats` holds is
  partial by accident, not by design, and no reader may treat an `'error'` row as a completed
  baseline for anything.
- `'partial'` (the one live user is `stigmergy.gardener`) — the run's PRIMARY work completed and
  committed (findings persisted, a report renderable), but an independent AUXILIARY sub-pass
  inside the SAME run failed and produced nothing this time. Safe to read for the primary work
  (`gardener.store.latest_completed_run` widens to `'ok'`/`'partial'` for exactly this reason);
  never safe as a baseline for whatever the failed sub-pass was measuring
  (`gardener.sweep.previous_run_watermark` stays `'ok'`-only on purpose — see that function's own
  docstring, and `gardener.run.run_gardener`'s module docstring for the failure this status exists
  to stop: a sweep-failed run committing `'ok'` silently advanced the sweep's own watermark past
  pages nothing had actually judged, so a week of model outage meant a week of pages permanently
  excluded from the "changed" set while `job_runs WHERE status='error'` said nothing had failed).

Most jobs in this codebase are all-or-nothing and should keep using `'ok'`/`'error'` only —
`'partial'` exists for a run with a genuinely independent, separately-failable sub-pass, not as a
generic "mostly fine" escape hatch. `job_runs.status` semantics are per-caller convention, and
this docstring is where that convention is written down; before inventing a fourth value, or
reusing `'partial'` for an unrelated meaning, read this paragraph and update it.
"""
import contextlib
import logging

from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

# `ingest_errors.source` for anything that failed inside the capture queue. `source_doc_id` is
# then the queue row's id as text — the join back to `capture_queue`.
SOURCE_CAPTURE_QUEUE = "capture_queue"

_INSERT_JOB_RUN = """
INSERT INTO job_runs (job, status, started_at, finished_at, stats, error)
VALUES (%s, %s, now(), now(), %s, %s)
RETURNING id
"""
_INSERT_INGEST_ERROR = """
INSERT INTO ingest_errors (source, source_doc_id, stage, error, attempts, last_at)
VALUES (%s, %s, %s, %s, %s, now())
RETURNING id
"""


def record_job_run(conn, job: str, *, status: str = "ok", stats: dict | None = None,
                   error: str = "") -> int | None:
    """One `job_runs` row for a completed run. Returns its id, or None if the write failed (which
    is logged loudly and swallowed — see the module docstring)."""
    try:
        with conn.cursor() as cur:
            cur.execute(_INSERT_JOB_RUN, (job, status, Jsonb(stats or {}), error))
            return cur.fetchone()[0]
    except Exception:  # noqa: BLE001 — bookkeeping must never fail the work it records
        log.error("job_runs write failed (job=%s status=%s)", job, status, exc_info=True)
        return None


def record_ingest_error(conn, *, source_doc_id: str, stage: str, error: str, attempts: int,
                        source: str = SOURCE_CAPTURE_QUEUE) -> int | None:
    """One `ingest_errors` row for a failed item: which item, which stage, how many attempts it
    burned. `resolved` defaults to false — a steward flips it when the item is dealt with."""
    try:
        with conn.cursor() as cur:
            cur.execute(_INSERT_INGEST_ERROR, (source, source_doc_id, stage, error, attempts))
            return cur.fetchone()[0]
    except Exception:  # noqa: BLE001 — same reason as record_job_run
        log.error("ingest_errors write failed (doc=%s stage=%s)", source_doc_id, stage,
                  exc_info=True)
        return None


@contextlib.contextmanager
def job_run(conn, job: str):
    """Record a `job_runs` row around a block of work, ok or error.

        with job_run(conn, "capture-purge") as stats:
            stats["purged"] = purge(...)

    The yielded dict is the run's `stats` jsonb — mutate it in the block. An exception inside the
    block writes `status='error'` with the exception CLASS name (never `str(ex)`: a raised message
    can carry captured content, and content never reaches a log) and then re-raises.
    """
    stats: dict = {}
    try:
        yield stats
    except Exception as ex:
        record_job_run(conn, job, status="error", stats=stats, error=ex.__class__.__name__)
        raise
    else:
        record_job_run(conn, job, status="ok", stats=stats)
