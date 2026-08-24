import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from stigmergy.capture import schema
from stigmergy.entities.model import (
    EntityContractError,
    ExternalIdClaim,
    load_entities,
    parse_entity,
    registry_bytes,
)
from stigmergy.entities.service import (
    EntityOperationError,
    apply_proposals,
    delete_entity,
    merge_entities,
    remove_source_claims,
    write_records,
)
from stigmergy.knowledge.pages import parse_page, render_page
from stigmergy.knowledge.plan import EntityProposal
from stigmergy.server import entity_aliases

NOW = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)
SOURCE = "sources/2026/08/00000000-0000-4000-8000-000000000001.md"
SECOND_SOURCE = "sources/2026/08/00000000-0000-4000-8000-000000000002.md"


def _proposal(name: str, **values) -> EntityProposal:
    return EntityProposal(name=name, entity_type="organization", **values)


def _apply(root: Path, *proposals: EntityProposal, acl=None, at=NOW, allowed_same_as=frozenset()):
    return apply_proposals(
        str(root),
        proposals,
        acl=acl,
        source=SOURCE,
        actor="alice",
        at=at,
        allowed_same_as=allowed_same_as,
    )


def _note(root: Path, entity_id: str, *, link_target: str | None = None) -> Path:
    path = root / "wiki" / "notes" / "Relationship.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    link = f"[[wiki/entities/{link_target or entity_id}|Visible label]]"
    path.write_text(
        render_page(
            path="wiki/notes/Relationship.md",
            role="note",
            title="Relationship",
            body=f"# Relationship\n\nSubstantive knowledge about {link} remains useful.",
            acl=None,
            entities=(entity_id,),
            sources=(SOURCE,),
            status="mature",
            created=NOW.date(),
            updated=NOW.date(),
        ),
        encoding="utf-8",
    )
    return path


def _source(root: Path, assertion: str, relative: str = SOURCE) -> None:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"id: {path.stem}\n"
        "type: source\n"
        "submitted_by: alice\n"
        "acl: null\n"
        "captured_at: 2026-08-24T12:00:00+00:00\n"
        "origin: mcp\n"
        "artifacts:\n"
        f"  - sha256: {'a' * 64}\n"
        "    bytes: 1\n"
        "    media_type: text/plain\n"
        f"    readable_sha256: {'a' * 64}\n"
        "    extractor: utf8\n"
        "    extractor_version: '1'\n"
        "---\n\n"
        f"# Source\n\n{assertion}\n",
        encoding="utf-8",
    )


def _source_evidence(assertion: str) -> schema.EntityMergeEvidence:
    return schema.EntityMergeEvidence(
        source_assertions=(schema.SourceMergeAssertion(path=SOURCE, assertion=assertion),),
    )


def test_birth_uses_an_opaque_path_and_name_collision_does_not_merge(tmp_path):
    first = _apply(tmp_path, _proposal("Acme"))["Acme"]
    second = _apply(tmp_path, _proposal("Acme"))["Acme"]

    assert first.startswith("ent_") and second.startswith("ent_")
    assert first != second
    assert (tmp_path / "wiki" / "entities" / f"{first}.md").is_file()


def test_scoped_rename_preserves_id_and_demotes_the_old_preferred_name(tmp_path):
    entity_id = _apply(tmp_path, _proposal("Acme"), acl=("finance",))["Acme"]
    later = NOW + dt.timedelta(days=1)
    renamed = _apply(
        tmp_path,
        _proposal("Acme Holdings", same_as=entity_id),
        acl=("finance",),
        at=later,
        allowed_same_as=frozenset({entity_id}),
    )["Acme Holdings"]
    record = load_entities(str(tmp_path))[entity_id]

    assert renamed == entity_id
    assert {(claim.value, claim.kind) for claim in record.claims} == {
        ("Acme", "alias"),
        ("Acme Holdings", "preferred"),
    }
    assert all(claim.acl == ("finance",) for claim in record.claims)


