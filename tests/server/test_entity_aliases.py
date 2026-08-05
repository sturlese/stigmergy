"""`server.entity_aliases`: the read-only reader over `ops/entity-registry.json`, and the
alias-resolution primitive `ask`'s entity-first retrieval is built on. It reads the registry file
directly rather than importing whatever wrote it — the architecture rule this package's own
docstring states. Pure, keyless, DB-less.
"""
import json
import os

import pytest

from stigmergy.server import entity_aliases


def _write_registry(tmp_path, entities: dict) -> str:
    repo = tmp_path / "repo"
    ops = repo / "ops"
    ops.mkdir(parents=True)
    path = ops / "entity-registry.json"
    path.write_text(json.dumps({"entities": entities}), encoding="utf-8")
    return str(repo)


def test_default_path_follows_the_repo_convention():
    assert entity_aliases.default_path("/x/repo") == os.path.join("/x/repo", "ops",
                                                                   "entity-registry.json")
    assert entity_aliases.default_path(None) == ""


def test_load_aliases_missing_file_returns_empty_dict(tmp_path):
    assert entity_aliases.load_aliases(str(tmp_path / "nope.json")) == {}
    assert entity_aliases.load_aliases("") == {}
    assert entity_aliases.load_aliases(None) == {}


def test_load_aliases_reads_id_name_and_every_alias(tmp_path):
    repo = _write_registry(tmp_path, {
        "globex": {"name": "Globex", "type": "organization", "aliases": ["Globex Corp", "GX Industries"]}
    })
    path = entity_aliases.default_path(repo)
    aliases = entity_aliases.load_aliases(path)
    assert aliases["globex"] == "globex"
    assert aliases["globex corp"] == "globex"
    assert aliases["gx industries"] == "globex"


def test_load_aliases_normalizes_accents_and_case(tmp_path):
    repo = _write_registry(tmp_path, {
        "acme": {"name": "Ácme Çorp", "type": "organization", "aliases": []}
    })
    aliases = entity_aliases.load_aliases(entity_aliases.default_path(repo))
    assert aliases["acme corp"] == "acme"


def test_load_aliases_malformed_top_level_raises(tmp_path):
    repo = tmp_path / "repo"
    ops = repo / "ops"
    ops.mkdir(parents=True)
    (ops / "entity-registry.json").write_text(json.dumps({"not-entities": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="entities"):
        entity_aliases.load_aliases(str(ops / "entity-registry.json"))


def test_resolve_entity_finds_the_longest_matching_alias():
    aliases = {"acme": "acme", "acme corp": "acme-corp", "globex": "globex"}
    assert entity_aliases.resolve_entity(aliases, "what is the arr for acme corp this year?") == "acme-corp"


def test_resolve_entity_is_whole_word_not_substring():
    aliases = {"gx": "gx-shortco"}
    assert entity_aliases.resolve_entity(aliases, "logging into the system") is None
    assert entity_aliases.resolve_entity(aliases, "the numbers for gx look good") == "gx-shortco"


def test_resolve_entity_no_match_returns_none():
    aliases = {"acme": "acme"}
    assert entity_aliases.resolve_entity(aliases, "what is the weather in antarctica?") is None


def test_resolve_entity_empty_aliases_or_question_returns_none():
    assert entity_aliases.resolve_entity({}, "acme") is None
    assert entity_aliases.resolve_entity({"acme": "acme"}, "") is None


# ── load_registry (full records) and resolve_exact (describe_entity's input) ───────────────────
def test_load_registry_missing_file_returns_empty_dict(tmp_path):
    assert entity_aliases.load_registry(str(tmp_path / "nope.json")) == {}
    assert entity_aliases.load_registry("") == {}
    assert entity_aliases.load_registry(None) == {}


def test_load_registry_returns_full_records(tmp_path):
    repo = _write_registry(tmp_path, {
        "globex": {"name": "Globex", "type": "organization",
                  "aliases": ["Globex Corp", "GX Industries"]}
    })
    registry = entity_aliases.load_registry(entity_aliases.default_path(repo))
    assert registry == {"globex": {"id": "globex", "name": "Globex", "type": "organization",
                                   "aliases": ["Globex Corp", "GX Industries"]}}


def test_load_registry_defaults_missing_fields_honestly(tmp_path):
    """A registry entry may omit `type`/`aliases` — served as empty, never a KeyError."""
    repo = _write_registry(tmp_path, {"acme": {"name": "Acme"}})
    registry = entity_aliases.load_registry(entity_aliases.default_path(repo))
    assert registry["acme"] == {"id": "acme", "name": "Acme", "type": "", "aliases": []}


def test_load_registry_skips_a_non_mapping_record_like_load_aliases_does(tmp_path):
    repo = _write_registry(tmp_path, {"acme": "not-a-dict", "globex": {"name": "Globex"}})
    registry = entity_aliases.load_registry(entity_aliases.default_path(repo))
    assert "acme" not in registry
    assert registry["globex"]["id"] == "globex"


def test_load_registry_malformed_top_level_raises_like_load_aliases_does(tmp_path):
    """Same loader (`_load_entities`), same fail-loud posture — proven independently rather than
    assumed from the shared function alone."""
    repo = tmp_path / "repo"
    ops = repo / "ops"
    ops.mkdir(parents=True)
    (ops / "entity-registry.json").write_text(json.dumps({"not-entities": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="entities"):
        entity_aliases.load_registry(str(ops / "entity-registry.json"))


def test_resolve_exact_matches_id_name_or_alias_case_and_accent_insensitively(tmp_path):
    repo = _write_registry(tmp_path, {
        "globex": {"name": "Globex Corp", "type": "organization", "aliases": ["GX Industries"]}
    })
    aliases = entity_aliases.load_aliases(entity_aliases.default_path(repo))
    assert entity_aliases.resolve_exact(aliases, "globex") == "globex"
    assert entity_aliases.resolve_exact(aliases, "Globex Corp") == "globex"
    assert entity_aliases.resolve_exact(aliases, "gx industries") == "globex"
    assert entity_aliases.resolve_exact(aliases, "GX INDUSTRIES") == "globex"


def test_resolve_exact_does_not_substring_match_inside_a_longer_phrase():
    """The exact-match sibling of `resolve_entity`'s substring search: a question CONTAINING the
    alias must not resolve here — that is `resolve_entity`'s job, not this one's."""
    aliases = {"globex": "globex"}
    assert entity_aliases.resolve_exact(aliases, "tell me about globex now") is None


def test_resolve_exact_unknown_input_returns_none():
    assert entity_aliases.resolve_exact({"acme": "acme"}, "totally-unregistered") is None


def test_resolve_exact_empty_aliases_or_text_returns_none():
    assert entity_aliases.resolve_exact({}, "acme") is None
    assert entity_aliases.resolve_exact({"acme": "acme"}, "") is None
