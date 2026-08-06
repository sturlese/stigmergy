"""Contract ranking, offline: the contract factors over fabricated candidates — no database, no
embedder. The adversarial case (a superseded chain carrying a figure the successor corrects) is
planted explicitly."""
from datetime import date

from stigmergy.index import rank

TODAY = date(2026, 7, 19)


def _page(path, **kw):
    base = {"path": path, "page_id": path, "title": path, "body": f"content of {path}",
            "type": "report", "status": "", "entity": [], "owner": "", "tier": 1,
            "as_of": "2026-06", "updated": "2026-06-01", "superseded_by": "", "supersedes": "",
            "acl": None, "inlinks": 0, "content_hash": "sha256:x"}
    base.update(kw)
    return base


def _run(pages, query, fts=None, vec=None, **kw):
    paths = [p["path"] for p in pages]
    return rank.rank({p["path"]: p for p in pages}, fts if fts is not None else paths,
                     vec if vec is not None else paths, query, today=TODAY, **kw)


# --- a superseded page never outranks its successor ------------------------------------------

def test_superseded_page_never_outranks_its_successor():
    draft = _page("draft.md", superseded_by="drive:G2", body="revenue impact was 1.2M draft")
    final = _page("final.md", supersedes="drive:G1", body="revenue impact was 1.3M corrected")
    # worst case for the contract: the stale draft wins BOTH raw arms
    hits = _run([draft, final], "globex revenue impact",
                fts=["draft.md", "final.md"], vec=["draft.md", "final.md"])
    assert [h["path"] for h in hits] == ["final.md", "draft.md"]
    demoted = next(h for h in hits if h["path"] == "draft.md")
    assert "superseded" in demoted["factors"]
    assert next(h for h in hits if h["path"] == "final.md")["factors"] == []


def test_current_only_drops_superseded_instead_of_demoting():
    draft = _page("draft.md", superseded_by="drive:G2")
    final = _page("final.md")
    hits = _run([draft, final], "anything", include_superseded=False)
    assert [h["path"] for h in hits] == ["final.md"]


# --- split-chain demotion: continuation parts inherit the chain's supersession -----------------
# `superseded_by` is stamped on a document's PRIMARY page only — a continuation part's OWN
# frontmatter carries an empty `superseded_by`. That gap used to be closed HERE, at rank time, by
# reconstructing chain membership from whichever candidates happened to be in a given search's
# pool (`rank.py`'s old `superseded_bases`) — a reconstruction that silently failed whenever the
# chain's primary fell outside that pool. The fix lives at BUILD time instead
# (`corpus.load_pages` propagates the primary's `superseded_by` onto every sibling in the chain,
# `tests/index/test_corpus.py`/`test_rank_edges.py` cover that half) — so by the time a row
# reaches `rank()`, its own `superseded_by` column already tells the truth, chain and all. The
# tests below construct parts with `superseded_by` ALREADY propagated (the post-`load_pages`
# shape), proving `rank()` needs nothing more than its own per-row `contract_factors` check.

def test_superseded_split_continuation_parts_are_demoted_via_their_own_propagated_field():
    # a superseded document split into three parts: `corpus.load_pages` has already propagated
    # part 1's `superseded_by` onto parts 2 and 3 by the time rows like these reach `rank()`.
    part1 = _page("draft.md", page_id="drive:G1", superseded_by="drive:G2",
                  body="revenue impact 1.2M part one")
    part2 = _page("draft-p2.md", page_id="drive:G1#p2", superseded_by="drive:G2",
                  body="revenue impact 1.2M part two continued")
    part3 = _page("draft-p3.md", page_id="drive:G1#p3", superseded_by="drive:G2",
                  body="revenue impact 1.2M part three continued")
    current = _page("final.md", page_id="drive:G2", supersedes="drive:G1",
                    body="revenue impact 1.3M corrected current")
    # worst case: every stale part leads both raw arms; the current page trails
    pages = [part1, part2, part3, current]
    order = ["draft.md", "draft-p2.md", "draft-p3.md", "final.md"]
    hits = _run(pages, "revenue impact", fts=order, vec=order)
    # the current page must outrank the stale document — which, under the chain collapse,
    # occupies exactly ONE slot (its best-scoring member), demoted via its own propagated field.
    ranked = [h["path"] for h in hits]
    assert ranked == ["final.md", "draft.md"]
    assert "superseded" in next(h for h in hits if h["path"] == "draft.md")["factors"]
    assert next(h for h in hits if h["path"] == "final.md")["factors"] == []


