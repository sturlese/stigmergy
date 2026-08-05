"""Ranking edge cases `test_rank.py` leaves open.

Offline, pure: `_period_end` calendar edges, a forged `superseded_by` planted in an AUTHORED
page (adversarial: the demotion must key off the parsed contract field regardless of zone),
and RRF behavior at the candidate-pool edges (pool = 40).
"""
from datetime import date

from stigmergy.index import corpus, rank

TODAY = date(2026, 7, 19)


# --- _period_end calendar edges ---------------------------------------------------------------

def test_period_end_quarter_year_month_and_day():
    assert rank._period_end("2026-Q4") == date(2026, 12, 28)
    assert rank._period_end("2026-q1") == date(2026, 3, 28)       # case-insensitive
    assert rank._period_end("2026") == date(2026, 12, 31)
    assert rank._period_end("2026-02") == date(2026, 2, 28)
    assert rank._period_end("2026-03-15") == date(2026, 3, 15)
    assert rank._period_end('"2026-03-15"') == date(2026, 3, 15)  # quoted frontmatter scalar


def test_period_end_rejects_invalid_values_instead_of_crashing():
    for bad in ("2026-13", "2026-00", "2026-02-30", "2026-Q5", "Q1-2026", "next year", ""):
        assert rank._period_end(bad) is None, bad


def test_invalid_as_of_never_produces_a_stale_factor():
    page = {"path": "p.md", "as_of": "2026-13", "updated": "", "superseded_by": ""}
    factors = rank.contract_factors(page, "query", today=TODAY)
    assert not any(label.startswith("stale") for _f, label in factors)


def test_q4_page_is_fresh_through_its_quarter_and_stale_a_year_after():
    page = {"path": "p.md", "as_of": "2024-Q4", "updated": "", "superseded_by": ""}
    fresh_today = date(2025, 12, 28)      # exactly 365 days after 2024-12-28: not yet stale
    assert not any(lbl.startswith("stale")
                   for _f, lbl in rank.contract_factors(page, "q", today=fresh_today))
    stale_today = date(2025, 12, 29)      # one day beyond the horizon
    assert any(lbl.startswith("stale")
               for _f, lbl in rank.contract_factors(page, "q", today=stale_today))


# --- forged superseded_by in an authored page -------------------------------------------------

def test_forged_superseded_by_in_an_authored_page_is_parsed_and_demoted(tmp_path):
    """Adversarial: someone plants `superseded_by` in a wiki/ page. The corpus parser
    must surface it and the ranking must demote it — zone grants no immunity — and
    current-only must drop it. (Whether authored pages may carry the field at all is the
    knowledge repo linter's business, not the index's.)"""
    kdir = tmp_path / "wiki"
    kdir.mkdir()
    (kdir / "honest-page.md").write_text(
        "---\ntitle: Honest Policy\nstatus: canonical\n---\n# Honest Policy\npricing policy body\n")
    (kdir / "forged-page.md").write_text(
        "---\ntitle: Forged Policy\nsuperseded_by: nonexistent-successor\n---\n"
        "# Forged Policy\npricing policy body\n")
    rows = {r.path: r for r in corpus.load_pages(str(tmp_path))}
    forged = rows["wiki/forged-page.md"]
    assert forged.zone == "wiki"
    assert forged.superseded_by == "nonexistent-successor"

    candidates = {r.path: {**r.__dict__} for r in rows.values()}
    order = [r.path for r in rows.values()]
    hits = rank.rank(candidates, order, order, "pricing policy", today=TODAY)
    assert [h["path"] for h in hits][-1] == "wiki/forged-page.md"
    assert "superseded" in hits[-1]["factors"]

    current = rank.rank(candidates, order, order, "pricing policy", today=TODAY,
                        include_superseded=False)
    assert "wiki/forged-page.md" not in [h["path"] for h in current]


# --- RRF at the candidate-pool edges (pool = 40) ---------------------------------------------

def _pool(prefix: str, n: int = rank.CANDIDATE_POOL) -> list[str]:
    return [f"{prefix}{i:02d}.md" for i in range(n)]


def test_rank_40_in_both_arms_beats_a_mid_rank_single_arm_page():
    """A page scraping the bottom of BOTH pools outscores a page at rank 20 of one arm:
    2/(60+40) > 1/(60+20). If the pool constant changes, this pins the fusion consequence."""
    scores = rank.rrf_fuse([[*_pool("f")[:-1], "edge.md"], [*_pool("v")[:-1], "edge.md"]])
    assert scores["edge.md"] > scores["v19.md"]


def test_symmetric_pool_edge_tie_breaks_on_path_and_is_insertion_order_free():
    """Two pages mirror-imaged across full 40-deep pools fuse to the same score; the hit
    order must come from the path tie-break, never from dict insertion order."""
    fts = [*_pool("x"), "a-tie.md", "b-tie.md"][2:]        # both inside a full pool
    vec = [*_pool("x"), "b-tie.md", "a-tie.md"][2:]
    pages = {p: {"path": p, "page_id": p, "title": p, "body": "", "superseded_by": ""}
             for p in set(fts) | set(vec)}
    hits_fwd = rank.rank(dict(sorted(pages.items())), fts, vec, "q", k=50, today=TODAY)
    hits_rev = rank.rank(dict(sorted(pages.items(), reverse=True)), fts, vec, "q", k=50, today=TODAY)
    assert [h["path"] for h in hits_fwd] == [h["path"] for h in hits_rev]
    a = next(h for h in hits_fwd if h["path"] == "a-tie.md")
    b = next(h for h in hits_fwd if h["path"] == "b-tie.md")
    assert a["score"] == b["score"]
    assert [h["path"] for h in hits_fwd].index("a-tie.md") < [h["path"] for h in hits_fwd].index("b-tie.md")
