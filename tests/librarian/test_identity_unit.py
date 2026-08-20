"""`librarian.identity.write_proposals`: the entities and spellings a filing CREATES, for a steward
to confirm afterwards — against a real checkout (the fixture knowledge repo: one registered
entity, `Acme Corp` / `acme-corp`, its page, the template and the registry), with no agent, no queue
and no git push.

The module's own contract, asserted from both sides: a well-formed proposal becomes a page with
`approved_by` empty and a regenerated registry the gates can resolve against; and each of the
honesty checks — the name must be in the material, must not collide with a registered spelling,
must not be an identity a steward declined — is a `Finding` the corrective brief can act on,
with NOTHING written. Every refusal has its benign twin here, because each of these checks can
bounce a real proposal.
"""
import json
import os
from types import SimpleNamespace

import yaml

from stigmergy.entities import generator
from stigmergy.kernel import registry as registry_module
from stigmergy.librarian import identity
from tests.librarian import support

TODAY = "2026-08-20"


def _proposal(name="Scircle", **over) -> dict:
    base = {"name": name, "entity_type": "organization", "role": "a perfume startup",
            "aliases": ("S-Circle",), "summary": f"{name} sells personalised perfume online.",
            "facts": ("Raised a seed round in 2026",), "connections": ("[[A Note]] — the note",)}
    base.update(over)
    return base


def _outcome(new_entities=(), new_aliases=()):
    return SimpleNamespace(new_entities=tuple(new_entities), new_aliases=tuple(new_aliases))


def _registry(repo: str):
    return registry_module.load_registry(os.path.join(repo, "ops", "entity-registry.json"))


def _write(repo, outcome, *, material, hints=None, declined=()):
    return identity.write_proposals(repo, outcome=outcome, base_registry=_registry(repo),
                                    material=material, hints=hints, declined_ids=set(declined),
                                    today=TODAY, related=["A Note"])


def _read(repo: str, relpath: str) -> str:
    with open(os.path.join(repo, *relpath.split("/")), encoding="utf-8") as f:
        return f.read()


