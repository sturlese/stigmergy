"""Structured mutation plan returned by the librarian model."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stigmergy.kernel.normalize import resolution_key

Alias = Annotated[str, Field(min_length=1, max_length=300)]
# Ledger prose asks the librarian for at most 1000 characters but accepts longer text: a long
# summary or reason must never cost a whole plan.
LEDGER_TEXT_LIMIT = {"maxLength": 1000}
EntityType = Literal["person", "organization", "product", "tool", "repository", "place"]


class PageMutation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Annotated[
        Literal["create", "update", "delete"],
        Field(description="The intended mutation: create a page, update its path, or delete its path."),
    ]
    role: Annotated[
        Literal["note", "concept", "entity", "project"] | None,
        Field(description="Required only for create. Update role values are tolerated but ignored."),
    ] = None
    path: Annotated[
        str | None,
        Field(description="Existing page path required for update and delete; never provide it for create."),
    ] = None
    title: Annotated[
        str | None,
        Field(max_length=300, description="Required for create. Update title values are tolerated but ignored."),
    ] = None
    body: Annotated[
        str | None,
        Field(max_length=50_000, description="Complete Markdown body required for create and update; omit for delete."),
    ] = None
    status: Annotated[
        Literal["seed", "developing", "mature", "evergreen"] | None,
        Field(description="Optional editorial maturity for create or update; omit for delete."),
    ] = None
    entity_type: Annotated[
        EntityType | None,
        Field(description="Required when creating an entity page; never provide it for notes or concepts."),
    ] = None
    aliases: Annotated[
        tuple[Alias, ...] | None,
        Field(
            max_length=20,
            description=(
                "Entity pages only: alternate names copied verbatim from the source. Create may omit it. "
                "Update null preserves existing aliases; update [] clears them."
            ),
        ),
    ] = None
    reason: Annotated[
        str,
        Field(min_length=1, json_schema_extra=LEDGER_TEXT_LIMIT,
              description="Concise factual explanation for the change ledger."),
    ]

    @model_validator(mode="after")
    def complete(self):
        if self.action == "create" and (not self.role or not self.title or not self.body):
            raise ValueError("create requires role, title, and body")
        if self.action == "create" and self.path:
            raise ValueError("create paths are derived from role and title")
        if self.action == "create" and self.role == "entity" and not self.entity_type:
            raise ValueError("creating an entity page requires its entity type")
        if self.action == "create" and self.role != "entity" and (self.entity_type or self.aliases):
            raise ValueError("only entity pages carry an entity type or aliases")
        if self.action == "update" and (not self.path or not self.body):
            raise ValueError("update requires path and body")
        if self.action == "delete" and not self.path:
            raise ValueError("delete requires path")
        if self.action == "delete" and any(
            value is not None
            for value in (self.role, self.title, self.body, self.status, self.entity_type, self.aliases)
        ):
            raise ValueError("delete accepts only a path and reason")
        if self.aliases is not None:
            keys = [resolution_key(value) for value in self.aliases]
            if len(set(keys)) != len(keys) or any(not any(c.isalnum() for c in key) for key in keys):
                raise ValueError("aliases must be unique searchable names")
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

    summary: Annotated[
        str,
        Field(min_length=1, json_schema_extra=LEDGER_TEXT_LIMIT,
              description="Plain-English account of what the wiki learned."),
    ]
    mutations: Annotated[
        tuple[PageMutation, ...],
        Field(max_length=32,
              description="Only deliberate note, concept, entity, or project mutations for this source."),
    ] = ()
    contradictions: Annotated[
        tuple[ContradictionProposal, ...],
        Field(max_length=10, description="Evidence-backed unresolved contradictions to file."),
    ] = ()
    resolved_contradictions: Annotated[
        tuple[str, ...],
        Field(max_length=20, description="Only contradiction IDs explicitly resolved by this capture."),
    ] = ()


class RepairMutation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: Annotated[str, Field(min_length=1, max_length=500)]
    body: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100_000,
            description="Replacement Markdown body only; the writer preserves page metadata.",
        ),
    ]
    reason: Annotated[str, Field(min_length=1, json_schema_extra=LEDGER_TEXT_LIMIT)]


class RepairPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: Annotated[str, Field(min_length=1, json_schema_extra=LEDGER_TEXT_LIMIT)]
    mutations: Annotated[tuple[RepairMutation, ...], Field(max_length=12)] = ()
