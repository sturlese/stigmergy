"""The `entity-alias` kind's plan computation, as pure functions of a worktree's bytes.

No database, no git history, no gates — `test_deletion.py`'s posture, and for the same reason: this
kind's whole safety argument is that `plan` is a deterministic function of what is on disk, so it
has to be provable against a directory. The APPLY through a real clone and the eight gates is
`test_remote_pg.py`'s; the propose road is `test_propose_entity_alias_pg.py`'s.

**The constraint this file exists to pin, and it is not a choice.** The knowledge repo's own
contract linter refuses an alias that names an existing page (`alias 'X' collides with page
wiki/entities/X.md`) because the wikilink namespace is keyed on page STEMS. The absorbed page stays
by governance, so the survivor can claim the absorbed entity's ALIASES and never its own name.
`plan` refuses such a claim HERE, with a sentence, rather than letting `gate_contract` veto it at
apply time.
"""
import json
import os

import pytest

from stigmergy.entities import generator
from stigmergy.kernel import registry as registry_module
from stigmergy.repair import entity_alias, schema
from stigmergy.repair.errors import RepairError

SURVIVOR = "wiki/entities/Cofers.md"
ABSORBED = "wiki/entities/Cofers Holdings.md"
REGISTRY = "ops/entity-registry.json"


