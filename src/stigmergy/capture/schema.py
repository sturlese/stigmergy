"""Normalized capture contracts and the fresh operational schema."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from contextlib import contextmanager
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stigmergy.capture.errors import SubmissionRejected
from stigmergy.capture.provenance import without_capability

QUEUED = "queued"
PROCESSING = "processing"
LANDED = "landed"
FAILED = "failed"
STATUSES = (QUEUED, PROCESSING, LANDED, FAILED)
TERMINAL_STATUSES = frozenset({LANDED, FAILED})

CAPTURE = "capture"
DELETE = "delete"
ENTITY = "entity"
GARDEN = "garden"
OPERATIONS = (CAPTURE, DELETE, ENTITY, GARDEN)

ADAPTERS = ("mcp", "slack", "admin")
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_CAPTURE_BYTES = 50 * 1024 * 1024
MAX_ARTIFACTS = 20
MAX_IDEMPOTENCY_KEY_CHARS = 200
MAX_TITLE_CHARS = 500
MAX_LOCATOR_CHARS = 4096

MEDIA_TEXT = "text/plain"
MEDIA_MARKDOWN = "text/markdown"
MEDIA_HTML = "text/html"
MEDIA_PDF = "application/pdf"
MEDIA_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MEDIA_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MEDIA_PNG = "image/png"
MEDIA_JPEG = "image/jpeg"
MEDIA_SLACK = "application/vnd.stigmergy.slack-thread+json"
SUPPORTED_MEDIA_TYPES = frozenset(
    {
        MEDIA_TEXT,
        MEDIA_MARKDOWN,
        MEDIA_HTML,
        MEDIA_PDF,
        MEDIA_DOCX,
        MEDIA_PPTX,
        MEDIA_PNG,
        MEDIA_JPEG,
        MEDIA_SLACK,
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BLOB_RE = re.compile(r"^sha256/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})$")
_ENTITY_ID_RE = re.compile(
    r"^ent_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DRIVE_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,300}$")
_SOURCE_PATH_RE = re.compile(
    r"^sources/\d{4}/(?:0[1-9]|1[0-2])/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.md$"
)


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(dt.UTC)


def content_ref(digest: str) -> str:
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: Annotated[str, Field(min_length=1, max_length=300)]
    display_name: Annotated[str, Field(min_length=1, max_length=300)]

    @field_validator("subject", "display_name")
    @classmethod
    def clean_identity(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned or any(ord(char) < 32 for char in cleaned):
            raise ValueError("identity contains invalid characters")
        return cleaned


class Participant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    display_name: Annotated[str, Field(min_length=1, max_length=300)]


class AcquisitionProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    original_url: Annotated[str, Field(max_length=MAX_LOCATOR_CHARS)]
    final_url: Annotated[str, Field(max_length=MAX_LOCATOR_CHARS)]
    acquired_at: dt.datetime
    drive_file_id: Annotated[str, Field(max_length=300)] | None = None
    drive_media_type: Annotated[str, Field(max_length=255)] | None = None
    export_media_type: Annotated[str, Field(max_length=255)] | None = None

    @field_validator("original_url", "final_url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        cleaned = value.strip()
        sanitized = without_capability(cleaned)
        if sanitized != cleaned or not sanitized.startswith(("http://", "https://")):
            raise ValueError("acquisition URLs must be sanitized HTTP or HTTPS URLs")
        return sanitized

    @field_validator("acquired_at")
    @classmethod
    def acquired_at_utc(cls, value: dt.datetime) -> dt.datetime:
        return _utc(value)

    @field_validator("drive_file_id")
    @classmethod
    def valid_drive_file_id(cls, value: str | None) -> str | None:
        if value is not None and not _DRIVE_FILE_ID_RE.fullmatch(value):
            raise ValueError("drive_file_id is invalid")
        return value

    @field_validator("drive_media_type", "export_media_type")
    @classmethod
    def clean_media_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if not cleaned or any(ord(char) < 33 or ord(char) == 127 for char in cleaned):
            raise ValueError("acquisition media types must be printable tokens")
        return cleaned

    @model_validator(mode="after")
    def complete_drive_provenance(self):
        drive_fields = (self.drive_file_id, self.drive_media_type, self.export_media_type)
        if any(value is not None for value in drive_fields) and (
            self.drive_file_id is None or self.drive_media_type is None
        ):
            raise ValueError("Drive provenance requires a file id and media type")
        return self


class Origin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter: Literal["mcp", "slack", "admin"]
    captured_at: dt.datetime
    occurred_at: dt.datetime | dt.date | None = None
    title: Annotated[str, Field(max_length=MAX_TITLE_CHARS)] | None = None
    locator: Annotated[str, Field(max_length=MAX_LOCATOR_CHARS)] | None = None
    participants: tuple[Participant, ...] = ()
    acquisition: AcquisitionProvenance | None = None

    @field_validator("captured_at")
    @classmethod
    def captured_at_utc(cls, value: dt.datetime) -> dt.datetime:
        return _utc(value)

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_utc(cls, value: dt.datetime | dt.date | None):
        return _utc(value) if isinstance(value, dt.datetime) else value

    @field_validator("title", "locator")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if any(ord(char) < 32 and char != "\t" for char in cleaned):
            raise ValueError("value contains control characters")
        return cleaned

    @field_validator("locator")
    @classmethod
    def safe_locator(cls, value: str | None) -> str | None:
        return without_capability(value) if value is not None else None


class ArtifactRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    blob_ref: str
    sha256: str
    bytes: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES)]
    media_type: str
    original_name: Annotated[str, Field(max_length=500)] | None = None
    source_url: Annotated[str, Field(max_length=MAX_LOCATOR_CHARS)] | None = None

    @field_validator("sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("media_type")
    @classmethod
    def supported_media_type(cls, value: str) -> str:
        normalized = value.split(";", 1)[0].strip().lower()
        if normalized not in SUPPORTED_MEDIA_TYPES:
            raise ValueError(f"unsupported media type: {normalized or '<empty>'}")
        return normalized

    @field_validator("original_name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or name in {".", ".."} or any(ord(char) < 32 for char in name):
            raise ValueError("original_name is invalid")
        return name

    @field_validator("source_url")
    @classmethod
    def safe_source_url(cls, value: str | None) -> str | None:
        return without_capability(value.strip()) if value else None

    @model_validator(mode="after")
    def blob_matches_digest(self):
        match = _BLOB_RE.fullmatch(self.blob_ref)
        if not match or match.group(3) != self.sha256:
            raise ValueError("blob_ref does not match sha256")
        return self


class CaptureIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    resolution_of: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    rationale: Annotated[str, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def resolution_has_rationale(self):
        if self.resolution_of and not self.rationale:
            raise ValueError("a contradiction resolution requires a rationale")
        if self.rationale and not self.resolution_of:
            raise ValueError("rationale is only valid with resolution_of")
        return self


class CaptureEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    idempotency_key: Annotated[
        str, Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_CHARS)
    ]
    actor: Actor
    audience: tuple[str, ...] | None
    origin: Origin
    artifacts: Annotated[tuple[ArtifactRef, ...], Field(min_length=1, max_length=MAX_ARTIFACTS)]
    intent: CaptureIntent = Field(default_factory=CaptureIntent)

    @field_validator("idempotency_key")
    @classmethod
    def clean_idempotency_key(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(ord(char) < 33 or ord(char) == 127 for char in cleaned):
            raise ValueError("idempotency_key must be one printable token")
        return cleaned

    @field_validator("audience")
    @classmethod
    def valid_audience(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        cleaned = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not cleaned:
            raise ValueError("audience cannot be empty")
        if len(cleaned) != len(value):
            raise ValueError("audience contains empty or duplicate groups")
        return cleaned

    @model_validator(mode="after")
    def capture_bytes_are_bounded(self):
        if sum(artifact.bytes for artifact in self.artifacts) > MAX_CAPTURE_BYTES:
            raise ValueError("artifacts exceed the capture-wide byte limit")
        return self

    def as_json(self) -> dict:
        return self.model_dump(mode="json")


class DeleteRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    idempotency_key: Annotated[
        str, Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_CHARS)
    ]
    actor: Actor
    paths: Annotated[tuple[str, ...], Field(min_length=1, max_length=20)]
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]

    @field_validator("paths")
    @classmethod
    def safe_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(path.strip().replace("\\", "/") for path in value))
        for path in normalized:
            if (
                not path.startswith(("wiki/", "sources/"))
                or path.startswith("wiki/entities/")
                or path.startswith("/")
                or ".." in path.split("/")
                or not path.endswith(".md")
            ):
                raise ValueError(f"path is not deletable: {path}")
        return normalized

    def as_json(self) -> dict:
        return self.model_dump(mode="json")


class SharedExternalIdEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace: Annotated[str, Field(min_length=1, max_length=200)]
    value: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("namespace", "value")
    @classmethod
    def clean_value(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned or any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
            raise ValueError("external-id evidence contains invalid characters")
        return cleaned


class SourceMergeAssertion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: Annotated[str, Field(min_length=1, max_length=500)]
    assertion: Annotated[str, Field(min_length=1, max_length=2000)]

    @field_validator("path")
    @classmethod
    def valid_source_path(cls, value: str) -> str:
        cleaned = value.strip().replace("\\", "/")
        if not _SOURCE_PATH_RE.fullmatch(cleaned):
            raise ValueError("merge evidence must cite an immutable source path")
        return cleaned

    @field_validator("assertion")
    @classmethod
    def clean_assertion(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("source assertion is required")
        return cleaned


class EntityMergeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    shared_external_id: SharedExternalIdEvidence | None = None
    source_assertions: Annotated[tuple[SourceMergeAssertion, ...], Field(max_length=5)] = ()

    @model_validator(mode="after")
    def exactly_one_evidence_mode(self):
        modes = int(self.shared_external_id is not None) + int(bool(self.source_assertions))
        if modes != 1:
            raise ValueError("merge evidence requires one verified evidence mode")
        paths = tuple(item.path for item in self.source_assertions)
        if len(set(paths)) != len(paths):
            raise ValueError("merge evidence contains duplicate source paths")
        return self

    def label(self) -> str:
        if self.shared_external_id is not None:
            return (
                "shared external id "
                f"{self.shared_external_id.namespace}:{self.shared_external_id.value}"
            )
        return "source assertion " + ", ".join(item.path for item in self.source_assertions)


class EntityOperationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    idempotency_key: Annotated[
        str, Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_CHARS)
    ]
    actor: Actor
    action: Literal["merge", "delete"]
    entity_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=20)]
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence: EntityMergeEvidence | None = None

    @field_validator("entity_ids")
    @classmethod
    def valid_entity_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip() for item in value))
        if any(not _ENTITY_ID_RE.fullmatch(item) for item in normalized):
            raise ValueError("entity_ids must contain opaque entity identifiers")
        return normalized

    @model_validator(mode="after")
    def action_arity(self):
        if self.action == "merge" and len(self.entity_ids) < 2:
            raise ValueError("merge requires at least two entity identifiers")
        if self.action == "merge" and self.evidence is None:
            raise ValueError("merge requires verifiable evidence")
        if self.action == "delete" and len(self.entity_ids) != 1:
            raise ValueError("delete requires exactly one entity identifier")
        if self.action == "delete" and self.evidence is not None:
            raise ValueError("delete does not accept merge evidence")
        return self

    def as_json(self) -> dict:
        return self.model_dump(mode="json")


class GardenRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    idempotency_key: Annotated[
        str, Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_CHARS)
    ]
    actor: Actor
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]

    def as_json(self) -> dict:
        return self.model_dump(mode="json")


def parse_capture(value: dict) -> CaptureEnvelope:
    try:
        return CaptureEnvelope.model_validate(value)
    except ValueError as error:
        raise SubmissionRejected(str(error)) from error


def parse_delete(value: dict) -> DeleteRequest:
    try:
        return DeleteRequest.model_validate(value)
    except ValueError as error:
        raise SubmissionRejected(str(error)) from error


def parse_entity_operation(value: dict) -> EntityOperationRequest:
    try:
        return EntityOperationRequest.model_validate(value)
    except ValueError as error:
        raise SubmissionRejected(str(error)) from error


def parse_garden(value: dict) -> GardenRequest:
    try:
        return GardenRequest.model_validate(value)
    except ValueError as error:
        raise SubmissionRejected(str(error)) from error


_STATUS_SQL = ", ".join(f"'{value}'" for value in STATUSES)
_OPERATION_SQL = ", ".join(f"'{value}'" for value in OPERATIONS)

_CAPTURE_QUEUE_DDL = f"""
CREATE TABLE IF NOT EXISTS capture_queue (
    id UUID PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ({_OPERATION_SQL})),
    idempotency_key TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    actor JSONB NOT NULL,
    acl TEXT[],
    request JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT '{QUEUED}' CHECK (status IN ({_STATUS_SQL})),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processing_started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    source_path TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    change_id UUID,
    extraction JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    report JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    error_category TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    UNIQUE (submitted_by, idempotency_key)
)
"""

_CAPTURE_QUEUE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS capture_queue_claim_idx "
    "ON capture_queue (status, next_attempt_at, created_at)",
    "CREATE INDEX IF NOT EXISTS capture_queue_actor_idx "
    "ON capture_queue (submitted_by, created_at DESC)",
)

_JOB_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS job_runs (
    id UUID PRIMARY KEY,
    job TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    base_commit_sha TEXT NOT NULL DEFAULT '',
    head_commit_sha TEXT NOT NULL DEFAULT '',
    stats JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_category TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
)
"""

