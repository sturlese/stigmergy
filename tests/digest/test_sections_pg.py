"""`digest.sections` against real Postgres — the DB half of what `test_sections.py`'s pure half
covers for the meeting blind spot. Every gather_* function is windowed on a real clock-injected
boundary (the `capture.retention` clock-injection pattern, several packages over: a fixture is
backdated via SQL, never a wall-clock sleep).

The `sla`-severity findings seeded below are synthetic. No live check produces one — both arms of
the contradiction SLA went with the canon lane — but the digest's grouping and rendering are
severity-driven and generic, so they stay worth proving over the shape a check would emit.
"""
import datetime

import pytest

from stigmergy.digest import sections
from stigmergy.gardener import notice
from stigmergy.gardener import schema as gardener_schema
from tests.digest import support

UTC = datetime.UTC


def _days_ago(n: int) -> datetime.datetime:
    return datetime.datetime.now(UTC) - datetime.timedelta(days=n)


SINCE_7D = _days_ago(7)
# every gather_corpus_deltas call needs an explicit upper bound too — a fixed instant safely in
# the future of every fixture row this file backdates via
# `finished_days_ago`/`decided_days_ago`/`created_days_ago`, mirroring `SINCE_7D`'s own "one
# clock-injected boundary, reused everywhere" posture.
UNTIL_NOW = datetime.datetime.now(UTC)


# ── corpus health ───────────────────────────────────────────────────────────────────────────────
def test_corpus_health_never_run(conn):
    assert sections.gather_corpus_health(conn, since=SINCE_7D) == {"state": "never_run"}


def test_corpus_health_stale_run_predates_the_window(conn):
    support.seed_gardener_run(conn, finished_days_ago=10)
    health = sections.gather_corpus_health(conn, since=SINCE_7D)
    assert health["state"] == "stale"
    assert health["last_run_date"] == _days_ago(10).date()
    assert health["days_before_window"] == 3


def test_corpus_health_ok_run_groups_findings_by_severity_and_check(conn):
    support.seed_gardener_run(conn, finished_days_ago=1, findings=[
        {"check": "contradiction-sla-open", "severity": "sla"},
        {"check": "contradiction-sla-orphaned", "severity": "sla"},
        {"check": "stale-view", "severity": "warn"},
        {"check": "stale-view", "severity": "warn"},
        {"check": "aging-seed", "severity": "warn"},
        {"check": "orphan-page", "severity": "info"},
    ])

    health = sections.gather_corpus_health(conn, since=SINCE_7D)

    assert health["state"] == "ok"
    assert health["run_date"] == _days_ago(1).date()
    assert health["total"] == 6
    assert health["counts_by_severity"] == {"sla": 2, "warn": 3, "info": 1}
    assert health["checks_by_severity"]["sla"] == {
        "contradiction-sla-open": 1, "contradiction-sla-orphaned": 1}
    assert health["checks_by_severity"]["warn"] == {"stale-view": 2, "aging-seed": 1}


def test_corpus_health_uses_the_latest_run_never_an_older_one(conn):
    support.seed_gardener_run(conn, finished_days_ago=5, findings=[
        {"check": "stale-view", "severity": "warn"}])
    support.seed_gardener_run(conn, finished_days_ago=1, findings=[])

    health = sections.gather_corpus_health(conn, since=SINCE_7D)

    assert health["state"] == "ok"
    assert health["total"] == 0
    assert health["run_date"] == _days_ago(1).date()


def test_corpus_health_ignores_a_failed_run(conn):
    """`status='error'` rows are never eligible — only a completed (`status='ok'`) run counts, the
    same "findings you can trust" boundary `gardener.sweep.previous_run_watermark` already draws
    for its own read."""
    support.seed_gardener_run(conn, finished_days_ago=1, status="error")
    assert sections.gather_corpus_health(conn, since=SINCE_7D) == {"state": "never_run"}


