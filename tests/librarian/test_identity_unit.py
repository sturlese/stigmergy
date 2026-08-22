"""`librarian.identity.write_births`: the entities and spellings a filing CREATES, confirmed by
the person whose capture it was — against a real checkout (the fixture knowledge repo: one
registered entity, `Acme Corp` / `acme-corp`, its page, the template and the registry), with no
agent, no queue and no git push.

The module's own contract, asserted from both sides: a well-formed declaration becomes a page whose
`approved_by` names the SUBMITTER and a regenerated registry the gates can resolve against; and
each of the honesty checks — the name must be in the material, and must not collide with a
registered spelling — is a `Finding` the corrective brief can act on, with NOTHING written. Every
refusal has its benign twin here, because each of these checks can bounce real work.
"""
import json
import os
import re
from types import SimpleNamespace

import yaml

from stigmergy.entities import generator
from stigmergy.kernel import registry as registry_module
from stigmergy.librarian import identity
from tests.librarian import support

TODAY = "2026-08-20"
# The capture's own submitter, and therefore the one name every identity this run creates may
# carry (ADR 044 D1). Told to the writer by the caller, never read out of the account.
SUBMITTER = "marc@example.com"


def _declared(name="Scircle", **over) -> dict:
    base = {"name": name, "entity_type": "organization", "role": "a perfume startup",
            "aliases": ("S-Circle",), "summary": f"{name} sells personalised perfume online.",
            "facts": ("Raised a seed round in 2026",), "connections": ("[[A Note]] — the note",)}
    base.update(over)
    return base


def _outcome(new_entities=(), new_aliases=()):
    return SimpleNamespace(new_entities=tuple(new_entities), new_aliases=tuple(new_aliases))


def _registry(repo: str):
    return registry_module.load_registry(os.path.join(repo, "ops", "entity-registry.json"))


def _write(repo, outcome, *, material, hints=None, approver=SUBMITTER):
    return identity.write_births(repo, outcome=outcome, base_registry=_registry(repo),
                                 material=material, hints=hints, today=TODAY,
                                 related=["A Note"], approver=approver)


def _read(repo: str, relpath: str) -> str:
    with open(os.path.join(repo, *relpath.split("/")), encoding="utf-8") as f:
        return f.read()


# ── the birth itself ──────────────────────────────────────────────────────────────────────────
def test_a_declared_entity_becomes_a_page_confirmed_by_the_submitter_and_a_registry_that_resolves_it(tmp_path):
    """OLD BEHAVIOUR: the page was written with `approved_by: ""` and the registry entry carried
    `proposed: true`, waiting for a steward to confirm it from an inbox. ADR 044: the capture IS
    the approval — the page names the submitter, the entry carries no waiting state, and the
    identity is finished the moment it lands."""
    env = support.build_repo(str(tmp_path / "git"))
    before = _read(env.repo, "ops/entity-registry.json")

    births = _write(env.repo, _outcome([_declared()]),
                    material="Scircle (S-Circle) is raising a Seed Tranche II.")

    assert isinstance(births, identity.Births), births
    assert births.entity_pages == {"wiki/entities/Scircle.md": "scircle"}
    assert births.entities == [{"id": "scircle", "name": "Scircle", "type": "organization",
                                "confirmed_by": SUBMITTER}]
    assert births.confirmed == {"wiki/entities/Scircle.md": SUBMITTER}
    assert births.confirmed_ids == ["scircle"]
    assert births.touched() and births.lane == ("wiki/entities/", "ops/entity-registry.json")
    page = _read(env.repo, "wiki/entities/Scircle.md")
    front = yaml.safe_load(page.split("---")[1])
    assert front["approved_by"] == SUBMITTER and front["entity"] == ["scircle"]
    assert front["aliases"] == ["S-Circle"] and front["role"] == "a perfume startup"
    assert front["related"] == ["[[A Note]]"]
    assert "Scircle sells personalised perfume online." in page
    assert "- Raised a seed round in 2026" in page
    assert "<One clear paragraph" not in page           # every section the agent filled is filled
    # the registry this commit will PUBLISH: the new id resolves, confirmed by the submitter
    assert births.registry.canonical_id("Scircle") == "scircle"
    assert births.registry.canonical_id("S-Circle") == "scircle"
    assert births.registry.entities["scircle"]["approved_by"] == SUBMITTER
    assert births.registry.canonical_id("Acme") == "acme-corp"          # the old one still does
    written = json.loads(_read(env.repo, "ops/entity-registry.json"))
    assert written["entities"]["scircle"]["approved_by"] == SUBMITTER
    assert "proposed" not in written["entities"]["scircle"]
    assert written != json.loads(before)
    # byte-proven for the gates: the registry, and nothing else (no existing page was edited)
    assert set(births.expected_bytes) == {"ops/entity-registry.json"}
    assert births.expected_bytes["ops/entity-registry.json"] == _read(
        env.repo, "ops/entity-registry.json")
    assert generator.check(env.repo).divergences == []