def _write(root, relpath: str, text: str) -> str:
    path = os.path.join(root, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return relpath


def _read(root, relpath: str) -> str:
    with open(os.path.join(root, *relpath.split("/")), encoding="utf-8") as f:
        return f.read()


def _entity(stem: str, entity_id: str, *, aliases=(), related=(), superseded_by: str = "") -> str:
    front = ["type: entity", f'title: "{stem}"', "status: developing",
             "entity_type: organization", 'role: ""',
             f"aliases: {json.dumps(list(aliases), ensure_ascii=False)}", "created: 2026-01-01",
             "updated: 2026-01-01", "tags: [entity, organization]", f'entity: ["{entity_id}"]',
             f"related: {json.dumps([f'[[{r}]]' for r in related], ensure_ascii=False)}",
             "sources: []"]
    if superseded_by:
        front.append(f'superseded_by: "[[{superseded_by}]]"')
    return ("---\n" + "\n".join(front) + "\n---\n\n"
            + f"# {stem}\n\n## What / Who\n\n{stem} is an organization in this corpus.\n")


def _note(title: str, entities, *, zone: str = "wiki/notes") -> str:
    front = ["type: note", f'title: "{title}"', "status: developing", "created: 2026-02-01",
             "updated: 2026-02-01", "tags: [note]",
             f"entity: {json.dumps(list(entities), ensure_ascii=False)}", "related: []",
             "sources: []"]
    return ("---\n" + "\n".join(front) + "\n---\n\n"
            + f"# {title}\n\nWhat this page records.\n")


@pytest.fixture()
def worktree(tmp_path):
    """A minimal knowledge-repo shape: two entity pages, one note anchored to each, and a registry
    DERIVED from the pages by the one thing that derives it."""
    root = str(tmp_path / "repo")
    _write(root, SURVIVOR, _entity("Cofers", "cofers"))
    _write(root, ABSORBED, _entity("Cofers Holdings", "cofers-holdings",
                                   aliases=["Cofers Grupo", "Grupo Cofers"]))
    _write(root, "wiki/notes/Holdings Renewal.md", _note("Holdings Renewal", ["cofers-holdings"]))
    _write(root, "wiki/notes/Cofers Kickoff.md", _note("Cofers Kickoff", ["cofers"]))
    generator.regenerate(root)
    return root


def _plan(worktree, survivor=SURVIVOR, absorbed=ABSORBED):
    return entity_alias.plan(worktree, survivor, absorbed)


def _op_names(ops):
    return [str(o[schema.OP_KIND_KEY]) for o in ops]


# ── the plan's shape ──────────────────────────────────────────────────────────────────────────
def test_the_plan_is_the_survivor_the_absorbed_page_the_reanchors_and_the_registry(worktree):
    ops = _plan(worktree)

    assert _op_names(ops) == [entity_alias.OP_ALIAS, entity_alias.OP_RETIRE,
                              entity_alias.OP_REANCHOR, entity_alias.OP_REGISTRY]
    assert entity_alias.survivor_path(ops) == SURVIVOR
    assert entity_alias.absorbed_path(ops) == ABSORBED
    assert entity_alias.reanchored_paths(ops) == ["wiki/notes/Holdings Renewal.md"]
    assert entity_alias.registry_paths(ops) == [REGISTRY]


def test_the_plan_is_ordered_so_two_runs_over_the_same_bytes_are_IDENTICAL(worktree):
    """The apply's proof is `recomputed == stored`, op for op — a set would pass a plan whose
    order had drifted, and the byte comparison is what makes the stored plan mean anything."""
    assert _plan(worktree) == _plan(worktree)


def test_the_survivor_gains_the_absorbed_entitys_aliases_and_a_link_to_its_page(worktree):
    ops = _plan(worktree)
    after = entity_alias.expected_bytes(ops)[SURVIVOR]

    assert 'aliases: ["Cofers Grupo", "Grupo Cofers"]' in after
    assert "[[Cofers Holdings]]" in after
    # Its own identity is untouched: a merge moves spellings, never the surviving name.
    assert 'title: "Cofers"' in after
    assert 'entity: ["cofers"]' in after


def test_the_absorbed_page_is_marked_superseded_and_gives_up_its_aliases(worktree):
    ops = _plan(worktree)
    after = entity_alias.expected_bytes(ops)[ABSORBED]

    assert 'superseded_by: "[[Cofers]]"' in after
    assert "aliases: []" in after
    # Its title, its id and its body survive — the record of what was believed before the merge.
    assert 'title: "Cofers Holdings"' in after
    assert 'entity: ["cofers-holdings"]' in after
    assert "Cofers Holdings is an organization in this corpus." in after


def test_the_absorbed_pages_aliases_are_MOVED_not_copied():
    """Both halves in one sentence, because the linter is what makes them one rule: two pages
    claiming one alias is `alias 'X' already declared by <page>`, an ERROR that `gate_contract`
    turns into a veto. So the survivor gaining a spelling and the absorbed page losing it are the
    same edit, not two."""
    text = _entity("Cofers Holdings", "cofers-holdings", aliases=["Cofers Grupo"])
    after = entity_alias.retired(text, "Cofers")

    assert "aliases: []" in after
    assert "Cofers Grupo" not in after


def test_a_page_anchored_to_the_absorbed_entity_is_reanchored_in_place(worktree):
    ops = _plan(worktree)
    after = entity_alias.expected_bytes(ops)["wiki/notes/Holdings Renewal.md"]

    assert 'entity: ["cofers"]' in after
    assert "cofers-holdings" not in after


def test_a_page_anchored_to_BOTH_keeps_one_anchor_and_loses_the_duplicate():
    text = _note("Both", ["cofers", "cofers-holdings"])
    after = entity_alias.reanchored(text, absorbed_id="cofers-holdings", survivor_id="cofers")
    assert 'entity: ["cofers"]' in after


def test_a_page_anchored_to_a_THIRD_entity_keeps_it_and_its_position():
    text = _note("Two anchors", ["globex", "cofers-holdings"])
    after = entity_alias.reanchored(text, absorbed_id="cofers-holdings", survivor_id="cofers")
    assert 'entity: ["globex", "cofers"]' in after


def test_a_page_anchored_to_neither_is_not_in_the_plan_at_all(worktree):
    """The benign twin, and it is what the whole apply's cross-check rests on: a path nobody named
    must be judged byte-identically to before, which begins with never being planned."""
    ops = _plan(worktree)
    assert "wiki/notes/Cofers Kickoff.md" not in entity_alias.expected_bytes(ops)
    assert schema.target_paths(ops) == sorted(
        [SURVIVOR, ABSORBED, "wiki/notes/Holdings Renewal.md", REGISTRY])


def test_the_absorbed_pages_own_self_anchor_is_never_reanchored(worktree):
    """It keeps pointing at its own retired id, which is what keeps it a registered identity the
    registry still names and `describe_entity` can still answer about. Re-anchoring it would make
    the absorbed entity vanish from `scoped_entities` and turn a governed retirement into a silent
    disappearance."""
    ops = _plan(worktree)
    assert ABSORBED not in entity_alias.reanchored_paths(ops)
    assert 'entity: ["cofers-holdings"]' in entity_alias.expected_bytes(ops)[ABSORBED]


# ── the registry: predicted here, written by the generator ────────────────────────────────────
def test_the_planned_registry_is_what_the_real_generator_produces(worktree):
    """The prediction and the write are the same file format because they go through the same
    serializer. Proven by actually applying and regenerating, not by comparing two predictions."""
    ops = _plan(worktree)
    predicted = entity_alias.expected_bytes(ops)[REGISTRY]

    edited, findings = entity_alias.apply_declared(worktree, ops)

    assert findings == []
    assert REGISTRY in edited
    assert _read(worktree, REGISTRY) == predicted


def test_an_alias_with_adversarial_but_roundtrippable_characters_still_applies_clean(tmp_path):
    """The round-trip property behind the byte-compare, at its hard edge: quotes, YAML comment
    and mapping markers, and non-ASCII all survive `yaml_list` -> page -> `generator` intact, so
    the registry the apply produces is byte-identical to the plan's prediction and the merge
    lands. Without this twin, the drift test below would read as "the byte-compare fires on
    anything unusual" — it does not; it fires on the ONE class that genuinely diverges."""
    root = str(tmp_path / "repo")
    _write(root, SURVIVOR, _entity("Cofers", "cofers"))
    _write(root, ABSORBED, _entity("Cofers Holdings", "cofers-holdings",
                                   aliases=['Grupo "Cofers"', "Cofers: SL #1", "Nubelo Andalucía"]))
    generator.regenerate(root)
    ops = _plan(root)

    edited, findings = entity_alias.apply_declared(root, ops)

    assert findings == []
    assert _read(root, REGISTRY) == entity_alias.expected_bytes(ops)[REGISTRY]


def test_an_alias_only_a_yaml_escape_can_spell_makes_the_registry_drift_refusal_FIRE(tmp_path):
    """`registry-drift` had never been seen to fire, and the reason it CAN is precise: the page
    serializer (`json.dumps`) writes U+0085 NEL as a raw byte, and PyYAML — YAML 1.1 — folds a raw
    NEL inside a double-quoted scalar to a space on the way back in. So an absorbed alias spelled
    with the `\\x85` escape (a hand edit; the wiki zone is people's to edit) survives the PLAN
    intact, is written RAW into the survivor's `aliases:` line, and comes back FOLDED when the real
    generator re-reads it — the registry produced is not the registry predicted, byte for byte,
    and the approval no longer describes what would land. The refusal is the difference between
    PREDICTING a file and ASSERTING one, and this is the one honest way to watch it work: no
    monkeypatched generator, a genuinely lossy round trip."""
    root = str(tmp_path / "repo")
    _write(root, SURVIVOR, _entity("Cofers", "cofers"))
    absorbed_text = _entity("Cofers Holdings", "cofers-holdings").replace(
        "aliases: []", 'aliases: ["Cofers\\x85Nubelo"]')
    _write(root, ABSORBED, absorbed_text)
    generator.regenerate(root)
    ops = _plan(root)

    edited, findings = entity_alias.apply_declared(root, ops)

    assert edited == []
    assert [f.code for f in findings] == [entity_alias.REGISTRY_DRIFT_CODE]
    assert findings[0].locator == REGISTRY


def test_after_the_merge_the_absorbed_entitys_spellings_resolve_to_the_SURVIVOR(worktree):
    """**The point of the alias, pinned rather than assumed.** A capture or a question naming
    `Cofers Grupo` reaches the surviving identity, and therefore the pages that were re-anchored
    onto it."""
    entity_alias.apply_declared(worktree, _plan(worktree))
    registry = registry_module.load_registry(os.path.join(worktree, *REGISTRY.split("/")))

    assert registry.canonical_id("Cofers Grupo") == "cofers"
    assert registry.canonical_id("Grupo Cofers") == "cofers"


def test_the_absorbed_entitys_own_NAME_still_names_its_retired_page(worktree):
    """**The constraint, asserted so nobody reads its absence as a bug.** `Cofers Holdings` is the
    name of a page that still exists, and the knowledge repo's contract linter refuses an alias
    that collides with a page — so that one spelling cannot move onto the survivor, and it keeps
    resolving to the identity the merge retired. That identity's page now says what absorbed it."""
    entity_alias.apply_declared(worktree, _plan(worktree))
    registry = registry_module.load_registry(os.path.join(worktree, *REGISTRY.split("/")))

    assert registry.canonical_id("Cofers Holdings") == "cofers-holdings"
    assert 'superseded_by: "[[Cofers]]"' in _read(worktree, ABSORBED)


def test_the_absorbed_page_still_EXISTS_after_the_merge(worktree):
    entity_alias.apply_declared(worktree, _plan(worktree))
    assert os.path.isfile(os.path.join(worktree, *ABSORBED.split("/")))


def test_the_registry_op_is_stored_even_when_the_file_comes_out_identical(worktree):
    """The ordinary case: the absorbed entity declares no alias, so nothing about the registry
    changes. The op is still stored — it is what the apply proves the real generator's output
    against — and `target_paths` is what keeps an unchanged file out of the set the diff is
    cross-checked against. Without that split the cross-check would demand a diff entry git will
    never produce and refuse every such merge."""
    _write(worktree, ABSORBED, _entity("Cofers Holdings", "cofers-holdings"))
    generator.regenerate(worktree)
    before = _read(worktree, REGISTRY)

    ops = _plan(worktree)

    assert entity_alias.registry_paths(ops) == [REGISTRY]
    assert entity_alias.expected_bytes(ops)[REGISTRY] == before
    assert REGISTRY not in schema.target_paths(ops)


def test_an_apply_whose_registry_came_out_identical_does_not_claim_it_in_the_ledger(worktree):
    """Red before the fix: `apply_declared` returned a hand-built set that ALWAYS named the
    registry, so the governance ledger's `paths` claimed a file the commit does not contain for
    every ordinary merge (an absorbed entity with no alias to move). The honest set is
    `schema.target_paths(ops)` — the same fact the diff cross-check judges, so the ledger and the
    cross-check can never disagree about what an approval touched."""
    _write(worktree, ABSORBED, _entity("Cofers Holdings", "cofers-holdings"))
    generator.regenerate(worktree)
    ops = _plan(worktree)

    edited, findings = entity_alias.apply_declared(worktree, ops)

    assert findings == []
    assert REGISTRY not in edited
    assert edited == schema.target_paths(ops)


# ── what the plan refuses, and why ────────────────────────────────────────────────────────────
def test_an_alias_that_names_an_existing_page_refuses_the_whole_plan(worktree):
    """The linter's rule, enforced at PLAN time. Refused rather than dropped: a merge that silently
    lost a spelling would be a repair whose whole point quietly did not happen for that name."""
    _write(worktree, "wiki/notes/Cofers Grupo.md", _note("Cofers Grupo", []))
    generator.regenerate(worktree)

    with pytest.raises(RepairError) as excinfo:
        _plan(worktree)

    assert "Cofers Grupo" in str(excinfo.value)
    assert "wiki/notes/Cofers Grupo.md" in str(excinfo.value)
    assert "collides with page" in str(excinfo.value)


def test_the_survivor_may_still_claim_its_OWN_name_the_benign_twin(worktree):
    """The linter skips a page declaring its own stem as an alias, and so must this — otherwise a
    perfectly ordinary entity page could never be a merge survivor."""
    _write(worktree, SURVIVOR, _entity("Cofers", "cofers", aliases=["Cofers"]))
    generator.regenerate(worktree)

    ops = _plan(worktree)

    assert "Cofers" in entity_alias.expected_bytes(ops)[SURVIVOR]


@pytest.mark.parametrize("path", ["wiki/notes/Holdings Renewal.md", "ops/entity-registry.json",
                                  "sources/Something.md"])
def test_a_page_outside_the_entity_zone_can_be_neither_survivor_nor_absorbed(worktree, path):
    with pytest.raises(RepairError) as excinfo:
        _plan(worktree, survivor=SURVIVOR, absorbed=path)
    assert "not an entity page" in str(excinfo.value)


def test_one_page_cannot_absorb_itself(worktree):
    with pytest.raises(RepairError) as excinfo:
        _plan(worktree, survivor=SURVIVOR, absorbed=SURVIVOR)
    assert "cannot absorb itself" in str(excinfo.value)


def test_an_entity_page_the_registry_cannot_be_derived_from_refuses_the_plan(worktree):
    """`generator.read_entity_pages` is strict, and the strictness is inherited rather than worked
    around: those are exactly the states in which `stigmergy-entities regenerate` refuses to run,
    so a merge planned against one would store a registry the apply could never produce."""
    _write(worktree, "wiki/entities/Untitled.md",
           "---\ntype: entity\nstatus: developing\n---\n\n# Untitled\n")

    with pytest.raises(RepairError) as excinfo:
        _plan(worktree)

    assert "cannot be rebuilt" in str(excinfo.value)


def test_a_missing_entity_page_refuses_the_plan(worktree):
    with pytest.raises(RepairError) as excinfo:
        _plan(worktree, absorbed="wiki/entities/Never Existed.md")
    assert "does not exist" in str(excinfo.value)


def test_a_merge_that_would_change_nothing_is_refused(worktree):
    """Already merged: the survivor links the absorbed page and carries its spellings, the absorbed
    page is superseded, and nothing is anchored to it. There is no question left for a steward."""
    _write(worktree, SURVIVOR, _entity("Cofers", "cofers", aliases=["Cofers Grupo"],
                                       related=["Cofers Holdings"]))
    _write(worktree, ABSORBED, _entity("Cofers Holdings", "cofers-holdings",
                                       superseded_by="Cofers"))
    _write(worktree, "wiki/notes/Holdings Renewal.md", _note("Holdings Renewal", ["cofers"]))
    generator.regenerate(worktree)

    with pytest.raises(RepairError) as excinfo:
        _plan(worktree)

    assert "already say what this merge would say" in str(excinfo.value)


# ── the validator, which runs at BOTH ends ────────────────────────────────────────────────────
def test_a_well_formed_plan_validates_clean(worktree):
    assert entity_alias.validate(worktree, _plan(worktree)) == []


def test_an_empty_op_list_is_refused_by_name(worktree):
    assert [f.code for f in entity_alias.validate(worktree, [])] == ["no-ops"]


def test_an_op_this_kind_does_not_perform_is_refused_by_name(worktree):
    ops = _plan(worktree)
    ops[2] = {**ops[2], schema.OP_KIND_KEY: "delete-page"}
    assert "unknown-kind" in {f.code for f in entity_alias.validate(worktree, ops)}


def test_a_second_survivor_op_is_refused_by_count(worktree):
    ops = _plan(worktree)
    ops.append({**ops[0], "path": "wiki/entities/Cofers Holdings.md"})
    codes = {f.code for f in entity_alias.validate(worktree, ops)}
    assert "wrong-op-count" in codes or "duplicate-path" in codes


def test_a_registry_op_naming_another_file_is_refused_by_name(worktree):
    ops = _plan(worktree)
    ops[-1] = {**ops[-1], "path": "ops/stewards.json"}
    assert "not-the-registry" in {f.code for f in entity_alias.validate(worktree, ops)}


def test_an_op_carrying_no_planned_bytes_is_refused_by_name(worktree):
    ops = _plan(worktree)
    ops[2] = {**ops[2], "planned_after": ""}
    assert "no-planned-bytes" in {f.code for f in entity_alias.validate(worktree, ops)}


def test_a_reanchor_op_outside_the_corpus_is_refused_by_name(worktree):
    ops = _plan(worktree)
    ops[2] = {**ops[2], "path": "ops/something.md"}
    assert "outside-corpus" in {f.code for f in entity_alias.validate(worktree, ops)}


# ── the apply: recompute, byte-compare, perform ───────────────────────────────────────────────
def test_the_apply_refuses_a_plan_the_corpus_has_moved_under(worktree):
    """A page that gained the absorbed entity's anchor since the proposal was made is a DIFFERENT
    merge, and performing the old one would leave that page anchored to a retired identity."""
    ops = _plan(worktree)
    _write(worktree, "wiki/notes/Arrived Later.md", _note("Arrived Later", ["cofers-holdings"]))

    edited, findings = entity_alias.apply_declared(worktree, ops)

    assert edited == []
    assert [f.code for f in findings] == [entity_alias.PLAN_DRIFT_CODE]


def test_a_tampered_planned_body_is_refused_by_the_recomputation(worktree):
    """The stored `planned_after` is the only column in this kind that carries whole page CONTENT,
    so a row edited between Approve and apply is exactly what the recomputation exists to catch."""
    ops = _plan(worktree)
    ops[0] = {**ops[0], "planned_after": ops[0]["planned_after"] + "\nsmuggled sentence\n"}

    edited, findings = entity_alias.apply_declared(worktree, ops)

    assert edited == []
    assert [f.code for f in findings] == [entity_alias.PLAN_DRIFT_CODE]


def test_a_tampered_extra_reanchor_op_is_refused_by_the_recomputation(worktree):
    """**The tampered proposal, at the plan layer.** An extra page in the re-anchor set is a page
    nobody's finding named, and the recomputation refuses the whole merge rather than performing
    the parts it agrees with."""
    ops = _plan(worktree)
    victim = "wiki/notes/Cofers Kickoff.md"
    ops.insert(3, {schema.OP_KIND_KEY: entity_alias.OP_REANCHOR, "path": victim,
                   "expected_before_hash": "0" * 64,
                   "planned_after": _note("Cofers Kickoff", ["cofers"])})
    before = _read(worktree, victim)

    edited, findings = entity_alias.apply_declared(worktree, ops)

    assert edited == []
    assert [f.code for f in findings] == [entity_alias.PLAN_DRIFT_CODE]
    assert _read(worktree, victim) == before


def test_the_apply_writes_exactly_the_planned_bytes(worktree):
    ops = _plan(worktree)
    planned = entity_alias.expected_bytes(ops)

    edited, findings = entity_alias.apply_declared(worktree, ops)

    assert findings == []
    assert sorted(edited) == sorted({SURVIVOR, ABSORBED, REGISTRY,
                                     "wiki/notes/Holdings Renewal.md"})
    for path in (SURVIVOR, ABSORBED, "wiki/notes/Holdings Renewal.md"):
        assert _read(worktree, path) == planned[path]


def test_an_unrelated_page_is_byte_identical_after_the_merge(worktree):
    """**The benign twin.** A path nobody named is judged byte-identically to before — the property
    every non-additive kind in this loop has to be able to state about itself."""
    victim = "wiki/notes/Cofers Kickoff.md"
    before = _read(worktree, victim)

    entity_alias.apply_declared(worktree, _plan(worktree))

    assert _read(worktree, victim) == before


# ── the readers every other surface goes through ──────────────────────────────────────────────
def test_the_lane_is_derived_from_the_plan_and_covers_nothing_else(worktree):
    assert entity_alias.lane_for(_plan(worktree)) == ("ops/", "wiki/entities/", "wiki/notes/")


def test_the_derived_file_set_is_the_registry_and_only_the_registry(worktree):
    """Narrower than `expected_bytes` on purpose: `gate_zone` refuses an in-lane write that is not
    a `.md` page, which is the right default. A caller is trusted about WHICH derived file its
    approval covers and about nothing else."""
    ops = _plan(worktree)
    assert entity_alias.derived_files(ops) == frozenset({REGISTRY})
    assert set(entity_alias.expected_bytes(ops)) > entity_alias.derived_files(ops)


def test_the_oversize_reason_names_the_size_and_the_page_count(worktree):
    ops = _plan(worktree)
    assert entity_alias.oversize_reason(ops, 10_000_000) == ""
    reason = entity_alias.oversize_reason(ops, 1)
    assert "entity-alias-plan-too-large" in reason
    assert str(entity_alias.plan_bytes(ops)) in reason


# ── the dismissal memory is keyed on the PAIR ─────────────────────────────────────────────────
def test_the_content_key_is_the_same_whichever_entity_the_model_picks(worktree):
    """**#69's lesson applied to a judgment that can legitimately flip.** Which of two entities
    survives is the model's call and it may come out the other way tomorrow; a steward who declined
    the merge declined the PAIR, and a key carrying the direction would ask them again the moment
    the answer flipped."""
    forward = schema.content_key(_plan(worktree, SURVIVOR, ABSORBED),
                                 kind=schema.KIND_ENTITY_ALIAS)
    backward = schema.content_key(_plan(worktree, ABSORBED, SURVIVOR),
                                  kind=schema.KIND_ENTITY_ALIAS)
    assert forward == backward


def test_the_content_key_ignores_which_pages_happen_to_be_anchored(worktree):
    """A page that gained the absorbed entity's anchor overnight changes the plan and changes
    nothing a steward was asked — `delete`'s own argument about its scrubs."""
    before = schema.content_key(_plan(worktree), kind=schema.KIND_ENTITY_ALIAS)
    _write(worktree, "wiki/notes/Arrived Later.md", _note("Arrived Later", ["cofers-holdings"]))
    after = schema.content_key(_plan(worktree), kind=schema.KIND_ENTITY_ALIAS)
    assert before == after


def test_a_merge_and_a_body_draft_about_one_page_are_two_different_questions(worktree):
    """`content_key` hashes the KIND with the ops, so a steward declining a merge has not declined
    a body draft for the same page."""
    ops = _plan(worktree)
    assert (schema.content_key(ops, kind=schema.KIND_ENTITY_ALIAS)
            != schema.content_key(ops, kind=schema.KIND_ENTITY_BODY))
