from pathlib import Path

import pytest
from pydantic import ValidationError

from stigmergy.knowledge.contract import (
    KnowledgeContractError,
    expected_librarian_skill,
    validate_librarian_skill,
    validate_source_template,
    validate_workflows,
)
from stigmergy.knowledge.plan import (
    ContradictionClaim,
    ContradictionProposal,
    EntityProposal,
    FilingPlan,
    PageMutation,
)

ROOT = Path(__file__).resolve().parents[2]
FROZEN = ROOT / "tests/librarian/fixtures/repo/.claude/skills/librarian/SKILL.md"
EVALUATION = ROOT / "evals/filing/repo/.claude/skills/librarian/SKILL.md"


def test_packaged_frozen_and_evaluation_librarian_contracts_are_identical():
    expected = expected_librarian_skill()
    assert FROZEN.read_bytes() == expected
    assert EVALUATION.read_bytes() == expected


def test_repository_validator_requires_the_exact_librarian_contract(tmp_path):
    skill = tmp_path / ".claude" / "skills" / "librarian" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(expected_librarian_skill())
    validate_librarian_skill(tmp_path)

    skill.write_text("incomplete\n", encoding="utf-8")
    with pytest.raises(KnowledgeContractError, match="does not match"):
        validate_librarian_skill(tmp_path)


def test_repository_validator_requires_the_nightly_rebuild_contract(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    pin = "a" * 40
    uv = (
        "uses: astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78\n"
        'version: "0.11.16"\n'
        'checksum: "74947fe2c03315cf07e82ab3acc703eddef01aba4d5232a98e4c6825ec116131"\n'
    )
    (workflows / "lint.yml").write_text(
        f"repository: sturlese/stigmergy\nref: {pin}\n{uv}",
        encoding="utf-8",
    )
    (workflows / "index-rebuild.yml").write_text(
        "schedule:\n"
        '  - cron: "17 4 * * *"\n'
        "workflow_dispatch:\n"
        "repository: sturlese/stigmergy\n"
        f"ref: {pin}\n"
        f"{uv}"
        "OPENROUTER_API_KEY: secret\n"
        "run: .platform/.venv/bin/stigmergy-index --rebuild --repo .\n",
        encoding="utf-8",
    )
    validate_workflows(tmp_path)

    with (workflows / "index-rebuild.yml").open("a", encoding="utf-8") as handle:
        handle.write("continue-on-error: true\n")
    with pytest.raises(KnowledgeContractError, match="suppress failure"):
        validate_workflows(tmp_path)


def test_repository_validator_rejects_legacy_embedding_credentials(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    pin = "a" * 40
    uv = (
        "uses: astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78\n"
        'version: "0.11.16"\n'
        'checksum: "74947fe2c03315cf07e82ab3acc703eddef01aba4d5232a98e4c6825ec116131"\n'
    )
    (workflows / "lint.yml").write_text(
        f"repository: sturlese/stigmergy\nref: {pin}\n{uv}",
        encoding="utf-8",
    )
    (workflows / "index-rebuild.yml").write_text(
        "schedule:\n"
        '  - cron: "17 4 * * *"\n'
        "workflow_dispatch:\n"
        "repository: sturlese/stigmergy\n"
        f"ref: {pin}\n"
        f"{uv}"
        "OPENROUTER_API_KEY: secret\n"
        "EMBED_API_KEY: legacy\n"
        "run: .platform/.venv/bin/stigmergy-index --rebuild --repo .\n",
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeContractError, match="unsupported model configuration"):
        validate_workflows(tmp_path)


def test_repository_validator_requires_every_source_template_field(tmp_path):
    template = tmp_path / "ops" / "templates" / "source.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "---\n"
        "id: <capture-id>\n"
        "type: source\n"
        "submitted_by: <subject>\n"
        "acl: null\n"
        "captured_at: <timestamp>\n"
        "origin: mcp\n"
        "participants: []\n"
        "artifacts:\n"
        "  - sha256: <digest>\n"
        "    bytes: <count>\n"
        "    media_type: <mime>\n"
        "    readable_sha256: <digest>\n"
        "    extractor: <name>\n"
        "    extractor_version: <version>\n"
        "    ocr_pages: []\n"
        "---\n",
        encoding="utf-8",
    )
    validate_source_template(tmp_path)

    template.write_text(
        template.read_text(encoding="utf-8").replace("    ocr_pages: []\n", ""),
        encoding="utf-8",
    )
    with pytest.raises(KnowledgeContractError, match="source schema"):
        validate_source_template(tmp_path)


def test_every_promised_librarian_operation_exists_in_the_structured_contract():
    claim = ContradictionClaim(
        text="The term is annual.",
        source="sources/2026/08/00000000-0000-4000-8000-000000000001.md",
        date="2026-08-24",
    )
    plan = FilingPlan(
        summary="Updated durable knowledge",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Renewal term",
                body="# Renewal term\n\nThe term is annual.",
                entities=("Acme",),
                reason="Created the durable conclusion",
            ),
            PageMutation(
                action="update",
                path="wiki/concepts/Contracts.md",
                body="# Contracts\n\nCurrent explanation.",
                reason="Rewrote the explanation",
            ),
            PageMutation(
                action="delete",
                path="wiki/notes/Redundant.md",
                reason="Consolidated the conclusion",
            ),
        ),
        entities=(EntityProposal(name="Acme", entity_type="organization"),),
        contradictions=(
            ContradictionProposal(
                page_path="wiki/notes/Renewal term.md",
                explanation="Two signed sources disagree.",
                claims=(claim, claim.model_copy(update={"text": "The term is monthly."})),
            ),
        ),
        resolved_contradictions=("con_00000000-0000-4000-8000-000000000001",),
    )

    assert {mutation.action for mutation in plan.mutations} == {"create", "update", "delete"}
    assert plan.entities and plan.contradictions and plan.resolved_contradictions


