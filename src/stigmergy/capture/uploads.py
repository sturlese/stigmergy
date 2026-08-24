"""Short-lived upload sessions for the local bridge and browser uploads."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

from stigmergy.capture import schema
from stigmergy.capture.errors import EvidenceError, SubmissionRejected, UploadError

UPLOAD_TTL_S = 300
UPLOAD_PREFIX = "uploads"

_DDL = """
CREATE TABLE IF NOT EXISTS upload_sessions (
    id UUID PRIMARY KEY,
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    blob_ref TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes BIGINT NOT NULL,
    media_type TEXT NOT NULL,
    original_name TEXT,
    source_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ,
    consumed_by UUID,
    consumed_at TIMESTAMPTZ,
    UNIQUE (actor, idempotency_key)
)
"""


def ensure_upload_schema(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(_DDL)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS upload_sessions_expiry_idx "
            "ON upload_sessions (expires_at) WHERE consumed_by IS NULL"
        )


def create_upload(
    conn,
    evidence,
    *,
    actor: str,
    idempotency_key: str,
    sha256: str,
    bytes: int,
    media_type: str,
    original_name: str | None = None,
    source_url: str | None = None,
) -> dict:
    artifact = schema.ArtifactRef(
        blob_ref=schema.content_ref(sha256),
        sha256=sha256,
        bytes=bytes,
        media_type=media_type,
        original_name=original_name,
        source_url=source_url,
    )
    session_id = uuid.uuid4()
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO upload_sessions (
                id, actor, idempotency_key, blob_ref, sha256, bytes,
                media_type, original_name, source_url, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                      now() + make_interval(secs => %s))
            ON CONFLICT (actor, idempotency_key) DO NOTHING
            RETURNING id, blob_ref, sha256, bytes, media_type,
                      original_name, source_url, expires_at, verified_at
            """,
            (
                session_id,
                actor,
                idempotency_key,
                artifact.blob_ref,
                artifact.sha256,
                artifact.bytes,
                artifact.media_type,
                artifact.original_name,
                artifact.source_url,
                UPLOAD_TTL_S,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """
                SELECT id, blob_ref, sha256, bytes, media_type,
                       original_name, source_url, expires_at, verified_at
                FROM upload_sessions
                WHERE actor = %s AND idempotency_key = %s
                """,
                (actor, idempotency_key),
            )
            row = cursor.fetchone()
    existing = _row(row)
    if existing["artifact"].model_dump() != artifact.model_dump():
        raise SubmissionRejected("upload idempotency key was already used for different bytes")
    if existing["expires_at"] <= dt.datetime.now(dt.UTC) and not existing["verified_at"]:
        raise UploadError("upload session expired")
    return {
        "upload_id": existing["id"],
        "upload_url": (
            None
            if existing["verified_at"]
            else evidence.presign_put(
                staging_ref(existing["id"]),
                bytes=existing["artifact"].bytes,
            )
        ),
        "expires_at": existing["expires_at"].isoformat(),
    }


def finalize_upload(
    conn,
    evidence,
    *,
    actor: str,
    upload_id: str | uuid.UUID,
) -> schema.ArtifactRef:
    return finalize_uploads(
        conn,
        evidence,
        actor=actor,
        upload_ids=[upload_id],
    )[0]


def finalize_uploads(
    conn,
    evidence,
    *,
    actor: str,
    upload_ids: list[str | uuid.UUID],
) -> tuple[schema.ArtifactRef, ...]:
    if not upload_ids or len({str(value) for value in upload_ids}) != len(upload_ids):
        raise UploadError("upload session list is invalid")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, blob_ref, sha256, bytes, media_type,
                   original_name, source_url, expires_at, verified_at
            FROM upload_sessions
            WHERE actor = %s AND id = ANY(%s::uuid[])
            ORDER BY array_position(%s::uuid[], id)
            FOR UPDATE
            """,
            (actor, upload_ids, upload_ids),
        )
        rows = cursor.fetchall()
        if len(rows) != len(upload_ids):
            raise UploadError("upload session not found")
        sessions = tuple(_row(row) for row in rows)
        if sum(session["artifact"].bytes for session in sessions) > schema.MAX_CAPTURE_BYTES:
            raise UploadError("uploads exceed the capture-wide byte limit")
        now = dt.datetime.now(dt.UTC)
        if any(
            session["expires_at"] <= now and not session["verified_at"]
            for session in sessions
        ):
            raise UploadError("upload session expired")

        pending = []
        for session in sessions:
            if session["verified_at"]:
                continue
            artifact = session["artifact"]
            temporary_ref = staging_ref(session["id"])
            try:
                info = evidence.head(temporary_ref)
                if info.bytes != artifact.bytes:
                    raise UploadError(
                        "uploaded bytes do not match the declared digest and size"
                    )
                data = evidence.get_limited(temporary_ref, max_bytes=artifact.bytes)
            except EvidenceError as error:
                raise UploadError("uploaded bytes are unavailable") from error
            if len(data) != artifact.bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
                raise UploadError("uploaded bytes do not match the declared digest and size")
            pending.append((session, data))

        for session, data in pending:
            artifact = session["artifact"]
            if evidence.put(data) != artifact.blob_ref:
                raise UploadError("uploaded bytes could not be promoted safely")
        if pending:
            cursor.execute(
                "UPDATE upload_sessions SET verified_at = now() "
                "WHERE id = ANY(%s::uuid[]) RETURNING id",
                ([session["id"] for session, _data in pending],),
            )
            if len(cursor.fetchall()) != len(pending):
                raise UploadError("upload verification could not be recorded")
    return tuple(session["artifact"] for session in sessions)


def consume_uploads(
    conn,
    *,
    actor: str,
    upload_ids: list[str | uuid.UUID],
    capture_id: str | uuid.UUID,
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE upload_sessions
            SET consumed_by = %s, consumed_at = COALESCE(consumed_at, now())
            WHERE actor = %s
              AND id = ANY(%s::uuid[])
              AND verified_at IS NOT NULL
              AND (consumed_by IS NULL OR consumed_by = %s)
            RETURNING id
            """,
            (capture_id, actor, upload_ids, capture_id),
        )
        consumed = {str(row[0]) for row in cursor.fetchall()}
    if consumed != {str(value) for value in upload_ids}:
        raise UploadError("uploads could not be linked to this capture")


def purge_expired(conn, evidence=None) -> int:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, blob_ref FROM upload_sessions "
            "WHERE expires_at < now() FOR UPDATE"
        )
        rows = cursor.fetchall()
    if evidence is not None:
        from stigmergy.capture.references import is_live

        for upload_id, _reference in rows:
            evidence.delete(staging_ref(upload_id))
        for reference in {row[1] for row in rows}:
            if not is_live(conn, reference):
                evidence.delete(reference)
    if not rows:
        return 0
    with conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM upload_sessions WHERE id = ANY(%s::uuid[])",
            ([row[0] for row in rows],),
        )
    return len(rows)


def staging_ref(upload_id: str | uuid.UUID) -> str:
    return f"{UPLOAD_PREFIX}/{upload_id}"


def _row(row) -> dict:
    artifact = schema.ArtifactRef(
        blob_ref=row[1],
        sha256=row[2],
        bytes=row[3],
        media_type=row[4],
        original_name=row[5],
        source_url=row[6],
    )
    return {
        "id": str(row[0]),
        "artifact": artifact,
        "expires_at": row[7],
        "verified_at": row[8],
    }
