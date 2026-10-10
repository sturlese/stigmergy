"""Entity-aware ranking through the service and MCP surfaces: a query naming a visible entity
page (by title or alias) boosts that page and the pages linking it, without narrowing recall."""
import asyncio
import json
import os

import pytest

from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.identity import resolve_audiences
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.server.conftest import call_json, connect_or_skip, mcp_session, write_page

BOREALIS_PAGE = "wiki/notes/borealis-quarterly-update.md"
CONTOSO_PAGE = "wiki/notes/contoso-quarterly-update.md"
PIPELINE_PAGE = "wiki/notes/reporting-extraction-pipeline.md"
BOREALIS = "wiki/entities/Borealis Inc.md"
CONTOSO = "wiki/entities/Contoso Ltd.md"
ZEPHYR = "wiki/entities/Zephyr Systems.md"
HELIX = "wiki/entities/Helix Labs.md"          # finance-scoped: its alias is invisible to eng
PIPELINE_BODY = ("The reporting extraction pipeline demo runs the parser over each quarterly "
                "update before rollout, so extraction defects surface in staging rather than in "
                "a customer's own report.")
SHARED_BODY = ("Quarterly update covering the renewal terms discussed with the account team "
              "this cycle, including the proposed uplift.")


class _ServiceEntityFirstFixture:
    STEWARD = "steward@example.com"
    ENG = "eng@example.com"

    def __init__(self, root: str):
        self.repo = os.path.join(root, "repo")
        self.identities_path = os.path.join(self.repo, "ops", "identities.json")
        write_page(self.repo, BOREALIS,
                   {"title": "Borealis Inc", "aliases": ["BorealisCo"]},
                   "A logistics customer.")
        write_page(self.repo, CONTOSO, {"title": "Contoso Ltd"}, "A retail customer.")
        write_page(self.repo, ZEPHYR,
                   {"title": "Zephyr Systems", "aliases": ["ZephyrCo"]},
                   "A prospect with no filed material yet.")
        write_page(self.repo, HELIX,
                   {"title": "Helix Labs", "aliases": ["HelixCo"], "acl": ["finance"]},
                   "A finance-only counterparty.")
        write_page(self.repo, BOREALIS_PAGE,
                  {"title": "Borealis Quarterly Update"},
                  f"{SHARED_BODY} Prepared for [[Borealis Inc]].")
        write_page(self.repo, CONTOSO_PAGE,
                  {"title": "Contoso Quarterly Update"},
                  f"{SHARED_BODY} Prepared for [[Contoso Ltd]].")
        write_page(self.repo, PIPELINE_PAGE,
                  {"title": "Reporting Extraction Pipeline Demo"}, PIPELINE_BODY)
        os.makedirs(os.path.dirname(self.identities_path), exist_ok=True)
        with open(self.identities_path, "w", encoding="utf-8") as f:
            json.dump({
                self.STEWARD: {
                    "display_name": "Steward",
                    "groups": ["brain-admins"],
                    "default_audience": ["public"],
                },
                self.ENG: {
                    "display_name": "Engineer",
                    "groups": ["eng"],
                    "default_audience": ["eng"],
                },
            }, f)


@pytest.fixture(scope="module")
def service_entity_first_indexed(tmp_path_factory):
    fx = _ServiceEntityFirstFixture(str(tmp_path_factory.mktemp("service-entity-first")))
    conn = connect_or_skip()
    build.rebuild(conn, fx.repo, build_embedder("fake"))
    yield conn, fx
    conn.close()


def _service(conn, fx, subject=None) -> BrainService:
    subject = subject or fx.STEWARD
    audiences_value = resolve_audiences(fx.identities_path, subject)
    audiences = set(audiences_value) if audiences_value is not None else None
    settings = Settings(identity=subject)
    return BrainService(settings, conn, build_embedder("fake"), audiences=audiences,
                        identity=subject)


def test_brain_service_search_resolves_an_alias_and_boosts_the_pages_linking_it(
        service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)
    out = svc.search("what's new for BorealisCo this quarter — renewal terms uplift?")
    paths = [h["path"] for h in out["hits"]]
    assert BOREALIS_PAGE in paths and CONTOSO_PAGE in paths
    assert paths.index(BOREALIS_PAGE) < paths.index(CONTOSO_PAGE), (
        "the resolved alias's own material ranks above its sibling")
    borealis = next(h for h in out["hits"] if h["path"] == BOREALIS_PAGE)
    assert f"entity:{BOREALIS}" in borealis["factors"]
    contoso = next(h for h in out["hits"] if h["path"] == CONTOSO_PAGE)
    assert not any(f.startswith("entity:") for f in contoso["factors"])


