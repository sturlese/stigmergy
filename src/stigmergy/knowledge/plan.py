"""Structured mutation plan returned by the librarian model."""

from __future__ import annotations

from pathlib import PurePosixPath
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


class GraphEntity(BaseModel):
    """One identity that is materially about a durable graph subject."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: EntityName
    entity_type: Literal[
        "person", "organization", "product", "tool", "repository", "project", "place"
    ]
    aliases: Annotated[
        tuple[EntityName, ...],
        Field(
            max_length=20,
            description=(
                "Every source-supported alternate name or handle for this identity. If the source "
                "writes a preferred name beside an @handle, the handle is required here."
            ),
        ),
    ]
    relationship_kind: Literal[
        "authored", "responsible", "participated", "produced_evidence"
    ]
    relationship: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description=(
                "Source-grounded relationship fact to express once in natural prose. For "
                "produced_evidence it must state the concrete distinguishing result, not a generic "
                "attribution. It may already contain the preferred name and must never be concatenated "
                "with a second copy of that name."
            ),
        ),
    ]
    evidence_terms: Annotated[
        tuple[RequiredTerm, ...],
        Field(
            max_length=8,
            description=(
                "Exact source spans that distinguish the material result this identity produced. "
                "Required for produced_evidence and optional for other relationship kinds. Preserve any "
                "number or quantity that makes the result distinctive."
            ),
        ),
    ]

    @model_validator(mode="after")
    def valid_identity(self):
        preferred = resolution_key(self.name)
        aliases = tuple(resolution_key(value) for value in self.aliases)
        if preferred in aliases or len(set(aliases)) != len(aliases):
            raise ValueError("graph entity aliases must be unique and differ from the preferred name")
        if self.relationship_kind == "produced_evidence" and not self.evidence_terms:
            raise ValueError("a produced_evidence entity requires exact evidence_terms")
        if self.relationship_kind == "produced_evidence":
            name_key = resolution_key(self.name)
            substantive = [
                term for term in self.evidence_terms if name_key not in resolution_key(term)
            ]
            if not substantive:
                raise ValueError("produced_evidence requires a result term beyond the entity name")
        relationship_key = resolution_key(self.relationship)
        missing = [
            term for term in self.evidence_terms if resolution_key(term) not in relationship_key
        ]
        if missing:
            raise ValueError("entity relationship must contain every evidence_term")
        return self


class GraphTopologySubject(BaseModel):
    """The source-grounded identity and ontological kind of one durable subject."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: Annotated[
        str,
        Field(
            min_length=1,
            max_length=300,
            description=(
                "Shortest stable source-stated noun phrase that remains unambiguous; never reduce "
                "a compound subject name to a bare generic category noun."
            ),
        ),
    ]
    title_evidence: Annotated[
        str,
        Field(
            min_length=1,
            max_length=300,
            description=(
                "Exact source noun phrase that supplies the canonical title. Remove only a leading "
                "article when setting title; never invent or paraphrase a qualifier."
            ),
        ),
    ]
    name_variants: Annotated[
        tuple[EntityName, ...],
        Field(
            min_length=1,
            max_length=12,
            description=(
                "All exact source noun phrases that refer to this same subject, including descriptive "
                "and type-qualified variants, so title_evidence can select the most specific name."
            ),
        ),
    ]
    role: Annotated[
        Literal["note", "concept"],
        Field(
            description=(
                "Use concept for a reusable subject that can accumulate knowledge across sources. "
                "Use note only for a source-specific event, decision, observation, or record."
            )
        ),
    ]
    abstraction: Annotated[
        Literal[
            "practice", "system", "artifact", "method", "organization", "person", "other"
        ],
        Field(
            description=(
                "Ontological kind. A system is an operational assembly with interacting components, "
                "state, loops, or capabilities. An artifact is a static bounded output such as a "
                "document, file, dataset, or model."
            )
        ),
    ]
    abstraction_evidence: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description=(
                "Exact source wording that proves the ontological kind. A repeated activity that "
                "designs or improves something is a practice or method; the operational thing it "
                "changes is a system or artifact. Words such as engineering, design, management, "
                "governance, and operations normally name work people practice, not the thing "
                "that work produces or improves."
            ),
        ),
    ]
    significance: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def valid_topology(self):
        title_key = resolution_key(self.title)
        if title_key in {"system", "framework", "runtime", "harness", "practice", "method"}:
            raise ValueError("graph subject title must not be a bare generic category noun")
        if title_key.split(maxsplit=1)[0] in {"a", "an", "the"}:
            raise ValueError("graph subject title must not start with an article")
        evidence_words = self.title_evidence.strip().split()
        if evidence_words and resolution_key(evidence_words[0]) in {"a", "an", "the"}:
            evidence_words = evidence_words[1:]
        if resolution_key(" ".join(evidence_words)) != title_key:
            raise ValueError("graph subject title must match exact title_evidence")
        variants = tuple(resolution_key(value) for value in self.name_variants)
        if len(set(variants)) != len(variants):
            raise ValueError("graph subject name_variants must be unique")
        if resolution_key(self.title_evidence) not in variants:
            raise ValueError("title_evidence must be present in name_variants")
        activity_head = title_key.split()[-1]
        if (
            activity_head in {"engineering", "design", "management", "governance", "operations"}
            and self.abstraction not in {"practice", "method"}
        ):
            raise ValueError("an activity-noun subject must be a practice or method")
        if self.abstraction in {"practice", "method", "system", "artifact"} and self.role != "concept":
            raise ValueError("a reusable practice, method, system, or artifact must be a concept")
        if self.abstraction in {"person", "organization"}:
            raise ValueError(
                "person and organization identities belong to entity enrichment, not wiki topology"
            )
        return self


