"""`kernel.registry` — the curated identity file the whole system defers to.

The graph build and the agent-proposed merge lane this file also used to cover (`build_graph`,
`build_entities`, `merges.propose`/`apply_merge`) are gone: the registry has exactly one writer,
`stigmergy-entities`, the governed birth door.
"""
import json
import pathlib

import pytest

from stigmergy.kernel.registry import Registry, load_registry, save_registry


def _registry_file(tmp_path, entities):
    path = tmp_path / "entity-registry.json"
    path.write_text(json.dumps({"entities": entities}))
    return str(path)


# ── the registry ─────────────────────────────────────────────────────────────
def test_load_registry_builds_alias_map(tmp_path):
    path = _registry_file(tmp_path, {
        "globex": {"name": "Globex", "type": "organization",
                   "aliases": ["Globex Corp", "GX Industries"]}})
    reg = load_registry(path)
    assert reg.canonical_id("GLOBEX CORP") == "globex"        # case and spacing are not judgments
    assert reg.canonical_id("gx industries") == "globex"
    assert reg.canonical_id("Initech") is None
    assert reg.title("globex") == "Globex"


def test_a_legal_form_no_longer_resolves_a_capture_and_still_blocks_a_mint(tmp_path):
    """The one fold that MOVED, pinned from both sides.

    OLD BEHAVIOUR: `canonical_id("Globex, S.L.")` returned `"globex"` — `normalize`'s legal-suffix
    table decided, at filing time, that those were one company. That is a claim about the world a
    suffix list cannot make: the day `Globex` and `Globex Co` are two real entities, code merges
    them with no human anywhere in the loop and no signal anywhere. So filing asks the narrow key
    and the JUDGMENT belongs to the agent, fenced by `gates.resolve_entity_ids` (the id it declares
    must exist) and by the park (unsure asks a steward).

    The mint gate is unchanged, and that is the half this test exists to keep honest: there a false
    negative lets a duplicate identity through a governed door, and the refusal falls closed onto a
    human. `collision_id` still folds the suffix.
    """
    path = _registry_file(tmp_path, {
        "globex": {"name": "Globex", "type": "organization", "aliases": []}})
    reg = load_registry(path)

    assert reg.canonical_id("Globex, S.L.") is None
    assert reg.collision_id("Globex, S.L.") == "globex"
    # ...and the benign twin, so this is a boundary and not a lookup that stopped working:
    assert reg.canonical_id("Globex") == reg.collision_id("Globex") == "globex"


def test_load_registry_missing_is_empty_malformed_is_loud(tmp_path):
    assert load_registry(None).entities == {}
    assert load_registry(str(tmp_path / "nope.json")).entities == {}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"entities": {"x": {}}}))
    with pytest.raises(ValueError, match="needs at least a 'name'"):
        load_registry(str(bad))


def test_registry_merges_aliases_across_normalize_boundaries(tmp_path):
    """'GX Industries' would never merge with 'Globex' mechanically — the registry decides, and
    every consumer that resolves a name goes through `canonical_id`."""
    path = _registry_file(tmp_path, {
        "globex": {"name": "Globex", "type": "organization", "aliases": ["GX Industries"]}})
    reg = load_registry(path)
    assert reg.canonical_id("GX Industries") == reg.canonical_id("Globex") == "globex"
    assert reg.title("globex") == "Globex"
    assert reg.type_of("globex") == "organization"


def test_save_registry_round_trips_and_sorts(tmp_path):
    """The file is a human artifact in git: stable key order and sorted aliases, so a mint shows
    up as one added block in a diff rather than a reshuffle of the whole file."""
    path = str(tmp_path / "entity-registry.json")
    reg = Registry()
    reg.entities["globex"] = {"name": "Globex", "type": "organization",
                              "aliases": ["GX Industries", "Globex Corp", "GX Industries"]}
    reg.entities["acme"] = {"name": "Acme", "type": "organization", "aliases": []}
    save_registry(path, reg)
    written = json.loads(pathlib.Path(path).read_text())
    assert list(written["entities"]) == ["acme", "globex"]
    assert written["entities"]["globex"]["aliases"] == ["GX Industries", "Globex Corp"]
    assert load_registry(path).canonical_id("globex corp") == "globex"


def test_a_top_level_array_registry_is_refused_loudly_not_an_attribute_error(tmp_path):
    """Malformed means LOUD, and loud means the module's own sentence.
    OLD BEHAVIOUR: a top-level JSON array raised AttributeError from `data.get`,
    a traceback instead of the named refusal every other malformed shape gets."""
    p = tmp_path / "entity-registry.json"
    p.write_text('[{"name": "Acme"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="top level must be an object"):
        load_registry(str(p))


# ── the lifecycle keys: a proposal resolves like an approval, and absence is not a proposal ──────
def test_a_proposed_entity_and_its_proposed_aliases_resolve_and_round_trip(tmp_path):
    """The librarian files a capture about a new name by creating the entity itself, unconfirmed.
    The whole point of proposing rather than parking is that nothing waits: the next capture using
    the name — or the proposed spelling — anchors to the same id. So both are keyed like approved
    spellings, and both survive `save_registry`/`load_registry` with their lifecycle intact."""
    from stigmergy.kernel.registry import entry, registry_text

    reg = Registry()
    reg.entities["scircle"] = entry("Scircle", "organization", proposed=True)
    reg.entities["ferrovial-nexus"] = entry("Ferrovial Nexus", "organization", ["FN"],
                                            approved_by="marc", proposed_aliases=["Ferrovial"])
    for cid, e in reg.entities.items():
        from stigmergy.kernel.registry import index_entity
        index_entity(reg, cid, e)
    assert reg.canonical_id("Scircle") == "scircle"
    assert reg.canonical_id("Ferrovial") == "ferrovial-nexus"       # the PROPOSED spelling
    assert reg.collision_id("scircle ltd") == "scircle"              # a proposal blocks a twin too
    assert reg.is_proposed("scircle") and not reg.is_proposed("ferrovial-nexus")
    assert reg.proposed_ids() == ["scircle"]
    assert reg.proposed_alias_pairs() == [("ferrovial-nexus", "Ferrovial")]

    path = str(tmp_path / "entity-registry.json")
    save_registry(path, reg)
    written = json.loads(pathlib.Path(path).read_text())
    assert written["entities"]["scircle"] == {
        "aliases": [], "approved_by": "", "name": "Scircle", "proposed": True,
        "proposed_aliases": [], "type": "organization"}
    assert written["entities"]["ferrovial-nexus"]["proposed_aliases"] == ["Ferrovial"]
    again = load_registry(path)
    assert again.is_proposed("scircle") and again.canonical_id("Ferrovial") == "ferrovial-nexus"
    assert registry_text(again) == registry_text(reg)


def test_a_registry_written_before_the_lifecycle_keys_reads_as_approved(tmp_path):
    """The benign twin, and the migration in one line: every entity in a pre-lifecycle file was
    born through a steward's door. Absent is NOT proposed — or the day this shipped, every
    deployment's inbox would have asked a steward to re-approve their whole registry."""
    path = _registry_file(tmp_path, {
        "globex": {"name": "Globex", "type": "organization", "aliases": ["GX"]}})
    reg = load_registry(path)
    assert reg.is_proposed("globex") is False
    assert reg.proposed_ids() == [] and reg.proposed_alias_pairs() == []
    assert reg.entities["globex"]["approved_by"] == ""
    assert reg.is_proposed("never-registered") is False   # unknown is refused elsewhere, by name
