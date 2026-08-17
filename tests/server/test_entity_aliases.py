"""`server.entity_aliases`: the read-only reader over `ops/entity-registry.json`, and the
alias-resolution primitive `ask`'s entity-first retrieval is built on. It reads the registry file
directly rather than importing whatever wrote it — the architecture rule this package's own
docstring states. Pure, keyless, DB-less.

Since issue #74 the registry reaches the service from TWO places — the index's snapshot and the
file a process was started with — so the TEXT is the unit and the path-taking loaders are
`read_file` plus that one parser. Both halves are exercised here, and so is the property that
makes the split honest: the same bytes mean the same thing whichever road carried them.
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
    """Same parser (`_entities_from_text`), same fail-loud posture — proven independently rather
    than assumed from the shared function alone."""
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
# `_entities_from_text`'s docstring promises a missing registry and a malformed one behave
# IDENTICALLY for both readers; `load_registry`'s promises "a record whose shape is not a mapping is skipped,
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


# ── the TEXT is the unit (issue #74) ───────────────────────────────────────────────────────────
# The service no longer holds a path at all on the road that matters: the index's snapshot hands it
# BYTES, and `--entity-registry` is the fallback. So every promise the file road was proven to keep
# has to be a promise of the parser, and the two roads have to be provably the same parser — not
# the same by inspection, which is what "we refactored the read out of it" is worth on its own.

SNAPSHOT_ORIGIN = "snapshot in the index (ops/entity-registry.json)"


def test_the_relative_path_is_posix_because_a_pushed_path_list_is():
    """`server.webhook.registry_was_pushed` matches this string VERBATIM against GitHub's changed
    paths, which are POSIX on every platform — so this constant may never be os-joined, and
    `default_path` is the one place that re-splits it for the local filesystem."""
    assert entity_aliases.ENTITY_REGISTRY_RELPATH == "ops/entity-registry.json"
    assert entity_aliases.default_path("/x/repo").endswith(
        os.path.join("ops", "entity-registry.json"))


def test_no_registry_at_all_parses_to_nothing_rather_than_raising():
    """`None` is what `read_file` answers for an unset path and what `store.read_entity_registry`
    answers for a database with no snapshot — one shape, two sources, and the fail-OPEN half of
    the parser's contract: resolution finds nothing, the server still serves."""
    assert entity_aliases._entities_from_text(None, SNAPSHOT_ORIGIN) == {}
    assert entity_aliases.aliases_from_text(None, SNAPSHOT_ORIGIN) == {}
    assert entity_aliases.registry_from_text(None, SNAPSHOT_ORIGIN) == {}


def test_malformed_json_text_raises_rather_than_degrading_silently():
    """The fail-LOUD half. Bytes that are not JSON at all reach the parser from the snapshot now
    (a truncated fetch, a half-written row) exactly as they could from a hand-edited file, and a
    registry that silently parses to `{}` disables entity-first retrieval with no signal anywhere
    an operator or a golden run would see it."""
    with pytest.raises(ValueError):
        entity_aliases._entities_from_text("{not json at all", SNAPSHOT_ORIGIN)
    with pytest.raises(ValueError):
        entity_aliases.aliases_from_text("{not json at all", SNAPSHOT_ORIGIN)
    with pytest.raises(ValueError):
        entity_aliases.registry_from_text("{not json at all", SNAPSHOT_ORIGIN)


@pytest.mark.parametrize("text", [
    json.dumps({"not-entities": {}}),
    json.dumps({"entities": []}),
    json.dumps({"entities": "acme"}),
    json.dumps({"entities": None}),
], ids=["no-entities-key", "entities-is-a-list", "entities-is-a-string", "entities-is-null"])
def test_a_top_level_that_is_not_an_entities_object_raises_naming_its_origin(text):
    """The message is written for the OPERATOR who has to fix the registry, and with two sources
    it must say WHICH copy is broken — there is no path to give when the bytes came from the
    index. That is also exactly why this message may never reach a tool caller: the service
    converts it to `RegistryError` (see `tests/server/test_registry_freshness_pg.py`)."""
    with pytest.raises(ValueError, match="entities") as ex:
        entity_aliases._entities_from_text(text, SNAPSHOT_ORIGIN)
    assert SNAPSHOT_ORIGIN in str(ex.value)


@pytest.mark.parametrize("text", ["[{\"entities\": {}}]", '"a bare string"', "null", "42"],
                         ids=["list", "string", "null", "number"])
def test_a_top_level_that_is_not_a_json_object_raises_the_same_way(text):
    """The twin of the test above, for the shapes that used to escape it.

    **Old behaviour, and what it cost**: valid JSON whose top level is not an object reached
    `data.get("entities")` on a `list`, a `str`, `None` or an `int` and raised `AttributeError`,
    not the `ValueError` every other malformed shape raises. `service._registry_aliases` /
    `_registry_records` convert only `ValueError` into `RegistryError`, so for exactly these shapes
    `list_entities` broke its own documented promise and every registry reader failed with a raw
    `AttributeError`. Confidentiality was never at stake — the MCP closures collapse an
    unanticipated exception to its class name — the typed, uniform failure the service exists to
    give was.

    It mattered more since #74 than before it: the snapshot is a SECOND source of arbitrary bytes
    (a truncated fetch, a half-written row, a hand-repaired table), and neither source can be
    inspected with `cat`.
    """
    with pytest.raises(ValueError, match="top level must be an object") as ex:
        entity_aliases._entities_from_text(text, SNAPSHOT_ORIGIN)
    assert SNAPSHOT_ORIGIN in str(ex.value)


