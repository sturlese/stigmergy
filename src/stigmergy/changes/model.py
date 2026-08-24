"""Typed change manifests."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class PathChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    action: Literal["created", "updated", "deleted"]
    page_role: Literal["note", "concept", "entity", "source", "ops"]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    before_sha256: str = ""
    after_sha256: str = ""
    additions: int = 0
    deletions: int = 0
    contradictions_added: tuple[str, ...]
    contradictions_resolved: tuple[str, ...]


class ChangeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: uuid.UUID
    trigger: Literal[
        "capture",
        "garden",
        "delete",
        "contradiction_resolution",
        "entity",
    ]
    actor: str
    capture_id: uuid.UUID | None = None
    job_run_id: uuid.UUID | None = None
    parent_commit_sha: str
    commit_sha: str
    summary: str
    manifest: tuple[PathChange, ...]
    exact_patch_ref: str
    exact_patch_sha256: str
    exact_patch_bytes: int
    created_at: str | None = None
