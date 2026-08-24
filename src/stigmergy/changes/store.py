"""Append-only storage for landed knowledge changes."""

from __future__ import annotations

import gzip
import uuid

from psycopg.types.json import Jsonb

from stigmergy.changes import diff
from stigmergy.changes.model import ChangeRecord, PathChange

_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_changes (
    id UUID PRIMARY KEY,
    trigger TEXT NOT NULL CHECK (
        trigger IN ('capture', 'garden', 'delete', 'contradiction_resolution', 'entity')
    ),
    actor TEXT NOT NULL,
    capture_id UUID,
    job_run_id UUID,
    parent_commit_sha TEXT NOT NULL,
    commit_sha TEXT NOT NULL UNIQUE,
    summary TEXT NOT NULL,
    manifest JSONB NOT NULL,
    exact_patch_ref TEXT NOT NULL,
    exact_patch_sha256 TEXT NOT NULL,
    exact_patch_bytes BIGINT NOT NULL CHECK (exact_patch_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS knowledge_changes_created_idx "
    "ON knowledge_changes (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS knowledge_changes_capture_idx "
    "ON knowledge_changes (capture_id) WHERE capture_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS knowledge_changes_job_idx "
    "ON knowledge_changes (job_run_id) WHERE job_run_id IS NOT NULL",
)

_COLUMNS = (
    "id",
    "trigger",
    "actor",
    "capture_id",
    "job_run_id",
    "parent_commit_sha",
    "commit_sha",
    "summary",
    "manifest",
    "exact_patch_ref",
    "exact_patch_sha256",
    "exact_patch_bytes",
    "created_at",
)
_COLUMN_SQL = ", ".join(_COLUMNS)


def ensure_change_schema(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(_DDL)
        for statement in _INDEXES:
            cursor.execute(statement)


def record_change(
    conn,
    evidence,
    *,
    repo: str,
    trigger: str,
    actor: str,
    parent_commit_sha: str,
    commit_sha: str,
    summary: str,
    reasons: dict[str, str] | None = None,
    capture_id: str | uuid.UUID | None = None,
    job_run_id: str | uuid.UUID | None = None,
) -> ChangeRecord:
    patch = diff.exact_patch(repo, parent_commit_sha, commit_sha)
    compressed = diff.compressed_patch(patch)
    patch_ref = evidence.put(compressed)
    manifest = diff.build_manifest(
        repo,
        parent_commit_sha,
        commit_sha,
        reasons=reasons,
        default_reason=summary,
    )
    change_id = uuid.uuid4()
    params = (
        change_id,
        trigger,
        actor,
        capture_id,
        job_run_id,
        parent_commit_sha,
        commit_sha,
        summary,
        Jsonb([item.model_dump(mode="json") for item in manifest]),
        patch_ref,
        diff.patch_sha256(patch),
        len(patch),
    )
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO knowledge_changes (
                id, trigger, actor, capture_id, job_run_id, parent_commit_sha,
                commit_sha, summary, manifest, exact_patch_ref,
                exact_patch_sha256, exact_patch_bytes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (commit_sha) DO NOTHING
            RETURNING {_COLUMN_SQL}
            """,
            params,
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                f"SELECT {_COLUMN_SQL} FROM knowledge_changes WHERE commit_sha = %s",
                (commit_sha,),
            )
            row = cursor.fetchone()
    record = _shape(row)
    if (
        record.parent_commit_sha != parent_commit_sha
        or record.exact_patch_sha256 != diff.patch_sha256(patch)
    ):
        raise RuntimeError("commit is already linked to a different change record")
    return record


def get_change(conn, change_id: str | uuid.UUID) -> ChangeRecord | None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {_COLUMN_SQL} FROM knowledge_changes WHERE id = %s",
            (change_id,),
        )
        row = cursor.fetchone()
    return _shape(row) if row else None


def get_change_by_commit(conn, commit_sha: str) -> ChangeRecord | None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {_COLUMN_SQL} FROM knowledge_changes WHERE commit_sha = %s",
            (commit_sha,),
        )
        row = cursor.fetchone()
    return _shape(row) if row else None


def list_changes(
    conn,
    *,
    trigger: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ChangeRecord]:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {_COLUMN_SQL}
            FROM knowledge_changes
            WHERE (%s::text IS NULL OR trigger = %s)
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (trigger, trigger, max(1, min(int(limit), 200)), max(0, int(offset))),
        )
        return [_shape(row) for row in cursor.fetchall()]


def load_exact_patch(record: ChangeRecord, evidence, *, repo: str, reconstruct=None) -> bytes:
    try:
        compressed = evidence.get(record.exact_patch_ref)
        patch = gzip.decompress(compressed)
    except Exception:
        patch = (
            reconstruct(record)
            if reconstruct is not None
            else diff.exact_patch(repo, record.parent_commit_sha, record.commit_sha)
        )
    if len(patch) != record.exact_patch_bytes:
        raise RuntimeError("exact patch byte count does not match the change record")
    if diff.patch_sha256(patch) != record.exact_patch_sha256:
        raise RuntimeError("exact patch digest does not match the change record")
    return patch


def _shape(row) -> ChangeRecord:
    data = dict(zip(_COLUMNS, row, strict=True))
    data["manifest"] = tuple(PathChange.model_validate(item) for item in data["manifest"])
    data["created_at"] = data["created_at"].isoformat()
    return ChangeRecord.model_validate(data)
