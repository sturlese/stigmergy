"""Entity-first resolution AT the service layer: `BrainService._search` does what
`answer/brain.py::_search_entity_first` used to do alone — every client gets it, not only `ask`.
Witnessed twice: directly against `BrainService.search` (pg), and once more through the real
`search_brain` MCP surface (MCP harness).

Its own small corpus + registry + identities, isolated from `tests/server/conftest.py`'s shared
`Fixture` — same reason `tests/answer/test_entity_first_pg.py` already gives for its own.
"""
import asyncio
import json
import os

import pytest

from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.server.conftest import call_json, connect_or_skip, mcp_session, write_page

BOREALIS_PAGE = "wiki/entities/borealis/quarterly-update.md"
CONTOSO_PAGE = "wiki/entities/contoso/quarterly-update.md"
# company-wide (`entity: []`), never anchored, and the BEST answer to a question that
# happens to name a registered entity. Under scope-first-then-fallback it was structurally
# unreachable: the scoped pass returned hits, so the unscoped pass never ran, and this page could
# not appear for any query naming a company — which is most real questions.
PIPELINE_PAGE = "wiki/decisions/reporting-extraction-pipeline.md"
PIPELINE_BODY = ("The reporting extraction pipeline demo runs the parser over each quarterly "
                "update before rollout, so extraction defects surface in staging rather than in "
                "a customer's own report.")
SHARED_BODY = ("Quarterly update covering the renewal terms discussed with the account team "
              "this cycle, including the proposed uplift.")


class _ServiceEntityFirstFixture:
    STEWARD = "steward@example.com"

    def __init__(self, root: str):
        self.repo = os.path.join(root, "repo")
        self.identities_path = os.path.join(self.repo, "ops", "identities.json")
        write_page(self.repo, BOREALIS_PAGE,
                  {"type": "report", "title": "Borealis Quarterly Update", "entity": "borealis",
                   "as_of": "2026-Q2", "verification": "verified"}, SHARED_BODY)
        write_page(self.repo, CONTOSO_PAGE,
                  {"type": "report", "title": "Contoso Quarterly Update", "entity": "contoso",
                   "as_of": "2026-Q2", "verification": "verified"}, SHARED_BODY)
        write_page(self.repo, PIPELINE_PAGE,
                  {"type": "decision", "title": "Reporting Extraction Pipeline Demo",
                   "entity": [], "as_of": "2026-Q2", "verification": "verified"}, PIPELINE_BODY)
        ops_dir = os.path.join(self.repo, "ops")
        os.makedirs(ops_dir, exist_ok=True)
        self.entity_registry_path = os.path.join(ops_dir, "entity-registry.json")
        with open(self.entity_registry_path, "w", encoding="utf-8") as f:
            json.dump({"entities": {
                "borealis": {"name": "Borealis Inc", "type": "organization",
                            "aliases": ["BorealisCo"]},
                "contoso": {"name": "Contoso Ltd", "type": "organization", "aliases": []},
                # registered, but anchored to NO page at all — an entity-scoped search
                # filtered to this id is STRUCTURALLY guaranteed zero hits
                # (not merely "the query terms happen not to match"), so a query naming it forces
                # the fallback-on-zero branch to run provably, every time, regardless of ranking.
                "zephyr": {"name": "Zephyr Systems", "type": "organization",
                          "aliases": ["ZephyrCo"]},
            }}, f)
        os.makedirs(os.path.dirname(self.identities_path), exist_ok=True)
        with open(self.identities_path, "w", encoding="utf-8") as f:
            json.dump({self.STEWARD: "*"}, f)


@pytest.fixture(scope="module")
def service_entity_first_indexed(tmp_path_factory):
    fx = _ServiceEntityFirstFixture(str(tmp_path_factory.mktemp("service-entity-first")))
    conn = connect_or_skip()
    build.rebuild(conn, fx.repo, build_embedder("fake"))
    yield conn, fx
    conn.close()


