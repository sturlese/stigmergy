"""The operational spine: `job_runs` and `ingest_errors` writers. One place that knows how a
run is recorded; the DDL lives in `schema.py`.

`job_run` is a context manager so the finished/failed bookkeeping cannot be forgotten, and a
failure to WRITE the bookkeeping never fails the work it was recording.

**`job_runs.status` vocabulary — convention only (plain TEXT, no CHECK), and this docstring is
the shared spec: update it before adding a fourth value or reusing one.**

- `'ok'` — the run completed; trustworthy end to end, including as a baseline for the next run.
- `'error'` — the run aborted; `stats` is partial by accident, and no reader may treat the row
  as a completed baseline.
- `'partial'` — the run's PRIMARY work completed and committed, but an independent AUXILIARY
  sub-pass inside the same run failed. Safe to read for the primary work; never safe as a
  baseline for what the failed sub-pass was measuring (a sweep-failed run committing `'ok'` once
  advanced a watermark past pages nothing had judged). `stigmergy.gardener` is the one live
  user; it is not a generic "mostly fine" escape hatch.
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
    burned. `resolved` defaults to false — nothing in this system flips it; an operator does,
    once the item is dealt with."""
    try:
        with conn.cursor() as cur:
            cur.execute(_INSERT_INGEST_ERROR, (source, source_doc_id, stage, error, attempts))
            return cur.fetchone()[0]
    except Exception:  # noqa: BLE001 — same reason as record_job_run
        log.error("ingest_errors write failed (doc=%s stage=%s)", source_doc_id, stage,
                  exc_info=True)
        return None


@contextlib.contextmanager
def try_advisory_lock(conn, key: int):
    """Hold a session-scoped advisory lock for the block if it is free, yielding whether it was
    taken. NON-BLOCKING (`pg_try_advisory_lock`): a caller that cannot have it must be able to say
    so and move on, never queue behind a run that may take minutes.

    RELEASED on the way out rather than left to the connection, because the callers are
    long-running processes: a worker that held its maintenance lock for the life of its connection
    would lock every other worker out permanently, which is a different rule from the one it asked
    for. Failure to release is logged and swallowed — the lock dies with the connection anyway, and
    a cleanup error must not mask whatever the block raised.

    Each caller owns its own `key`, declared beside its own use: two locks sharing one key
    interfere silently. `capture.schema.startup_ddl_lock` is the BLOCKING sibling (a startup DDL
    run must wait, not skip); `slack.app.acquire_singleton_lock` is a third spelling this module
    cannot serve without a `slack -> capture` import that the layering does not have.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s::bigint)", (key,))
        acquired = bool(cur.fetchone()[0])
    try:
        yield acquired
    finally:
        if acquired:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s::bigint)", (key,))
            except Exception:  # noqa: BLE001 — see the docstring: never mask the block's own error
                log.warning("could not release advisory lock %s; it is released when this "
                            "connection closes", key, exc_info=True)


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