# ── a run whose model sweep failed commits status='partial', never 'ok' — corpus health must
# still read it (the deterministic findings are complete and trustworthy) and must surface that
# the sweep itself did not complete ─────────────────────────────────────────────────────────────
def test_corpus_health_reads_a_partial_run_a_sweep_failure_must_not_blank_the_section(conn):
    support.seed_gardener_run(conn, finished_days_ago=1, status="partial", findings=[
        {"check": "stale-view", "severity": "warn"}],
        extra_stats={"sweep": {"error": "SweepGarbage"}})

    health = sections.gather_corpus_health(conn, since=SINCE_7D)

    assert health["state"] == "ok"
    assert health["total"] == 1
    assert health["model_passes_incomplete"] == ["sweep"]


def test_corpus_health_the_benign_twin_a_completed_sweep_is_not_flagged_incomplete(conn):
    support.seed_gardener_run(conn, finished_days_ago=1, findings=[
        {"check": "stale-view", "severity": "warn"}],
        extra_stats={"sweep": {"error": ""}})

    health = sections.gather_corpus_health(conn, since=SINCE_7D)

    assert health["model_passes_incomplete"] == []


def test_corpus_health_an_ok_run_with_no_sweep_stats_at_all_is_not_flagged_incomplete(conn):
    """A run written before the sweep existed — or one where the sweep simply had nothing to do —
    carries no `stats.sweep` key at all, and must not be misread as an incomplete sweep."""
    support.seed_gardener_run(conn, finished_days_ago=1, findings=[])

    health = sections.gather_corpus_health(conn, since=SINCE_7D)

    assert health["model_passes_incomplete"] == []


def test_corpus_health_prefers_the_latest_run_when_an_older_ok_run_also_exists(conn):
    """`latest_completed_run` orders by `started_at DESC` across BOTH statuses — a `'partial'` run
    more recent than an `'ok'` one must win, proving the widened `IN ('ok', 'partial')` predicate
    is actually reached end to end through this section, not merely at the store layer."""
    support.seed_gardener_run(conn, finished_days_ago=5, status="ok", findings=[])
    support.seed_gardener_run(conn, finished_days_ago=1, status="partial", findings=[
        {"check": "stale-view", "severity": "warn"}],
        extra_stats={"sweep": {"error": "AgentRunError"}})

    health = sections.gather_corpus_health(conn, since=SINCE_7D)

    assert health["run_date"] == _days_ago(1).date()
    assert health["total"] == 1
    assert health["model_passes_incomplete"] == ["sweep"]


def test_corpus_health_a_partial_run_from_a_NON_sweep_pass_is_not_rendered_clean(conn):
    """Red before the fix: the flag read `stats.sweep.error` alone, so a run committed 'partial'
    because the EMPTY-BODY or the DUPLICATE-IDENTITY pass failed rendered in the weekly digest as
    a clean run — the exact silent-clean-bill failure ADR 024's amendment says this surface exists
    to end, closed in the terminal report and left open one layer up."""
    support.seed_gardener_run(conn, finished_days_ago=1, status="partial", findings=[
        {"check": "stale-view", "severity": "warn"}],
        extra_stats={"sweep": {"error": ""}, "empty_body": {"error": "AgentRunError"}})

    health = sections.gather_corpus_health(conn, since=SINCE_7D)

    assert health["model_passes_incomplete"] == ["empty_body"]


def test_corpus_health_names_every_pass_that_did_not_complete(conn):
    support.seed_gardener_run(conn, finished_days_ago=1, status="partial", findings=[],
        extra_stats={"sweep": {"error": "SweepGarbage"},
                     "duplicate_entity": {"error": "AgentRunError"}})

    health = sections.gather_corpus_health(conn, since=SINCE_7D)

    assert health["model_passes_incomplete"] == ["duplicate_entity", "sweep"]


# ── corpus deltas: pages filed, the meeting blind spot, entities born ───────────────────────────
def test_pages_filed_counts_and_titles_a_single_page_capture(conn, repo):
    path = support.unlabelled_page(repo, "decisions/floor.md", title="Q3 Pricing Floor")
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{path}@sha1", finished_days_ago=1)

    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences=set())

    assert deltas["pages_filed_count"] == 1
    assert deltas["pages_filed_titles"] == ["Q3 Pricing Floor"]