def _service(conn, fx) -> BrainService:
    settings = Settings(identity=fx.STEWARD, identities_path=fx.identities_path,
                        entity_registry_path=fx.entity_registry_path)
    return BrainService(settings, conn, build_embedder("fake"), audiences=None, identity=fx.STEWARD)


# ── first witness: directly against BrainService.search ────────────────────────────────────────
def test_brain_service_search_resolves_a_registered_alias_and_boosts_it(
        service_entity_first_indexed):
    """OLD BEHAVIOUR: resolution SCOPED the search — `CONTOSO_PAGE` was absent because
    a filter removed it, not because it ranked lower. That filter is what made a company-wide page
    unreachable for any query naming a registered company, so resolution now feeds the rank-time
    boost instead: the alias still resolves, its entity still comes first, and everything else is
    still THERE to be ranked."""
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)
    out = svc.search("what's new for BorealisCo this quarter — renewal terms uplift?")
    paths = [h["path"] for h in out["hits"]]
    assert BOREALIS_PAGE in paths
    assert paths.index(BOREALIS_PAGE) == 0, "the resolved alias's own entity still ranks first"
    borealis = next(h for h in out["hits"] if h["path"] == BOREALIS_PAGE)
    assert any("entity:borealis" in f for f in borealis["factors"])


def test_brain_service_search_an_explicit_entity_filter_is_never_overridden(
        service_entity_first_indexed):
    """Entity-first resolution applies only when the caller passed NO explicit entity filter: a
    caller who already named `contoso` explicitly must not have it silently replaced by a name the
    query text happens to also mention."""
    conn, fx = service_entity_first_indexed
    out = _service(conn, fx).search("BorealisCo renewal terms", filters={"entity": "contoso"})
    assert all("contoso" in h["entity"] for h in out["hits"])


def test_a_resolved_entity_never_narrows_what_can_be_found(service_entity_first_indexed):
    """OLD BEHAVIOUR: this pinned a FALLBACK — the unscoped search ran only when the
    scoped one returned zero hits. There is no fallback now because there is no scoping: one
    blended search, with the resolved id told to the ranker. The property that replaces it is
    stronger and is what the old branch was groping towards — resolving an entity can only ever
    change the ORDER of the results, never their membership."""
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)
    query = "quarterly update renewal terms uplift"

    plain = [h["path"] for h in svc.search(query, max_results=10)["hits"]]
    named = [h["path"] for h in svc.search(f"BorealisCo {query}", max_results=10)["hits"]]

    assert set(plain) <= set(named), (
        "naming a registered entity removed pages the same query found without it — that is the "
        "eclipse this issue exists to close")


# ── a registered entity that anchors nothing: the case the fallback used to exist for ─────────
def test_a_registered_entity_anchoring_no_page_still_returns_the_ordinary_results(
        service_entity_first_indexed):
    """`zephyr` is registered (its alias "ZephyrCo" resolves) but anchored to NO page at all.
    Under the old scope-then-fallback this was the branch that PROVED the fallback ran; under
    blending it proves something better — a resolution that matches nothing costs nothing at all,
    because there was never a filter to recover from. Kept, renamed, and still the structural
    case: the scoped search it used to run was unwinnable by construction."""
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)
    # sanity: zephyr really is registered and really anchors no page — the scoped search this
    # query resolves to is unwinnable by construction, every time, regardless of ranking.
    assert "zephyr" not in svc.scoped_entities()

    out = svc.search("ZephyrCo — anything at all about the quarterly update renewal terms uplift?")
    paths = [h["path"] for h in out["hits"]]
    # the unscoped fallback's own hits: both shared-body pages, neither anchored to zephyr at all
    assert BOREALIS_PAGE in paths and CONTOSO_PAGE in paths