@pytest.mark.parametrize(
    "aliases",
    [
        ("ACME HOLDINGS",),
        ("Acme", "ACME"),
    ],
)
def test_entity_proposal_rejects_names_that_duplicate_the_preferred_or_an_alias(aliases):
    with pytest.raises(ValidationError):
        EntityProposal(
            name="Acme Holdings",
            entity_type="organization",
            aliases=aliases,
        )


def test_entity_proposal_rejects_an_alias_without_searchable_text():
    with pytest.raises(ValidationError):
        EntityProposal(
            name="Acme Holdings",
            entity_type="organization",
            aliases=("!!!",),
        )


@pytest.mark.parametrize("role", ["meeting", "document", "page", "raw", "source", "entity", "view"])
def test_librarian_contract_rejects_retired_page_roles(role):
    with pytest.raises(ValidationError):
        PageMutation(
            action="create",
            role=role,
            title="Invalid",
            body="# Invalid",
            reason="invalid",
        )


def test_librarian_skill_has_no_nonexistent_writer_or_human_workflow():
    text = FROZEN.read_text().casefold()
    forbidden = (
        "meeting distiller",
        "document door",
        "view regenerator",
        "awaiting review",
        "approved_by",
        "identity gardener",
    )
    assert all(term not in text for term in forbidden)
    assert "treat a submitted synthesis as the complete source" in text


def test_librarian_preserves_existing_sourced_knowledge_during_rewrites():
    text = " ".join(FROZEN.read_text().casefold().split())
    assert "every newly added or changed factual conclusion" in text
    assert "preserve existing sourced conclusions" in text
    assert "every conclusion must remain supported by the supplied source" not in text


def test_librarian_groups_explicit_names_and_identifiers_into_one_entity_proposal():
    text = " ".join(FROZEN.read_text().casefold().split())
    assert "use one proposal per identity" in text
    assert "every other explicitly asserted name, abbreviation, or acronym" in text
    assert "include the paired `external_namespace` and `external_id`" in text


def test_librarian_prevents_new_identity_proposals_without_a_page_anchor():
    text = " ".join(FROZEN.read_text().casefold().split())
    assert "every new identity proposal must be referenced" in text
    assert "so the plan cannot create an orphan" in text
    assert "if no durable page has a material relationship to an identity, do not propose it" in text


def test_librarian_distinguishes_evidentiary_identities_from_incidental_examples():
    text = " ".join(FROZEN.read_text().casefold().split())
    assert "stated action, measurement, decision, report, or result" in text
    assert "used as cited evidence for a conclusion" in text
    assert "even when the page is not primarily about that identity" in text
    assert "catalog entry, name-drop, or example with no stated action or result remains prose" in text
    assert "actors with a material evidentiary role have not been dropped" in text


def test_librarian_declares_entity_anchor_shape_for_every_mutation_action():
    text = " ".join(FROZEN.read_text().casefold().split())

    assert "create has no path and must explicitly emit `entities: [...]`" in text
    assert "use `entities: []` when there are no deliberate entity anchors" in text
    assert "`entities: null` to preserve current anchors" in text
    assert "complete `entities: [...]` list to replace them" in text
    assert "emit `entities: null`" in text


