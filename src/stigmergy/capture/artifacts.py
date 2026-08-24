"""Artifact validation and media detection."""

from __future__ import annotations

import io
import zipfile

from stigmergy.capture import schema
from stigmergy.capture.errors import ArtifactRejected

MAX_CONTAINER_ENTRIES = 10_000
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_PDF = b"%PDF-"
_ZIP = b"PK\x03\x04"
_TEXT_MEDIA_TYPES = frozenset({schema.MEDIA_TEXT, schema.MEDIA_MARKDOWN})


def detect_media(
    data: bytes,
    *,
    declared: str | None = None,
    original_name: str | None = None,
) -> str:
    if not data:
        raise ArtifactRejected("artifact is empty")
    if len(data) > schema.MAX_ARTIFACT_BYTES:
        raise ArtifactRejected(
            f"artifact exceeds the {schema.MAX_ARTIFACT_BYTES}-byte limit"
        )
    normalized = declared.split(";", 1)[0].strip().lower() if declared else ""
    if normalized == schema.MEDIA_SLACK:
        from stigmergy.slack.snapshot import validate_snapshot

        validate_snapshot(data)
        return schema.MEDIA_SLACK
    detected = _detect(data, original_name=original_name)
    if normalized in _TEXT_MEDIA_TYPES and detected in _TEXT_MEDIA_TYPES:
        # UTF-8 bytes do not encode a Markdown/plain distinction; retain the validated declaration.
        return normalized
    if declared and normalized not in {"application/octet-stream", detected}:
        raise ArtifactRejected("declared media type does not match the artifact bytes")
    return detected


def _detect(data: bytes, *, original_name: str | None) -> str:
    if data.startswith(_PDF):
        return schema.MEDIA_PDF
    if data.startswith(_PNG):
        return schema.MEDIA_PNG
    if data.startswith(_JPEG):
        return schema.MEDIA_JPEG
    if data.startswith(_ZIP):
        return _detect_openxml(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ArtifactRejected("unsupported or corrupt binary artifact") from error
    if "\x00" in text:
        raise ArtifactRejected("text artifact contains null bytes")
    prefix = text[:4096].lstrip().lower()
    if prefix.startswith(("<!doctype html", "<html", "<?xml")) or (
        "<html" in prefix and "</" in text.lower()
    ):
        return schema.MEDIA_HTML
    if str(original_name or "").lower().endswith((".md", ".markdown")):
        return schema.MEDIA_MARKDOWN
    return schema.MEDIA_TEXT


def _detect_openxml(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            _validate_archive(archive)
            names = set(archive.namelist())
            content_types = archive.read("[Content_Types].xml")
    except ArtifactRejected:
        raise
    except (KeyError, OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ArtifactRejected("unsupported or corrupt container artifact") from error
    if b"wordprocessingml.document.main+xml" in content_types and "word/document.xml" in names:
        return schema.MEDIA_DOCX
    if b"presentationml.presentation.main+xml" in content_types and "ppt/presentation.xml" in names:
        return schema.MEDIA_PPTX
    raise ArtifactRejected("unsupported container format")


def validate_openxml(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            _validate_archive(archive)
    except ArtifactRejected:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ArtifactRejected("unsafe or corrupt container artifact") from error


def _validate_archive(archive: zipfile.ZipFile) -> None:
    entries = archive.infolist()
    if len(entries) > MAX_CONTAINER_ENTRIES:
        raise ArtifactRejected("container has too many entries")
    total = 0
    for entry in entries:
        path = entry.filename.replace("\\", "/")
        if path.startswith("/") or ".." in path.split("/"):
            raise ArtifactRejected("container contains an unsafe path")
        if entry.flag_bits & 0x1:
            raise ArtifactRejected("encrypted containers are not supported")
        total += entry.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ArtifactRejected("container expands beyond the safety limit")
        if (
            entry.compress_size > 0
            and entry.file_size > 1024 * 1024
            and entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise ArtifactRejected("container compression ratio exceeds the safety limit")