def test_current_only_drops_superseded_continuation_parts_too():
    part1 = _page("draft.md", page_id="drive:G1", superseded_by="drive:G2")
    part2 = _page("draft-p2.md", page_id="drive:G1#p2", superseded_by="drive:G2")
    current = _page("final.md", page_id="drive:G2")
    hits = _run([part1, part2, current], "anything", include_superseded=False)
    assert [h["path"] for h in hits] == ["final.md"]   # the whole stale chain drops, part 2 included


def test_unsuperseded_split_parts_are_not_demoted():
    """A split page whose document is CURRENT must not be demoted just for being a part."""
    part1 = _page("doc.md", page_id="drive:D", superseded_by="")
    part2 = _page("doc-p2.md", page_id="drive:D#p2", superseded_by="")
    hits = _run([part1, part2], "content")
    for h in hits:
        assert "superseded" not in h["factors"]


# --- ONE document, ONE top-k slot — the chain collapse ----------------------------------------

def test_a_split_document_cannot_flood_the_top_k():
    """A measured miss, as a unit case: a four-part transcript led both arms and occupied four of
    five slots, burying the page that answered (vec rank 2). Collapsed, the chain holds one slot
    and the answering page enters the top-k."""
    parts = [_page(f"transcript{'' if n == 1 else f'-p{n}'}.md",
                   page_id=f"meeting-transcript{'' if n == 1 else f'-p{n}'}")
             for n in (1, 2, 3, 4)]
    answer = _page("decision.md", page_id="training-decision")
    order = [p["path"] for p in parts] + ["decision.md"]   # worst case: every part leads
    hits = _run(parts + [answer], "formacion", fts=order, vec=order, k=2)

    assert [h["path"] for h in hits] == ["transcript.md", "decision.md"]


def test_the_live_stem_convention_collapses_like_the_historical_marker():
    """`_build_source_parts` names files `<stem>-p<n>.md` and writes NO frontmatter id, so the
    live corpus's part ids are suffixed stems (`…-transcript-p2`). `chain_base` used to know only
    the older `<id>#p2` marker and was blind to that convention; it now reads both."""
    assert rank.chain_base("x-transcript-p2") == "x-transcript"
    assert rank.chain_base("drive:G1#p3") == "drive:G1"
    assert rank.chain_base("x-transcript") == "x-transcript"


def test_an_independent_page_with_a_part_shaped_id_keeps_its_own_slot():
    """`roadmap-p2` with no `roadmap` in the ranking collapses with nothing — grouping merges
    rows that SHARE a base, it never invents one."""
    lone = _page("roadmap-p2.md", page_id="roadmap-p2")
    other = _page("notes.md", page_id="notes")
    hits = _run([lone, other], "content")
    assert {h["path"] for h in hits} == {"roadmap-p2.md", "notes.md"}


def test_stem_twins_in_different_directories_never_collapse():
    """The cross-stamping class, at rank time: two ID-LESS pages named `quarterly-update.md` in
    different folders both fall back to the SAME stem-derived page_id — unrelated documents that
    a bare chain_base key would merge (measured on the borealis/contoso service fixtures). The
    directory dimension of the collapse key keeps both."""
    borealis = _page("wiki/entities/borealis/quarterly-update.md", page_id="quarterly-update")
    contoso = _page("wiki/entities/contoso/quarterly-update.md", page_id="quarterly-update")
    hits = _run([borealis, contoso], "quarterly update")
    assert len(hits) == 2


