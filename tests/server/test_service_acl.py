"""BrainService end to end against the real index (fake embedder), in-process: ACL enforcement
on every read surface, the existence-leak guarantee, and the structured output shape. Skips
cleanly without postgres."""
from tests.server.conftest import make_service


# ── search: out-of-scope pages are simply not there ────────────────────────────────────────────
def test_search_hits_carry_factors_score_arms_and_index_meta(indexed):
    conn, fx = indexed
    out = make_service(fx, conn, fx.STEWARD).search("quarterly revenue")
    assert out["built_at"] and out["embedding_model"] == "fake-hashed-bow-256"
    assert out["hits"]
    for h in out["hits"]:
        assert set(h) >= {"path", "title", "snippet", "score", "arms", "factors",
                          "superseded"}
        # `verification` is deliberately NOT in this shape (the purge amended) — a hit does not
        # report a verdict nothing computes. Pinned as a negative so the key cannot drift back
        # into the payload unnoticed.
        assert "verification" not in h
        assert isinstance(h["factors"], list) and h["arms"] and h["score"] > 0


def test_search_discards_out_of_scope_pages(indexed):
    conn, fx = indexed
    finance = make_service(fx, conn, fx.ANA).search("acme payroll total compensation")
    eng = make_service(fx, conn, fx.ENG).search("acme payroll total compensation")
    assert any(h["path"] == fx.ACME_PAGE for h in finance["hits"])
    assert not any(h["path"] == fx.ACME_PAGE for h in eng["hits"])
    # the open page stays visible to the scoped client
    assert any(h["path"] == fx.OPEN_PAGE
               for h in make_service(fx, conn, fx.ENG).search("initech kpi")["hits"])


def test_search_filters_roundtrip_and_unknown_filter_errors(indexed):
    conn, fx = indexed
    out = make_service(fx, conn, fx.STEWARD).search("revenue", filters={"entity": "initech"})
    # `entity` is a LIST — membership, not equality.
    assert out["hits"] and all("initech" in h["entity"] for h in out["hits"])
    import pytest
    with pytest.raises(ValueError):
        make_service(fx, conn, fx.STEWARD).search("x", filters={"body": "nope"})


def test_search_clamps_max_results_and_count_matches_hits(indexed):
    """A hostile/careless max_results must never slice open or misreport: it is clamped to
    [1, _CANDIDATE_HITS] and `count` always equals the number of hits returned."""
    conn, fx = indexed
    svc = make_service(fx, conn, fx.STEWARD)
    for bad in (-1, 0):
        out = svc.search("quarterly revenue", max_results=bad)
        assert out["count"] == len(out["hits"]) >= 1     # clamped to >=1, never negative
    huge = svc.search("quarterly revenue", max_results=10_000)
    assert huge["count"] == len(huge["hits"])            # count is the hits length, not the ask


# ── read_page: trust signals first, UNTRUSTED fence, no existence leak ─────────────────────────
def test_read_page_leads_with_trust_signals_and_fences_body(indexed):
    conn, fx = indexed
    page = make_service(fx, conn, fx.STEWARD).read_page(fx.OPEN_PAGE)
    keys = list(page)
    assert keys[0] == "path" and "title" in keys[:3]      # trust signals lead
    # `verification` is deliberately NOT a trust signal here (the purge amended). Asserted as a
    # negative: the fixture page still carries the key in its frontmatter, so a reader coming back
    # would turn this red rather than pass unnoticed.
    assert "verification" not in page
    assert page["body"].startswith("<<<UNTRUSTED-DATA") and page["body"].endswith("UNTRUSTED-DATA;end>>>")


def test_read_page_shows_the_superseded_banner(indexed):
    conn, fx = indexed
    page = make_service(fx, conn, fx.STEWARD).read_page(fx.SUPERSEDED_PAGE)
    assert page["superseded_by"] == "drive:new"
    assert page["banner"] and "SUPERSEDED" in page["banner"]
    # a current page carries no banner
    assert make_service(fx, conn, fx.STEWARD).read_page(fx.OPEN_PAGE)["banner"] is None


def test_read_page_neutralizes_an_in_band_fence_token(indexed):
    """End to end: a stored body reproducing the closing delimiter must not close the fence early
    — read_page neutralizes every in-band token, so exactly one real closing delimiter (ours)
    survives and the injected payload stays inside the fence."""
    conn, fx = indexed
    body = make_service(fx, conn, fx.STEWARD).read_page(fx.HOSTILE_PAGE)["body"]
    assert body.startswith("<<<UNTRUSTED-DATA\n") and body.endswith("\nUNTRUSTED-DATA;end>>>")
    assert body.count("UNTRUSTED-DATA;end>>>") == 1                 # the in-band close was neutralized
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in body              # payload preserved, just fenced
    assert body.index("IGNORE ALL PREVIOUS INSTRUCTIONS") < body.rindex("UNTRUSTED-DATA;end>>>")


