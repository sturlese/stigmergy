from datetime import date

from stigmergy.index import rank

TODAY = date(2026, 8, 24)


def _page(path: str, **values):
    return {
        "path": path,
        "page_id": path,
        "title": path,
        "body": "relevant searchable body",
        "type": "note",
        "status": "developing",
        "entity": [],
        "updated": "2026-08-01",
        "acl": None,
        "inlinks": 0,
        "links": [],
        "sources": [],
        "content_hash": "sha256:x",
        **values,
    }


def _run(pages, query="relevant", **kwargs):
    paths = [page["path"] for page in pages]
    return rank.rank({page["path"]: page for page in pages}, paths, paths, query, **kwargs)


def test_rrf_rewards_candidates_present_in_both_arms():
    scores = rank.rrf_fuse([["both", "fts"], ["vec", "both"]])
    assert scores["both"] > scores["fts"]
    assert scores["both"] > scores["vec"]


def test_evergreen_status_is_an_explainable_boost():
    ordinary = _page("a.md")
    evergreen = _page("b.md", status="evergreen")
    hits = _run([ordinary, evergreen])
    assert hits[0]["path"] == "b.md"
    assert "status-evergreen" in hits[0]["factors"]


def test_resolved_entity_hint_boosts_only_anchored_pages():
    unrelated = _page("a.md", entity=["ent_other"])
    anchored = _page("b.md", entity=["ent_target"])
    hits = _run([unrelated, anchored], entity_hint="ent_target")
    assert hits[0]["path"] == "b.md"
    assert "entity:ent_target" in hits[0]["factors"]


def test_stale_pages_are_demoted_when_a_reference_date_is_supplied():
    stale = _page("a.md", updated="2020-01-01")
    current = _page("b.md", updated="2026-08-01")
    hits = _run([stale, current], today=TODAY)
    assert hits[0]["path"] == "b.md"
    assert any(label.startswith("stale:") for label in hits[1]["factors"])


def test_invalid_update_date_never_breaks_ranking():
    page = _page("a.md", updated="not-a-date")
    assert _run([page], today=TODAY)[0]["path"] == "a.md"


def test_ties_break_by_path_independently_of_mapping_order():
    a = _page("a.md")
    b = _page("b.md")
    paths = ["a.md", "b.md"]
    forward = rank.rank({"a.md": a, "b.md": b}, paths, paths, "q", k=10)
    reverse = rank.rank({"b.md": b, "a.md": a}, paths, paths, "q", k=10)
    assert [hit["path"] for hit in forward] == ["a.md", "b.md"]
    assert [hit["path"] for hit in reverse] == ["a.md", "b.md"]


def test_snippet_centers_the_longest_matching_token_and_is_sanitized():
    body = "prefix " * 50 + "distinctive-token answer"
    snippet = rank._snippet(body, {"answer", "distinctive-token"}, width=80)
    assert "distinctive-token" in snippet
    assert "\n" not in snippet


def test_top_k_is_applied_after_contract_factors():
    pages = [_page(f"{index}.md") for index in range(10)]
    assert len(_run(pages, k=3)) == 3
