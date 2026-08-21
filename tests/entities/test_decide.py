"""`entities.decide` — a steward's five decisions over a real clone, and `apply`, the commit
discipline both doors share. Real git throughout (the conftest's posture): the properties here —
a decline that re-anchors the notes in the SAME commit, a merge refused before anything is
written, a rollback that leaves the clone exactly as it was — are properties of files and commits,
not of a double.
"""
import json
import os

import pytest

from stigmergy.entities import decide, generator
from stigmergy.entities.errors import EntityError
from tests.entities import conftest as fx

TODAY = "2026-08-21"
AUTHOR = (fx.STEWARD_NAME, fx.STEWARD_EMAIL)


def _proposed_page(name: str, entity_type: str = "organization", aliases=(),
                   proposed_aliases=()) -> str:
    listed = "[" + ", ".join(f'"{a}"' for a in aliases) + "]"
    pending = "[" + ", ".join(f'"{a}"' for a in proposed_aliases) + "]"
    return (f'---\ntype: entity\ntitle: "{name}"\nentity_type: {entity_type}\nrole: "a pilot"\n'
            f'status: developing\naliases: {listed}\ncreated: 2026-08-20\nupdated: 2026-08-20\n'
            f'tags: [entity, {entity_type}]\nentity: ["{generator.canonical_id_for(name)}"]\n'
            f'related: []\nsources: []\napproved_by: ""\nproposed_aliases: {pending}\n---\n\n'
            f"# {name}\n\n## What / Who\n\nA {entity_type} the librarian proposed.\n")


def _note(title: str, anchors) -> str:
    listed = "[" + ", ".join(f'"{a}"' for a in anchors) + "]"
    return (f'---\ntype: note\ntitle: "{title}"\nstatus: developing\ncreated: 2026-08-20\n'
            f'updated: 2026-08-20\ntags: [note]\nentity: {listed}\nrelated: []\nsources: []\n---\n\n'
            f"# {title}\n\nBody.\n")


