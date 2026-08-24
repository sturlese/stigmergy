"""Operational run records and advisory locking."""

from __future__ import annotations

import contextlib
import logging
import uuid
from dataclasses import dataclass, field

from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
RUN_STATUSES = (RUNNING, SUCCEEDED, FAILED)

_RUN_COLUMNS = (
    "id",
    "job",
    "status",
    "started_at",
    "finished_at",
    "base_commit_sha",
    "head_commit_sha",
    "stats",
    "error_category",
    "error",
)
_RUN_SQL = ", ".join(_RUN_COLUMNS)


@dataclass
class JobRun:
    id: uuid.UUID
    job: str
    base_commit_sha: str = ""
    head_commit_sha: str = ""
    stats: dict = field(default_factory=dict)


def start_job(conn, job: str, *, base_commit_sha: str = "") -> JobRun:
    run = JobRun(id=uuid.uuid4(), job=job, base_commit_sha=base_commit_sha)
    with conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO job_runs (id, job, status, base_commit_sha) VALUES (%s, %s, %s, %s)",
            (run.id, job, RUNNING, base_commit_sha),
        )
    return run


def finish_job(
    conn,
    run: JobRun,
    *,
    status: str,
    error_category: str = "",
    error: str = "",
) -> None:
    if status not in {SUCCEEDED, FAILED}:
        raise ValueError("a finished job must be succeeded or failed")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE job_runs
            SET status = %s,
                finished_at = now(),
                head_commit_sha = %s,
                stats = %s,
                error_category = %s,
                error = %s
            WHERE id = %s AND status = %s
            """,
            (
                status,
                run.head_commit_sha,
                Jsonb(run.stats),
                _safe(error_category, 100),
                _safe(error, 1000),
                run.id,
                RUNNING,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("job run is not active")


def _safe(value: str, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def reconcile_job(
    conn,
    run_id: str | uuid.UUID,
    *,
    head_commit_sha: str,
    stats: dict,
) -> dict:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE job_runs
            SET status = %s,
                finished_at = now(),
                head_commit_sha = %s,
                stats = stats || %s,
                error_category = '',
                error = ''
            WHERE id = %s
            RETURNING {_RUN_SQL}
            """,
            (SUCCEEDED, head_commit_sha, Jsonb(stats), run_id),
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("job run does not exist")
    return _shape_run(row)


@contextlib.contextmanager
def job_run(conn, job: str, *, base_commit_sha: str = ""):
    run = start_job(conn, job, base_commit_sha=base_commit_sha)
    try:
        yield run
    except Exception as error:
        finish_job(
            conn,
            run,
            status=FAILED,
            error_category=error.__class__.__name__,
            error="job failed",
        )
        raise
    else:
        finish_job(conn, run, status=SUCCEEDED)


def latest_run(conn, job: str, *, successful: bool = False) -> dict | None:
    query = f"SELECT {_RUN_SQL} FROM job_runs WHERE job = %s"
    params: list = [job]
    if successful:
        query += " AND status = %s"
        params.append(SUCCEEDED)
    query += " ORDER BY started_at DESC LIMIT 1"
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        row = cursor.fetchone()
    return _shape_run(row) if row else None


def list_runs(conn, job: str | None = None, *, limit: int = 50) -> list[dict]:
    query = (
        f"SELECT {_RUN_SQL} FROM job_runs "
        "WHERE (%s::text IS NULL OR job = %s) ORDER BY started_at DESC LIMIT %s"
    )
    with conn.cursor() as cursor:
        cursor.execute(query, (job, job, max(1, min(int(limit), 200))))
        rows = cursor.fetchall()
    return [_shape_run(row) for row in rows]


def _shape_run(row) -> dict:
    item = dict(zip(_RUN_COLUMNS, row, strict=True))
    item["id"] = str(item["id"])
    item["started_at"] = item["started_at"].isoformat()
    item["finished_at"] = (
        item["finished_at"].isoformat() if item["finished_at"] else None
    )
    item["stats"] = item["stats"] or {}
    return item


def heartbeat(conn, state: str) -> None:
    safe_state = _safe(state, 40) or "idle"
    with conn.cursor() as cursor:
        cursor.execute(
            "UPDATE worker_heartbeat SET heartbeat_at = now(), state = %s WHERE singleton",
            (safe_state,),
        )


def read_heartbeat(conn) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT heartbeat_at, state FROM worker_heartbeat WHERE singleton")
        row = cursor.fetchone()
    return {"heartbeat_at": row[0].isoformat(), "state": row[1]} if row else None


@contextlib.contextmanager
def try_advisory_lock(conn, key: int):
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s::bigint)", (key,))
        acquired = bool(cursor.fetchone()[0])
    try:
        yield acquired
    finally:
        if acquired:
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s::bigint)", (key,))
            except Exception as error:
                log.warning(
                    "could not release advisory lock (%s)",
                    error.__class__.__name__,
                )