def test_a_real_chain_collapses_within_its_own_directory():
    """The positive twin: parts sit BESIDE their primary, so the directory-keyed collapse still
    merges a genuine chain."""
    primary = _page("sources/meetings/x-transcript.md", page_id="x-transcript")
    part = _page("sources/meetings/x-transcript-p2.md", page_id="x-transcript-p2")
    hits = _run([primary, part], "content")
    assert len(hits) == 1


def test_collapse_keeps_the_best_scoring_member_of_the_chain():
    """Which member represents the document is decided by the final score, not by primacy: a
    continuation part that outranks its primary (here via both arms) is the one shown."""
    primary = _page("doc.md", page_id="doc")
    part2 = _page("doc-p2.md", page_id="doc-p2")
    order = ["doc-p2.md", "doc.md"]
    hits = _run([primary, part2], "content", fts=order, vec=order)
    assert [h["path"] for h in hits] == ["doc-p2.md"]


# --- the rank-time reconstruction is absent, on purpose — proven, not assumed -------------------
def test_rank_does_not_reconstruct_supersession_from_a_sibling_in_the_candidate_set():
    """The compensation must not ossify. A continuation part whose OWN `superseded_by` was empty
    used to get demoted anyway if its PRIMARY happened to be in the same candidate set
    (`rank.py`'s old `superseded_bases` cross-reference). That mechanism is gone —
    `corpus.load_pages` is the ONLY place that propagates the field, at build time, over the
    WHOLE corpus. A part with a genuinely empty `superseded_by` (as it would be BEFORE
    `load_pages` ever ran, or if the propagation step were skipped) is not demoted here, even
    with its primary sitting right next to it in the same candidates dict — the red half of the
    pair that pins the fix upstream rather than here."""
    part1 = _page("draft.md", page_id="drive:G1", superseded_by="drive:G2")
    part2 = _page("draft-p2.md", page_id="drive:G1#p2", superseded_by="")   # NOT propagated
    hits = _run([part1, part2], "content")
    # The un-propagated part carries NO superseded factor (nothing reconstructed it from its
    # sibling) — proven by it OUTSCORING its demoted primary and so representing the chain in
    # the collapsed top-k. Were rank-time reconstruction alive, part 2 would be demoted
    # identically to part 1 and the primary's path order would represent the chain instead.
    assert [h["path"] for h in hits] == ["draft-p2.md"]
    assert "superseded" not in hits[0]["factors"]


# --- no quality/trust demotions: `verification-failed`, `verification-partial`, `manual-review`
# are all absent. Each keyed off a column no producer feeds any more — no template offers one, no
# code stamps one. A ranking input nothing writes is a score nobody can reason about, and a test
# pinning one asserts arithmetic about a value that can never arrive.


# --- maturity: evergreen outranks seed on equal relevance -------------------------------------
# This factor was RE-BOUND rather than deleted. `canonical` died with the canon lane, which would
# have left the boost with no producer — the same shape that retired `verification`. `evergreen`
# is the direct successor at the top of the maturity axis, so the factor keeps its meaning ("this
# page is kept current") instead of becoming a score nobody feeds.

def test_evergreen_outranks_seed_on_equal_relevance():
    seed = _page("a-seed.md", status="seed")
    evergreen = _page("b-evergreen.md", status="evergreen", owner="steward")
    hits = _run([seed, evergreen], "refund policy",
                fts=["a-seed.md", "b-evergreen.md"], vec=["a-seed.md", "b-evergreen.md"])
    # NOTE: identical arm positions would be truly equal relevance; here seed even leads both
    # arms by one rank and evergreen still must win on the maturity boost
    assert [h["path"] for h in hits] == ["b-evergreen.md", "a-seed.md"]
    assert "status-evergreen" in hits[0]["factors"]


def test_a_stale_canonical_status_boosts_nothing():
    """The negative half, so the re-binding cannot silently become a second live factor: a page
    left carrying `status: canonical` (written before the lane died) ranks like any other."""
    seed = _page("a-seed.md", status="seed")
    stale = _page("b-canonical.md", status="canonical", owner="steward")
    hits = _run([seed, stale], "refund policy",
                fts=["a-seed.md", "b-canonical.md"], vec=["a-seed.md", "b-canonical.md"])
    assert not [f for h in hits for f in h["factors"] if "canonical" in f]


