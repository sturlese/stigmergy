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


# ── a malformed RECORD, not a malformed file ────────────────────────────────────────────────────
# `_load_entities`' docstring promises a missing file and a malformed one "behave IDENTICALLY for
# both readers"; `load_registry`'s promises "a record whose own shape is not a mapping is skipped,
# same as `load_aliases`". Parity was asserted only for the TOP level and the non-mapping record —
# one level down, `load_registry` defended every field and `load_aliases` defended none.

@pytest.mark.parametrize("record, expected, why", [
    ({"name": "Acme", "aliases": None}, {"acme"},
     "aliases null crashed load_aliases with a TypeError"),
    ({"name": None, "aliases": []}, {"acme"},
     "name null minted the phantom alias 'none'"),
    ({"name": "Acme", "aliases": "acme corp"}, {"acme"},
     "a string was unpacked into one-character aliases"),
    # The scalar survives on purpose — `load_registry` keeps `str | int | float` elements and
    # stringifies them, so `7` is a real alias to BOTH readers. Only the null is dropped, and the
    # point of this arm is that the two readers agree about which is which.
    ({"name": "Acme", "aliases": [None, 7]}, {"acme", "7"},
     "a null ELEMENT became the alias 'none'"),
])
def test_load_aliases_survives_a_malformed_record_like_load_registry_does(
        tmp_path, record, expected, why):
    """OLD BEHAVIOUR, per arm, all reachable from one hand-edited registry file:

    - `"aliases": null` -> `TypeError: Value after * must be an iterable` out of `load_aliases`,
      which `search_brain` then reported as `search_brain failed (TypeError)` — a whole-server
      retrieval outage from one JSON null, while `list_entities` kept working.
    - `"name": null` -> `str(None)` normalizes to `"none"`, so the registry gained an alias
      `none` and `resolve_entity` mapped ANY question containing that word to this entity. That is
      the arm that fails OPEN: no error anywhere, just a wrong `entity_hint` and a wrong
      `fts_expansion` fed into ranking.
    - `"aliases": "acme corp"` -> the STRING is unpacked by `*`, one character per alias, so a
      bare "a" in ordinary prose resolved to this entity.

    `load_registry` already survived all four; that asymmetry is the bug.
    """
    repo = _write_registry(tmp_path, {"acme": record})

    aliases = entity_aliases.load_aliases(entity_aliases.default_path(repo))

    assert set(aliases) == expected, f"{why}: got {aliases}"
    assert entity_aliases.resolve_entity(aliases, "we have none of that data yet") is None
    assert entity_aliases.resolve_entity(aliases, "a report about a company") is None


@pytest.mark.parametrize("record", [
    {"name": "Acme", "aliases": None},
    {"name": None, "aliases": []},
    {"name": "Acme", "aliases": "acme corp"},
    {"name": "Acme", "aliases": [None, 7]},
])
def test_both_readers_survive_the_same_malformed_record(tmp_path, record):
    """The parity itself, asserted directly: whatever one reader tolerates, the other must too.
    Pinned as a property so the next field added to a record cannot be defended in one and
    forgotten in the other."""
    path = entity_aliases.default_path(_write_registry(tmp_path, {"acme": record}))

    aliases = entity_aliases.load_aliases(path)
    registry = entity_aliases.load_registry(path)

    assert registry["acme"]["id"] == "acme"
    assert all(isinstance(a, str) for a in registry["acme"]["aliases"])
    # The parity itself: every alias one reader recognizes is a field the OTHER reader kept.
    record = registry["acme"]
    from_registry = {entity_aliases._norm(t)
                     for t in ("acme", record["name"], *record["aliases"])} - {""}
    assert set(aliases) == from_registry


def test_a_well_formed_record_still_registers_every_alias(tmp_path):
    """The benign twin. Defending the fields must not cost a real registry its aliases — the
    canonical id, the display name and every listed alias all still resolve."""
    repo = _write_registry(tmp_path, {
        "acme-corp": {"name": "Acme Corp", "type": "organization",
                      "aliases": ["Acme", "ACME Corporation"]},
    })

    aliases = entity_aliases.load_aliases(entity_aliases.default_path(repo))

    assert entity_aliases.resolve_entity(aliases, "how is Acme Corp doing?") == "acme-corp"
    assert entity_aliases.resolve_entity(aliases, "any news on ACME Corporation?") == "acme-corp"
    assert entity_aliases.resolve_entity(aliases, "what about acme-corp?") == "acme-corp"
