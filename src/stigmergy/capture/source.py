"""Neutral immutable source-page rendering."""

from __future__ import annotations

import datetime as dt

import yaml

from stigmergy.capture import schema
from stigmergy.capture.extraction import ExtractedArtifact


def source_path(envelope: schema.CaptureEnvelope) -> str:
    captured = envelope.origin.captured_at.astimezone(dt.UTC)
    return (
        f"sources/{captured.year:04d}/{captured.month:02d}/"
        f"{envelope.capture_id}.md"
    )


def render_source(
    envelope: schema.CaptureEnvelope,
    extracted: tuple[ExtractedArtifact, ...],
) -> str:
    if tuple(item.original for item in extracted) != envelope.artifacts:
        raise ValueError("extractions do not match the capture artifacts")
    frontmatter = {
        "id": str(envelope.capture_id),
        "type": "source",
        "submitted_by": envelope.actor.subject,
        "acl": list(envelope.audience) if envelope.audience is not None else None,
        "captured_at": envelope.origin.captured_at.isoformat(),
        "occurred_at": (
            envelope.origin.occurred_at.isoformat()
            if envelope.origin.occurred_at is not None
            else None
        ),
        "origin": envelope.origin.adapter,
        "title": envelope.origin.title,
        "locator": envelope.origin.locator,
        "acquisition": (
            envelope.origin.acquisition.model_dump(mode="json", exclude_none=True)
            if envelope.origin.acquisition is not None
            else None
        ),
        "participants": [
            participant.model_dump(mode="json")
            for participant in envelope.origin.participants
        ],
        "artifacts": [
            {
                "sha256": item.original.sha256,
                "bytes": item.original.bytes,
                "media_type": item.original.media_type,
                "original_name": item.original.original_name,
                "source_url": item.original.source_url,
                "readable_sha256": item.readable_sha256,
                "extractor": item.result.extractor,
                "extractor_version": item.result.extractor_version,
                "ocr_pages": list(item.result.ocr_pages),
            }
            for item in extracted
        ],
        "resolution_of": envelope.intent.resolution_of,
        "resolution_rationale": envelope.intent.rationale,
    }
    frontmatter = {key: value for key, value in frontmatter.items() if value is not None}
    heading = envelope.origin.title or "Captured source"
    body = [f"# {heading}\n"]
    for index, item in enumerate(extracted, start=1):
        name = item.original.original_name or f"artifact-{index}"
        body.append(
            f"\n## Artifact {index}: {name}\n\n"
            f"- Media type: {item.original.media_type}\n"
            f"- Bytes: {item.original.bytes}\n"
            f"- SHA-256: {item.original.sha256}\n"
            f"- Extraction: {item.result.extractor}@{item.result.extractor_version}\n"
            "\n### Readable content\n\n"
        )
        body.append(item.result.text)
        if not item.result.text.endswith("\n"):
            body.append("\n")
    yaml_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
        width=1000,
    ).rstrip()
    return f"---\n{yaml_text}\n---\n\n{''.join(body)}"