# --- staleness --------------------------------------------------------------------------------

def test_staleness_penalty_applies_beyond_the_horizon():
    stale = _page("stale.md", as_of="2023", updated="2023-03-01")
    fresh = _page("fresh.md", as_of="2026-06")
    hits = _run([stale, fresh], "pricing", fts=["stale.md", "fresh.md"],
                vec=["stale.md", "fresh.md"])
    assert [h["path"] for h in hits] == ["fresh.md", "stale.md"]
    assert "stale:2023" in next(h for h in hits if h["path"] == "stale.md")["factors"]


def test_no_staleness_penalty_without_injected_today():
    factors = rank.contract_factors(_page("p.md", as_of="2019"), "query", today=None)
    assert not any(label.startswith("stale") for _f, label in factors)


def test_coarse_but_recent_as_of_is_not_penalized():
    # a page dated just "2026" is fresh through 2026-12-31 — coarse granularity never punished
    factors = rank.contract_factors(_page("p.md", as_of="2026", updated=""), "q", today=TODAY)
    assert not any(label.startswith("stale") for _f, label in factors)


# --- entity / period / fresh boosts ----------------------------------------------------------

def test_entity_and_period_boosts_with_labels():
    """TOLD, not inferred: the boost fires on the SERVICE-resolved `entity_hint`, matched by
    membership of the page's `entity` list. Token inference is gone — it was structurally dead
    for every multi-word id anyway."""
    page = _page("kpi.md", entity=["initech"], as_of="2026-01")
    factors = dict((label, f) for f, label in
                   rank.contract_factors(page, "initech kpi 2026-01", TODAY, entity_hint="initech"))
    assert "entity:initech" in factors
    assert "period-match" in factors


def test_entity_boost_requires_a_told_hint_never_a_query_token():
    """The TOLD pin, red side: the same query naming the entity verbatim fires NOTHING without a
    hint — resolution belongs to the service (it owns the registry), ranking only applies what
    it is told. This is what makes the factor honest for `northwind-group`-shaped ids, which
    token inference could never match against the two tokens of "Northwind Group"."""
    page = _page("kpi.md", entity=["initech"])
    factors = rank.contract_factors(page, "initech kpi", TODAY)
    assert not any(label.startswith("entity:") for _f, label in factors)


def test_a_multi_word_entity_id_gets_the_boost_via_the_hint():
    """The measured defect this factor exists for: an id no query token can ever equal."""
    page = _page("mrr.md", entity=["northwind-group"])
    factors = dict((label, f) for f, label in
                   rank.contract_factors(page, "What is the MRR for Northwind Group?", TODAY,
                                         entity_hint="northwind-group"))
    assert factors.get("entity:northwind-group") == rank._BOOST_ENTITY


def test_entity_boost_fires_on_any_matching_element_of_a_multi_valued_list():
    """A page anchored to several entities gets the boost if the hint matches ANY element, and
    the label names the hint — never the whole list."""
    page = _page("kpi.md", entity=["borealis-dynamics", "initech"])
    factors = dict((label, f) for f, label in
                   rank.contract_factors(page, "initech kpi", TODAY, entity_hint="initech"))
    assert factors.get("entity:initech") == rank._BOOST_ENTITY
    assert "entity:borealis-dynamics" not in factors


def test_a_hint_for_an_unanchored_page_fires_nothing():
    page = _page("kpi.md", entity=["initech"])
    factors = rank.contract_factors(page, "contoso kpi", TODAY, entity_hint="contoso")
    assert not any(label.startswith("entity:") for _f, label in factors)


def test_entity_boost_applies_once_even_when_every_element_matches():
    """One boost per page, at its current weight — not one per matching element."""
    page = _page("kpi.md", entity=["initech", "Initech"])
    factors = [label for _f, label in
               rank.contract_factors(page, "initech kpi", TODAY, entity_hint="initech")
               if label.startswith("entity:")]
    assert len(factors) == 1


