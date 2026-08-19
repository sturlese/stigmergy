"""The two corpus-wide checks: orphan pages, and aging seeds. Both read `pages_index`, built for
real from a fixture repo (`index.build.rebuild`) — never from a hand-crafted row a parsing bug
could silently disagree with.
"""
import datetime

from stigmergy.gardener import checks
from tests.gardener import support

# UTC, never local time: the age check reads `current_date - updated::date`, computed IN Postgres
# (this package's own `checks.py` docstring), and the docker container is pinned to Etc/UTC. A
# fixture backdated from the MACHINE's local calendar day drifts by one during the nightly window
# where local has already rolled to a new day and UTC has not (e.g. 00:00-02:00 CEST) — an
# off-by-one age mismatch with nothing wrong in the code. Do not simplify this back to
# `date.today()`.
TODAY = datetime.datetime.now(datetime.UTC).date()


def _days_ago(n: int) -> str:
    return (TODAY - datetime.timedelta(days=n)).isoformat()


# ── orphans ─────────────────────────────────────────────────────────────────────────────────────
def test_orphan_fires_for_a_knowledge_page_nothing_links_to(conn, repo):
    support.write_page(repo, "wiki", "notes/legacy-pricing-notes.md",
                       frontmatter={"type": "note", "title": "Legacy Pricing Notes",
                                   "entity": [], "status": "developing",
                                   "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)

    findings = checks.check_orphans(conn)

    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == checks.CHECK_ORPHAN_PAGE
    assert f["severity"] == "info"
    assert f["subject"] == "wiki/notes/legacy-pricing-notes.md"
    assert 'type "note"' in f["detail"]


def test_orphan_the_benign_twin_a_linked_page_fires_nothing(conn, repo):
    support.write_page(repo, "wiki", "notes/target.md",
                       frontmatter={"type": "note", "title": "Target", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.write_page(repo, "wiki", "notes/linker.md",
                       frontmatter={"type": "note", "title": "Linker", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)},
                       body="See [[target]] for details.")
    support.rebuild_index(conn, repo)

    findings = checks.check_orphans(conn)

    subjects = [f["subject"] for f in findings]
    assert "wiki/notes/target.md" not in subjects
    # "linker.md" itself has no inbound links either — it is the OTHER, expected orphan here.
    assert "wiki/notes/linker.md" in subjects


def test_orphan_exemption_an_entity_page_with_zero_inbound_links_fires_nothing(conn, repo):
    """An exempted page type fires nothing — the exemption list is code with stated reasons, and
    this fixture pins it."""
    support.write_page(repo, "wiki", "entities/acme-corp.md",
                       frontmatter={"type": "entity", "title": "Acme Corp",
                                   "entity": ["acme-corp"], "status": "developing",
                                   "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)

    assert checks.check_orphans(conn) == []


def test_orphan_population_excludes_zones_outside_knowledge(conn, repo):
    """The population is `zone = 'wiki'` — an `sources/` page with no inbound link is a
    different fact (raw source material), never this check's concern."""
    support.write_page(repo, "sources", "general/some-source.md",
                       frontmatter={"type": "source", "title": "Some Source", "entity": []})
    support.rebuild_index(conn, repo)

    assert checks.check_orphans(conn) == []





# ── aging seeds ─────────────────────────────────────────────────────────────────────────────────
def test_aging_seed_fires_past_the_threshold_for_developing(conn, repo):
    support.write_page(repo, "wiki", "onboarding/new-hire-draft.md",
                       frontmatter={"type": "note", "title": "New Hire Draft", "entity": [],
                                   "status": "developing", "updated": _days_ago(46)})
    support.rebuild_index(conn, repo)

    findings = checks.check_aging_seeds(conn, threshold_days=30)

    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == checks.CHECK_AGING_SEED
    assert f["severity"] == "warn"
    assert f["subject"] == "wiki/onboarding/new-hire-draft.md"
    assert f"developing, updated {_days_ago(46)}, 46 days ago (threshold 30)" in f["detail"]
    # the action used to hand the steward a pasteable `brain_propose(target=…)`. That tool is
    # gone, and a message naming a command that does not exist is worse than no command — a
    # message containing a command is an executable promise. What the action owes instead is a
    # judgement to make, stated as one.
    assert "brain_propose" not in f["suggested_action"]
    assert "no command runs itself" in f["suggested_action"]


def test_aging_seed_fires_past_the_threshold_for_the_seed_status_too(conn, repo):
    support.write_page(repo, "wiki", "notes/early-stub.md",
                       frontmatter={"type": "note", "title": "Early Stub", "entity": [],
                                   "status": "seed", "updated": _days_ago(31)})
    support.rebuild_index(conn, repo)

    findings = checks.check_aging_seeds(conn, threshold_days=30)

    assert len(findings) == 1
    assert findings[0]["detail"].startswith("seed, updated")


def test_aging_seed_the_benign_twin_a_fresh_seed_fires_nothing(conn, repo):
    support.write_page(repo, "wiki", "notes/new-stub.md",
                       frontmatter={"type": "note", "title": "New Stub", "entity": [],
                                   "status": "developing", "updated": _days_ago(5)})
    support.rebuild_index(conn, repo)

    assert checks.check_aging_seeds(conn, threshold_days=30) == []


def test_aging_seed_ignores_canonical_pages_however_old(conn, repo):
    support.write_page(repo, "wiki", "product/old-review.md",
                       frontmatter={"type": "note", "title": "Old Canon", "entity": [],
                                   "status": "canonical", "updated": _days_ago(400)})
    support.rebuild_index(conn, repo)

    assert checks.check_aging_seeds(conn, threshold_days=30) == []



def test_aging_seed_a_malformed_updated_value_is_skipped_and_counted_not_crashed(conn, repo):
    support.write_page(repo, "wiki", "onboarding/new-hire-draft.md",
                       frontmatter={"type": "note", "title": "New Hire Draft", "entity": [],
                                   "status": "developing", "updated": _days_ago(46)})
    support.write_page(repo, "wiki", "onboarding/garbled-date.md",
                       frontmatter={"type": "note", "title": "Garbled Date", "entity": [],
                                   "status": "developing", "updated": "TBD"})
    support.rebuild_index(conn, repo)

    stats: dict = {}
    findings = checks.check_aging_seeds(conn, threshold_days=30, population_stats=stats)

    assert [f["subject"] for f in findings] == ["wiki/onboarding/new-hire-draft.md"]
    assert stats["malformed_updated"] == 1



def test_aging_seed_a_calendar_invalid_updated_value_is_skipped_and_counted_not_crashed(conn, repo):
    support.write_page(repo, "wiki", "onboarding/new-hire-draft.md",
                       frontmatter={"type": "note", "title": "New Hire Draft", "entity": [],
                                   "status": "developing", "updated": _days_ago(46)})
    support.write_page(repo, "wiki", "onboarding/garbled-date.md",
                       frontmatter={"type": "note", "title": "Garbled Date", "entity": [],
                                   "status": "developing", "updated": "2026-13-40"})
    support.rebuild_index(conn, repo)

    stats: dict = {}
    findings = checks.check_aging_seeds(conn, threshold_days=30, population_stats=stats)

    assert [f["subject"] for f in findings] == ["wiki/onboarding/new-hire-draft.md"]
    assert stats["malformed_updated"] == 1

# ── pages anchored to a retired identity ────────────────────────────────────────────────────────
def _merged_pair(repo, *, anchored_note: bool = True):
    """The state an applied `entity-alias` merge leaves behind: the absorbed entity's page stays
    (governance), keeps its self-anchor (by design — its own history is its own), and declares
    `superseded_by:` naming the survivor. Optionally one NOTE still anchored to the retired id —
    the accumulation this check exists to count."""
    support.write_page(repo, "wiki", "entities/Cofers.md",
                       frontmatter={"type": "entity", "title": "Cofers", "entity": ["cofers"],
                                   "status": "developing", "updated": _days_ago(1)})
    support.write_page(repo, "wiki", "entities/Cofers Holdings.md",
                       frontmatter={"type": "entity", "title": "Cofers Holdings",
                                   "entity": ["cofers-holdings"], "status": "developing",
                                   "superseded_by": "[[Cofers]]", "updated": _days_ago(1)})
    if anchored_note:
        support.write_page(repo, "wiki", "notes/late-capture.md",
                           frontmatter={"type": "note", "title": "Late Capture",
                                       "entity": ["cofers-holdings"], "status": "developing",
                                       "updated": _days_ago(1)})


def test_a_page_anchored_to_a_retired_identity_is_reported(conn, repo):
    """The residual an applied merge cannot sweep up: the absorbed id stays REGISTERED (the
    absorbed page stays by governance and the contract linter refuses an alias naming an existing
    page), so a capture filed later spelling that name anchors to the retired identity — and the
    repair loop can never re-propose the pair (`content_key` is permanent). Until the filing-time
    fix lands, this count is the only place the accumulation is visible."""
    _merged_pair(repo)
    support.rebuild_index(conn, repo)

    findings = checks.check_anchored_to_superseded_entity(conn)

    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == checks.CHECK_ANCHORED_TO_SUPERSEDED_ENTITY
    assert f["severity"] == "info"
    assert f["subject"] == "wiki/notes/late-capture.md"
    assert "cofers-holdings" in f["detail"]
    assert "superseded" in f["detail"]


def test_the_absorbed_pages_own_self_anchor_never_fires_this_check(conn, repo):
    """**The population rule that keeps the baseline at zero.** The absorbed page keeps its
    self-anchor forever, BY DESIGN (its own history is its own), and its view keeps declaring the
    id as a member set of one — so a check whose predicate were only "anchored to a superseded id"
    would report two permanent, unfixable findings per merge, forever: exactly the disease this
    loop exists to end. The entity zone and the machine zones are outside the population, and the
    moment a merge lands the count is exactly zero."""
    _merged_pair(repo, anchored_note=False)
    support.write_page(repo, "views", "cofers-holdings.md",
                       frontmatter={"type": "view", "title": "Cofers Holdings",
                                   "entity": ["cofers-holdings"]},
                       body="A derived rollup.")
    support.rebuild_index(conn, repo)

    assert checks.check_anchored_to_superseded_entity(conn) == []


def test_a_page_anchored_to_a_LIVE_identity_fires_nothing(conn, repo):
    """The benign twin: anchoring is the system working. Only a superseded id makes an anchor a
    finding."""
    support.write_page(repo, "wiki", "entities/Acme.md",
                       frontmatter={"type": "entity", "title": "Acme", "entity": ["acme"],
                                   "status": "developing", "updated": _days_ago(1)})
    support.write_page(repo, "wiki", "notes/ordinary.md",
                       frontmatter={"type": "note", "title": "Ordinary", "entity": ["acme"],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)

    assert checks.check_anchored_to_superseded_entity(conn) == []


def test_a_page_anchored_to_both_a_live_and_a_retired_id_names_only_the_retired_one(conn, repo):
    _merged_pair(repo, anchored_note=False)
    support.write_page(repo, "wiki", "notes/both.md",
                       frontmatter={"type": "note", "title": "Both",
                                   "entity": ["cofers", "cofers-holdings"],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)

    findings = checks.check_anchored_to_superseded_entity(conn)

    assert [f["subject"] for f in findings] == ["wiki/notes/both.md"]
    assert "cofers-holdings" in findings[0]["detail"]
    assert "'cofers'" not in findings[0]["detail"]