# ── the proposal itself ───────────────────────────────────────────────────────────────────────
def test_a_proposed_entity_becomes_a_page_with_approved_by_empty_and_a_registry_that_resolves_it(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    before = _read(env.repo, "ops/entity-registry.json")

    proposals = _write(env.repo, _outcome([_proposal()]),
                       material="Scircle (S-Circle) is raising a Seed Tranche II.")

    assert isinstance(proposals, identity.Proposals), proposals
    assert proposals.entity_pages == {"wiki/entities/Scircle.md": "scircle"}
    assert proposals.entities == [{"id": "scircle", "name": "Scircle", "type": "organization"}]
    assert proposals.touched() and proposals.lane == ("wiki/entities/", "ops/entity-registry.json")
    page = _read(env.repo, "wiki/entities/Scircle.md")
    front = yaml.safe_load(page.split("---")[1])
    assert front["approved_by"] == "" and front["entity"] == ["scircle"]
    assert front["aliases"] == ["S-Circle"] and front["role"] == "a perfume startup"
    assert front["related"] == ["[[A Note]]"]
    assert "Scircle sells personalised perfume online." in page
    assert "- Raised a seed round in 2026" in page
    assert "<One clear paragraph" not in page           # every section the agent filled is filled
    # the registry this commit will PUBLISH: the new id resolves, and is marked proposed
    assert proposals.registry.canonical_id("Scircle") == "scircle"
    assert proposals.registry.canonical_id("S-Circle") == "scircle"
    assert proposals.registry.is_proposed("scircle")
    assert proposals.registry.canonical_id("Acme") == "acme-corp"          # the old one still does
    written = json.loads(_read(env.repo, "ops/entity-registry.json"))
    assert written["entities"]["scircle"]["proposed"] is True
    assert written != json.loads(before)
    # byte-proven for the gates: the registry, and nothing else (no existing page was edited)
    assert set(proposals.expected_bytes) == {"ops/entity-registry.json"}
    assert proposals.expected_bytes["ops/entity-registry.json"] == _read(
        env.repo, "ops/entity-registry.json")
    assert generator.check(env.repo).divergences == []


def test_nothing_proposed_touches_nothing_and_keeps_the_base_registry(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    base = _registry(env.repo)
    proposals = _write(env.repo, _outcome(), material="an ordinary note about Acme Corp")
    assert proposals.registry.entities == base.entities      # the base registry, unregenerated
    assert not proposals.touched() and proposals.lane == () and proposals.expected_bytes == {}
    assert not os.path.exists(os.path.join(env.repo, "wiki", "entities", "Scircle.md"))


def test_two_proposals_land_together_and_the_second_collides_with_the_first_if_it_is_the_same(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    proposals = _write(env.repo, _outcome([_proposal("Scircle"), _proposal("Nubelo", aliases=())]),
                       material="Scircle and Nubelo both appear here.")
    assert sorted(proposals.entity_pages.values()) == ["nubelo", "scircle"]

    env2 = support.build_repo(str(tmp_path / "git2"))
    findings = _write(env2.repo, _outcome([_proposal("Scircle"), _proposal("SCIRCLE", aliases=())]),
                      material="Scircle twice.")
    assert [f.code for f in findings] == ["collides"]
    assert not os.path.exists(os.path.join(env2.repo, "wiki", "entities", "Scircle.md")), (
        "a refused account writes nothing, not even the half that was fine")


# ── the three honesty checks, each with its twin ──────────────────────────────────────────────
def test_a_name_that_collides_with_a_registered_spelling_is_refused_and_the_brief_names_the_id(tmp_path):
    """The material is ABOUT the registered entity: anchor there, never mint a twin. The brief is
    what the agent reads on its one corrective pass, so it names the id to anchor to and the
    `new_aliases` road for a spelling the material uses."""
    env = support.build_repo(str(tmp_path / "git"))
    findings = _write(env.repo, _outcome([_proposal("Acme Corp S.L.", aliases=())]),
                      material="Acme Corp S.L. signed the renewal.")
    (finding,) = findings
    assert finding.code == "collides" and finding.repairable
    assert "'acme-corp'" in finding.brief and "new_aliases" in finding.brief
    assert _registry(env.repo).entities.keys() == {"acme-corp"}     # nothing regenerated


def test_a_name_the_material_never_uses_is_refused_but_a_hint_naming_it_counts(tmp_path):
    """A model that proposes an entity the capture never mentions is inventing one. The submitter's
    own hint is the one other place the name may come from — `hints.entity: Scircle` is them
    saying what the capture is about."""
    env = support.build_repo(str(tmp_path / "git"))
    findings = _write(env.repo, _outcome([_proposal("Scircle", aliases=())]),
                      material="a note that never says the name")
    assert [f.code for f in findings] == ["unnamed-in-material"]

    env2 = support.build_repo(str(tmp_path / "git2"))
    proposals = _write(env2.repo, _outcome([_proposal("Scircle", aliases=())]),
                       material="a note that never says the name", hints={"entity": "Scircle"})
    assert isinstance(proposals, identity.Proposals)


def test_an_identity_a_steward_declined_is_not_proposed_again(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    findings = _write(env.repo, _outcome([_proposal("Scircle", aliases=())]),
                      material="Scircle again.", declined={"scircle"})
    assert [f.code for f in findings] == ["declined"]
    assert "declined" in findings[0].brief and "Do not propose it again" in findings[0].brief


def test_an_invalid_identity_is_refused_with_the_birth_gates_own_sentence(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    findings = _write(env.repo, _outcome([_proposal("Scircle", entity_type="spaceship",
                                                    aliases=())]),
                      material="Scircle is a spaceship.")
    assert [f.code for f in findings] == ["invalid"]
    assert "entity types" in findings[0].message


# ── a proposed spelling for an entity that already exists ────────────────────────────────────
def test_a_proposed_alias_is_appended_to_the_entitys_page_and_resolves_in_the_registry(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    page_before = _read(env.repo, "wiki/entities/Acme Corp.md")

    proposals = _write(env.repo, _outcome(new_aliases=[{"entity": "acme-corp",
                                                        "alias": "Acme Corporation"}]),
                       material="Acme Corporation renewed the contract.")

    assert isinstance(proposals, identity.Proposals), proposals
    assert proposals.alias_pages == {"wiki/entities/Acme Corp.md": [("acme-corp", "Acme Corporation")]}
    assert proposals.aliases == [{"entity": "acme-corp", "alias": "Acme Corporation"}]
    page = _read(env.repo, "wiki/entities/Acme Corp.md")
    front = yaml.safe_load(page.split("---")[1])
    assert front["proposed_aliases"] == ["Acme Corporation"]
    assert front["aliases"] == ["Acme"]                   # the approved list is untouched
    assert page.startswith(page_before.split("\n---\n")[0][:60])
    assert proposals.registry.canonical_id("Acme Corporation") == "acme-corp"
    assert proposals.registry.proposed_alias_pairs() == [("acme-corp", "Acme Corporation")]
    assert set(proposals.expected_bytes) == {"wiki/entities/Acme Corp.md",
                                             "ops/entity-registry.json"}
    assert proposals.expected_bytes["wiki/entities/Acme Corp.md"] == page
    assert generator.check(env.repo).divergences == []


def test_an_alias_the_entity_already_has_proposes_nothing(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    proposals = _write(env.repo, _outcome(new_aliases=[{"entity": "acme-corp", "alias": "Acme"}]),
                       material="Acme renewed.")
    assert isinstance(proposals, identity.Proposals) and not proposals.touched()


def test_an_alias_for_an_unknown_entity_or_one_that_resolves_elsewhere_is_refused(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    unknown = _write(env.repo, _outcome(new_aliases=[{"entity": "globex", "alias": "GX"}]),
                     material="GX renewed.")
    assert [f.code for f in unknown] == ["unknown-entity"]

    env2 = support.build_repo(str(tmp_path / "git2"))
    both = _write(env2.repo, _outcome([_proposal("Scircle", aliases=())],
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
def test_a_drifting_registry_refuses_every_proposal_unrepairably(tmp_path):
    """`mint.mint`'s own rule, applied to the filing: regenerating would resolve somebody else's
    drift inside a commit whose message says it filed a note. A steward fixes the knowledge repo;
    the capture fails naming the command."""
    env = support.build_repo(str(tmp_path / "git"))
    registry_path = os.path.join(env.repo, "ops", "entity-registry.json")
    data = json.loads(_read(env.repo, "ops/entity-registry.json"))
    data["entities"]["ghost"] = {"name": "Ghost", "type": "organization", "aliases": []}
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    findings = _write(env.repo, _outcome([_proposal("Scircle", aliases=())]),
                      material="Scircle.")
    (finding,) = findings
    assert finding.code == "drift" and finding.repairable is False
    assert generator.FIX_COMMAND in finding.message


def test_a_checkout_without_the_template_refuses_unrepairably(tmp_path):
    env = support.build_repo(str(tmp_path / "git"))
    os.remove(os.path.join(env.repo, "ops", "templates", "entity.md"))
    findings = _write(env.repo, _outcome([_proposal("Scircle", aliases=())]),
                      material="Scircle.")
    assert [f.code for f in findings] == ["no-template"] and findings[0].repairable is False
