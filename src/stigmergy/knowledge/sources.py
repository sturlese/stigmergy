"""Strict parsing and repository-safe reads for immutable source pages."""

from __future__ import annotations

import datetime as dt
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from stigmergy.capture import schema
from stigmergy.index.corpus import split_frontmatter_checked
from stigmergy.knowledge.pages import PageContractError

SOURCE_PATH_RE = re.compile(
    r"^sources/(?P<year>\d{4})/(?P<month>0[1-9]|1[0-2])/"
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.md$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_FIELDS = frozenset(
    {
        "id",
        "type",
        "submitted_by",
        "acl",
        "captured_at",
        "occurred_at",
        "origin",
        "title",
        "locator",
        "acquisition",
        "participants",
        "artifacts",
        "resolution_of",
        "resolution_rationale",
    }
)
REQUIRED_SOURCE_FIELDS = frozenset(
    {
        "id",
        "type",
        "submitted_by",
        "captured_at",
        "origin",
        "participants",
        "artifacts",
    }
)
ARTIFACT_FIELDS = frozenset(
    {
        "sha256",
        "bytes",
        "media_type",
        "original_name",
        "source_url",
        "readable_sha256",
        "extractor",
        "extractor_version",
        "ocr_pages",
    }
)
REQUIRED_ARTIFACT_FIELDS = frozenset(
    {
        "sha256",
        "bytes",
        "media_type",
        "readable_sha256",
        "extractor",
        "extractor_version",
        "ocr_pages",
    }
)


class SourceContractError(PageContractError):
    pass


@dataclass(frozen=True)
class SourceDocument:
    path: str
    acl: tuple[str, ...] | None
    title: str
    captured_at: dt.datetime
    body: str
    text: str
    byte_size: int


def parse_source(path: str, text: str, *, byte_size: int | None = None) -> SourceDocument:
    match = SOURCE_PATH_RE.fullmatch(path)
    metadata, body, malformed = split_frontmatter_checked(text)
    if not match or malformed or metadata.get("type") != "source":
        raise SourceContractError("source path or frontmatter is invalid")
    fields = set(metadata)
    if fields - SOURCE_FIELDS or not fields >= REQUIRED_SOURCE_FIELDS:
        raise SourceContractError("source frontmatter contains unsupported fields")
    if str(metadata.get("id") or "") != match.group("id") or not body.strip():
        raise SourceContractError("source id or body is invalid")
    try:
        origin = schema.Origin.model_validate(
            {
                "adapter": metadata.get("origin"),
                "captured_at": metadata.get("captured_at"),
                "occurred_at": metadata.get("occurred_at"),
                "title": metadata.get("title"),
                "locator": metadata.get("locator"),
                "participants": metadata.get("participants"),
                "acquisition": metadata.get("acquisition"),
            }
        )
        submitted_by = str(metadata.get("submitted_by") or "")
        schema.Actor(subject=submitted_by, display_name=submitted_by)
        schema.CaptureIntent(
            resolution_of=metadata.get("resolution_of"),
            rationale=metadata.get("resolution_rationale"),
        )
    except ValueError as error:
        raise SourceContractError("source provenance is invalid") from error
    captured_at = origin.captured_at
    if (
        captured_at.year != int(match.group("year"))
        or captured_at.month != int(match.group("month"))
    ):
        raise SourceContractError("source path does not match captured_at")
    _validate_artifacts(metadata.get("artifacts"))
    title = origin.title or "Captured source"
    if body.strip().splitlines()[0].strip() != f"# {title}":
        raise SourceContractError("source heading does not match its title")
    return SourceDocument(
        path=path,
        acl=_acl(metadata.get("acl")),
        title=title,
        captured_at=captured_at,
        body=body,
        text=text,
        byte_size=byte_size if byte_size is not None else len(text.encode("utf-8")),
    )


def source_file_size(root: str, relative: str) -> int:
    path = _safe_source_path(root, relative)
    return path.stat(follow_symlinks=False).st_size


def read_source(root: str, relative: str, *, max_bytes: int) -> SourceDocument:
    path = _safe_source_path(root, relative)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SourceContractError("source is not a regular file")
        if info.st_size > max_bytes:
            raise SourceContractError("source exceeds its read limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(data) > max_bytes:
        raise SourceContractError("source exceeds its read limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceContractError("source is not valid UTF-8") from error
    return parse_source(relative, text, byte_size=len(data))


def _safe_source_path(root: str, relative: str) -> Path:
    if not SOURCE_PATH_RE.fullmatch(relative):
        raise SourceContractError("source path is invalid")
    resolved_root = Path(root).resolve(strict=True)
    candidate = resolved_root.joinpath(*relative.split("/"))
    current = resolved_root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise SourceContractError("source path contains a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise SourceContractError("source does not exist") from error
    if not resolved.is_relative_to(resolved_root):
        raise SourceContractError("source resolves outside the repository")
    try:
        mode = resolved.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise SourceContractError("source cannot be read") from error
    if not stat.S_ISREG(mode):
        raise SourceContractError("source is not a regular file")
    return resolved


def _validate_artifacts(value) -> None:
    if not isinstance(value, list) or not value:
        raise SourceContractError("source artifacts must be a non-empty list")
    for artifact in value:
        if not isinstance(artifact, dict):
            raise SourceContractError("source artifact metadata is incomplete")
        fields = set(artifact)
        if fields - ARTIFACT_FIELDS or not fields >= REQUIRED_ARTIFACT_FIELDS:
            raise SourceContractError("source artifact metadata is incomplete")
        digest = str(artifact.get("sha256") or "")
        try:
            schema.ArtifactRef(
                blob_ref=schema.content_ref(digest),
                sha256=digest,
                bytes=artifact.get("bytes"),
                media_type=artifact.get("media_type"),
                original_name=artifact.get("original_name"),
                source_url=artifact.get("source_url"),
            )
        except ValueError as error:
            raise SourceContractError("source artifact metadata is invalid") from error
        if not _SHA256_RE.fullmatch(str(artifact.get("readable_sha256") or "")):
            raise SourceContractError("source readable sha256 is invalid")
        for key in ("media_type", "extractor", "extractor_version"):
            if not isinstance(artifact.get(key), str) or not artifact[key].strip():
                raise SourceContractError("source artifact metadata is incomplete")
        ocr_pages = artifact.get("ocr_pages")
        if (
            not isinstance(ocr_pages, list)
            or any(not isinstance(page, int) or isinstance(page, bool) or page < 1 for page in ocr_pages)
            or len(ocr_pages) != len(set(ocr_pages))
        ):
            raise SourceContractError("source artifact OCR pages are invalid")


def _acl(value) -> tuple[str, ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise SourceContractError("source acl is invalid")
    if len(value) != len(set(value)):
        raise SourceContractError("source acl contains duplicates")
    return tuple(value)
