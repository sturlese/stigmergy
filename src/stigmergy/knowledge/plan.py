"""Structured mutation plan returned by the librarian model."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stigmergy.kernel.normalize import resolution_key

EntityName = Annotated[str, Field(min_length=1, max_length=300)]
RequiredTerm = Annotated[
    str,
    Field(
        min_length=1,
        max_length=120,
        description="A one-to-eight-word contiguous phrase copied verbatim from the source.",
    ),
]


def _require_entities_in_json_schema(schema: dict) -> None:
    required = schema.setdefault("required", [])
    if "entities" not in required:
        required.append("entities")


class PageMutation(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra=_require_entities_in_json_schema,
    )

    action: Annotated[
        Literal["create", "update", "delete"],
        Field(description="The intended mutation: create a page, update its path, or delete its path."),
    ]
    role: Annotated[
        Literal["note", "concept"] | None,
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
    entities: Annotated[
        tuple[str, ...] | None,
        Field(
            description=(
                "Explicit complete entity anchor set. Create must include this field, using [] when "
                "there are no deliberate links. Update null preserves existing anchors; update [] clears "
                "them. Delete must use null."
            )
        ),
    ] = None
    reason: Annotated[
        str,
        Field(min_length=1, max_length=1000, description="Concise factual explanation for the change ledger."),
    ]

    @model_validator(mode="after")
    def complete(self):
        if self.action == "create" and (not self.role or not self.title or not self.body):
            raise ValueError("create requires role, title, and body")
        if self.action == "create" and self.path:
            raise ValueError("create paths are derived from role and title")
        if self.action == "create" and ("entities" not in self.model_fields_set or self.entities is None):
            raise ValueError("create requires explicit entities; use [] when no entity links are intended")
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

    name: EntityName
    entity_type: Annotated[str, Field(min_length=1, max_length=100)]
    aliases: Annotated[tuple[EntityName, ...], Field(max_length=20)] = ()
    same_as: str | None = None
    external_namespace: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    external_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    description: Annotated[
        str | None,
        Field(
            max_length=1000,
            description="Concise source-grounded account of who or what this entity is in context.",
        ),
    ] = None
    facts: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=500)], ...],
        Field(
            max_length=10,
            description="Durable source-grounded facts about this entity, without citations or repetition.",
        ),
    ] = ()

    @model_validator(mode="after")
    def valid_identity_evidence(self):
        if bool(self.external_namespace) != bool(self.external_id):
            raise ValueError("external namespace and id must be provided together")
        preferred = resolution_key(self.name)
        aliases = tuple(resolution_key(value) for value in self.aliases)
        if any(not any(character.isalnum() for character in value) for value in (preferred, *aliases)):
            raise ValueError("entity names must contain searchable text")
        if preferred in aliases or len(set(aliases)) != len(aliases):
            raise ValueError("entity aliases must be unique and differ from the preferred name")
        prose = tuple(value for value in (self.description, *self.facts) if value is not None)
        if any("\n" in value or "\r" in value for value in prose):
            raise ValueError("entity descriptions and facts must be single-line prose")
        normalized_facts = tuple(resolution_key(value) for value in self.facts)
        if len(set(normalized_facts)) != len(normalized_facts):
            raise ValueError("entity facts must be unique")
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
        Field(min_length=1, max_length=1000, description="Plain-English account of what the wiki learned."),
    ]
    mutations: Annotated[
        tuple[PageMutation, ...],
        Field(max_length=12, description="Only deliberate note or concept mutations for this source."),
    ] = ()
    entities: Annotated[
        tuple[EntityProposal, ...],
        Field(max_length=20, description="Reusable identity proposals, independent from page entity links."),
    ] = ()
    contradictions: Annotated[
        tuple[ContradictionProposal, ...],
        Field(max_length=10, description="Evidence-backed unresolved contradictions to file."),
    ] = ()
    resolved_contradictions: Annotated[
        tuple[str, ...],
        Field(max_length=20, description="Only contradiction IDs explicitly resolved by this capture."),
    ] = ()

    @model_validator(mode="after")
    def link_proposed_entities(self):
        linked_references = {
            resolution_key(reference)
            for mutation in self.mutations
            if mutation.action in {"create", "update"}
            for reference in mutation.entities or ()
        }
        unlinked = [
            proposal.name
            for proposal in self.entities
            if not linked_references.intersection(resolution_key(value) for value in (proposal.name, *proposal.aliases))
        ]
        if unlinked:
            raise ValueError(
                "each entity proposal must be linked by a create or update mutation: " + ", ".join(unlinked)
            )
        return self


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
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class RepairPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    mutations: Annotated[tuple[RepairMutation, ...], Field(max_length=12)] = ()