def test_nothing_declared_touches_nothing_and_keeps_the_base_registry(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    base = _registry(env.repo)
    births = _write(env.repo, _outcome(), material="an ordinary note about Acme Corp")
    assert births.registry.entities == base.entities      # the base registry, unregenerated
    assert not births.touched() and births.lane == () and births.expected_bytes == {}
    assert not os.path.exists(os.path.join(env.repo, "wiki", "entities", "Scircle.md"))


def test_two_entities_are_born_together_and_the_second_collides_with_the_first_if_it_is_the_same(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    births = _write(env.repo, _outcome([_declared("Scircle"), _declared("Nubelo", aliases=())]),
                    material="Scircle and Nubelo both appear here.")
    assert sorted(births.entity_pages.values()) == ["nubelo", "scircle"]
    # both are the same submitter's: one capture, one approver, however many identities it names
    assert set(births.confirmed.values()) == {SUBMITTER}

    env2 = support.build_repo(str(tmp_path / "git2"))
    findings = _write(env2.repo, _outcome([_declared("Scircle"), _declared("SCIRCLE", aliases=())]),
                      material="Scircle twice.")
    assert [f.code for f in findings] == ["collides"]
    assert not os.path.exists(os.path.join(env2.repo, "wiki", "entities", "Scircle.md")), (
        "a refused account writes nothing, not even the half that was fine")


# ── the two honesty checks, each with its twin ────────────────────────────────────────────────
def test_a_name_that_collides_with_a_registered_spelling_is_refused_and_the_brief_names_the_id(tmp_path):
    """The material is ABOUT the registered entity: anchor there, never create a twin. The brief is
    what the agent reads on its one corrective pass, so it names the id to anchor to and the
    `new_aliases` road for a spelling the material uses."""
    env = support.build_repo(str(tmp_path / "git"))
    findings = _write(env.repo, _outcome([_declared("Acme Corp S.L.", aliases=())]),
                      material="Acme Corp S.L. signed the renewal.")
    (finding,) = findings
    assert finding.code == "collides" and finding.repairable
    assert "'acme-corp'" in finding.brief and "new_aliases" in finding.brief
    assert _registry(env.repo).entities.keys() == {"acme-corp"}     # nothing regenerated


def test_a_name_the_material_never_uses_is_refused_but_a_hint_naming_it_counts(tmp_path):
    """A model that introduces an entity the capture never mentions is inventing one. The
    submitter's own hint is the one other place the name may come from — `hints.entity: Scircle`
    is them saying what the capture is about."""
    env = support.build_repo(str(tmp_path / "git"))
    findings = _write(env.repo, _outcome([_declared("Scircle", aliases=())]),
                      material="a note that never says the name")
    assert [f.code for f in findings] == ["unnamed-in-material"]

    env2 = support.build_repo(str(tmp_path / "git2"))
    births = _write(env2.repo, _outcome([_declared("Scircle", aliases=())]),
                    material="a note that never says the name", hints={"entity": "Scircle"})
    assert isinstance(births, identity.Births)


def test_an_invalid_identity_is_refused_with_the_birth_gates_own_sentence(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    findings = _write(env.repo, _outcome([_declared("Scircle", entity_type="spaceship",
                                                    aliases=())]),
                      material="Scircle is a spaceship.")
    assert [f.code for f in findings] == ["invalid"]
    assert "entity types" in findings[0].message


# ── a new spelling for an entity that already exists ─────────────────────────────────────────
def test_a_declared_alias_is_appended_to_the_entitys_page_and_resolves_in_the_registry(tmp_path):
    """OLD BEHAVIOUR: a spelling the material used for a registered entity went onto a second
    frontmatter list, `proposed_aliases:`, until a steward moved it across. ADR 044: a spelling the
    material uses IS one of the entity's names, so it goes straight onto `aliases:` and resolves
    from this commit on — there is one list, and no waiting."""
    env = support.build_repo(str(tmp_path / "git"))
    page_before = _read(env.repo, "wiki/entities/Acme Corp.md")

    births = _write(env.repo, _outcome(new_aliases=[{"entity": "acme-corp",
                                                     "alias": "Acme Corporation"}]),
                    material="Acme Corporation renewed the contract.")

    assert isinstance(births, identity.Births), births
    assert births.alias_pages == {"wiki/entities/Acme Corp.md": [("acme-corp", "Acme Corporation")]}
    assert births.aliases == [{"entity": "acme-corp", "alias": "Acme Corporation"}]
    page = _read(env.repo, "wiki/entities/Acme Corp.md")
    front = yaml.safe_load(page.split("---")[1])
    assert front["aliases"] == ["Acme", "Acme Corporation"]
    assert "proposed_aliases" not in front
    assert page.startswith(page_before.split("\n---\n")[0][:60])
    assert births.registry.canonical_id("Acme Corporation") == "acme-corp"
    assert set(births.expected_bytes) == {"wiki/entities/Acme Corp.md",
                                          "ops/entity-registry.json"}
    assert births.expected_bytes["wiki/entities/Acme Corp.md"] == page
    assert generator.check(env.repo).divergences == []


def test_an_alias_the_entity_already_has_changes_nothing(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    births = _write(env.repo, _outcome(new_aliases=[{"entity": "acme-corp", "alias": "Acme"}]),
                    material="Acme renewed.")
    assert isinstance(births, identity.Births) and not births.touched()


def test_an_alias_for_an_unknown_entity_or_one_that_resolves_elsewhere_is_refused(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    unknown = _write(env.repo, _outcome(new_aliases=[{"entity": "globex", "alias": "GX"}]),
                     material="GX renewed.")
    assert [f.code for f in unknown] == ["unknown-entity"]

    env2 = support.build_repo(str(tmp_path / "git2"))
    both = _write(env2.repo, _outcome([_declared("Scircle", aliases=())],
                                      [{"entity": "Scircle", "alias": "S-Circle"}]),
                  material="Scircle, also S-Circle.")
    assert [f.code for f in both] == ["alias-of-new-entity"]


def test_an_alias_the_material_never_uses_is_refused(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    findings = _write(env.repo, _outcome(new_aliases=[{"entity": "acme-corp",
                                                       "alias": "Acme Corporation"}]),
                      material="Acme renewed.")
    assert [f.code for f in findings] == ["unnamed-in-material"]


# ── the checkout itself can refuse ────────────────────────────────────────────────────────────
def test_a_drifting_registry_refuses_every_birth_unrepairably(tmp_path):
    """Regenerating would resolve somebody else's drift inside a commit whose message says it filed
    a note. An operator puts the knowledge repo back in step; the capture fails saying so.

    OLD BEHAVIOUR: the refusal ended in `generator.FIX_COMMAND`, then a runnable command. ADR 044:
    both sides are derived and a person hand-editing either is what caused this, so the sentence
    says whose the fix is instead of promising one nobody can run."""
    env = support.build_repo(str(tmp_path / "git"))
    registry_path = os.path.join(env.repo, "ops", "entity-registry.json")
    data = json.loads(_read(env.repo, "ops/entity-registry.json"))
    data["entities"]["ghost"] = {"name": "Ghost", "type": "organization", "aliases": []}
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    findings = _write(env.repo, _outcome([_declared("Scircle", aliases=())]),
                      material="Scircle.")
    (finding,) = findings
    assert finding.code == "drift" and finding.repairable is False
    assert identity.FIX_HINT in finding.message


def test_a_checkout_without_the_template_refuses_unrepairably(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    os.remove(os.path.join(env.repo, "ops", "templates", "entity.md"))
    findings = _write(env.repo, _outcome([_declared("Scircle", aliases=())]),
                      material="Scircle.")
    assert [f.code for f in findings] == ["no-template"] and findings[0].repairable is False


# ── a registration: the name the capture PINS, born like every other (ADR 042, ADR 044) ───────
def _registration(name="Scircle", entity_type="organization", aliases=("S-Circle",), source="admin"):
    from stigmergy.capture import schema
    return schema.Registration(name=name, entity_type=entity_type, aliases=tuple(aliases),
                               source=source)


def test_the_registered_entity_is_born_confirmed_by_the_person_who_registered_it(tmp_path):
    """Before ADR 042 a person registered an entity through a script that copied the template with
    the name filled in — twelve of the first brain's nineteen entity pages were born that way, with
    nothing said about the entity. Now their description is a capture, the librarian writes the page
    from it and from what the brain held, and the identity is born CONFIRMED by them: `approved_by`
    names the submitter, the registry entry says the same, and the gates are told which page."""
    env = support.build_repo(str(tmp_path / "git"))

    births = identity.write_births(
        env.repo, outcome=_outcome([_declared()]), base_registry=_registry(env.repo),
        material="Scircle sells personalised perfume online; I met them at a trade fair.",
        hints={"entity": "Scircle", "register_name": "Scircle"},
        today=TODAY, related=["A Note"], registration=_registration(), approver=SUBMITTER)

    assert isinstance(births, identity.Births), births
    page = yaml.safe_load(_read(env.repo, "wiki/entities/Scircle.md").split("---")[1])
    assert page["approved_by"] == SUBMITTER
    registry = json.loads(_read(env.repo, "ops/entity-registry.json"))["entities"]["scircle"]
    assert registry["approved_by"] == SUBMITTER
    assert births.confirmed == {"wiki/entities/Scircle.md": SUBMITTER}
    assert births.confirmed_ids == ["scircle"]
    assert births.entities[0]["confirmed_by"] == SUBMITTER
    assert "Scircle sells personalised perfume online." in _read(env.repo, "wiki/entities/Scircle.md")


def test_a_second_entity_beside_the_registration_is_born_confirmed_by_the_same_person(tmp_path):
    """OLD BEHAVIOUR: a registration confirmed exactly ONE name, and anything else the same account
    introduced was left `proposed: true` for the inbox. ADR 044: a registration only PINS the name
    and type; it carries no authority the capture did not already carry, so every identity in the
    run is born confirmed by the same submitter."""
    env = support.build_repo(str(tmp_path / "git"))
    outcome = _outcome([_declared(), _declared("Nebula Labs", aliases=(),
                                               summary="Nebula Labs is a lab.",
                                               connections=("[[A Note]] — the note",))])

    births = identity.write_births(
        env.repo, outcome=outcome, base_registry=_registry(env.repo),
        material="Scircle and Nebula Labs both came up.", hints={"register_name": "Scircle"},
        today=TODAY, related=["A Note"], registration=_registration(), approver=SUBMITTER)

    assert isinstance(births, identity.Births), births
    registry = json.loads(_read(env.repo, "ops/entity-registry.json"))["entities"]
    assert registry["scircle"]["approved_by"] == SUBMITTER
    assert registry["nebula-labs"]["approved_by"] == SUBMITTER
    assert sorted(births.confirmed_ids) == ["nebula-labs", "scircle"]


def test_an_account_that_ignores_the_registration_is_refused_with_a_brief_and_writes_nothing(tmp_path):
    """The capture asked for Scircle and the account introduced nothing: the refusal names the
    entity and the brief tells the retry exactly what to do — introduce it, or anchor to the
    registered entity it already is."""
    env = support.build_repo(str(tmp_path / "git"))
    before = _read(env.repo, "ops/entity-registry.json")

    findings = identity.write_births(
        env.repo, outcome=_outcome(), base_registry=_registry(env.repo),
        material="Scircle sells perfume.", hints={"register_name": "Scircle"},
        today=TODAY, registration=_registration(), approver=SUBMITTER)

    assert isinstance(findings, list) and [f.code for f in findings] == ["registration-missing"]
    assert findings[0].repairable and "Scircle" in findings[0].brief and "new_entities" in findings[0].brief
    assert _read(env.repo, "ops/entity-registry.json") == before
    assert not os.path.exists(os.path.join(env.repo, "wiki", "entities", "Scircle.md"))


def test_registering_a_name_the_registry_already_resolves_asks_nothing_of_the_account(tmp_path):
    """The other honest outcome: the capture registered `Acme Corp`, which the fixture registry
    already resolves to `acme-corp`. No twin is owed, so an account introducing nothing is not
    refused — the capture files anchored to the entity it already is."""
    env = support.build_repo(str(tmp_path / "git"))

    births = identity.write_births(
        env.repo, outcome=_outcome(), base_registry=_registry(env.repo),
        material="Acme Corp again.", hints={"register_name": "Acme Corp"},
        today=TODAY, registration=_registration(name="Acme Corp", aliases=()), approver=SUBMITTER)

    assert isinstance(births, identity.Births), births
    assert not births.touched() and births.confirmed == {}


# ── the spine accretes (ADR 042): facts a filing ADDS to a registered entity's page ───────────────
def _update(entity="acme-corp", facts=("Renewed the contract for another year.",),
            connections=("[[A Note]] — the note that established it",)):
    return {"entity": entity, "facts": tuple(facts), "connections": tuple(connections)}


def _outcome_with_updates(updates, new_entities=()):
    return SimpleNamespace(new_entities=tuple(new_entities), new_aliases=(),
                           entity_updates=tuple(updates))


def test_an_update_appends_facts_and_connections_to_the_registered_page_and_proves_the_bytes(tmp_path):
    """Before ADR 042 an entity page was written once — at birth — and everything the brain
    learned afterwards went to notes and views; the spine never grew. A filing may now declare
    what the material established about a REGISTERED entity, and the writer APPENDS it: under the
    page's own `## Facts` / `## Connections`, `updated:` moved to today, the whole file in
    `expected_bytes` so `gate_body_rewrite` proves the edit byte for byte."""
    env = support.build_repo(str(tmp_path / "git"))
    before = _read(env.repo, "wiki/entities/Acme Corp.md")

    births = _write(env.repo, _outcome_with_updates([_update()]), material="Acme Corp renewed.")

    assert isinstance(births, identity.Births), births
    after = _read(env.repo, "wiki/entities/Acme Corp.md")
    assert after != before and after.startswith(before.split("updated:")[0])
    assert "- Renewed the contract for another year." in after
    assert "- [[A Note]] — the note that established it" in after
    assert re.search(rf'^updated: "?{TODAY}"?$', after, re.M), "updated: moved to today"
    assert births.updated_pages == {"wiki/entities/Acme Corp.md": "acme-corp"}
    assert births.updates == [{"entity": "acme-corp", "facts": 1, "connections": 1}]
    assert births.expected_bytes["wiki/entities/Acme Corp.md"] == after
    assert births.touched() and births.entity_pages == {}


def test_a_page_without_the_section_gets_it_appended_and_a_repeated_line_is_not_added_twice(tmp_path):
    """Two edges of the append. A page born with nothing to say under Facts has no `## Facts` at
    all (empty sections are not written since the same change), so the section is created at the
    end; and a fact the page already carries — whitespace folded — is not learned twice, so an
    account that only repeats the page changes nothing and the writer says so by writing nothing."""
    env = support.build_repo(str(tmp_path / "git"))
    path = os.path.join(env.repo, "wiki", "entities", "Acme Corp.md")
    text = open(path, encoding="utf-8").read()
    front, tail = text.split("---\n", 2)[1], text.split("---\n", 2)[2]
    stripped = "\n".join(line for line in tail.split("\n")
                         if not line.startswith("## Facts") and not line.startswith("- "))
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{front}---\n{stripped}")
    support.commit_and_push(env.repo, "test: a page with no Facts section")

    births = _write(env.repo, _outcome_with_updates([_update(connections=())]),
                       material="Acme Corp renewed.")
    assert isinstance(births, identity.Births), births
    after = _read(env.repo, "wiki/entities/Acme Corp.md")
    assert "\n## Facts\n\n- Renewed the contract for another year.\n" in after

    support.commit_and_push(env.repo, "test: the fact is on the page now")
    again = _write(env.repo, _outcome_with_updates([_update(connections=())]),
                   material="Acme Corp renewed.")
    assert isinstance(again, identity.Births) and not again.touched()
    assert _read(env.repo, "wiki/entities/Acme Corp.md") == after


def test_an_update_naming_an_unknown_entity_or_an_entity_this_account_introduces_is_refused(tmp_path):
    """The two honesty checks, each with the brief the retry needs: a name the registry does not
    resolve is not a page to append to (introduce it instead), and facts about an entity this same
    account creates belong in that entry. Nothing is written on either."""
    env = support.build_repo(str(tmp_path / "git"))
    before = _read(env.repo, "wiki/entities/Acme Corp.md")

    unknown = _write(env.repo, _outcome_with_updates([_update(entity="Nobody Inc")]),
                     material="Nobody Inc renewed.")
    assert [f.code for f in unknown] == ["update-unknown-entity"] and "new_entities" in unknown[0].brief

    both = _write(env.repo, _outcome_with_updates([_update(entity="Scircle")], new_entities=[_declared()]),
                  material="Scircle (S-Circle) renewed.")
    assert [f.code for f in both] == ["update-of-new-entity"]
    assert _read(env.repo, "wiki/entities/Acme Corp.md") == before
    assert not os.path.exists(os.path.join(env.repo, "wiki", "entities", "Scircle.md"))