def test_repeated_identity_evidence_survives_deletion_of_its_first_source(tmp_path):
    entity_id = _apply(
        tmp_path,
        _proposal("Acme", external_namespace="crm", external_id="account-7"),
        acl=("finance",),
    )["Acme"]
    apply_proposals(
        str(tmp_path),
        (
            _proposal(
                "Acme",
                same_as=entity_id,
                external_namespace="crm",
                external_id="account-7",
            ),
        ),
        acl=("finance",),
        source=SECOND_SOURCE,
        actor="alice",
        at=NOW + dt.timedelta(days=1),
        allowed_same_as=frozenset({entity_id}),
    )

    record = load_entities(str(tmp_path))[entity_id]
    assert {claim.source for claim in record.claims} == {SOURCE, SECOND_SOURCE}
    assert {claim.source for claim in record.external_ids} == {SOURCE, SECOND_SOURCE}
    entity_aliases.registry_payload(
        (tmp_path / "ops" / "entity-registry.json").read_text(),
        "fixture",
    )

    remove_source_claims(str(tmp_path), {SOURCE}, at=NOW + dt.timedelta(days=2))

    remaining = load_entities(str(tmp_path))[entity_id]
    assert [(claim.source, claim.kind) for claim in remaining.claims] == [(SECOND_SOURCE, "preferred")]
    assert [claim.source for claim in remaining.external_ids] == [SECOND_SOURCE]


def test_reader_projection_never_falls_back_to_a_hidden_name(tmp_path):
    entity_id = _apply(tmp_path, _proposal("Secret Co"), acl=("finance",))["Secret Co"]
    payload = entity_aliases.registry_from_text(
        (tmp_path / "ops" / "entity-registry.json").read_text(),
        "fixture",
    )

    assert entity_aliases.project_record(payload[entity_id], {"sales"}) is None
    assert entity_aliases.project_record(payload[entity_id], {"finance"})["name"] == "Secret Co"


def test_strong_external_id_reuses_hidden_canonical_without_revealing_claims(tmp_path):
    hidden = _apply(
        tmp_path,
        _proposal("Secret Co", external_namespace="crm", external_id="account-7"),
        acl=("finance",),
    )["Secret Co"]

    visible = _apply(
        tmp_path,
        _proposal("Engineering Vendor", external_namespace="crm", external_id="account-7"),
        acl=("engineering",),
        allowed_same_as=frozenset(),
    )["Engineering Vendor"]

    records = load_entities(str(tmp_path))
    assert visible == hidden
    assert set(records) == {hidden}
    payload = entity_aliases.registry_from_text(
        (tmp_path / "ops" / "entity-registry.json").read_text(),
        "fixture",
    )
    assert entity_aliases.project_record(payload[hidden], {"finance"})["name"] == "Secret Co"
    engineering = entity_aliases.project_record(payload[hidden], {"engineering"})
    assert engineering["name"] == "Engineering Vendor"
    assert [claim["value"] for claim in engineering["claims"]] == ["Engineering Vendor"]


def test_merge_preserves_claim_provenance_rewrites_anchors_and_adds_redirect(tmp_path):
    assertion = "Acme One and Acme Two are duplicate company identities."
    _source(tmp_path, assertion)
    first = _apply(tmp_path, _proposal("Acme One"))["Acme One"]
    second = _apply(tmp_path, _proposal("Acme Two"), at=NOW + dt.timedelta(seconds=1))["Acme Two"]
    note = _note(tmp_path, second)

    canonical = merge_entities(
        str(tmp_path),
        (second, first),
        at=NOW + dt.timedelta(days=1),
        evidence=_source_evidence(assertion),
    )
    records = load_entities(str(tmp_path))
    page = parse_page("wiki/notes/Relationship.md", note.read_text())
    payload = entity_aliases.registry_payload(
        (tmp_path / "ops" / "entity-registry.json").read_text(),
        "fixture",
    )

    assert canonical == first
    assert second not in records
    assert {claim.value for claim in records[first].claims} == {"Acme One", "Acme Two"}
    assert page.entities == (first,)
    assert f"wiki/entities/{first}" in page.body
    assert payload["redirects"] == {second: first}


def test_merge_keeps_updated_at_monotonic_when_the_worker_clock_is_behind(tmp_path):
    assertion = "Acme One and Acme Two are aliases for the same entity."
    _source(tmp_path, assertion)
    first = _apply(tmp_path, _proposal("Acme One"))["Acme One"]
    later = NOW + dt.timedelta(minutes=5)
    second = _apply(tmp_path, _proposal("Acme Two"), at=later)["Acme Two"]

    canonical = merge_entities(
        str(tmp_path),
        (first, second),
        at=NOW - dt.timedelta(minutes=5),
        evidence=_source_evidence(assertion),
    )

    assert load_entities(str(tmp_path))[canonical].updated_at == later