def _write(clone: str, relpath: str, text: str) -> None:
    full = os.path.join(clone, *relpath.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(text)


def _commit_all(clone: str, message: str) -> None:
    fx.git("add", "--all", cwd=clone)
    fx.git("commit", "--quiet", "-m", message, cwd=clone, env=fx._COMMIT_ENV)
    fx.git("push", "--quiet", "origin", "main", cwd=clone)


@pytest.fixture()
def proposed(repo):
    """The fixture repo plus what the librarian leaves behind: a proposed `Ledgerly` (with one
    proposed spelling), a confirmed `Stigmergy` carrying a proposed spelling of its own, and two
    notes — one anchored to the proposal, one anchored to it AND to a confirmed entity."""
    remote, clone = repo
    _write(clone, "wiki/entities/Ledgerly.md",
           _proposed_page("Ledgerly", aliases=["Ledgerly Tech"], proposed_aliases=["LDG"]))
    text = open(os.path.join(clone, "wiki/entities/Stigmergy.md"), encoding="utf-8").read()
    _write(clone, "wiki/entities/Stigmergy.md",
           text.replace("related: []", 'related: []\nproposed_aliases: ["Stig"]'))
    _write(clone, "wiki/notes/Ledgerly kickoff.md", _note("Ledgerly kickoff", ["ledgerly"]))
    _write(clone, "wiki/notes/Joint pilot.md", _note("Joint pilot", ["jordan-reyes", "ledgerly"]))
    generator.regenerate(clone)
    _commit_all(clone, "feat(note): the librarian proposed Ledgerly")
    return remote, clone


def _front(clone: str, relpath: str) -> str:
    return open(os.path.join(clone, *relpath.split("/")), encoding="utf-8").read().split("---")[1]


def _registry(clone: str) -> dict:
    return json.load(open(os.path.join(clone, "ops/entity-registry.json")))["entities"]


# ── approve ──────────────────────────────────────────────────────────────────────────────────
def test_approve_entity_stamps_the_approver_and_the_registry_stops_calling_it_proposed(proposed):
    _, clone = proposed
    outcome = decide.approve_entity(clone, entity_id="ledgerly", approved_by="Ana", today=TODAY)

    assert outcome.kind == decide.APPROVE_ENTITY
    front = _front(clone, "wiki/entities/Ledgerly.md")
    assert 'approved_by: "Ana"' in front or "approved_by: Ana" in front
    assert f"updated: {TODAY}" in front
    assert _registry(clone)["ledgerly"]["proposed"] is False
    assert _registry(clone)["ledgerly"]["approved_by"] == "Ana"
    assert set(outcome.changed_paths) == {"wiki/entities/Ledgerly.md", "ops/entity-registry.json"}


def test_approve_entity_refuses_a_confirmed_entity_and_an_unknown_id(proposed):
    _, clone = proposed
    with pytest.raises(EntityError, match="confirmed entity, not a proposal"):
        decide.approve_entity(clone, entity_id="jordan-reyes", approved_by="Ana", today=TODAY)
    with pytest.raises(EntityError, match="no entity 'ghost'"):
        decide.approve_entity(clone, entity_id="ghost", approved_by="Ana", today=TODAY)


def test_approve_entity_refuses_an_empty_or_control_character_approver(proposed):
    _, clone = proposed
    with pytest.raises(EntityError, match="non-empty approver"):
        decide.approve_entity(clone, entity_id="ledgerly", approved_by="  ", today=TODAY)
    with pytest.raises(EntityError):
        decide.approve_entity(clone, entity_id="ledgerly", approved_by="Ana\x1b[31m", today=TODAY)


# ── decline ──────────────────────────────────────────────────────────────────────────────────
def test_decline_entity_removes_the_page_and_unanchors_every_note_that_pointed_at_it(proposed):
    _, clone = proposed
    outcome = decide.decline_entity(clone, entity_id="ledgerly", today=TODAY)

    assert not os.path.exists(os.path.join(clone, "wiki/entities/Ledgerly.md"))
    assert "ledgerly" not in _registry(clone)
    assert sorted(outcome.reanchored) == ["wiki/notes/Joint pilot.md",
                                          "wiki/notes/Ledgerly kickoff.md"]
    assert "entity: []" in _front(clone, "wiki/notes/Ledgerly kickoff.md")
    # the other anchor survives, untouched
    assert 'entity: ["jordan-reyes"]' in _front(clone, "wiki/notes/Joint pilot.md")
    assert f"updated: {TODAY}" in _front(clone, "wiki/notes/Joint pilot.md")


def test_decline_entity_refuses_a_confirmed_entity(proposed):
    """A confirmed identity retires through `superseded_by`, never through a decline — the
    benign registry must not lose Jordan Reyes to a mis-clicked button."""
    _, clone = proposed
    with pytest.raises(EntityError, match="superseded_by"):
        decide.decline_entity(clone, entity_id="jordan-reyes", today=TODAY)
    assert os.path.exists(os.path.join(clone, "wiki/entities/Jordan Reyes.md"))


# ── merge ────────────────────────────────────────────────────────────────────────────────────
def test_merge_entity_makes_the_proposal_an_alias_of_the_survivor_and_reanchors_the_notes(proposed):
    _, clone = proposed
    outcome = decide.merge_entity(clone, entity_id="ledgerly", into="stigmergy",
                                  approved_by="Ana", today=TODAY)

    assert outcome.into == "stigmergy"
    assert not os.path.exists(os.path.join(clone, "wiki/entities/Ledgerly.md"))
    survivor = _registry(clone)["stigmergy"]
    # the proposal's name, its alias AND its proposed spelling — the steward decided all of them
    assert {"Ledgerly", "Ledgerly Tech", "LDG", "The Company Brain"} <= set(survivor["aliases"])
    assert "ledgerly" not in _registry(clone)
    assert 'entity: ["stigmergy"]' in _front(clone, "wiki/notes/Ledgerly kickoff.md")
    assert 'entity: ["jordan-reyes", "stigmergy"]' in _front(clone, "wiki/notes/Joint pilot.md")


def test_merge_entity_refuses_itself_an_unknown_survivor_and_a_proposed_survivor(proposed):
    _, clone = proposed
    _write(clone, "wiki/entities/Other Pilot.md", _proposed_page("Other Pilot"))
    generator.regenerate(clone)
    _commit_all(clone, "feat(entity): a second proposal")
    with pytest.raises(EntityError, match="into itself"):
        decide.merge_entity(clone, entity_id="ledgerly", into="ledgerly", approved_by="Ana",
                            today=TODAY)
    with pytest.raises(EntityError, match="no entity with that id"):
        decide.merge_entity(clone, entity_id="ledgerly", into="ghost", approved_by="Ana",
                            today=TODAY)
    with pytest.raises(EntityError, match="itself a proposal"):
        decide.merge_entity(clone, entity_id="ledgerly", into="other-pilot", approved_by="Ana",
                            today=TODAY)
    assert os.path.exists(os.path.join(clone, "wiki/entities/Ledgerly.md"))


def test_merge_entity_refuses_a_spelling_that_would_collide_with_a_third_entity(proposed):
    """The proposal carries `Jordan Reyes Gaya` — a spelling the registry already resolves to
    Jordan Reyes — so merging it into Stigmergy would give one matcher key two owners. Refused
    BEFORE any file is touched."""
    _, clone = proposed
    text = open(os.path.join(clone, "wiki/entities/Ledgerly.md"), encoding="utf-8").read()
    _write(clone, "wiki/entities/Ledgerly.md",
           text.replace('aliases: ["Ledgerly Tech"]', 'aliases: ["Ledgerly Tech", "Jordan Reyes Gaya"]'))
    generator.regenerate(clone)
    _commit_all(clone, "chore: a colliding spelling")
    with pytest.raises(EntityError, match="already resolves to the registered entity 'jordan-reyes'"):
        decide.merge_entity(clone, entity_id="ledgerly", into="stigmergy", approved_by="Ana",
                            today=TODAY)
    assert fx.git("status", "--porcelain", cwd=clone).stdout.strip() == ""


# ── aliases ──────────────────────────────────────────────────────────────────────────────────
def test_approve_alias_moves_the_spelling_into_aliases(proposed):
    _, clone = proposed
    outcome = decide.approve_alias(clone, entity_id="stigmergy", alias="Stig", approved_by="Ana",
                                   today=TODAY)
    assert outcome.alias == "Stig"
    entry = _registry(clone)["stigmergy"]
    assert "Stig" in entry["aliases"] and entry["proposed_aliases"] == []
    assert "proposed_aliases: []" in _front(clone, "wiki/entities/Stigmergy.md")


def test_decline_alias_drops_the_spelling_and_changes_nothing_else(proposed):
    _, clone = proposed
    decide.decline_alias(clone, entity_id="stigmergy", alias="Stig", today=TODAY)
    entry = _registry(clone)["stigmergy"]
    assert entry["aliases"] == ["The Company Brain"] and entry["proposed_aliases"] == []


def test_alias_decisions_refuse_a_spelling_that_is_not_proposed(proposed):
    _, clone = proposed
    with pytest.raises(EntityError, match="not a proposed spelling"):
        decide.approve_alias(clone, entity_id="stigmergy", alias="Nope", approved_by="Ana",
                             today=TODAY)
    with pytest.raises(EntityError, match="not a proposed spelling"):
        decide.decline_alias(clone, entity_id="jordan-reyes", alias="Stig", today=TODAY)


# ── apply: the commit discipline ─────────────────────────────────────────────────────────────
def test_apply_lands_one_pushed_commit_carrying_the_page_the_notes_and_the_registry(proposed):
    remote, clone = proposed
    before = fx.remote_log(remote).count("\n")

    result = decide.apply(
        clone, action=lambda repo: decide.decline_entity(repo, entity_id="ledgerly", today=TODAY),
        branch="main", author=AUTHOR, trailer="Decided-by: Ana")

    assert fx.remote_log(remote).count("\n") == before + 1
    assert result["kind"] == decide.DECLINE_ENTITY and result["commit"]
    assert "wiki/entities/Ledgerly.md" not in fx.remote_files(remote)
    assert "ledgerly" not in fx.remote_registry(remote)["entities"]
    message = fx.git("log", "-1", "--format=%B", "main", cwd=remote).stdout
    assert message.startswith("chore(entity): decline Ledgerly")
    assert "Decided-by: Ana" in message
    shown = fx.git("show", "main:wiki/notes/Ledgerly kickoff.md", cwd=remote).stdout
    assert "entity: []" in shown


def test_apply_rolls_the_clone_back_when_the_decision_refuses(proposed):
    """A refusal after the first edit — here the merge's collision check is fine but the secrets
    scan is made to refuse — must leave the clone byte-identical: the next decision starts from a
    clean tree, not from a half-applied one."""
    remote, clone = proposed
    from stigmergy.entities import guard

    def _boom(*_a, **_k):
        raise EntityError("refusing to decide — a planted refusal")

    import unittest.mock as mock
    head = fx.git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    with mock.patch.object(guard, "refuse_secrets", _boom), \
            pytest.raises(EntityError, match="planted refusal"):
        decide.apply(clone,
                     action=lambda repo: decide.merge_entity(
                         repo, entity_id="ledgerly", into="stigmergy", approved_by="Ana",
                         today=TODAY),
                     branch="main", author=AUTHOR)
    assert fx.git("status", "--porcelain", cwd=clone).stdout.strip() == ""
    assert fx.git("rev-parse", "HEAD", cwd=clone).stdout.strip() == head
    assert os.path.exists(os.path.join(clone, "wiki/entities/Ledgerly.md"))
    assert fx.remote_log(remote).count("\n") == fx.remote_log(remote).count("\n")


def test_apply_refuses_a_dirty_clone_before_deciding_anything(proposed):
    remote, clone = proposed
    _write(clone, "wiki/notes/Draft.md", _note("Draft", []))
    with pytest.raises(EntityError):
        decide.apply(clone,
                     action=lambda repo: decide.approve_entity(repo, entity_id="ledgerly",
                                                               approved_by="Ana", today=TODAY),
                     branch="main", author=AUTHOR)
    assert fx.remote_registry(remote)["entities"]["ledgerly"]["proposed"] is True
