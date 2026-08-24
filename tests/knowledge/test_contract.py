import datetime as dt
import json

import pytest

from stigmergy.capture import schema
from stigmergy.entities.model import (
    EntityRecord,
    load_entities,
    new_name_claim,
    registry_bytes,
    render_entity,
)
from stigmergy.entities.service import merge_entities, write_records
from stigmergy.knowledge import contradictions
from stigmergy.knowledge.contradictions import Contradiction
from stigmergy.knowledge.lint import check
from stigmergy.knowledge.pages import PageContractError, page_path, render_page
from stigmergy.knowledge.plan import ContradictionClaim


def _write_source(
    tmp_path,
    source_id: str,
    *,
    acl=None,
    body="Evidence.",
    acquisition=None,
) -> str:
    relative = f"sources/2026/08/{source_id}.md"
    path = tmp_path.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "id": source_id,
        "type": "source",
        "submitted_by": "marc",
        "acl": acl,
        "captured_at": "2026-08-24T00:00:00+00:00",
        "origin": "mcp",
        "participants": [],
        "artifacts": [
            {
                "sha256": "a" * 64,
                "bytes": 1,
                "media_type": "text/plain",
                "readable_sha256": "a" * 64,
                "extractor": "text",
                "extractor_version": "1",
                "ocr_pages": [],
            }
        ],
    }
    if acquisition is not None:
        metadata["acquisition"] = acquisition
    path.write_text(
        f"---\n{json.dumps(metadata, sort_keys=True)}\n---\n\n# Captured source\n\n{body}\n",
        encoding="utf-8",
    )
    return relative


def test_page_path_rejects_a_title_that_exceeds_the_filesystem_component_limit():
    with pytest.raises(PageContractError, match="oversized filename"):
        page_path("note", "a" * 253)


def test_page_path_accepts_the_maximum_portable_filename_component():
    assert page_path("note", "a" * 252) == f"wiki/notes/{'a' * 252}.md"


def test_entity_registry_is_reproducible_and_merge_reanchors(tmp_path):
    at = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
    assertion = "Acme and Globex are duplicate company identities."
    source = _write_source(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        body=assertion,
    )
    first = EntityRecord(
        entity_id="ent_11111111-1111-4111-8111-111111111111",
        entity_type="organization",
        created_at=at,
        updated_at=at,
        claims=(
            new_name_claim(
                "Acme",
                kind="preferred",
                acl=None,
                source=source,
                actor="marc",
                introduced_at=at,
            ),
        ),
    )
    second = EntityRecord(
        entity_id="ent_22222222-2222-4222-8222-222222222222",
        entity_type="organization",
        created_at=at + dt.timedelta(seconds=1),
        updated_at=at + dt.timedelta(seconds=1),
        claims=(
            new_name_claim(
                "Globex",
                kind="preferred",
                acl=("finance",),
                source=source,
                actor="ana",
                introduced_at=at,
            ),
        ),
    )
    write_records(str(tmp_path), {first.entity_id: first, second.entity_id: second})
    note_path = tmp_path / "wiki" / "notes" / "Acme review.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(
        render_page(
            path="wiki/notes/Acme review.md",
            role="note",
            title="Acme review",
            body="# Acme review\n\nCurrent review.",
            acl=("finance",),
            entities=(second.entity_id,),
            sources=(source,),
            created=at.date(),
            updated=at.date(),
        ),
        encoding="utf-8",
    )

    canonical = merge_entities(
        str(tmp_path),
        (second.entity_id, first.entity_id),
        at=at + dt.timedelta(days=1),
        evidence=schema.EntityMergeEvidence(
            source_assertions=(schema.SourceMergeAssertion(path=source, assertion=assertion),),
        ),
    )

    assert canonical == first.entity_id
    records = load_entities(str(tmp_path))
    assert set(records) == {first.entity_id}
    assert records[first.entity_id].absorbed_ids == (second.entity_id,)
    assert second.entity_id not in note_path.read_text(encoding="utf-8")
    assert first.entity_id in note_path.read_text(encoding="utf-8")
    assert (tmp_path / "ops" / "entity-registry.json").read_bytes() == registry_bytes(records)


