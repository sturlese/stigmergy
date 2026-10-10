"""Graph-aware search against the real index (fake embedder): catalog lines on every hit, and the
ACL invariance every graph feature must keep — a scoped reader's results are identical whether or
not pages it cannot see exist in the index."""
import os
import shutil

import pytest

from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.service import HIT_LINK_CAP, BrainService
from stigmergy.server.settings import Settings
from tests.server.conftest import connect_or_skip, write_page

SCOPED = {"engineering"}
FINANCE = ["finance"]
TESSERA = "wiki/entities/Tessera.md"
HARBOR = "wiki/entities/Harbor Fund.md"
QUILL = "wiki/entities/Quillstack.md"
VELTRIX = "wiki/entities/Veltrix.md"
RENEWAL = "wiki/notes/Tessera renewal.md"
HUB = "wiki/notes/Supplier roundup.md"
BOARD = "wiki/notes/Board terms.md"
PILOT = "wiki/notes/Quillstack pilot.md"
PRICING = "wiki/concepts/Per-seat pricing.md"
PLAN = "wiki/notes/Quarter plan.md"
TIE_SEED = "wiki/notes/Tie seed.md"
ECHO = "wiki/notes/Tie echo.md"


def _write_corpus(repo: str, *, with_restricted: bool) -> None:
    write_page(repo, TESSERA, {"title": "Tessera", "aliases": ["Tessera Learning"]},
               "# Tessera\n\n## What / Who\nTessera is a learning platform for nurses.\n")
    write_page(repo, HARBOR, {"title": "Harbor Fund", "aliases": ["Harbor"]},
               "# Harbor Fund\n\n## What / Who\nHarbor Fund is a seed investor in [[Tessera]].\n")
    write_page(repo, QUILL, {"title": "Quillstack", "entity_type": "product"},
               "# Quillstack\n\n## What / Who\nQuillstack is the billing tool [[Tessera]] uses.\n")
    write_page(repo, RENEWAL, {"title": "Tessera renewal", "updated": "2026-04-09"},
               "# Tessera renewal\n\n[[Tessera]] renewed for two years with [[Harbor Fund]] "
               "backing the expansion. (Source: `sources/2026/04/x.md`)\n\n"
               "Billing moves to [[Quillstack]].\n")
    write_page(repo, PILOT, {"title": "Quillstack pilot", "updated": "2026-03-01"},
               "# Quillstack pilot\n\nThe [[Quillstack]] pilot cut invoice errors by half for "
               "[[Tessera]].\n")
    write_page(repo, PRICING, {"title": "Per-seat pricing"},
               "# Per-seat pricing\n\n## Definition\nPer-seat pricing charges for every learner "
               "account, which [[Tessera]] adopted.\n")
    hub_links = " ".join(f"[[Open {n}]]" for n in range(9))
    write_page(repo, HUB, {"title": "Supplier roundup"},
               f"# Supplier roundup\n\nA roundup of every supplier page: {hub_links}.\n")
    for n in range(9):
        write_page(repo, f"wiki/notes/Open {n}.md", {"title": f"Open {n}"},
                   f"# Open {n}\n\nOpen supplier {n} ships probes to [[Tessera]].\n")
    write_page(repo, PLAN, {"title": "Quarter plan"},
               "# Quarter plan\n\nThe plan reviews the [[Supplier roundup]] and [[Per-seat pricing]].\n")
    write_page(repo, TIE_SEED, {"title": "Tie seed"},
               "# Tie seed\n\nCompares [[Tie older]] with [[Tie newer]].\n")
    write_page(repo, "wiki/notes/Tie older.md", {"title": "Tie older", "updated": "2026-01-01"},
               "# Tie older\n\nAn older comparison page.\n")
    write_page(repo, "wiki/notes/Tie newer.md", {"title": "Tie newer", "updated": "2026-02-01"},
               "# Tie newer\n\nA newer comparison page.\n")
    write_page(repo, ECHO, {"title": "Tie echo"}, "# Tie echo\n\nRevisits [[Tie older]].\n")
    if with_restricted:
        write_page(repo, VELTRIX, {"title": "Veltrix", "acl": FINANCE},
                   "# Veltrix\n\n## What / Who\nVeltrix is an acquirer in talks with [[Tessera]].\n")
        write_page(repo, BOARD, {"title": "Board terms", "acl": FINANCE, "updated": "2026-05-06"},
                   "# Board terms\n\n[[Harbor Fund]] and [[Veltrix]] discussed [[Tessera]] "
                   "valuation and renewal terms with [[Quillstack]] billing data.\n")
        for n in range(3):
            write_page(repo, f"wiki/notes/Archived {n}.md", {"title": f"Archived {n}", "acl": FINANCE},
                       f"# Archived {n}\n\nArchived supplier {n} and [[Tessera]] pricing.\n")
        # The finance layer's parts of two open pages: their links exist only for finance readers.
        write_page(repo, RENEWAL, {"id": "page_renewal_finance", "title": "Tessera renewal",
                                   "acl": FINANCE, "updated": "2026-04-09"},
                   "# Tessera renewal\n\nThe contract links [[Veltrix]].\n")
        archived = " ".join(f"[[Archived {n}]]" for n in range(3))
        write_page(repo, HUB, {"id": "page_hub_finance", "title": "Supplier roundup", "acl": FINANCE},
                   f"# Supplier roundup\n\nFinance also tracks {archived}.\n")


