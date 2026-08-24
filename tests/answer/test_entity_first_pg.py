"""Entity-aware ranking through the answer renderer."""
import json
import os
from pathlib import Path

import pytest

from stigmergy.answer.brain import AnswerBrain
from stigmergy.index import build, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.index.support import write_controls
from tests.server.conftest import connect_or_skip, write_page

BOREALIS_PAGE = "wiki/notes/borealis-quarterly-update.md"
CONTOSO_PAGE = "wiki/notes/contoso-quarterly-update.md"
BOREALIS_ID = "ent_50000000-0000-4000-8000-000000000001"
CONTOSO_ID = "ent_50000000-0000-4000-8000-000000000002"
SHARED_BODY = ("Quarterly update covering the renewal terms discussed with the account team "
              "this cycle, including the proposed uplift.")


def _entity(entity_id: str, name: str, aliases=()) -> dict:
    values = [(name, "preferred"), *((alias, "alias") for alias in aliases)]
    return {
        "entity_type": "organization",
        "created_at": "2026-08-24T00:00:00Z",
        "updated_at": "2026-08-24T00:00:00Z",
        "claims": [
            {
                "claim_id": f"{entity_id}-{index}",
                "value": value,
                "normalized": value.casefold(),
                "kind": kind,
                "acl": None,
                "source": "sources/2026/08/fixture.md",
                "actor": "steward",
                "introduced_at": "2026-08-24T00:00:00Z",
            }
            for index, (value, kind) in enumerate(values)
        ],
        "external_ids": [],
        "absorbed_ids": [],
    }


class _EntityFirstFixture:
    def __init__(self, root: str):
        self.repo = os.path.join(root, "repo")
        write_page(self.repo, BOREALIS_PAGE,
                  {"type": "note", "title": "Borealis Quarterly Update",
                   "entity": f"['{BOREALIS_ID}']",
                   "as_of": "2026-Q2", "verification": "verified"}, SHARED_BODY)
        write_page(self.repo, CONTOSO_PAGE,
                  {"type": "note", "title": "Contoso Quarterly Update",
                   "entity": f"['{CONTOSO_ID}']",
                   "as_of": "2026-Q2", "verification": "verified"}, SHARED_BODY)
        ops_dir = os.path.join(self.repo, "ops")
        os.makedirs(ops_dir, exist_ok=True)
        with open(os.path.join(ops_dir, "entity-registry.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": 1,
                    "entities": {
                        BOREALIS_ID: _entity(
                            BOREALIS_ID,
                            "Borealis Inc",
                            ["Borealis Corp", "BorealisCo"],
                        ),
                        CONTOSO_ID: _entity(CONTOSO_ID, "Contoso Ltd"),
                    },
                    "redirects": {},
                },
                f,
            )
        write_controls(Path(self.repo))
        self.entity_registry_path = os.path.join(ops_dir, "entity-registry.json")


@pytest.fixture(scope="module")
def entity_first_indexed(tmp_path_factory):
    fx = _EntityFirstFixture(str(tmp_path_factory.mktemp("entity-first")))
    conn = connect_or_skip()
    build.rebuild(conn, fx.repo, build_embedder("fake"))
    yield conn, fx
    store.clear_ops_file(conn, store.ENTITY_REGISTRY_RELPATH)
    store.clear_ops_file(conn, store.IDENTITIES_RELPATH)
    conn.close()


def _brain(conn, fx) -> AnswerBrain:
    settings = Settings(llm="fake",
                        entity_registry_path=fx.entity_registry_path)
    service = BrainService(settings, conn, build_embedder("fake"), audiences=None)
    return AnswerBrain(service)


def test_without_a_registered_alias_both_similar_pages_can_surface(entity_first_indexed):
    conn, fx = entity_first_indexed
    brain = _brain(conn, fx)
    listing = brain.search_text("quarterly update renewal terms uplift")
    assert BOREALIS_PAGE in listing and CONTOSO_PAGE in listing


def test_a_question_naming_the_alias_resolves_to_its_entity_before_searching(entity_first_indexed):
    conn, fx = entity_first_indexed
    brain = _brain(conn, fx)
    listing = brain.search_text("what's new for BorealisCo this quarter — renewal terms uplift?")
    assert BOREALIS_PAGE in listing
    assert listing.index(BOREALIS_PAGE) < listing.index(CONTOSO_PAGE)


def test_the_canonical_name_also_resolves_the_same_way(entity_first_indexed):
    conn, fx = entity_first_indexed
    brain = _brain(conn, fx)
    listing = brain.search_text("Borealis Inc renewal terms uplift this quarter")
    assert BOREALIS_PAGE in listing
    assert listing.index(BOREALIS_PAGE) < listing.index(CONTOSO_PAGE)


def test_a_resolved_entity_never_costs_the_agent_a_page(entity_first_indexed):
    conn, fx = entity_first_indexed
    brain = _brain(conn, fx)
    plain = brain.search_text("quarterly update renewal terms uplift")
    named = brain.search_text("BorealisCo quarterly update renewal terms uplift")
    for page in (BOREALIS_PAGE, CONTOSO_PAGE):
        if page in plain:
            assert page in named, (
                "naming a registered entity hid a page the same question found without it")


def test_a_question_naming_no_registered_entity_is_the_ordinary_unscoped_search(
        entity_first_indexed):
    conn, fx = entity_first_indexed
    brain = _brain(conn, fx)
    listing = brain.search_text("quarterly update renewal terms uplift")
    assert BOREALIS_PAGE in listing and CONTOSO_PAGE in listing