_JOB_RUNS_INDEX = """
CREATE INDEX IF NOT EXISTS job_runs_job_started_idx ON job_runs (job, started_at DESC)
"""

_WORKER_HEARTBEAT_DDL = """
CREATE TABLE IF NOT EXISTS worker_heartbeat (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    state TEXT NOT NULL DEFAULT 'starting'
)
"""

_WORKER_HEARTBEAT_SEED = """
INSERT INTO worker_heartbeat (singleton) VALUES (TRUE) ON CONFLICT (singleton) DO NOTHING
"""

DURABLE_TABLES = (
    "capture_queue",
    "capture_artifacts",
    "job_runs",
    "knowledge_changes",
    "upload_sessions",
    "index_health",
    "worker_heartbeat",
    "audit_log",
)

_EXPECTED_CAPTURE_COLUMNS = frozenset(
    {
        "id",
        "operation",
        "idempotency_key",
        "submitted_by",
        "actor",
        "acl",
        "request",
        "status",
        "attempts",
        "next_attempt_at",
        "created_at",
        "processing_started_at",
        "finished_at",
        "source_path",
        "commit_sha",
        "change_id",
        "extraction",
        "report",
        "error_category",
        "error",
    }
)

_STARTUP_DDL_LOCK_KEY = int.from_bytes(b"SYNCDDL", "big")


@contextmanager
def startup_ddl_lock(conn):
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s::bigint)", (_STARTUP_DDL_LOCK_KEY,))
        try:
            yield cursor
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s::bigint)", (_STARTUP_DDL_LOCK_KEY,))


def ensure_capture_schema(conn) -> None:
    with startup_ddl_lock(conn) as cursor:
        cursor.execute(_CAPTURE_QUEUE_DDL)
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'capture_queue'"
        )
        found = frozenset(row[0] for row in cursor.fetchall())
        if found != _EXPECTED_CAPTURE_COLUMNS:
            raise RuntimeError("capture_queue does not match the current clean schema")
        for statement in _CAPTURE_QUEUE_INDEXES:
            cursor.execute(statement)
        cursor.execute(_JOB_RUNS_DDL)
        cursor.execute(_JOB_RUNS_INDEX)
        cursor.execute(_WORKER_HEARTBEAT_DDL)
        cursor.execute(_WORKER_HEARTBEAT_SEED)
        from stigmergy.capture.references import ensure_schema as ensure_reference_schema

        ensure_reference_schema(conn)
        from stigmergy.index.health import ensure_schema as ensure_index_health_schema

        ensure_index_health_schema(conn, cursor=cursor)