QUERIES = (
    "Tessera renewal terms",
    "Harbor Fund and Tessera",
    "Quillstack billing for Tessera",
    "supplier roundup probes",
    "per-seat pricing learners",
    "Veltrix acquisition talks",
)


def _service(conn, audiences):
    return BrainService(Settings(llm="fake"), conn, build_embedder("fake"), audiences=audiences)


def _results(conn, audiences) -> list[dict]:
    out = []
    for query in QUERIES:
        result = _service(conn, audiences)._search(query, None, 32)
        out.append({"query": query, "hits": result["hits"], "count": result["count"]})
    return out


@pytest.fixture(scope="module")
def graph_repos(tmp_path_factory):
    root = tmp_path_factory.mktemp("graph-search")
    full, public = os.path.join(root, "full"), os.path.join(root, "public")
    _write_corpus(full, with_restricted=True)
    _write_corpus(public, with_restricted=False)
    conn = connect_or_skip()
    yield conn, full, public
    conn.close()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def full_index(graph_repos):
    conn, full, _public = graph_repos
    build.rebuild(conn, full, build_embedder("fake"))
    return conn


def _hit(result: dict, path: str) -> dict:
    return next(h for h in result["hits"] if h["path"] == path)


def test_every_hit_carries_a_summary_and_its_visible_links(full_index):
    result = _service(full_index, None)._search("Tessera renewal terms", None, 32)
    renewal = _hit(result, RENEWAL)
    assert renewal["summary"] == "Tessera renewed for two years with Harbor Fund backing the expansion."
    assert [link["path"] for link in renewal["links"]] == [HARBOR, QUILL, TESSERA, VELTRIX]
    assert renewal["links"][0] == {"path": HARBOR, "title": "Harbor Fund"}


def test_a_restricted_link_target_is_absent_for_a_scoped_reader(full_index):
    result = _service(full_index, SCOPED)._search("Tessera renewal terms", None, 32)
    links = [link["path"] for link in _hit(result, RENEWAL)["links"]]
    assert links == [HARBOR, QUILL, TESSERA]
    assert all(link["title"] != "Veltrix" for hit in result["hits"] for link in hit["links"])


def test_the_link_cap_is_applied_after_visibility(full_index):
    scoped = _hit(_service(full_index, SCOPED)._search("supplier roundup probes", None, 32), HUB)
    assert [link["title"] for link in scoped["links"]] == [f"Open {n}" for n in range(HIT_LINK_CAP)]
    steward = _hit(_service(full_index, None)._search("supplier roundup probes", None, 32), HUB)
    assert [link["title"] for link in steward["links"]][:3] == [f"Archived {n}" for n in range(3)]
    assert len(steward["links"]) == HIT_LINK_CAP


