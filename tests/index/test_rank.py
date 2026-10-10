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
        "entity_type": "",
        "aliases": [],
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


ENTITY = "wiki/entities/Target.md"


def test_resolved_entity_hint_boosts_only_pages_linking_the_entity():
    unrelated = _page("a.md", links=["wiki/entities/Other.md"])
    linking = _page("b.md", links=[ENTITY])
    hits = _run([unrelated, linking], entity_hints=(ENTITY,))
    assert hits[0]["path"] == "b.md"
    assert f"entity:{ENTITY}" in hits[0]["factors"]
    assert hits[1]["factors"] == []


def test_resolved_entity_hint_boosts_the_entity_page_itself():
    unrelated = _page("a.md")
    entity_page = _page(ENTITY, type="entity", entity_type="organization")
    hits = _run([unrelated, entity_page], entity_hints=(ENTITY,))
    assert hits[0]["path"] == ENTITY
    assert f"entity:{ENTITY}" in hits[0]["factors"]


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
    snippet = rank.snippet(body, {"answer", "distinctive-token"}, width=80)
    assert "distinctive-token" in snippet
    assert "\n" not in snippet


def test_top_k_is_applied_after_contract_factors():
    pages = [_page(f"{index}.md") for index in range(10)]
    assert len(_run(pages, k=3)) == 3


SECOND = "wiki/entities/Second.md"


def test_a_page_linking_every_named_entity_collects_one_boost_per_entity():
    one = _page("a.md", links=[ENTITY])
    both = _page("b.md", links=[ENTITY, SECOND])
    hits = _run([one, both], entity_hints=(ENTITY, SECOND))
    assert hits[0]["path"] == "b.md"
    assert hits[0]["factors"] == [f"entity:{ENTITY}", f"entity:{SECOND}", "entities:all"]
    assert hits[1]["factors"] == [f"entity:{ENTITY}"]


def test_the_entity_boost_is_bounded_however_many_entities_a_page_links():
    hints = tuple(f"wiki/entities/E{n}.md" for n in range(4))
    some = _page("a.md", links=list(hints[:3]))
    every = _page("b.md", links=list(hints))
    factors = {h["path"]: h for h in _run([some, every], entity_hints=hints)}
    boost = [f for f, _ in rank.contract_factors(some, "q", None, hints) if f != 1.0]
    assert boost == [0.5]
    assert [f for f, _ in rank.contract_factors(every, "q", None, hints) if f != 1.0] == [0.5, 0.5]
    assert "entities:all" in factors["b.md"]["factors"]


def test_an_extra_ranking_fuses_like_an_arm_and_is_reported():
    base = _page("a.md")
    extra_only = _page("z.md")
    hits = rank.rank({"a.md": base, "z.md": extra_only}, ["a.md"], ["a.md"], "q", k=5,
                     extra_rankings={"graph": ["z.md", "a.md"]})
    assert {h["path"]: h["arms"] for h in hits} == {"a.md": ["fts", "vec", "graph"], "z.md": ["graph"]}
    assert hits[0]["path"] == "a.md"


def test_the_neighbor_ranking_counts_a_quarter_as_much_as_a_retrieval_arm():
    scores = rank.rrf_fuse([["a"], ["b"]], weights=[1.0, rank.ARM_WEIGHTS["graph"]])
    assert scores["a"] == 4 * scores["b"]
