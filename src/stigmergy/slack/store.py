"""Slack event deduplication and terminal delivery tracking."""

from __future__ import annotations

import uuid

from stigmergy.capture import schema
from stigmergy.capture.schema import ensure_capture_schema, startup_ddl_lock

REPORTABLE_STATUSES = tuple(sorted(schema.TERMINAL_STATUSES))
ACQUISITION_LEASE_MINUTES = 30

_DDL = """
CREATE TABLE IF NOT EXISTS slack_submissions (
    id UUID PRIMARY KEY,
    team_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    slack_user_id TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submission_id UUID,
    last_status TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (team_id, channel_id, message_ts, slack_user_id),
    UNIQUE (team_id, channel_id, thread_ts, slack_user_id)
)
"""

_EXPECTED_COLUMNS = frozenset(
    {
        "id",
        "team_id",
        "channel_id",
        "message_ts",
        "thread_ts",
        "slack_user_id",
        "submitted_by",
        "submission_id",
        "last_status",
        "created_at",
    }
)


def ensure_slack_schema(conn) -> None:
    with startup_ddl_lock(conn) as cursor:
        cursor.execute(_DDL)
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'slack_submissions'"
        )
        if frozenset(row[0] for row in cursor.fetchall()) != _EXPECTED_COLUMNS:
            raise RuntimeError("slack_submissions does not match the current clean schema")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS slack_submissions_submission_idx "
            "ON slack_submissions (submission_id)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS slack_submissions_reaction_idx "
            "ON slack_submissions (team_id, channel_id, message_ts, slack_user_id)"
        )


def ensure_write_path_schema(conn) -> None:
    ensure_capture_schema(conn)
    ensure_slack_schema(conn)


def reserve(
    conn,
    *,
    team_id: str,
    channel_id: str,
    message_ts: str,
    thread_ts: str,
    slack_user_id: str,
    submitted_by: str,
) -> uuid.UUID | None:
    reservation_id = uuid.uuid4()
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO slack_submissions (
                id, team_id, channel_id, message_ts, thread_ts,
                slack_user_id, submitted_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (team_id, channel_id, thread_ts, slack_user_id) DO NOTHING
            RETURNING id
            """,
            (
                reservation_id,
                team_id,
                channel_id,
                message_ts,
                thread_ts,
                slack_user_id,
                submitted_by,
            ),
        )
        row = cursor.fetchone()
    return row[0] if row else None


def reserve_reaction(
    conn,
    *,
    team_id: str,
    channel_id: str,
    message_ts: str,
    slack_user_id: str,
    submitted_by: str,
) -> uuid.UUID | None:
    with conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM slack_submissions "
            "WHERE submission_id IS NULL "
            "AND created_at < now() - make_interval(mins => %s)",
            (ACQUISITION_LEASE_MINUTES,),
        )
    return reserve(
        conn,
        team_id=team_id,
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=message_ts,
        slack_user_id=slack_user_id,
        submitted_by=submitted_by,
    )


def bind_thread(
    conn,
    reservation_id: uuid.UUID,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    slack_user_id: str,
) -> bool:
    lock_key = "\x1f".join((team_id, channel_id, thread_ts, slack_user_id))
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
        cursor.execute(
            """
            SELECT id FROM slack_submissions
            WHERE team_id = %s AND channel_id = %s AND thread_ts = %s
              AND slack_user_id = %s AND id <> %s
            LIMIT 1
            """,
            (team_id, channel_id, thread_ts, slack_user_id, reservation_id),
        )
        if cursor.fetchone() is not None:
            cursor.execute(
                "DELETE FROM slack_submissions WHERE id = %s AND submission_id IS NULL",
                (reservation_id,),
            )
            return False
        cursor.execute(
            "UPDATE slack_submissions SET thread_ts = %s "
            "WHERE id = %s AND submission_id IS NULL RETURNING id",
            (thread_ts, reservation_id),
        )
        return cursor.fetchone() is not None


def attach_submission(conn, reservation_id: uuid.UUID, submission_id: uuid.UUID | str) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "UPDATE slack_submissions SET submission_id = %s WHERE id = %s",
            (submission_id, reservation_id),
        )


def release_reservation(conn, reservation_id: uuid.UUID) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM slack_submissions WHERE id = %s AND submission_id IS NULL",
            (reservation_id,),
        )


def due_for_report(conn) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.id, s.channel_id, s.thread_ts, s.slack_user_id,
                   q.id AS submission_id, q.status, q.report,
                   q.source_path, q.commit_sha, q.change_id,
                   q.error_category, q.error
            FROM slack_submissions s
            JOIN capture_queue q ON q.id = s.submission_id
            WHERE s.submission_id IS NOT NULL
              AND q.status = ANY(%s)
              AND q.status IS DISTINCT FROM NULLIF(s.last_status, '')
            ORDER BY s.created_at, s.id
            """,
            (list(REPORTABLE_STATUSES),),
        )
        columns = [column.name for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def mark_reported(conn, reservation_id: uuid.UUID, status: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "UPDATE slack_submissions SET last_status = %s WHERE id = %s",
            (status, reservation_id),
        )
