"""Canonical Slack thread evidence."""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stigmergy.capture.errors import ArtifactRejected

MAX_THREAD_MESSAGES = 500
MAX_SNAPSHOT_BYTES = 1024 * 1024


class SnapshotAttachment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_index: Annotated[int, Field(ge=1, le=20)]
    file_id: Annotated[str, Field(min_length=1, max_length=200)]
    filename: Annotated[str, Field(min_length=1, max_length=500)]
    media_type: Annotated[str, Field(min_length=1, max_length=200)]
    bytes: Annotated[int, Field(gt=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SnapshotMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order: Annotated[int, Field(ge=1)]
    ts: Annotated[str, Field(min_length=1, max_length=100)]
    occurred_at: dt.datetime
    user_id: Annotated[str, Field(min_length=1, max_length=200)]
    speaker: Annotated[str, Field(min_length=1, max_length=300)]
    text: str
    permalink: Annotated[str, Field(min_length=1, max_length=4096)]
    attachments: tuple[SnapshotAttachment, ...] = ()

    @field_validator("occurred_at")
    @classmethod
    def timestamp_is_aware(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(dt.UTC)


class SlackSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Annotated[int, Field(ge=1, le=1)] = 1
    team_id: Annotated[str, Field(min_length=1, max_length=200)]
    channel_id: Annotated[str, Field(min_length=1, max_length=200)]
    channel_name: Annotated[str, Field(min_length=1, max_length=300)]
    thread_ts: Annotated[str, Field(min_length=1, max_length=100)]
    permalink: Annotated[str, Field(min_length=1, max_length=4096)]
    messages: Annotated[
        tuple[SnapshotMessage, ...],
        Field(min_length=1, max_length=MAX_THREAD_MESSAGES),
    ]

    @model_validator(mode="after")
    def ordered_messages(self):
        expected = tuple(range(1, len(self.messages) + 1))
        if tuple(message.order for message in self.messages) != expected:
            raise ValueError("Slack messages must be in canonical order")
        return self


def canonical_bytes(snapshot: SlackSnapshot) -> bytes:
    data = (
        json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ArtifactRejected("Slack capture exceeds the snapshot byte limit")
    return data


def validate_snapshot(data: bytes) -> SlackSnapshot:
    try:
        value = json.loads(data.decode("utf-8"))
        snapshot = SlackSnapshot.model_validate(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ArtifactRejected("Slack snapshot is invalid") from error
    if canonical_bytes(snapshot) != data:
        raise ArtifactRejected("Slack snapshot is not canonical")
    return snapshot


def timestamp_from_slack(value: str) -> dt.datetime:
    try:
        seconds = float(value)
    except ValueError as error:
        raise ArtifactRejected("Slack timestamp is invalid") from error
    return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)


def render_snapshot(snapshot: SlackSnapshot) -> str:
    parts = [
        f"# Slack thread in #{snapshot.channel_name}\n\n"
        f"- Thread: {snapshot.permalink}\n"
        f"- Messages: {len(snapshot.messages)}\n"
    ]
    for message in snapshot.messages:
        parts.append(
            f"\n## Message {message.order}\n\n"
            f"- Speaker: {message.speaker}\n"
            f"- Timestamp: {message.occurred_at.isoformat()}\n"
            f"- Permalink: {message.permalink}\n\n"
            f"{message.text}\n"
        )
        for attachment in message.attachments:
            parts.append(
                f"\n### Attachment {attachment.artifact_index}: "
                f"{attachment.filename}\n\n"
                f"- Media type: {attachment.media_type}\n"
                f"- Bytes: {attachment.bytes}\n"
                f"- SHA-256: {attachment.sha256}\n"
            )
    return "".join(parts)