class GraphSubject(GraphTopologySubject):
    """A reusable subject enriched for compilation into a cold-readable page."""

    required_terms: Annotated[
        tuple[RequiredTerm, ...],
        Field(
            min_length=1,
            max_length=30,
            description=(
                "Verbatim loss-prevention anchors that the compiled page must preserve naturally, not "
                "an outline or output order. Across all subjects, the inventories must preserve every "
                "explicitly enumerated framework member, named extension, and distinguishing result. "
                "Give each anchor one primary owner by aboutness; repeat it on a sibling only when the "
                "exact source span independently proves a distinct fact about both subjects. For every "
                "produced_evidence entity assigned to this subject, include the source spans that "
                "distinguish what that identity demonstrated."
            ),
        ),
    ]
    entities: Annotated[
        tuple[GraphEntity, ...],
        Field(
            max_length=20,
            description=(
                "Every source author and named identity with a material page-specific relationship "
                "assigned to this subject; exclude identities that are merely mentioned or whose "
                "evidence is owned by a sibling page."
            ),
        ),
    ]

    @model_validator(mode="after")
    def unique_inventory(self):
        terms = tuple(resolution_key(value) for value in self.required_terms)
        if len(set(terms)) != len(terms):
            raise ValueError("graph subject required terms must be unique")
        entities = tuple(resolution_key(value.name) for value in self.entities)
        if len(set(entities)) != len(entities):
            raise ValueError("graph subject entities must be unique")
        return self


