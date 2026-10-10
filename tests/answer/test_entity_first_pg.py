"""Entity-aware ranking through the answer renderer: a question naming an entity page's title or
alias lands on the pages that LINK that page first."""
import os

import pytest

from stigmergy.answer.brain import AnswerBrain
from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.answer.conftest import write_entity
from tests.server.conftest import connect_or_skip, write_page

BOREALIS_PAGE = "wiki/notes/borealis-quarterly-update.md"
CONTOSO_PAGE = "wiki/notes/contoso-quarterly-update.md"
BOREALIS_ENTITY = "wiki/entities/Borealis Inc.md"
CONTOSO_ENTITY = "wiki/entities/Contoso Ltd.md"
SHARED_BODY = ("Quarterly update covering the renewal terms discussed with the account team "
              "this cycle, including the proposed uplift.")


class _EntityFirstFixture:
    def __init__(self, root: str):
        self.repo = os.path.join(root, "repo")
        assert write_entity(self.repo, "Borealis Inc",
                            "Borealis Inc is an account the team reviews every cycle.",
                            aliases=("Borealis Corp", "BorealisCo")) == BOREALIS_ENTITY
        assert write_entity(self.repo, "Contoso Ltd",
                            "Contoso Ltd is an account the team reviews every cycle.") == CONTOSO_ENTITY
        write_page(self.repo, BOREALIS_PAGE,
                  {"type": "note", "title": "Borealis Quarterly Update"},
                  f"{SHARED_BODY} Filed under [[Borealis Inc]].")
        write_page(self.repo, CONTOSO_PAGE,
                  {"type": "note", "title": "Contoso Quarterly Update"},
                  f"{SHARED_BODY} Filed under [[Contoso Ltd]].")


@pytest.fixture(scope="module")
def entity_first_indexed(tmp_path_factory):
    fx = _EntityFirstFixture(str(tmp_path_factory.mktemp("entity-first")))
    conn = connect_or_skip()
    build.rebuild(conn, fx.repo, build_embedder("fake"))
    yield conn, fx
    conn.close()


def _brain(conn, fx) -> AnswerBrain:
    service = BrainService(Settings(llm="fake"), conn, build_embedder("fake"), audiences=None)
    return AnswerBrain(service)


def test_without_a_named_entity_both_similar_pages_can_surface(entity_first_indexed):
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


def test_the_page_title_also_resolves_the_same_way(entity_first_indexed):
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
                "naming an entity page hid a page the same question found without it")


def test_a_question_naming_no_entity_page_is_the_ordinary_unscoped_search(
        entity_first_indexed):
    conn, fx = entity_first_indexed
    brain = _brain(conn, fx)
    listing = brain.search_text("quarterly update renewal terms uplift")
    assert BOREALIS_PAGE in listing and CONTOSO_PAGE in listing


def test_the_entity_filter_enumerates_the_page_and_its_linkers_only(entity_first_indexed):
    """`filters={"entity": <page path>}` is the enumeration tool: the entity page itself plus every
    page whose links carry it, and nothing that merely resembles them."""
    conn, fx = entity_first_indexed
    hits = _brain(conn, fx).service.search("quarterly update renewal terms uplift",
                                           filters={"entity": BOREALIS_ENTITY})
    paths = {hit["path"] for hit in hits["hits"]}
    assert BOREALIS_PAGE in paths
    assert CONTOSO_PAGE not in paths
    assert paths <= {BOREALIS_PAGE, BOREALIS_ENTITY}


def test_describe_entity_resolves_an_alias_to_the_page_and_its_linking_notes(entity_first_indexed):
    conn, fx = entity_first_indexed
    brain = _brain(conn, fx)
    described = brain.service.describe_entity("borealisco")
    assert described["found"] is True
    assert described["entity"]["path"] == BOREALIS_ENTITY
    assert described["entity"]["aliases"] == ["Borealis Corp", "BorealisCo"]
    assert [item["path"] for item in described["knowledge"]] == [BOREALIS_PAGE]
    listed = brain.service.entity_vocabulary()
    assert [entity["path"] for entity in listed["entities"]] == [BOREALIS_ENTITY, CONTOSO_ENTITY]
