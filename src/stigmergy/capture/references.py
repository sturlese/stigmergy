"""Live evidence references and safe content-addressed garbage collection."""

from __future__ import annotations

import uuid

from stigmergy.capture import schema
from stigmergy.capture.extraction import ExtractedArtifact

_DDL = """
CREATE TABLE IF NOT EXISTS capture_artifacts (
    capture_id UUID NOT NULL,
    position INTEGER NOT NULL CHECK (position > 0),
    source_path TEXT NOT NULL,
    original_ref TEXT NOT NULL,
    readable_ref TEXT NOT NULL,
    live BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ,
    PRIMARY KEY (capture_id, position)
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS capture_artifacts_source_idx "
    "ON capture_artifacts (source_path) WHERE live",
    "CREATE INDEX IF NOT EXISTS capture_artifacts_original_idx "
    "ON capture_artifacts (original_ref) WHERE live",
    "CREATE INDEX IF NOT EXISTS capture_artifacts_readable_idx "
    "ON capture_artifacts (readable_ref) WHERE live",
)


def ensure_schema(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(_DDL)
        for statement in _INDEXES:
            cursor.execute(statement)


def record_capture(
    conn,
    envelope: schema.CaptureEnvelope,
    extracted: tuple[ExtractedArtifact, ...],
    source_path: str,
) -> None:
    if len(extracted) != len(envelope.artifacts):
        raise RuntimeError("capture reference count does not match its artifacts")
    with conn.cursor() as cursor:
        for position, item in enumerate(extracted, start=1):
            cursor.execute(
                """
                INSERT INTO capture_artifacts (
                    capture_id, position, source_path, original_ref, readable_ref
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (capture_id, position) DO UPDATE
                SET live = TRUE, released_at = NULL
                WHERE capture_artifacts.source_path = EXCLUDED.source_path
                  AND capture_artifacts.original_ref = EXCLUDED.original_ref
                  AND capture_artifacts.readable_ref = EXCLUDED.readable_ref
                RETURNING capture_id
                """,
                (
                    envelope.capture_id,
                    position,
                    source_path,
                    item.original.blob_ref,
                    item.readable_ref,
                ),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("capture artifact reference conflicts with its recorded value")


def release_sources(conn, source_paths: set[str]) -> set[str]:
    if not source_paths:
        return set()
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT original_ref, readable_ref
            FROM capture_artifacts
            WHERE source_path = ANY(%s)
            FOR UPDATE
            """,
            (sorted(source_paths),),
        )
        candidates = {value for row in cursor.fetchall() for value in row}
        cursor.execute(
            """
            UPDATE capture_artifacts
            SET live = FALSE, released_at = COALESCE(released_at, now())
            WHERE source_path = ANY(%s) AND live
            """,
            (sorted(source_paths),),
        )
    return {reference for reference in candidates if not is_live(conn, reference)}


def is_live(conn, reference: str) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM capture_artifacts
                    WHERE live AND (original_ref = %s OR readable_ref = %s)
                )
                OR EXISTS (
                    SELECT 1 FROM upload_sessions
                    WHERE consumed_by IS NULL AND expires_at >= now() AND blob_ref = %s
                )
                OR EXISTS (
                    SELECT 1
                    FROM capture_queue AS queued,
                         jsonb_array_elements(
                             COALESCE(queued.request -> 'artifacts', '[]'::jsonb)
                         ) AS artifact
                    WHERE queued.operation = 'capture'
                      AND queued.status <> 'landed'
                      AND artifact ->> 'blob_ref' = %s
                )
                OR EXISTS (
                    SELECT 1 FROM knowledge_changes WHERE exact_patch_ref = %s
                )
            """,
            (reference, reference, reference, reference, reference),
        )
        return bool(cursor.fetchone()[0])


def live_for_capture(conn, capture_id: str | uuid.UUID) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT position, source_path, original_ref, readable_ref
            FROM capture_artifacts
            WHERE capture_id = %s AND live
            ORDER BY position
            """,
            (capture_id,),
        )
        return [
            {
                "position": row[0],
                "source_path": row[1],
                "original_ref": row[2],
                "readable_ref": row[3],
            }
            for row in cursor.fetchall()
        ]