class ExistingPageRelation(BaseModel):
    """The semantic relationship between a new subject and one visible existing page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_subject: Annotated[str, Field(min_length=1, max_length=300)]
    path: Annotated[str, Field(min_length=1, max_length=500)]
    relation: Literal["same_subject", "distinct_related", "unrelated"]
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def valid_page_path(self):
        if not self.path.endswith(".md") or not self.path.startswith(
            ("wiki/notes/", "wiki/concepts/")
        ):
            raise ValueError("existing graph relations must target a note or concept Markdown path")
        return self


class GraphTopology(BaseModel):
    """Agent-authored page topology before content inventory and entity enrichment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    subjects: Annotated[tuple[GraphTopologySubject, ...], Field(max_length=12)]
    existing_relations: Annotated[tuple[ExistingPageRelation, ...], Field(max_length=24)]

    @model_validator(mode="after")
    def coherent_identity(self):
        subjects = {resolution_key(subject.title): subject for subject in self.subjects}
        if type(self) is GraphTopology and len(subjects) != len(self.subjects):
            raise ValueError("graph subjects must have unique titles")

        same_paths: dict[str, str] = {}
        for relation in self.existing_relations:
            source_key = resolution_key(relation.source_subject)
            if source_key not in subjects:
                raise ValueError("existing relation source_subject must name a graph subject")
            if relation.relation != "same_subject":
                continue
            path_title = resolution_key(PurePosixPath(relation.path).stem)
            if path_title != source_key:
                raise ValueError(
                    "same_subject requires an exact normalized title match; use distinct_related "
                    "when alias evidence is absent"
                )
            expected_role = "concept" if relation.path.startswith("wiki/concepts/") else "note"
            if subjects[source_key].role != expected_role:
                raise ValueError("same_subject page role must match the existing page directory")
            if relation.path in same_paths and same_paths[relation.path] != source_key:
                raise ValueError("one existing path cannot represent multiple graph subjects")
            same_paths[relation.path] = source_key

        for subject_key, subject in subjects.items():
            candidates = {
                relation.path
                for relation in self.existing_relations
                if resolution_key(PurePosixPath(relation.path).stem) == subject_key
            }
            if len(candidates) > 1:
                raise ValueError("one graph subject cannot resolve to multiple exact-title paths")
            for path in candidates:
                expected_role = "concept" if path.startswith("wiki/concepts/") else "note"
                if subject.role != expected_role:
                    raise ValueError("exact-title existing page role must match the graph subject")
        return self


class GraphShape(GraphTopology):
    """A reviewed topology enriched for compilation into a concrete FilingPlan."""

    subjects: Annotated[tuple[GraphSubject, ...], Field(max_length=12)]

    @model_validator(mode="after")
    def consistent_entities(self):
        entity_specs: dict[str, tuple[str, tuple[str, ...]]] = {}
        for subject in self.subjects:
            for entity in subject.entities:
                key = resolution_key(entity.name)
                spec = (
                    entity.entity_type,
                    tuple(sorted(resolution_key(value) for value in entity.aliases)),
                )
                if key in entity_specs and entity_specs[key] != spec:
                    raise ValueError("graph entity type and aliases must be consistent across subjects")
                entity_specs[key] = spec
        return self


def expected_graph_mutations(shape: GraphShape) -> tuple[dict[str, str | None], ...]:
    """Derive safe create/update targets from the agent's semantic graph decision."""
    relations_by_subject: dict[str, list[ExistingPageRelation]] = {}
    exact_paths: dict[str, str] = {}
    for relation in shape.existing_relations:
        relations_by_subject.setdefault(resolution_key(relation.source_subject), []).append(relation)
        path_key = resolution_key(PurePosixPath(relation.path).stem)
        exact_paths[path_key] = relation.path

    operations = []
    covered_paths = set()
    for subject in shape.subjects:
        subject_key = resolution_key(subject.title)
        same = next(
            (
                relation.path
                for relation in relations_by_subject.get(subject_key, ())
                if relation.relation == "same_subject"
            ),
            None,
        )
        path = same or exact_paths.get(subject_key)
        if path:
            covered_paths.add(path)
        operations.append(
            {
                "subject": subject.title,
                "action": "update" if path else "create",
                "role": None if path else subject.role,
                "path": path,
                "title": None if path else subject.title,
            }
        )
    return tuple(operations)


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
        if self.action == "create" and (
            "entities" not in self.model_fields_set or self.entities is None
        ):
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

    @model_validator(mode="after")
    def valid_identity_evidence(self):
        if bool(self.external_namespace) != bool(self.external_id):
            raise ValueError("external namespace and id must be provided together")
        preferred = resolution_key(self.name)
        aliases = tuple(resolution_key(value) for value in self.aliases)
        if any(
            not any(character.isalnum() for character in value)
            for value in (preferred, *aliases)
        ):
            raise ValueError("entity names must contain searchable text")
        if preferred in aliases or len(set(aliases)) != len(aliases):
            raise ValueError("entity aliases must be unique and differ from the preferred name")
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
            if not linked_references.intersection(
                resolution_key(value) for value in (proposal.name, *proposal.aliases)
            )
        ]
        if unlinked:
            raise ValueError(
                "each entity proposal must be linked by a create or update mutation: "
                + ", ".join(unlinked)
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
