"""Entity-aware ranking through the service and MCP surfaces."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.index.support import write_controls
from tests.server.conftest import call_json, connect_or_skip, mcp_session, write_page

BOREALIS_PAGE = "wiki/notes/borealis-quarterly-update.md"
CONTOSO_PAGE = "wiki/notes/contoso-quarterly-update.md"
PIPELINE_PAGE = "wiki/notes/reporting-extraction-pipeline.md"
BOREALIS_ID = "ent_00000000-0000-4000-8000-000000000001"
CONTOSO_ID = "ent_00000000-0000-4000-8000-000000000002"
ZEPHYR_ID = "ent_00000000-0000-4000-8000-000000000003"
PIPELINE_BODY = ("The reporting extraction pipeline demo runs the parser over each quarterly "
                "update before rollout, so extraction defects surface in staging rather than in "
                "a customer's own report.")
SHARED_BODY = ("Quarterly update covering the renewal terms discussed with the account team "
              "this cycle, including the proposed uplift.")


def _record(entity_id: str, name: str, aliases=()):
    values = [(name, "preferred"), *((alias, "alias") for alias in aliases)]
    claims = [
        {
            "claim_id": f"{entity_id}-{index}",
            "value": value,
            "normalized": value.casefold(),
            "kind": kind,
            "acl": None,
            "source": "sources/2026/08/capture.md",
            "actor": "steward@example.com",
            "introduced_at": "2026-08-24T00:00:00Z",
        }
        for index, (value, kind) in enumerate(values)
    ]
    return {
        "entity_type": "organization",
        "created_at": "2026-08-24T00:00:00Z",
        "updated_at": "2026-08-24T00:00:00Z",
        "claims": claims,
        "external_ids": [],
        "absorbed_ids": [],
    }


class _ServiceEntityFirstFixture:
    STEWARD = "steward@example.com"

    def __init__(self, root: str):
        self.repo = os.path.join(root, "repo")
        self.identities_path = os.path.join(self.repo, "ops", "identities.json")
        write_page(self.repo, BOREALIS_PAGE,
                  {"type": "report", "title": "Borealis Quarterly Update", "entity": BOREALIS_ID,
                   "as_of": "2026-Q2", "verification": "verified"}, SHARED_BODY)
        write_page(self.repo, CONTOSO_PAGE,
                  {"type": "report", "title": "Contoso Quarterly Update", "entity": CONTOSO_ID,
                   "as_of": "2026-Q2", "verification": "verified"}, SHARED_BODY)
        write_page(self.repo, PIPELINE_PAGE,
                  {"type": "decision", "title": "Reporting Extraction Pipeline Demo",
                   "entity": [], "as_of": "2026-Q2", "verification": "verified"}, PIPELINE_BODY)
        ops_dir = os.path.join(self.repo, "ops")
        os.makedirs(ops_dir, exist_ok=True)
        self.entity_registry_path = os.path.join(ops_dir, "entity-registry.json")
        with open(self.entity_registry_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entities": {
                BOREALIS_ID: _record(BOREALIS_ID, "Borealis Inc", ["BorealisCo"]),
                CONTOSO_ID: _record(CONTOSO_ID, "Contoso Ltd"),
                ZEPHYR_ID: _record(ZEPHYR_ID, "Zephyr Systems", ["ZephyrCo"]),
            }, "redirects": {}}, f)
        os.makedirs(os.path.dirname(self.identities_path), exist_ok=True)
        with open(self.identities_path, "w", encoding="utf-8") as f:
            json.dump({self.STEWARD: {
                "display_name": "Steward",
                "groups": ["brain-admins"],
                "default_audience": None,
            }}, f)
        write_controls(Path(self.repo))


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


def test_brain_service_search_resolves_a_registered_alias_and_boosts_it(
        service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)
    out = svc.search("what's new for BorealisCo this quarter — renewal terms uplift?")
    paths = [h["path"] for h in out["hits"]]
    assert BOREALIS_PAGE in paths
    assert paths.index(BOREALIS_PAGE) == 0, "the resolved alias's own entity still ranks first"
    borealis = next(h for h in out["hits"] if h["path"] == BOREALIS_PAGE)
    assert any(f"entity:{BOREALIS_ID}" in f for f in borealis["factors"])


def test_brain_service_search_an_explicit_entity_filter_is_never_overridden(
        service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    out = _service(conn, fx).search("BorealisCo renewal terms", filters={"entity": CONTOSO_ID})
    assert all(CONTOSO_ID in h["entity"] for h in out["hits"])


def test_a_resolved_entity_never_narrows_what_can_be_found(service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)
    query = "quarterly update renewal terms uplift"

    plain = [h["path"] for h in svc.search(query, max_results=10)["hits"]]
    named = [h["path"] for h in svc.search(f"BorealisCo {query}", max_results=10)["hits"]]

    assert set(plain) <= set(named)


def test_a_registered_entity_anchoring_no_page_still_returns_the_ordinary_results(
        service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)
    out = svc.search("ZephyrCo — anything at all about the quarterly update renewal terms uplift?")
    paths = [h["path"] for h in out["hits"]]
    assert BOREALIS_PAGE in paths and CONTOSO_PAGE in paths


def test_brain_service_search_no_registered_name_is_the_ordinary_unscoped_search(
        service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    out = _service(conn, fx).search("quarterly update renewal terms uplift")
    paths = [h["path"] for h in out["hits"]]
    assert BOREALIS_PAGE in paths and CONTOSO_PAGE in paths


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
    assert paths.index(BOREALIS_PAGE) == 0
    assert CONTOSO_PAGE in paths


def test_answer_brain_no_longer_carries_its_own_entity_first_wrapper():
    from stigmergy.answer.brain import AnswerBrain
    assert not hasattr(AnswerBrain, "_search_entity_first"), (
        "entity-first resolution lives in BrainService._search — "
        "AnswerBrain's own wrapper must stay deleted, not come back dead beside it")


def test_the_served_hit_carries_the_told_entity_factor(service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    scoped = _service(conn, fx).search("BorealisCo renewal terms uplift")
    hit = next(h for h in scoped["hits"] if h["path"] == BOREALIS_PAGE)
    assert f"entity:{BOREALIS_ID}" in hit["factors"]


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
    conn, fx = service_entity_first_indexed
    out = _service(conn, fx).search("Borealis Inc quarterly update renewal terms", max_results=10)

    paths = [hit["path"] for hit in out["hits"]]
    assert BOREALIS_PAGE in paths and CONTOSO_PAGE in paths
    assert paths.index(BOREALIS_PAGE) < paths.index(CONTOSO_PAGE)
    borealis = next(h for h in out["hits"] if h["path"] == BOREALIS_PAGE)
    assert any(f"entity:{BOREALIS_ID}" in f for f in borealis["factors"])


def test_expansion_terms_are_bounded_by_count_and_length(service_entity_first_indexed):
    from stigmergy.server import service as service_module
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)
    record = _record(
        BOREALIS_ID,
        "Borealis Logistics",
        [f"alias-{i}" for i in range(10_000)] + ["x" * 500],
    )

    svc._registry_memo = (
        json.dumps({"version": 1, "entities": {BOREALIS_ID: record}, "redirects": {}}),
        "test",
    )

    terms = svc._expansion_terms(BOREALIS_ID)

    assert len(terms) == service_module.MAX_EXPANSION_TERMS
    assert terms[0] == "Borealis Logistics", "the display name expands first, in registry order"
    assert all(len(t) <= service_module.MAX_EXPANSION_TERM_CHARS for t in terms)


def test_an_ordinary_record_expands_every_spelling_it_has(service_entity_first_indexed):
    conn, fx = service_entity_first_indexed
    svc = _service(conn, fx)

    assert svc._expansion_terms(BOREALIS_ID) == ("Borealis Inc", "BorealisCo")
