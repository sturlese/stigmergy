from __future__ import annotations

import datetime as dt

FULL_REBUILD_MAX_AGE = dt.timedelta(hours=26)
CONVERGENCE_GRACE = dt.timedelta(minutes=15)

_DDL = """
CREATE TABLE IF NOT EXISTS index_health (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    repository_head_sha TEXT NOT NULL DEFAULT '',
    indexed_commit_sha TEXT NOT NULL DEFAULT '',
    dirty BOOLEAN NOT NULL DEFAULT TRUE,
    dirty_since TIMESTAMPTZ,
    last_incremental_at TIMESTAMPTZ,
    last_full_rebuild_at TIMESTAMPTZ,
    indexed_rows BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def ensure_schema(conn, *, cursor=None) -> None:
    if cursor is not None:
        cursor.execute(_DDL)
        cursor.execute(
            "INSERT INTO index_health (singleton, dirty_since) VALUES (TRUE, now()) "
            "ON CONFLICT (singleton) DO NOTHING"
        )
        return
    with conn.cursor() as own:
        ensure_schema(conn, cursor=own)


def mark_dirty(conn, repository_head_sha: str) -> None:
    ensure_schema(conn)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE index_health
            SET repository_head_sha = %s,
                dirty_since = CASE WHEN dirty THEN dirty_since ELSE now() END,
                dirty = TRUE,
                updated_at = now()
            WHERE singleton
            """,
            (repository_head_sha or "",),
        )


def record_incremental(conn, commit_sha: str, *, indexed_rows: int | None = None) -> None:
    ensure_schema(conn)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE index_health
            SET repository_head_sha = %s,
                indexed_commit_sha = %s,
                last_incremental_at = now(),
                indexed_rows = COALESCE(%s, indexed_rows),
                updated_at = now()
            WHERE singleton
            """,
            (commit_sha or "", commit_sha or "", indexed_rows),
        )


def record_full_rebuild(conn, commit_sha: str, indexed_rows: int) -> None:
    ensure_schema(conn)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE index_health
            SET repository_head_sha = %s,
                indexed_commit_sha = %s,
                dirty = FALSE,
                dirty_since = NULL,
                last_full_rebuild_at = now(),
                indexed_rows = %s,
                updated_at = now()
            WHERE singleton
            """,
            (commit_sha or "", commit_sha or "", max(0, int(indexed_rows))),
        )


def read(conn, *, now: dt.datetime | None = None) -> dict:
    ensure_schema(conn)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT repository_head_sha, indexed_commit_sha, dirty, dirty_since,
                   last_incremental_at, last_full_rebuild_at, indexed_rows, updated_at
            FROM index_health WHERE singleton
            """
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("index health row is missing")
    current = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    dirty_since = row[3]
    full_at = row[5]
    warnings = []
    if row[2] and dirty_since and current - dirty_since > CONVERGENCE_GRACE:
        warnings.append("The search index has not converged to repository HEAD within 15 minutes.")
    if full_at is None or current - full_at > FULL_REBUILD_MAX_AGE:
        warnings.append("The last successful full index rebuild is older than 26 hours.")
    return {
        "repository_head_sha": row[0],
        "indexed_commit_sha": row[1],
        "dirty": bool(row[2]),
        "dirty_since": _iso(row[3]),
        "last_incremental_at": _iso(row[4]),
        "last_full_rebuild_at": _iso(row[5]),
        "indexed_rows": int(row[6]),
        "updated_at": _iso(row[7]),
        "warnings": warnings,
        "healthy": not warnings,
    }


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