def test_a_scoped_reader_cannot_tell_whether_restricted_pages_exist(graph_repos):
    conn, full, public = graph_repos
    build.rebuild(conn, full, build_embedder("fake"))
    with_restricted = _results(conn, SCOPED)
    build.rebuild(conn, public, build_embedder("fake"))
    without_restricted = _results(conn, SCOPED)
    assert with_restricted == without_restricted


def _every_read(conn, audiences) -> dict:
    """What every read surface answers one reader, including pages shared with a layer it cannot
    read."""
    service = _service(conn, audiences)
    return {
        "pages": service.browse_pages(),
        "graph": service.browse_graph(),
        "entities": service.entity_vocabulary(),
        "describe": [service.describe_entity(name) for name in ("Tessera", "Veltrix", "Harbor Fund")],
        "read": [service.read_page(path) for path in (RENEWAL, HUB, VELTRIX, BOARD)],
        "browse": [service.browse_page(path) for path in (RENEWAL, HUB, VELTRIX)],
    }


def test_no_read_surface_tells_a_scoped_reader_what_other_layers_hold(graph_repos):
    conn, full, public = graph_repos
    build.rebuild(conn, full, build_embedder("fake"))
    with_restricted = _every_read(conn, SCOPED)
    build.rebuild(conn, public, build_embedder("fake"))
    without_restricted = _every_read(conn, SCOPED)
    assert with_restricted == without_restricted


# ── several named entities ─────────────────────────────────────────────────────────────────────
def test_every_named_entity_resolves_in_order_of_mention(full_index):
    svc = _service(full_index, None)
    assert svc._entities_named_in("What did Harbor Fund and Tessera agree?") == (HARBOR, TESSERA)
    assert svc._entities_named_in("Tessera Learning and Quillstack billing") == (TESSERA, QUILL)


def test_a_longer_name_claims_its_span_before_a_shorter_one(full_index):
    svc = _service(full_index, None)
    assert svc._entities_named_in("news from Harbor Fund") == (HARBOR,)
    assert svc._entities_named_in("Tessera Learning pricing") == (TESSERA,)


def test_a_name_shared_by_two_entity_pages_resolves_to_neither(full_index):
    svc = _service(full_index, None)
    svc._entities_memo = [
        {"path": HARBOR, "title": "Harbor Fund", "aliases": ["Harbor"], "acl": None},
        {"path": "wiki/entities/Harbor Labs.md", "title": "Harbor Labs", "aliases": ["Harbor"],
         "acl": None},
        {"path": TESSERA, "title": "Tessera", "aliases": [], "acl": None},
    ]
    assert svc._entities_named_in("Harbor and Tessera") == (TESSERA,)


def test_at_most_four_entities_resolve(full_index):
    svc = _service(full_index, None)
    svc._entities_memo = [{"path": f"wiki/entities/E{n}.md", "title": f"Entity{n}", "aliases": [],
                           "acl": None} for n in range(6)]
    named = svc._entities_named_in(" ".join(f"Entity{n}" for n in range(6)))
    assert named == tuple(f"wiki/entities/E{n}.md" for n in range(4))


def test_a_restricted_entity_name_resolves_to_nothing_for_a_scoped_reader(full_index):
    assert _service(full_index, SCOPED)._entities_named_in("Veltrix and Tessera") == (TESSERA,)
    assert _service(full_index, None)._entities_named_in("Veltrix and Tessera") == (VELTRIX, TESSERA)


def test_pages_linking_every_named_entity_join_as_their_own_ranking(full_index):
    result = _service(full_index, None)._search("Harbor Fund and Quillstack", None, 32)
    hits = {h["path"]: h for h in result["hits"]}
    assert "entity" in hits[RENEWAL]["arms"] and "entity" in hits[BOARD]["arms"]
    assert "entity" not in hits[PILOT]["arms"]
    scoped = _service(full_index, SCOPED)._search("Harbor Fund and Quillstack", None, 32)
    assert BOARD not in {h["path"] for h in scoped["hits"]}