def test_pages_filed_counts_every_page_in_a_meeting_set_not_only_the_meeting_page(conn, repo):
    """The blind spot, end to end: `result_ref` names only the meeting page; the source transcript
    and every decision page must still be counted."""
    source = support.write_page(repo, "sources", "meetings/2026-07-30-standup.md",
                                frontmatter={"type": "source", "title": "Standup Transcript",
                                            "entity": [], "status": "developing",
                                            "updated": "2026-07-30"})
    meeting = support.write_page(repo, "wiki", "meetings/2026-07-30-standup.md",
                                 frontmatter={"type": "meeting", "title": "Standup",
                                             "entity": [], "status": "developing",
                                             "updated": "2026-07-30"})
    decision_a = support.write_page(repo, "wiki", "decisions/a.md",
                                    frontmatter={"type": "note", "title": "Decision A",
                                                "entity": [], "status": "developing",
                                                "updated": "2026-07-30"})
    decision_b = support.write_page(repo, "wiki", "decisions/b.md",
                                    frontmatter={"type": "note", "title": "Decision B",
                                                "entity": [], "status": "developing",
                                                "updated": "2026-07-30"})
    support.rebuild_index(conn, repo)
    report = {"filed_meeting": {"source_pages": [source], "meeting_page": meeting,
                                "decisions": [{"path": decision_a, "anchored_to": "x"},
                                             {"path": decision_b, "anchored_to": "y"}]}}
    support.seed_filed_capture(conn, result_ref=f"{meeting}@sha1", finished_days_ago=1,
                               report=report)

    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences=set())

    assert deltas["pages_filed_count"] == 4
    assert set(deltas["pages_filed_titles"]) == {
        "Standup Transcript", "Standup", "Decision A", "Decision B"}


def test_pages_filed_excludes_a_filing_older_than_the_window(conn, repo):
    path = support.unlabelled_page(repo, "decisions/floor.md", title="Old News")
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{path}@sha1", finished_days_ago=30)

    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences=set())

    assert deltas == {"pages_filed_count": 0, "pages_filed_titles": [],
                      "entities_born_count": 0}


def test_pages_filed_never_double_counts_a_path_filed_twice(conn, repo):
    path = support.unlabelled_page(repo, "decisions/floor.md", title="Pricing Floor")
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{path}@sha1", finished_days_ago=2)
    support.seed_filed_capture(conn, result_ref=f"{path}@sha2", finished_days_ago=1)

    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences=set())

    assert deltas["pages_filed_count"] == 1


def test_pages_filed_excludes_a_filing_at_or_after_until(conn, repo):
    """The upper-bound proof for corpus deltas' own query: a page filed "now"
    (`finished_days_ago=0`) is strictly after `UNTIL_NOW` (module load time) and must not be
    counted. The defect this closes: a run's own watermark used to be `job_runs.started_at`
    (written AFTER this query ran and after the Slack post), so an event landing between "this
    query ran" and "the row committed" fell into NO digest window at all — this run's own query
    never saw it, and the NEXT run's `since` started even later than that."""
    path = support.unlabelled_page(repo, "decisions/late.md", title="Too Late")
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{path}@sha1")   # finished "now" — after UNTIL_NOW

    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences=set())

    assert deltas["pages_filed_count"] == 0


# ── the ACL broadcast scope — a labelled page excluded, then included (package docstring) ───────
def test_a_labelled_page_is_excluded_at_the_empty_audience_default(conn, repo):
    path = support.write_labelled_page(repo, "leadership/steward-cv.md", title="Jordan Reyes CV",
                                       acl=["leadership"])
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{path}@sha1", finished_days_ago=1)

    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences=set())

    assert deltas["pages_filed_count"] == 0
    assert deltas["pages_filed_titles"] == []


def test_the_same_labelled_page_is_included_once_the_channel_carries_its_label(conn, repo):
    path = support.write_labelled_page(repo, "leadership/steward-cv.md", title="Jordan Reyes CV",
                                       acl=["leadership"])
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{path}@sha1", finished_days_ago=1)

    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences={"leadership"})

    assert deltas["pages_filed_count"] == 1
    assert deltas["pages_filed_titles"] == ["Jordan Reyes CV"]


