"""The file-based checks: stale views, dead vocabulary, and date-bearing body links. All three
read the repo directly (`views.staleness.list_stale_entities`/`list_all_anchored_entities`,
reused verbatim — NOT `views.regenerate`, which would load that module's own git write path) and
need no Postgres at all — no `_pg` suffix, no `conn` fixture, fast."""
import os

from stigmergy.gardener import checks
from stigmergy.kernel.registry import load_registry
from stigmergy.views import skeleton
from tests.gardener import support


def _load_registry(repo: str):
    return load_registry(os.path.join(repo, "ops", "entity-registry.json"))


# ── stale views ─────────────────────────────────────────────────────────────────────────────────
def test_stale_view_fires_when_the_stored_member_hash_does_not_match(repo):
    support.write_page(repo, "wiki", "entities/acme-corp.md",
                       frontmatter={"type": "entity", "title": "Acme Corp",
                                   "entity": ["acme-corp"], "status": "developing"})
    support.write_view(repo, "acme-corp", member_hash="not-the-real-hash")

    findings = checks.check_stale_views(repo)

    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == checks.CHECK_STALE_VIEW
    assert f["severity"] == "warn"
    assert f["subject"] == "acme-corp"
    assert f["suggested_action"] == "`stigmergy-views regenerate --entity acme-corp`"


def test_stale_view_the_benign_twin_a_fresh_view_fires_nothing(repo):
    support.write_page(repo, "wiki", "entities/acme-corp.md",
                       frontmatter={"type": "entity", "title": "Acme Corp",
                                   "entity": ["acme-corp"], "status": "developing"})
    real_hash = skeleton.member_hash(skeleton.members_of(repo, "acme-corp"))
    support.write_view(repo, "acme-corp", member_hash=real_hash)

    assert checks.check_stale_views(repo) == []


def test_stale_view_an_entity_with_no_view_yet_is_not_reported_stale(repo):
    """`list_stale_entities`' own population is "entities WITH an existing view" — an anchored
    entity that has never had one regenerated is a different, unrelated fact, not this check's
    concern."""
    support.write_page(repo, "wiki", "entities/acme-corp.md",
                       frontmatter={"type": "entity", "title": "Acme Corp",
                                   "entity": ["acme-corp"], "status": "developing"})
    assert checks.check_stale_views(repo) == []


# ── dead vocabulary ─────────────────────────────────────────────────────────────────────────────
def test_dead_vocabulary_fires_for_a_registered_entity_with_zero_anchored_pages(repo):
    support.write_registry(repo, {
        "meridian-partners": {"name": "Meridian Partners", "type": "organization", "aliases": []},
    })

    findings = checks.check_dead_vocabulary(repo, _load_registry(repo))

    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == checks.CHECK_DEAD_VOCABULARY
    assert f["severity"] == "info"
    assert f["subject"] == "meridian-partners"
    assert "zero pages anchored to it" in f["detail"]


def test_dead_vocabulary_the_benign_twin_an_entity_with_a_page_fires_nothing(repo):
    support.write_registry(repo, {
        "acme-corp": {"name": "Acme Corp", "type": "organization", "aliases": []},
    })
    support.write_page(repo, "wiki", "notes/about-acme.md",
                       frontmatter={"type": "note", "title": "About Acme",
                                   "entity": ["acme-corp"], "status": "developing"})

    assert checks.check_dead_vocabulary(repo, _load_registry(repo)) == []


def test_dead_vocabulary_an_ingested_zone_anchor_also_counts_as_alive(repo):
    """`MEMBER_ZONES = ("wiki", "sources")` — `views.skeleton`'s own scope, reused
    unmodified; an entity anchored only from `sources/` is not dead vocabulary either."""
    support.write_registry(repo, {
        "acme-corp": {"name": "Acme Corp", "type": "organization", "aliases": []},
    })
    support.write_page(repo, "sources", "general/some-source.md",
                       frontmatter={"type": "source", "title": "Some Source",
                                   "entity": ["acme-corp"]})

    assert checks.check_dead_vocabulary(repo, _load_registry(repo)) == []


# ── date-bearing wikilinks in body prose ────────────────────────────────────────────────────────
def test_date_bearing_body_link_fires_as_a_warn_finding(repo):
    """The meeting flow used to veto this shape at filing time; it surfaces as a gardener WARN
    over the committed corpus instead."""
    support.write_page(repo, "wiki", "decisions/pricing-floor.md",
                       frontmatter={"type": "decision", "title": "Pricing Floor",
                                   "status": "developing", "sources": []},
                       body="Decided after [[2026-07-29-q3-sync]], where the floor was set.\n")

    findings = checks.check_date_bearing_body_links(repo)

    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == checks.CHECK_DATE_BEARING_BODY_LINK
    assert f["severity"] == "warn"
    assert f["subject"] == "wiki/decisions/pricing-floor.md"
    assert "2026-07-29-q3-sync" in f["detail"]
    assert "sources:" in f["suggested_action"]


def test_date_bearing_link_in_frontmatter_is_the_benign_twin(repo):
    """The convention's OWN sanctioned home: a date-bearing target inside `sources:`/`related:`
    frontmatter fires nothing — only BODY prose is the finding's subject."""
    support.write_page(repo, "wiki", "decisions/pricing-floor.md",
                       frontmatter={"type": "decision", "title": "Pricing Floor",
                                   "status": "developing",
                                   "sources": ['[[2026-07-29-q3-sync]]']},
                       body="Decided at the Q3 sync, where the floor was set.\n")

    assert checks.check_date_bearing_body_links(repo) == []


def test_a_dateless_body_wikilink_is_also_benign(repo):
    support.write_page(repo, "wiki", "notes/renewal.md",
                       frontmatter={"type": "note", "title": "Renewal",
                                   "status": "developing"},
                       body="See [[Acme Corp]] and [[q3-sync-notes]] for context.\n")

    assert checks.check_date_bearing_body_links(repo) == []