def test_the_intersection_honors_filters_and_audiences(full_index):
    from stigmergy.index import search

    both = (HARBOR, QUILL)
    assert search.intersection_ranking(full_index, both) == [BOARD, RENEWAL]
    assert search.intersection_ranking(full_index, both, audiences=SCOPED) == [RENEWAL]
    assert search.intersection_ranking(full_index, both, filters={"type": "concept"}) == []
    assert search.intersection_ranking(full_index, (HARBOR,)) == []


# ── neighbors of the best hits ─────────────────────────────────────────────────────────────────
def test_a_page_reachable_only_through_a_link_is_ranked(full_index):
    from stigmergy.index import search

    ranked = search.neighbor_ranking(full_index, [(PRICING, 1.0)])
    assert set(ranked) == {TESSERA, PLAN}, "its one outbound link and its one backlink"


def test_a_page_voted_by_several_seeds_outranks_one_voted_by_one(full_index):
    from stigmergy.index import search

    ranked = search.neighbor_ranking(full_index, [(TIE_SEED, 1.0), (ECHO, 1.0)])
    assert ranked == ["wiki/notes/Tie older.md", "wiki/notes/Tie newer.md"]


def test_neighbors_honor_filters_and_never_include_what_the_reader_cannot_see(full_index):
    from stigmergy.index import search

    seeds = [(TESSERA, 1.0)]
    steward = search.neighbor_ranking(full_index, seeds)
    scoped = search.neighbor_ranking(full_index, seeds, audiences=SCOPED)
    assert VELTRIX in steward and BOARD in steward
    assert VELTRIX not in scoped and BOARD not in scoped
    concepts = search.neighbor_ranking(full_index, seeds, filters={"type": "concept"})
    assert concepts == [PRICING]


def test_a_hub_neighbor_ranks_below_a_quiet_one_with_the_same_votes(full_index):
    from stigmergy.index import search

    assert search.neighbor_ranking(full_index, [(PLAN, 1.0)]) == [PRICING, HUB]


def test_equal_neighbors_rank_newest_first(full_index):
    from stigmergy.index import search

    ranked = search.neighbor_ranking(full_index, [(TIE_SEED, 1.0)])
    assert ranked == ["wiki/notes/Tie newer.md", "wiki/notes/Tie older.md"]


def test_each_seed_enumerates_a_bounded_number_of_neighbors(full_index, monkeypatch):
    from stigmergy.index import search

    monkeypatch.setattr(search, "NEIGHBOR_FANOUT", 1)
    assert search.neighbor_ranking(full_index, [(TIE_SEED, 1.0)]) == ["wiki/notes/Tie newer.md"]


def test_the_mcp_search_shape_only_adds_fields(full_index):
    hit = _service(full_index, None).search("Tessera renewal terms", max_results=3)["hits"][0]
    assert set(hit) == {"path", "title", "type", "summary", "snippet", "links", "layers", "score",
                        "arms", "factors", "updated"}


def test_a_batch_read_spends_one_rate_limit_token(full_index):
    from stigmergy.server.errors import RateLimitError
    from stigmergy.server.ratelimit import RateLimiter

    limiter = RateLimiter(overall_per_min=2, clock=lambda: 0.0)
    svc = BrainService(Settings(llm="fake"), full_index, build_embedder("fake"), audiences=None,
                       identity="reader", rate_limiter=limiter)
    pages = svc.read_pages([RENEWAL, PILOT, PRICING, HUB, PLAN, TESSERA])
    assert [p["path"] for p in pages] == [RENEWAL, PILOT, PRICING, HUB, PLAN]
    svc.read_pages([RENEWAL])
    with pytest.raises(RateLimitError):
        svc.read_pages([RENEWAL])


def test_a_batch_renders_hidden_and_missing_pages_alike(full_index):
    pages = _service(full_index, SCOPED).read_pages([BOARD, "wiki/notes/Never existed.md"])
    assert [set(p) for p in pages] == [{"error"}, {"error"}]
    assert pages[0]["error"].replace(BOARD, "X") == pages[1]["error"].replace(
        "wiki/notes/Never existed.md", "X")