def test_an_unlabelled_page_is_visible_regardless_of_audiences(conn, repo):
    """`acl IS NULL` -> open to everyone (`server.acl.visible`'s own truth table) — the empty
    audience default must not hide a page that carries no label at all."""
    path = support.unlabelled_page(repo, "notes/open.md", title="Open Note")
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{path}@sha1", finished_days_ago=1)

    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences=set())

    assert deltas["pages_filed_count"] == 1


# ── the two fail-closed ACL seams, held to one rule ──────────────────────────────────────────────
# Two packages resolve "which of these paths may this channel see" independently — this one for the
# titles it prints, `gardener.notice` for the SLA wording it posts — and they broadcast to the SAME
# channel. Neither may be provable only for itself: a path nothing indexed cannot be shown to be
# visible, so both must omit it, and a label the audiences do not carry must exclude the page in
# both. The pair is deliberate (each names the other in its own docstring) and this is where the
# shared rule is stated once.
def _digest_visible_paths(conn, paths, audiences):
    return set(sections._visible_pages(conn, paths, audiences=audiences))


def _notice_visible_paths(conn, paths, audiences):
    findings = [{"check": "twin-probe", "severity": gardener_schema.SEVERITY_SLA, "subject": path,
                 "detail": "d", "suggested_action": "a", "_notice_page_paths": [path]}
                for path in paths]
    return notice._visible_page_paths(conn, findings, audiences=audiences)


@pytest.mark.parametrize("visible_paths", [_digest_visible_paths, _notice_visible_paths],
                         ids=["digest.sections", "gardener.notice"])
def test_both_broadcast_acl_seams_omit_an_unindexed_and_a_mislabelled_path(conn, repo,
                                                                          visible_paths):
    labelled = support.write_labelled_page(repo, "leadership/scope.md", title="Scope",
                                           acl=["leadership"])
    open_page = support.unlabelled_page(repo, "notes/open.md", title="Open")
    support.rebuild_index(conn, repo)
    never_indexed = "wiki/notes/never-indexed.md"

    visible = visible_paths(conn, [labelled, open_page, never_indexed], {"finance"})

    assert never_indexed not in visible
    assert labelled not in visible
    # The benign twin, in the same call: a seam that returned nothing at all would satisfy both
    # assertions above and prove no scoping whatsoever.
    assert open_page in visible


# ── entities born — the governed-birth log ──────────────────────────────────────────────────────
def test_entities_born_counts_approved_entity_proposal_decisions_in_window(conn):
    support.seed_entity_approval(conn, created_days_ago=1)
    support.seed_entity_approval(conn, created_days_ago=2)
    # a REJECTED entity proposal is not a birth.
    support.seed_entity_approval(conn, created_days_ago=1, verdict="reject")
    # outside the window.
    support.seed_entity_approval(conn, created_days_ago=30)

    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences=set())

    assert deltas["entities_born_count"] == 2


def test_entities_born_counts_an_approval_the_parked_capture_door_wrote_before_adr_041(conn):
    """The ledger keeps the approvals the retired mint door recorded under `entity-proposal`; a
    digest window straddling the change must count them as the births they were. (Before this
    test the count named one kind only, and a week's digest would have dropped every birth from
    before the deploy.)"""
    from stigmergy.review_kinds import LEGACY_KIND_ENTITY_PROPOSAL, LEGACY_KIND_PARKED_CAPTURE
    support.seed_entity_approval(conn, created_days_ago=1)
    support.seed_entity_approval(conn, created_days_ago=2, item_kind=LEGACY_KIND_ENTITY_PROPOSAL)
    # a parked capture's disposition was never a birth, whatever its verdict said.
    support.seed_entity_approval(conn, created_days_ago=1, item_kind=LEGACY_KIND_PARKED_CAPTURE)

    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences=set())

    assert deltas["entities_born_count"] == 2


def test_entities_born_zero_when_nothing_approved(conn):
    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences=set())
    assert deltas["entities_born_count"] == 0


def test_entities_born_excludes_an_approval_at_or_after_until(conn):
    """The same upper-bound proof, for the entities-born query."""
    support.seed_entity_approval(conn)   # created "now" — after UNTIL_NOW

    deltas = sections.gather_corpus_deltas(conn, since=SINCE_7D, until=UNTIL_NOW, audiences=set())

    assert deltas["entities_born_count"] == 0