def test_a_null_name_in_snapshot_bytes_mints_no_alias_spelled_none():
    """`str(None)` normalizes to `none`, and an alias spelled `none` resolves EVERY question
    containing that ordinary word to this entity — the arm that fails open, with no error
    anywhere. Proven over TEXT because a snapshot is now a road to the same records."""
    text = json.dumps({"entities": {"acme": {"name": None, "type": "organization"}}})

    aliases = entity_aliases.aliases_from_text(text, SNAPSHOT_ORIGIN)

    assert set(aliases) == {"acme"}
    assert entity_aliases.resolve_entity(aliases, "we have none of that data yet") is None
    assert entity_aliases.registry_from_text(text, SNAPSHOT_ORIGIN)["acme"]["name"] == ""


def test_a_bare_string_aliases_field_in_snapshot_bytes_yields_no_single_letter_aliases():
    """`aliases` is unpacked with `*`: a STRING unpacks one character at a time, so a bare "a" in
    ordinary prose would resolve to this entity."""
    text = json.dumps({"entities": {"acme": {"name": "Acme", "aliases": "acme corp"}}})

    aliases = entity_aliases.aliases_from_text(text, SNAPSHOT_ORIGIN)

    assert set(aliases) == {"acme"}
    assert entity_aliases.resolve_entity(aliases, "a report about a company") is None
    assert entity_aliases.registry_from_text(text, SNAPSHOT_ORIGIN)["acme"]["aliases"] == []


def test_a_non_mapping_record_in_snapshot_bytes_is_skipped_by_both_readers():
    """A record that is not a mapping is skipped, not fatal: one bad entry must not blank the
    whole vocabulary — and the two readers must skip the SAME one."""
    text = json.dumps({"entities": {"acme": "not-a-dict", "globex": {"name": "Globex"}}})

    aliases = entity_aliases.aliases_from_text(text, SNAPSHOT_ORIGIN)
    registry = entity_aliases.registry_from_text(text, SNAPSHOT_ORIGIN)

    assert "acme" not in registry and "acme" not in aliases
    assert registry["globex"]["id"] == "globex" and aliases["globex"] == "globex"


# The property that JUSTIFIES the read/parse split: one parser, two sources. Every arm is a shape
# the file road was already proven to survive, replayed as bytes — if the two ever diverged, an
# entity would resolve on a machine holding a checkout and not on the deployed one, which is the
# original bug wearing a different hat.
@pytest.mark.parametrize("entities", [
    {"acme-corp": {"name": "Acme Corp", "type": "organization",
                   "aliases": ["Acme", "ACME Corporation"]}},
    {"acme": {"name": "Ácme Çorp", "type": "organization", "aliases": []}},
    {"acme": {"name": "Acme", "aliases": None}},
    {"acme": {"name": None, "aliases": []}},
    {"acme": {"name": "Acme", "aliases": "acme corp"}},
    {"acme": {"name": "Acme", "aliases": [None, 7]}},
    {"acme": "not-a-dict", "globex": {"name": "Globex"}},
    {},
], ids=["well-formed", "accents", "aliases-null", "name-null", "aliases-string",
        "alias-elements", "non-mapping-record", "empty-registry"])
def test_the_same_bytes_mean_the_same_thing_through_the_file_road_and_the_text_road(
        tmp_path, entities):
    """The index's snapshot and the `--entity-registry` file are ONE parser plus a different read.
    `load_aliases`/`load_registry` are asserted to be exactly `read_file` + the text parser, over
    the very same bytes on disk — the equality that lets `service._registry_source` choose a source
    without knowing how either one is parsed."""
    path = entity_aliases.default_path(_write_registry(tmp_path, entities))
    with open(path, encoding="utf-8") as f:
        text = f.read()

    assert entity_aliases.read_file(path) == text
    assert entity_aliases.aliases_from_text(text, path) == entity_aliases.load_aliases(path)
    assert entity_aliases.registry_from_text(text, path) == entity_aliases.load_registry(path)


def test_the_two_roads_agree_on_a_malformed_registry_too(tmp_path):
    """Agreement includes the RAISE: a snapshot that is not a registry must be as loud as a file
    that is not one — a fallback that quietly tolerated what the primary refuses (or the reverse)
    would make the operator's diagnosis depend on which copy the process happened to read."""
    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    path = repo / "ops" / "entity-registry.json"
    path.write_text(json.dumps({"not-entities": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="entities"):
        entity_aliases.load_aliases(str(path))
    with pytest.raises(ValueError, match="entities"):
        entity_aliases.aliases_from_text(path.read_text(encoding="utf-8"), str(path))