def test_merge_accepts_an_external_id_present_on_every_selected_entity(tmp_path):
    first = _apply(tmp_path, _proposal("Acme One"))["Acme One"]
    second = _apply(tmp_path, _proposal("Acme Two"))["Acme Two"]
    records = load_entities(str(tmp_path))
    external = ExternalIdClaim(
        namespace="crm",
        value="account-7",
        acl=None,
        source=SOURCE,
        actor="alice",
        introduced_at=NOW,
    )
    records = {entity_id: replace(record, external_ids=(external,)) for entity_id, record in records.items()}
    write_records(str(tmp_path), records)

    canonical = merge_entities(
        str(tmp_path),
        (first, second),
        at=NOW + dt.timedelta(days=1),
        evidence=schema.EntityMergeEvidence(
            shared_external_id=schema.SharedExternalIdEvidence(
                namespace="crm",
                value="account-7",
            )
        ),
    )

    assert set(load_entities(str(tmp_path))) == {canonical}


def test_merge_rejects_unverified_evidence_without_mutating_the_repo(tmp_path):
    first = _apply(tmp_path, _proposal("Acme One"))["Acme One"]
    second = _apply(tmp_path, _proposal("Acme Two"))["Acme Two"]
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    with pytest.raises(EntityOperationError, match="not present on every entity"):
        merge_entities(
            str(tmp_path),
            (first, second),
            at=NOW + dt.timedelta(days=1),
            evidence=schema.EntityMergeEvidence(
                shared_external_id=schema.SharedExternalIdEvidence(
                    namespace="crm",
                    value="missing",
                )
            ),
        )

    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize(
    ("first_name", "second_name", "assertion"),
    (
        (
            "Eva",
            "Other",
            "Deva and Other are aliases for the same entity.",
        ),
        (
            "Acme",
            "Globex",
            "Acme and Globex used aliases during migration.",
        ),
        (
            "Acme",
            "Acme Inc",
            "Acme and Acme Inc are duplicate company identities.",
        ),
    ),
)
def test_merge_rejects_ambiguous_source_assertions_without_mutation(tmp_path, first_name, second_name, assertion):
    _source(tmp_path, assertion)
    first = _apply(tmp_path, _proposal(first_name))[first_name]
    second = _apply(tmp_path, _proposal(second_name))[second_name]
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    with pytest.raises(EntityOperationError, match="unambiguously equate"):
        merge_entities(
            str(tmp_path),
            (first, second),
            at=NOW + dt.timedelta(days=1),
            evidence=_source_evidence(assertion),
        )

    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


def test_out_of_order_claim_keeps_entity_timestamps_valid(tmp_path):
    entity_id = _apply(tmp_path, _proposal("Acme"))["Acme"]

    _apply(
        tmp_path,
        _proposal("Acme Holdings", same_as=entity_id),
        at=NOW - dt.timedelta(minutes=5),
        allowed_same_as=frozenset({entity_id}),
    )

    assert load_entities(str(tmp_path))[entity_id].updated_at == NOW


def test_delete_sweeps_identity_references_but_keeps_substantive_page_text(tmp_path):
    entity_id = _apply(tmp_path, _proposal("Acme"))["Acme"]
    note = _note(tmp_path, entity_id)

    delete_entity(str(tmp_path), entity_id)
    page = parse_page("wiki/notes/Relationship.md", note.read_text())

    assert page.entities == ()
    assert "Substantive knowledge" in page.body
    assert "Visible label" in page.body
    assert "[[" not in page.body
    assert load_entities(str(tmp_path)) == {}


def test_registry_is_reproducible_only_from_entity_pages(tmp_path):
    _apply(tmp_path, _proposal("Acme"))
    records = load_entities(str(tmp_path))

    assert (tmp_path / "ops" / "entity-registry.json").read_bytes() == registry_bytes(records)


def test_entity_body_rejects_a_dossier(tmp_path):
    entity_id = _apply(tmp_path, _proposal("Acme"))["Acme"]
    path = tmp_path / "wiki" / "entities" / f"{entity_id}.md"

    with pytest.raises(EntityContractError, match="stable id heading"):
        parse_entity(path.relative_to(tmp_path).as_posix(), path.read_text() + "\nFacts about Acme\n")
