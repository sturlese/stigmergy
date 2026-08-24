import json
import os

import pytest

from stigmergy.server import entity_aliases

E1 = "ent_00000000-0000-4000-8000-000000000001"
E2 = "ent_00000000-0000-4000-8000-000000000002"
E3 = "ent_00000000-0000-4000-8000-000000000003"
OLD = "ent_00000000-0000-4000-8000-000000000004"


def claim(claim_id, value, *, kind="preferred", acl=None, introduced_at="2026-08-24T00:00:00Z"):
    return {
        "claim_id": claim_id,
        "value": value,
        "normalized": value.casefold(),
        "kind": kind,
        "acl": acl,
        "source": "sources/2026/08/cap-1.md",
        "actor": "marc",
        "introduced_at": introduced_at,
    }


def record(*claims, absorbed=()):
    return {
        "entity_type": "organization",
        "created_at": "2026-08-24T00:00:00Z",
        "updated_at": "2026-08-24T00:00:00Z",
        "claims": list(claims),
        "external_ids": [],
        "absorbed_ids": list(absorbed),
    }


def payload(entities=None, redirects=None):
    return json.dumps({
        "version": 1,
        "entities": entities or {},
        "redirects": redirects or {},
    })


def write_registry(tmp_path, entities=None, redirects=None):
    path = tmp_path / "repo" / "ops" / "entity-registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload(entities, redirects), encoding="utf-8")
    return path


def test_default_path_follows_repository_layout():
    assert entity_aliases.default_path("/repo") == os.path.join(
        "/repo", "ops", "entity-registry.json"
    )
    assert entity_aliases.default_path(None) == ""


def test_missing_registry_is_empty():
    assert entity_aliases.registry_from_text(None, "missing") == {}
    assert entity_aliases.aliases_from_text(None, "missing") == {}
    assert entity_aliases.redirects_from_text(None, "missing") == {}


def test_registry_requires_the_current_contract():
    for value in (
        "{}",
        json.dumps({"version": 2, "entities": {}, "redirects": {}}),
        json.dumps({"version": 1, "entities": []}),
        "[]",
        "{not-json",
    ):
        with pytest.raises(ValueError):
            entity_aliases.registry_from_text(value, "registry")


def test_aliases_are_acl_scoped_and_ambiguous_names_do_not_resolve():
    text = payload({
        E1: record(
            claim("a-public", "Acme"),
            claim("a-finance", "Acme Capital", kind="alias", acl=["finance"]),
        ),
        E2: record(claim("b-public", "Acme")),
    })

    public = entity_aliases.aliases_from_text(text, "registry", audiences={"eng"})
    finance = entity_aliases.aliases_from_text(text, "registry", audiences={"finance"})

    assert "acme" not in public
    assert entity_aliases.resolve_exact(finance, "Acme Capital") == E1
    assert entity_aliases.resolve_exact(public, "Acme Capital") is None


def test_unrestricted_projection_sees_all_name_claims():
    item = {"id": E1, **record(
        claim("old", "Acme", introduced_at="2026-01-01T00:00:00Z"),
        claim("new", "Acme Group", acl=["finance"], introduced_at="2026-08-24T00:00:00Z"),
        claim("alias", "AC", kind="alias"),
    )}

    projection = entity_aliases.project_record(item, None)

    assert projection["name"] == "Acme Group"
    assert projection["aliases"] == ["AC", "Acme"]


def test_scoped_projection_uses_the_newest_visible_preferred_name():
    item = {"id": E1, **record(
        claim("public", "Acme"),
        claim("finance", "Acme Capital", acl=["finance"],
              introduced_at="2026-08-25T00:00:00Z"),
    )}

    assert entity_aliases.project_record(item, {"eng"})["name"] == "Acme"
    assert entity_aliases.project_record(item, {"finance"})["name"] == "Acme Capital"


def test_entity_without_visible_name_claim_is_not_projected():
    item = {"id": E1, **record(claim("finance", "Secret Co", acl=["finance"]))}
    assert entity_aliases.project_record(item, {"eng"}) is None


def test_longest_whole_name_wins_in_free_text_resolution():
    aliases = {"acme": E1, "acme capital": E2, "gx": E3}
    assert entity_aliases.resolve_entity(aliases, "Update on Acme Capital?") == E2
    assert entity_aliases.resolve_entity(aliases, "logging is healthy") is None
    assert entity_aliases.resolve_entity(aliases, "GX is healthy") == E3


def test_file_and_text_entry_points_have_identical_results(tmp_path):
    entities = {E1: record(claim("a", "Acme"))}
    path = write_registry(tmp_path, entities)
    text = path.read_text(encoding="utf-8")

    assert entity_aliases.load_registry(str(path)) == entity_aliases.registry_from_text(
        text, str(path)
    )
    assert entity_aliases.load_aliases(str(path)) == entity_aliases.aliases_from_text(
        text, str(path)
    )


def test_redirects_must_point_from_absorbed_ids_to_live_entities():
    entities = {E1: record(claim("live", "Live"), absorbed=(OLD,))}
    assert entity_aliases.redirects_from_text(
        payload(entities, {OLD: E1}), "registry"
    ) == {OLD: E1}

    for redirects in ({E1: E1}, {OLD: "missing"}):
        with pytest.raises(ValueError, match="redirect target"):
            entity_aliases.redirects_from_text(payload(entities, redirects), "registry")
