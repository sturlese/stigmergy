"""Entity-first resolution in `ask`: the answering agent resolves the entity FIRST, using the
registry's aliases, before searching — query -> entity -> its material -> rank, rather than a
bare semantic search that finds the door and hopes.

Isolated from `tests/answer/conftest.py`'s shared fixture on purpose: that fixture's many existing
tests already name "globex"/"initech"/"acme" directly in their questions, and adding a registry
to it would change what THOSE tests exercise for unrelated reasons. This fixture is its own small
repo + its own `ops/entity-registry.json`, built once.
"""
import os

import pytest

from stigmergy.answer.brain import AnswerBrain
from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.server.conftest import connect_or_skip, write_page

BOREALIS_PAGE = "wiki/entities/borealis/quarterly-update.md"
CONTOSO_PAGE = "wiki/entities/contoso/quarterly-update.md"
SHARED_BODY = ("Quarterly update covering the renewal terms discussed with the account team "
              "this cycle, including the proposed uplift.")


class _EntityFirstFixture:
    def __init__(self, root: str):
        self.repo = os.path.join(root, "repo")
        write_page(self.repo, BOREALIS_PAGE,
                  {"type": "report", "title": "Borealis Quarterly Update", "entity": "borealis",
                   "as_of": "2026-Q2", "verification": "verified"}, SHARED_BODY)
        write_page(self.repo, CONTOSO_PAGE,
                  {"type": "report", "title": "Contoso Quarterly Update", "entity": "contoso",
                   "as_of": "2026-Q2", "verification": "verified"}, SHARED_BODY)
        ops_dir = os.path.join(self.repo, "ops")
        os.makedirs(ops_dir, exist_ok=True)
        with open(os.path.join(ops_dir, "entity-registry.json"), "w", encoding="utf-8") as f:
            f.write('{"entities": {'
                   '"borealis": {"name": "Borealis Inc", "type": "organization", '
                   '"aliases": ["Borealis Corp", "BorealisCo"]}, '
                   '"contoso": {"name": "Contoso Ltd", "type": "organization", "aliases": []}'
                   '}}')
        self.entity_registry_path = os.path.join(ops_dir, "entity-registry.json")


@pytest.fixture(scope="module")
def entity_first_indexed(tmp_path_factory):
    fx = _EntityFirstFixture(str(tmp_path_factory.mktemp("entity-first")))
    conn = connect_or_skip()
    build.rebuild(conn, fx.repo, build_embedder("fake"))
    yield conn, fx
    conn.close()


def _brain(conn, fx) -> AnswerBrain:
    settings = Settings(llm="fake",
                        entity_registry_path=fx.entity_registry_path)
    service = BrainService(settings, conn, build_embedder("fake"), audiences=None)
    return AnswerBrain(service)


def test_without_a_registered_alias_both_similar_pages_can_surface(entity_first_indexed):
    """The baseline this test's OTHER half proves a difference against: a query with no entity
    name at all is a bare semantic search over both near-identical pages."""
    conn, fx = entity_first_indexed
    brain = _brain(conn, fx)
    listing = brain.search_text("quarterly update renewal terms uplift")
    assert BOREALIS_PAGE in listing and CONTOSO_PAGE in listing


def test_a_question_naming_the_alias_resolves_to_its_entity_before_searching(entity_first_indexed):
    """`ask` resolves a registry ALIAS ("BorealisCo", never registered as Borealis's canonical
    title or id) to its entity, demonstrated on a question that names the alias rather than the
    canonical title — and the resolved entity's own material comes FIRST in what the agent reads.

    OLD BEHAVIOUR: `CONTOSO_PAGE not in listing`, because resolution scoped the search
    and filtered everything else away. That filter is what made a company-wide page unreachable
    for any question naming a registered company, so resolution feeds the rank-time boost now:
    the alias still resolves, its entity still leads, and nothing has been removed to achieve it.
    """
    conn, fx = entity_first_indexed
    brain = _brain(conn, fx)
    listing = brain.search_text("what's new for BorealisCo this quarter — renewal terms uplift?")
    assert BOREALIS_PAGE in listing
    assert listing.index(BOREALIS_PAGE) < listing.index(CONTOSO_PAGE)


def test_the_canonical_name_also_resolves_the_same_way(entity_first_indexed):
    """The registry's own canonical `name` field is itself a valid alias (`load_aliases` indexes
    id, name AND every declared alias) — "Borealis Inc" resolves exactly like "BorealisCo" does,
    and ranks its entity first the same way (it used to FILTER the same way)."""
    conn, fx = entity_first_indexed
    brain = _brain(conn, fx)
    listing = brain.search_text("Borealis Inc renewal terms uplift this quarter")
    assert BOREALIS_PAGE in listing
    assert listing.index(BOREALIS_PAGE) < listing.index(CONTOSO_PAGE)


def test_a_resolved_entity_never_costs_the_agent_a_page(entity_first_indexed):
    """OLD BEHAVIOUR: this pinned the fallback that ran when a scoped search came back
    empty. There is no scoping and so no fallback now; the property it was protecting — resolving
    an entity must never SHOW LESS — is stated directly, and more strongly, as a superset."""
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
    """Byte-for-byte the ordinary unscoped search when no alias matches — entity-first resolution
    is additive, never a behavior change for a query that names nothing registered."""
    conn, fx = entity_first_indexed
    brain = _brain(conn, fx)
    listing = brain.search_text("quarterly update renewal terms uplift")
    assert BOREALIS_PAGE in listing and CONTOSO_PAGE in listing