def test_entity_boost_does_not_fire_per_character_when_entity_is_a_bare_string():
    """A scalar string reaching this function directly must not be iterated as characters —
    membership equality against the hint makes the single-letter class impossible, and this pins
    that a bare-string `entity` still cannot fire a spurious label."""
    page = _page("kpi.md", entity="initech")
    factors = dict((label, f) for f, label in
                   rank.contract_factors(page, "e kpi", TODAY, entity_hint="e"))
    assert not any(label.startswith("entity:") for label in factors)


def test_recency_words_prefer_fresh_as_of():
    page = _page("p.md", as_of="2026-06")
    for query in ("latest revenue", "current figures", "the most recent number"):
        labels = [label for _f, label in rank.contract_factors(page, query, TODAY)]
        assert any(label.startswith("fresh:") for label in labels), query
    labels = [label for _f, label in rank.contract_factors(page, "plain question", TODAY)]
    assert not any(label.startswith("fresh:") for label in labels)


# --- fusion -----------------------------------------------------------------------------------

def test_rrf_fusion_rewards_presence_in_both_arms():
    both = _page("both.md")
    fts_only = _page("fts-only.md")
    vec_only = _page("vec-only.md")
    hits = _run([both, fts_only, vec_only], "content",
                fts=["fts-only.md", "both.md"], vec=["vec-only.md", "both.md"])
    assert hits[0]["path"] == "both.md"
    assert sorted(hits[0]["arms"]) == ["fts", "vec"]


def test_rrf_fuse_matches_the_spike_formula():
    scores = rank.rrf_fuse([["a", "b"], ["b", "a"]], k=60)
    assert scores["a"] == scores["b"]
    assert abs(scores["a"] - (1 / 61 + 1 / 62)) < 1e-12


def test_candidates_missing_from_both_arms_never_surface():
    page = _page("orphan.md")
    hits = rank.rank({"orphan.md": page}, [], [], "query", today=TODAY)
    assert hits == []


# --- determinism & shape ----------------------------------------------------------------------

def test_ties_break_on_path_deterministically():
    a, b = _page("a.md"), _page("b.md")
    # a true tie: each page leads one arm — symmetric RRF, no factors; path decides, and
    # candidate-dict insertion order must not matter
    hits = rank.rank({"b.md": b, "a.md": a}, ["a.md", "b.md"], ["b.md", "a.md"],
                     "content", today=TODAY)
    assert [h["path"] for h in hits] == ["a.md", "b.md"]
    assert hits[0]["score"] == hits[1]["score"]


def test_every_hit_carries_factors_arms_score_and_snippet():
    page = _page("p.md", body="the needle sits in this body text somewhere")
    (hit,) = _run([page], "needle")
    assert set(hit) >= {"score", "factors", "arms", "snippet", "path", "page_id"}
    assert "needle" in hit["snippet"]
    assert "body" not in hit          # bodies never ship on hits, snippets do


def test_k_limits_the_hits():
    pages = [_page(f"p{i}.md") for i in range(10)]
    assert len(_run(pages, "content", k=3)) == 3


def test_snippets_strip_control_characters_from_hostile_content():
    """Corpus content is untrusted terminal output: ANSI escapes and other C0/C1 controls
    must never survive into a rendered snippet."""
    hostile = "before \x1b[31mred\x1b[0m \x00null \x9bcsi needle after"
    page = _page("p.md", body=hostile)
    (hit,) = _run([page], "needle")
    assert "\x1b" not in hit["snippet"] and "\x00" not in hit["snippet"] and "\x9b" not in hit["snippet"]
    assert "needle" in hit["snippet"]
    # `rank.sanitize` is `stigmergy.text.sanitize`, which `rank` imports to clean every snippet;
    # `tests/test_text.py` owns that seam's own tests.
    assert rank.sanitize("a\x1b[2Jb") == "a[2Jb"