def test_brain_service_search_an_explicit_entity_filter_is_never_overridden(
        service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    out = _service(conn, fx).search("BorealisCo renewal terms", filters={"entity": CONTOSO})
    paths = {h["path"] for h in out["hits"]}
    assert paths and paths <= {CONTOSO, CONTOSO_PAGE}
    assert BOREALIS_PAGE not in paths


def test_a_resolved_entity_never_narrows_what_can_be_found(service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)
    query = "quarterly update renewal terms uplift"

    plain = [h["path"] for h in svc.search(query, max_results=10)["hits"]]
    named = [h["path"] for h in svc.search(f"BorealisCo {query}", max_results=10)["hits"]]

    assert set(plain) <= set(named)


def test_an_entity_page_nothing_links_still_returns_the_ordinary_results(
        service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)
    out = svc.search("ZephyrCo — anything at all about the quarterly update renewal terms uplift?")
    paths = [h["path"] for h in out["hits"]]
    assert BOREALIS_PAGE in paths and CONTOSO_PAGE in paths


def test_brain_service_search_no_known_name_is_the_ordinary_unscoped_search(
        service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    out = _service(conn, fx).search("quarterly update renewal terms uplift")
    paths = [h["path"] for h in out["hits"]]
    assert BOREALIS_PAGE in paths and CONTOSO_PAGE in paths
    assert not any(f.startswith("entity:") for h in out["hits"] for f in h["factors"])


def test_search_brain_mcp_surface_sees_the_resolved_entity_ranked_above_its_sibling(
        service_entity_first_indexed):
    _, fx = service_entity_first_indexed

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            return await call_json(session, "search_brain",
                                   query="what's new for BorealisCo this quarter — "
                                        "renewal terms uplift?")

    out = asyncio.run(go())
    paths = [h["path"] for h in out["hits"]]
    assert BOREALIS_PAGE in paths and CONTOSO_PAGE in paths
    assert paths.index(BOREALIS_PAGE) < paths.index(CONTOSO_PAGE)


def test_answer_brain_no_longer_carries_its_own_entity_first_wrapper():
    from stigmergy.answer.brain import AnswerBrain
    assert not hasattr(AnswerBrain, "_search_entity_first"), (
        "entity-first resolution lives in BrainService._search — "
        "AnswerBrain's own wrapper must stay deleted, not come back dead beside it")


def test_the_served_hit_carries_the_told_entity_factor(service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    scoped = _service(conn, fx).search("BorealisCo renewal terms uplift")
    hit = next(h for h in scoped["hits"] if h["path"] == BOREALIS_PAGE)
    assert f"entity:{BOREALIS}" in hit["factors"]


def test_an_unlinked_page_still_surfaces_for_a_query_naming_a_known_entity(
        service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    out = _service(conn, fx).search("Borealis reporting extraction pipeline demo", max_results=10)

    paths = [hit["path"] for hit in out["hits"]]
    assert PIPELINE_PAGE in paths, (
        "the topically-best page is company-wide, so an entity-scoped pass cannot return it — "
        "naming a known company must not make it unreachable")
    assert BOREALIS_PAGE in paths, "the entity's own material must still be there too"


def test_the_entity_named_by_the_query_still_outranks_its_sibling(service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    out = _service(conn, fx).search("Borealis Inc quarterly update renewal terms", max_results=10)

    paths = [hit["path"] for hit in out["hits"]]
    assert BOREALIS_PAGE in paths and CONTOSO_PAGE in paths
    assert paths.index(BOREALIS_PAGE) < paths.index(CONTOSO_PAGE)
    borealis = next(h for h in out["hits"] if h["path"] == BOREALIS_PAGE)
    assert f"entity:{BOREALIS}" in borealis["factors"]


def test_a_hidden_entity_pages_alias_is_not_resolved_for_a_scoped_reader(
        service_entity_first_indexed):
    """Resolution reads the VISIBLE entity pages: a finance-scoped page's alias must not shape an
    eng reader's ranking (nor confirm, through a factor, that the page exists)."""
    conn, fx = service_entity_first_indexed
    query = "HelixCo quarterly update renewal terms"

    steward = _service(conn, fx).search(query, max_results=10)["hits"]
    eng = _service(conn, fx, fx.ENG).search(query, max_results=10)["hits"]

    assert any(f"entity:{HELIX}" in h["factors"] for h in steward)
    assert HELIX not in {h["path"] for h in eng}
    assert not any(f.startswith("entity:") for h in eng for f in h["factors"])


def test_expansion_terms_are_bounded_by_count_and_length(service_entity_first_indexed):
    from stigmergy.server import service as service_module
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)
    svc._entities_memo = [{
        "path": BOREALIS,
        "title": "Borealis Logistics",
        "aliases": [f"alias-{i}" for i in range(10_000)] + ["x" * 500],
        "acl": None,
    }]

    terms = svc._expansion_terms((BOREALIS,))

    assert len(terms) == service_module.MAX_EXPANSION_TERMS
    assert terms[0] == "Borealis Logistics", "the title expands first, then the aliases in page order"
    assert all(len(t) <= service_module.MAX_EXPANSION_TERM_CHARS for t in terms)


def test_an_ordinary_entity_page_expands_every_spelling_it_has(service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)

    assert svc._expansion_terms((BOREALIS,)) == ("Borealis Inc", "BorealisCo")
    assert svc._expansion_terms(("wiki/entities/Nobody.md",)) == ()
