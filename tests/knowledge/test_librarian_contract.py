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


def _skill_text() -> str:
    return " ".join(FROZEN.read_text().casefold().split())


def test_librarian_skill_is_one_coherent_agent_without_retired_workflows():
    text = _skill_text()
    assert "one coherent `filingplan`" in text
    assert "silent ownership ledger" in text
    assert "zero to four knowledge pages" in text
    assert all(
        term not in text
        for term in (
            "meeting distiller",
            "document door",
            "view regenerator",
            "awaiting review",
            "identity gardener",
        )
    )


def test_librarian_uses_hippocampus_page_templates_and_preserves_existing_evidence():
    text = _skill_text()
    assert "canonical hippocampus concept body template" in text
    assert "canonical hippocampus note body template" in text
    assert "## definition" in text and "## how it works" in text and "## connections" in text
    assert "treat the visible page as the base manuscript" in text
    assert "preserve every useful existing source-backed claim" in text


def test_librarian_keeps_page_boundaries_semantic_and_connected():
    text = _skill_text()
    assert "one primary subject" in text
    assert "likely to be referenced again from future sources" in text
    assert "one primary subject for each row" in text
    assert "compare every new subject with every visible existing page" in text
    assert "delete only to consolidate a true semantic duplicate" in text


def test_librarian_requires_rich_source_grounded_entity_proposals():
    text = _skill_text()
    assert "durable authorship, responsibility, participation, aboutness" in text
    assert "`description` of who or what the entity is" in text
    assert "`facts` list" in text
    assert "one primary page by aboutness" in text
    assert "entity pages are projected by the system" in text
    assert "exact local source citation" in text


def test_librarian_requires_standalone_nonduplicative_and_temporally_grounded_entity_knowledge():
    text = _skill_text()
    assert "descriptions stand alone for a cold reader" in text
    assert "never with the capture that mentioned the entity" in text
    assert "temporally volatile" in text and "occurred_at" in text


def test_librarian_requires_each_volatile_entity_fact_to_carry_its_own_time_anchor():
    text = _skill_text()
    assert "temporally volatile" in text and "entityproposal" in text
    assert "its own" in text and "occurred_at" in text and "evidence-time anchor" in text
    assert "same fact" in text
    assert "never rely on page context or a sibling fact" in text


def test_librarian_distinguishes_dated_records_from_reusable_concepts():
    text = _skill_text()
    assert all(term in text for term in ("dated event", "decision", "commitment", "operating state", "note"))
    assert all(term in text for term in ("named workflow", "product", "topic", "reusable mechanism"))


def test_librarian_requires_facts_to_add_a_distinct_predicate_from_identity():
    text = _skill_text()
    assert "different predicate" in text
    assert all(
        term in text
        for term in ("action", "decision", "result", "relationship change", "time-bounded commitment")
    )
    assert all(term in text for term in ("grammar", "tense", "voice"))


def test_librarian_treats_source_body_as_authoritative_over_nonexhaustive_metadata():
    text = _skill_text()
    assert "read and preserve the complete original source" in text
    assert "structured metadata is a non-exhaustive hint" in text
    assert "field never negates or limits evidence plainly supported by the readable body" in text


def test_librarian_uses_general_entity_relevance_and_preserves_prior_provenance():
    text = _skill_text()
    assert "responsibility, decision, authorship, commitment, or causal participation" in text
    assert "incidental mentions stay prose" in text
    assert "safe visible context may reconcile identity or enrich a page" in text
    assert "claim supported only there keeps its original local citation" in text
    assert "one coherent `filingplan`" in text


def test_librarian_preserves_conflicts_and_recompile_knowledge():
    text = _skill_text()
    assert "preserve both and emit a contradiction" in text
    assert "prior derived paths are historical hints" in text
    assert "tombstone only a genuinely obsolete prior page" in text


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
