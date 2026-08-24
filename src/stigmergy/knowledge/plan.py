"""Structured mutation plan returned by the librarian model."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PageMutation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["create", "update", "delete"]
    role: Literal["note", "concept"] | None = None
    path: str | None = None
    title: Annotated[str, Field(max_length=300)] | None = None
    body: Annotated[str, Field(max_length=50_000)] | None = None
    status: Literal["seed", "developing", "mature", "evergreen"] | None = None
    entities: tuple[str, ...] | None = None
    reason: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def complete(self):
        if self.action == "create" and (not self.role or not self.title or not self.body):
            raise ValueError("create requires role, title, and body")
        if self.action == "create" and self.path:
            raise ValueError("create paths are derived from role and title")
        if self.action == "update" and (not self.path or not self.body):
            raise ValueError("update requires path and body")
        if self.action == "delete" and not self.path:
            raise ValueError("delete requires path")
        if self.action == "delete" and any(
            value is not None for value in (self.role, self.title, self.body, self.status, self.entities)
        ):
            raise ValueError("delete accepts only a path and reason")
        return self


class EntityProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=300)]
    entity_type: Annotated[str, Field(min_length=1, max_length=100)]
    same_as: str | None = None
    external_namespace: str | None = None
    external_id: str | None = None

    @model_validator(mode="after")
    def paired_external_id(self):
        if bool(self.external_namespace) != bool(self.external_id):
            raise ValueError("external namespace and id must be provided together")
        return self


class ContradictionClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=2000)]
    source: Annotated[str, Field(min_length=1, max_length=500)]
    date: str | None = None


class ContradictionProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    page_path: Annotated[str, Field(min_length=1, max_length=500)]
    explanation: Annotated[str, Field(min_length=1, max_length=1000)]
    claims: Annotated[tuple[ContradictionClaim, ...], Field(min_length=2, max_length=10)]


class FilingPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    mutations: Annotated[tuple[PageMutation, ...], Field(max_length=12)] = ()
    entities: Annotated[tuple[EntityProposal, ...], Field(max_length=20)] = ()
    contradictions: Annotated[tuple[ContradictionProposal, ...], Field(max_length=10)] = ()
    resolved_contradictions: Annotated[tuple[str, ...], Field(max_length=20)] = ()


class RepairMutation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: Annotated[str, Field(min_length=1, max_length=500)]
    text: Annotated[str, Field(min_length=1, max_length=100_000)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class RepairPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    mutations: Annotated[tuple[RepairMutation, ...], Field(max_length=12)] = ()