def test_librarian_requires_reusable_identities_and_deliberate_entity_links():
    text = " ".join(FROZEN.read_text().casefold().split())
    required = {
        "expected future reuse",
        "passing mentions remain prose",
        "only explicit `entities` references are anchored",
        "inventory named people, organizations, products, and projects",
        "source supplies an entity-specific action",
        "every selected identity has a visible relationship and local source citation",
    }
    forbidden = {
        "automatic unambiguous same-plan anchoring",
        "include every identity discussed by the page",
        "cannot suppress an unambiguous visible or same-plan proposed identity",
    }
    assert not (missing := {rule for rule in required if rule not in text}), missing
    assert not (present := {rule for rule in forbidden if rule in text}), present


def test_librarian_links_named_content_authors_without_inferring_submitter_authorship():
    text = " ".join(FROZEN.read_text().casefold().split())
    required = {
        "source author qualifies when their authorship is itself useful provenance",
        "not merely because every source has an author",
    }

    assert not (missing := {rule for rule in required if rule not in text}), missing


def test_librarian_requires_full_frameworks_and_keeps_entity_anchors_out_of_wikilinks():
    text = " ".join(FROZEN.read_text().casefold().split())
    required = {
        "complete named or enumerated framework",
        "do not reduce a six-part mechanism to generic bullets",
        "a source-named central concept belongs in one central page",
        "split a child page only when it independently has a stable name, mechanism, significance",
        "assess a source-title named durable concept as a first-class page candidate",
        "dumping the source framework into the adjacent artifact page is incorrect",
        "use wikilinks only for visible normal note/concept pages",
        "never use entity names as wikilinks",
        "no provenance token became a page or entity",
    }

    assert not (missing := {rule for rule in required if rule not in text}), missing


def test_librarian_keeps_named_non_identity_material_as_prose_or_concepts():
    text = " ".join(FROZEN.read_text().casefold().split())
    required = {
        "do not create entities for generic methods, technical terms, models, protocols",
        "a named model or model family is a technology, not an identity node",
        "a distinct page needs a name, mechanism, significance, and plausible future reuse",
    }

    assert not (missing := {rule for rule in required if rule not in text}), missing


def test_librarian_reuses_a_stable_identifier_across_scopes_without_disclosing_hidden_claims():
    text = " ".join(FROZEN.read_text().casefold().split())
    assert "stable opaque identity whose name claims are outside this audience" in text
    assert "never create a second identity because they are hidden" in text


def test_librarian_requires_exact_available_paths_for_contradiction_claims():
    text = " ".join(FROZEN.read_text().casefold().split())
    assert "exact visible paths" in text


def test_librarian_treats_a_non_authoritative_later_value_as_a_contradiction():
    text = " ".join(FROZEN.read_text().casefold().split())
    assert "do not overwrite a visible claim merely because a new source differs" in text
    assert "authority, scope, and basis" in text


def test_librarian_files_contradictions_only_as_structured_proposals():
    text = " ".join(FROZEN.read_text().casefold().split())
    assert "emit a `contradictionproposal`" in text
    assert "preserve both" in text


def test_librarian_updates_resolution_prose_without_rewriting_marker_blocks():
    text = " ".join(FROZEN.read_text().casefold().split())
    assert "never put speculative contradiction markers in a page body" in text


def test_librarian_reuses_registry_spellings_from_the_context():
    text = " ".join(FROZEN.read_text().casefold().split())
    assert "reuse visible registry spelling and identifier values exactly" in text
    assert "rather than filling in unseen names" in text


@pytest.mark.parametrize(
    ("namespace", "identifier"),
    [("x" * 65, "11820473"), ("companies_house", "9" * 201), ("", "11820473")],
)
def test_entity_proposal_bounds_its_external_identifier(namespace, identifier):
    with pytest.raises(ValidationError):
        EntityProposal(
            name="Acme Holdings",
            entity_type="organization",
            external_namespace=namespace,
            external_id=identifier,
        )


def test_librarian_contract_tolerates_create_fields_on_path_targeted_updates():
    mutation = PageMutation(
        action="update",
        path="wiki/concepts/Existing.md",
        role="note",
        title="Ignored model title",
        body="# Existing\n\nCurrent knowledge.",
        reason="Updated the existing page.",
    )

    assert mutation.path == "wiki/concepts/Existing.md"
    assert mutation.role == "note"
    assert mutation.title == "Ignored model title"