def test_brain_service_search_no_registered_name_is_the_ordinary_unscoped_search(
        service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    out = _service(conn, fx).search("quarterly update renewal terms uplift")
    paths = [h["path"] for h in out["hits"]]
    assert BOREALIS_PAGE in paths and CONTOSO_PAGE in paths


# ── second witness: through the real search_brain MCP surface ──────────────────────────────────
def test_search_brain_mcp_surface_sees_the_resolved_entity_ranked_first(
        service_entity_first_indexed):
    _, fx = service_entity_first_indexed

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            return await call_json(session, "search_brain",
                                   query="what's new for BorealisCo this quarter — "
                                        "renewal terms uplift?")

    out = asyncio.run(go())
    paths = [h["path"] for h in out["hits"]]
    # OLD BEHAVIOUR: `CONTOSO_PAGE not in paths` — the scoped pass had filtered it
    # away. The second witness now shows what the first one does: the resolved entity ranks
    # first, over the real MCP protocol, and nothing has been removed to achieve it.
    assert paths.index(BOREALIS_PAGE) == 0
    assert CONTOSO_PAGE in paths


# ── the wrapper really is deleted, not merely unused (positive assertion) ───────────────────────
def test_answer_brain_no_longer_carries_its_own_entity_first_wrapper():
    from stigmergy.answer.brain import AnswerBrain
    assert not hasattr(AnswerBrain, "_search_entity_first"), (
        "entity-first resolution lives in BrainService._search — "
        "AnswerBrain's own wrapper must stay deleted, not come back dead beside it")


# ── TOLD, not inferred: the served hit's factors say what the service resolved ─────────────────
def test_the_served_hit_carries_the_told_entity_factor(service_entity_first_indexed):
    """The id the service resolved from the registry travels down to ranking as `entity_hint`
    and the served hit's own `factors` names it — ranking stays answerable, end to end.
    Token inference is gone (`rank.contract_factors`), so this label can ONLY have come from
    the service's resolution."""
    conn, fx = service_entity_first_indexed
    scoped = _service(conn, fx).search("BorealisCo renewal terms uplift")
    hit = next(h for h in scoped["hits"] if h["path"] == BOREALIS_PAGE)
    assert "entity:borealis" in hit["factors"]


# ── entity-first LAYERS on the ranking; it does not replace it ──────────────────────────────────
# Observed on staging: `demo pipeline extraction Globex` returned ONLY the three pages anchored to
# `globex`, each with `factors: ["entity:globex"]`, while the page actually about the asked topic
# — unanchored — never appeared. Raw hybrid search over the same DSN ranked it #2. The substrate
# was healthy; the scoped pass simply returned hits, so the unscoped pass never ran.
#
# Why this is structural rather than a ranking nit: a page that is genuinely company-wide (a
# policy, a process, a cross-cutting decision) is correctly `entity: []`, and under eclipse
# semantics it becomes unreachable through EVERY query that mentions a registered company. The
# golden retrieval suite cannot catch it — that corpus is fully anchored.
def test_an_unanchored_page_still_surfaces_for_a_query_naming_a_registered_entity(
        service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    out = _service(conn, fx).search("Borealis reporting extraction pipeline demo", max_results=10)

    paths = [hit["path"] for hit in out["hits"]]
    assert PIPELINE_PAGE in paths, (
        "the topically-best page is company-wide, so an entity-scoped pass cannot return it — "
        "naming a registered company must not make it unreachable")
    assert BOREALIS_PAGE in paths, "the entity's own material must still be there too"


def test_the_entity_named_by_the_query_still_outranks_its_sibling(service_entity_first_indexed):
    """The benign twin, and the property blending must NOT lose: two pages with byte-identical
    bodies, one anchored to the named entity and one to another company. The TOLD boost is what
    keeps the named one first — the pre-filter was never the only thing doing that work."""
    conn, fx = service_entity_first_indexed
    out = _service(conn, fx).search("Borealis quarterly update renewal terms", max_results=10)

    paths = [hit["path"] for hit in out["hits"]]
    assert BOREALIS_PAGE in paths and CONTOSO_PAGE in paths
    assert paths.index(BOREALIS_PAGE) < paths.index(CONTOSO_PAGE)
    borealis = next(h for h in out["hits"] if h["path"] == BOREALIS_PAGE)
    assert any("entity:borealis" in f for f in borealis["factors"]), (
        "the boost is TOLD by the resolved id, and it is what ranks the named entity first now "
        "that nothing filters the others out")