def test_read_page_out_of_scope_is_byte_identical_to_nonexistent(indexed):
    """Existence-leak guarantee: identity B's response for the ACL'd page must match its response
    for a genuinely nonexistent path in shape."""
    conn, fx = indexed
    eng = make_service(fx, conn, fx.ENG)
    denied = eng.read_page(fx.ACME_PAGE)
    ghost = eng.read_page("wiki/finance/does-not-exist.md")
    assert set(denied) == set(ghost) == {"error"}
    assert denied["error"].startswith("unknown page:")
    # and finance CAN read it — proving it really exists behind the identical shape
    assert "body" in make_service(fx, conn, fx.ANA).read_page(fx.ACME_PAGE)



# ── scoped_entities: existence scoping over a PLURAL `entity:` array ───────────────────────────
# `unnest(entity)` had no direct test at all — every existing exercise of `entity` in this suite
# used a single-scalar page, so a "simplification" from `%s = ANY(entity)` to `entity = ARRAY[%s]`
# (or an `unnest` that silently dropped every element past the first) would have passed every test
# that existed before this one. `fx.VAULT_QUILL_PAGE` is eng-scoped and anchored to TWO entity ids
# that appear nowhere else in the fixture, so both halves below are unambiguous: what an
# identity that CAN see it discovers, and what one that CANNOT see it never learns exists at all.
def test_scoped_entities_yields_both_ids_of_a_plural_entity_page(indexed):
    conn, fx = indexed
    steward = make_service(fx, conn, fx.STEWARD).scoped_entities()
    eng = make_service(fx, conn, fx.ENG).scoped_entities()
    assert {"vault-corp", "quill-industries"} <= set(steward)
    assert {"vault-corp", "quill-industries"} <= set(eng)


def test_scoped_entities_hides_both_ids_from_an_identity_that_cannot_see_the_page(indexed):
    conn, fx = indexed
    ana = make_service(fx, conn, fx.ANA).scoped_entities()          # scoped to ["finance"]
    assert "vault-corp" not in ana
    assert "quill-industries" not in ana


# ── the per-entity rollup is a READ-TIME answer, and it is ACL-scoped per reader ──────────────
# Three tests stood here while the rollup was a stored `views/<id>.md` page. Two of them —
# `test_view_is_readable_and_searchable_for_a_scoped_identity` and
# `test_view_out_of_scope_is_absent_from_search_and_byte_identical_to_nonexistent_in_read_page` —
# are DELETED rather than re-aimed: their subject was that a DERIVED page needs no special-cased
# ACL enforcement, and with no derived pages left they would only re-prove, on a fourth page
# shape, what the authored-page tests above already prove about `visible()`.
#
# What survives is the property the stored rollup existed to serve, and it moved somewhere it can
# actually be kept. `describe_entity` assembles the rollup per CALLER, so it can be scoped — which
# the stored page never could be: one file cannot be true for two readers at once, so it carried
# no `acl:` at all and summarised only the open subset. These two tests hold that at the seam.
def test_the_entity_filter_is_acl_scoped_absent_rather_than_merely_unranked(indexed):
    """A real `entity` filter roundtrip (`search_arms`'s own filter) crossed with the audience
    rule: the restricted page is a HIT for the identity that may read it and ABSENT for the one
    that may not — never merely ranked lower, which would leak its existence through the count."""
    conn, fx = indexed
    ana = make_service(fx, conn, fx.ANA)                             # scoped to ["finance"]
    out = ana.search("payroll", filters={"entity": "acme-corp"})
    assert any(h["path"] == fx.ACME_PAGE for h in out["hits"])
    assert all("acme-corp" in h["entity"] for h in out["hits"])

    eng = make_service(fx, conn, fx.ENG)                             # scoped to ["eng"] only
    eng_out = eng.search("payroll", filters={"entity": "acme-corp"})
    assert not any(h["path"] == fx.ACME_PAGE for h in eng_out["hits"])


def test_describe_entity_is_the_rollup_and_it_is_scoped_to_the_reader(indexed):
    """The read-time rollup, at the seam that decides who sees it.

    For the identity that may read Acme's only anchored page, the timeline names it. For the one
    that may not, the entity does not exist at all — and the absence is BYTE-IDENTICAL to an
    entity nobody ever registered, so the answer cannot be read as "there is something here you
    may not see". This is the guarantee a stored rollup could not offer: it had to be one page for
    everybody, so it was open, and its timeline named every member's path to every reader."""
    conn, fx = indexed
    ana = make_service(fx, conn, fx.ANA)                             # scoped to ["finance"]
    seen = ana.describe_entity("acme-corp")
    assert "error" not in seen
    assert any(item["path"] == fx.ACME_PAGE for item in seen["timeline"])

    eng = make_service(fx, conn, fx.ENG)                             # scoped to ["eng"] only
    denied = eng.describe_entity("acme-corp")
    ghost = eng.describe_entity("no-such-entity-ever")
    assert set(denied) == set(ghost) == {"error"}
