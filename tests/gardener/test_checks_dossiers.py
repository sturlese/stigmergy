"""The file-based checks: stale views, dead vocabulary, date-bearing body links, and entity pages
still carrying their template's placeholders. All four read the repo directly (the first two
through `views.staleness.list_stale_entities`/`list_all_anchored_entities`, reused verbatim — NOT
`views.regenerate`, which would load that module's own git write path) and need no Postgres at
all — no `_pg` suffix, no `conn` fixture, fast."""
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


def test_stale_view_survives_a_hand_written_page_sitting_in_views(repo):
    """OLD BEHAVIOUR: this raised `ViewError` and took the WHOLE gardener run down with it.

    `views/` is one of `index.corpus.ZONES`, so a hand-written page can legitimately sit beside the
    generated ones. `list_stale_entities` derived its population from `os.listdir` and fed every
    `.md` STEM into `view_relpath`, whose entity-id assertion then refused `README` — an assertion
    that exists to stop a CALLER-supplied id escaping `views/`, firing here on an id that came from
    inside that directory and so could never traverse anywhere.

    The blast radius is why this is not a niceties bug: `run_gardener` catches `Exception`, records
    `status='error'` with ZERO findings and re-raises, and `gardener/cli.py`'s last-resort handler
    prints only the class name — so one stray file killed the daily run with no diagnosable message.
    """
    support.write_page(repo, "wiki", "entities/acme-corp.md",
                       frontmatter={"type": "entity", "title": "Acme Corp",
                                   "entity": ["acme-corp"], "status": "developing"})
    real_hash = skeleton.member_hash(skeleton.members_of(repo, "acme-corp"))
    support.write_view(repo, "acme-corp", member_hash=real_hash)
    support.write_page(repo, "views", "README.md",
                       frontmatter={"type": "view", "title": "About these views"},
                       body="Generated pages live here.\n")

    assert checks.check_stale_views(repo) == []


def test_stale_view_still_fires_beside_a_hand_written_page(repo):
    """The benign twin of the case above: ignoring the foreign file must not cost the check its
    eyesight for the real views sitting next to it."""
    support.write_page(repo, "wiki", "entities/acme-corp.md",
                       frontmatter={"type": "entity", "title": "Acme Corp",
                                   "entity": ["acme-corp"], "status": "developing"})
    support.write_view(repo, "acme-corp", member_hash="sha256:stale-and-wrong")
    support.write_page(repo, "views", "README.md",
                       frontmatter={"type": "view", "title": "About these views"},
                       body="Generated pages live here.\n")

    findings = checks.check_stale_views(repo)

    assert [f["subject"] for f in findings] == ["acme-corp"]


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


# ── entity pages that are still their own template ──────────────────────────────────────────────
# The template's placeholders are angle-marked (`ops/templates/entity.md`), and `stigmergy-entities
# create` copies it VERBATIM: a minted entity page says nothing until somebody writes it. Before
# this check nothing counted those pages, so an identity with no content was invisible to every
# corpus-health pass — the gardener's orphan check exempts entity pages by type, and no other check
# reads their bodies at all.
PLACEHOLDER_ENTITY = {"type": "entity", "title": "Meridian Partners",
                      "entity": ["meridian-partners"], "status": "developing", "role": ""}


def test_entity_placeholder_body_fires_for_a_page_that_is_still_the_template(repo):
    support.write_page(repo, "wiki", "entities/Meridian Partners.md",
                       frontmatter=PLACEHOLDER_ENTITY,
                       body="# Meridian Partners\n\n## What / Who\n\n"
                            "<One clear paragraph: what this entity is and why it's in the "
                            "brain.>\n")

    findings = checks.check_entity_placeholder_bodies(checks.entity_zone_pages(repo))

    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == checks.CHECK_ENTITY_PLACEHOLDER_BODY
    assert f["severity"] == "info"
    assert f["subject"] == "wiki/entities/Meridian Partners.md"
    assert f["subjects"] == ["wiki/entities/Meridian Partners.md"]
    assert "repair" in f["suggested_action"]


def test_a_written_entity_page_is_the_benign_twin(repo):
    """The half that says the check does not fire on everything. It runs over every entity page
    in the corpus and its finding asks a steward to read a drafted body, so a check that flagged
    written pages would fill the review lane with rewrites of pages somebody already wrote."""
    support.write_page(repo, "wiki", "entities/Meridian Partners.md",
                       frontmatter=PLACEHOLDER_ENTITY,
                       body="# Meridian Partners\n\n## What / Who\n\nA freight broker the "
                            "renewal pipeline runs through.\n")

    assert checks.check_entity_placeholder_bodies(checks.entity_zone_pages(repo)) == []


def test_a_placeholder_on_a_note_page_is_not_this_checks_business(repo):
    """Population: `wiki/entities/` and nothing else. A note drafted around an angle-marked
    placeholder is a filing question, not an identity with no content."""
    support.write_page(repo, "wiki", "notes/draft.md",
                       frontmatter={"type": "note", "title": "Draft", "status": "developing"},
                       body="# Draft\n\n<the paragraph nobody wrote>\n")

    assert checks.check_entity_placeholder_bodies(checks.entity_zone_pages(repo)) == []


def test_a_placeholder_inside_the_frontmatter_is_not_a_body_placeholder(repo):
    """The template's own `created: <YYYY-MM-DD>` lines live in the frontmatter, and a page whose
    frontmatter is unfinished is the contract linter's finding, not this one."""
    support.write_page(repo, "wiki", "entities/Meridian Partners.md",
                       frontmatter={**PLACEHOLDER_ENTITY, "created": "<YYYY-MM-DD>"},
                       body="# Meridian Partners\n\n## What / Who\n\nA freight broker.\n")

    assert checks.check_entity_placeholder_bodies(checks.entity_zone_pages(repo)) == []


def test_a_symlinked_entity_page_is_never_read_by_this_check_either(repo, tmp_path):
    """The walk is shared with the model's empty-body pass, so its symlink refusal has to be
    proved from BOTH consumers — a leaf `islink` guard that a later refactor moved into the sweep
    would leave this check following the link, and this check's detail is persisted, printed and
    rendered in the admin console."""
    secret = tmp_path / "outside-the-checkout.txt"
    secret.write_text("<TOTALLY-SECRET-VALUE>\n", encoding="utf-8")
    os.makedirs(os.path.join(repo, "wiki", "entities"), exist_ok=True)
    os.symlink(secret, os.path.join(repo, "wiki", "entities", "leak.md"))

    findings = checks.check_entity_placeholder_bodies(checks.entity_zone_pages(repo))

    assert findings == []
    assert "TOTALLY-SECRET-VALUE" not in repr(findings)