def test_contradiction_round_trips_as_visible_canonical_markdown():
    record = Contradiction(
        contradiction_id="con_11111111-1111-4111-8111-111111111111",
        explanation="Two signed sources disagree.",
        claims=(
            ContradictionClaim(
                text="The renewal is annual.",
                source="sources/2026/08/11111111-1111-4111-8111-111111111111.md",
                date="2026-08-01",
            ),
            ContradictionClaim(
                text="The renewal is monthly.",
                source="sources/2026/08/22222222-2222-4222-8222-222222222222.md",
                date="2026-08-02",
            ),
        ),
    )

    block = contradictions.render(record)
    parsed = contradictions.parse_all(block)

    assert parsed[0].record == record
    assert "Unresolved contradiction" in block
    assert "The renewal is annual" in block
    assert contradictions.remove(block, record.contradiction_id) == ("\n", True)


def test_linter_accepts_valid_empty_target_scaffold(tmp_path):
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "entity-registry.json").write_bytes(registry_bytes({}))
    assert check(str(tmp_path)) == ()


def test_linter_rejects_capability_data_in_source_provenance(tmp_path):
    source = _write_source(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        acquisition={
            "original_url": "https://files.example/report?token=secret",
            "final_url": "https://files.example/report",
            "acquired_at": "2026-08-24T00:00:00+00:00",
        },
    )
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "entity-registry.json").write_bytes(registry_bytes({}))

    violations = check(str(tmp_path))

    assert [(item.path, item.code) for item in violations] == [(source, "page-contract")]


def test_linter_rejects_entity_claim_without_live_provenance(tmp_path):
    at = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
    source = "sources/2026/08/11111111-1111-4111-8111-111111111111.md"
    entity = EntityRecord(
        entity_id="ent_11111111-1111-4111-8111-111111111111",
        entity_type="organization",
        created_at=at,
        updated_at=at,
        claims=(
            new_name_claim(
                "Acme",
                kind="preferred",
                acl=None,
                source=source,
                actor="marc",
                introduced_at=at,
            ),
        ),
    )
    write_records(str(tmp_path), {entity.entity_id: entity})

    violations = check(str(tmp_path))

    assert [(item.path, item.code) for item in violations] == [(entity.path, "entity-claim-source")]


def test_linter_rejects_entity_claim_broader_than_its_source(tmp_path):
    at = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
    source = _write_source(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        acl=["finance"],
    )
    entity = EntityRecord(
        entity_id="ent_11111111-1111-4111-8111-111111111111",
        entity_type="organization",
        created_at=at,
        updated_at=at,
        claims=(
            new_name_claim(
                "Acme",
                kind="preferred",
                acl=None,
                source=source,
                actor="marc",
                introduced_at=at,
            ),
        ),
    )
    write_records(str(tmp_path), {entity.entity_id: entity})

    violations = check(str(tmp_path))

    assert [(item.path, item.code) for item in violations] == [(entity.path, "acl-entity-claim-leak")]


def test_entity_body_cannot_become_a_dossier(tmp_path):
    at = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
    source = "sources/2026/08/11111111-1111-4111-8111-111111111111.md"
    entity = EntityRecord(
        entity_id="ent_11111111-1111-4111-8111-111111111111",
        entity_type="person",
        created_at=at,
        updated_at=at,
        claims=(
            new_name_claim(
                "Ada",
                kind="preferred",
                acl=None,
                source=source,
                actor="marc",
                introduced_at=at,
            ),
        ),
    )
    path = tmp_path / "wiki" / "entities" / f"{entity.entity_id}.md"
    path.parent.mkdir(parents=True)
    path.write_text(render_entity(entity) + "\n## Facts\n\n- Hidden dossier\n", encoding="utf-8")
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "entity-registry.json").write_text(
        json.dumps({"entities": {}, "redirects": {}, "version": 1}),
        encoding="utf-8",
    )

    assert any(item.code == "page-contract" for item in check(str(tmp_path)))
