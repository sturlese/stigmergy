"""`digest.render` — pure text assembly, no DB, no clock: every branch of the body, exercised
against synthetic section dicts. Determinism (same inputs -> identical body) and the honest empty
states live here.
"""
import datetime

from stigmergy.digest.render import build_body

SINCE = datetime.datetime(2026, 7, 24, tzinfo=datetime.UTC)
UNTIL = datetime.datetime(2026, 7, 31, tzinfo=datetime.UTC)


def _health_never_run() -> dict:
    return {"state": "never_run"}


def _health_stale(*, last_run_date, days_before_window) -> dict:
    return {"state": "stale", "last_run_date": last_run_date,
            "days_before_window": days_before_window}


def _health_ok(*, run_date, sla=0, warn=0, info=0, checks_by_severity=None,
              model_passes_incomplete=()) -> dict:
    return {"state": "ok", "run_date": run_date, "total": sla + warn + info,
            "counts_by_severity": {"sla": sla, "warn": warn, "info": info},
            "checks_by_severity": checks_by_severity or {},
            "model_passes_incomplete": list(model_passes_incomplete)}


def _deltas(*, pages_count=0, titles=None, entities=0) -> dict:
    return {"pages_filed_count": pages_count, "pages_filed_titles": titles or [],
            "entities_born_count": entities}


# ── determinism ─────────────────────────────────────────────────────────────────────────────────
def test_same_inputs_produce_a_byte_identical_body():
    health = _health_ok(run_date=datetime.date(2026, 7, 31), warn=1,
                        checks_by_severity={"warn": {"stale-view": 1}})
    deltas = _deltas(pages_count=1, titles=["A Page"], entities=1)

    first = build_body(since=SINCE, until=UNTIL, health=health, deltas=deltas)
    second = build_body(since=SINCE, until=UNTIL, health=health, deltas=deltas)
    assert first == second


def test_header_names_the_window():
    body = build_body(since=SINCE, until=UNTIL, health=_health_never_run(),
                      deltas=_deltas())
    assert body.startswith("*Stigmergy digest — 2026-07-24 to 2026-07-31*")


# ── corpus health ───────────────────────────────────────────────────────────────────────────────
def test_health_never_run():
    body = build_body(since=SINCE, until=UNTIL, health=_health_never_run(),
                      deltas=_deltas())
    assert ("No gardener run has ever completed — this section will show corpus health once "
           "`stigmergy-gardener` runs at least once.") in body


def test_health_stale_run_predates_window():
    health = _health_stale(last_run_date=datetime.date(2026, 7, 20), days_before_window=4)
    body = build_body(since=SINCE, until=UNTIL, health=health,
                      deltas=_deltas())
    assert ("No gardener run in this window; last run 2026-07-20 (4 days before this window "
           "started). Run `stigmergy-gardener` to refresh it.") in body


def test_health_stale_run_singular_day():
    health = _health_stale(last_run_date=datetime.date(2026, 7, 23), days_before_window=1)
    body = build_body(since=SINCE, until=UNTIL, health=health,
                      deltas=_deltas())
    assert "(1 day before this window started)" in body
    assert "1 days" not in body


def test_health_zero_findings_this_run():
    health = _health_ok(run_date=datetime.date(2026, 7, 31))
    body = build_body(since=SINCE, until=UNTIL, health=health,
                      deltas=_deltas())
    assert ("Latest gardener run: 2026-07-31 — 0 findings: every check came back clean") in body
    # no per-severity bullets when the whole run is clean
    assert "• sla:" not in body
    assert "• warn:" not in body
    assert "• info:" not in body


def test_health_pluralizes_its_finding_count_like_every_sibling_count_in_this_body():
    """**Old spelling: `"N finding(s)"`, both places** — the two health-section counts were the
    last "(s)" in a body whose every other count (`_plural(days, 'day')`, `_plural(n, 'page')`,
    `"birth"/"births"`) is properly pluralized. `_plural` already lived in this module; the health
    section simply never used it. The four assertions in this file that pinned the parenthesized
    spelling were updated with this change — it is the contract, and this is the decision.

    Slack is where a human reads this, so the seam between "1 finding" and "1 finding(s)" is the
    difference between a system that writes and one that fills in a template.
    """
    one = build_body(since=SINCE, until=UNTIL, deltas=_deltas(),
                     health=_health_ok(run_date=datetime.date(2026, 7, 31), warn=1,
                                       checks_by_severity={"warn": {"stale-view": 1}}))
    assert "— 1 finding: 0 sla, 1 warn, 0 info" in one
    assert "finding(s)" not in one

    two = build_body(since=SINCE, until=UNTIL, deltas=_deltas(),
                     health=_health_ok(run_date=datetime.date(2026, 7, 31), warn=2,
                                       checks_by_severity={"warn": {"stale-view": 2}}))
    assert "— 2 findings: 0 sla, 2 warn, 0 info" in two

    # the clean-run branch is the second count, and 0 is plural
    clean = build_body(since=SINCE, until=UNTIL, deltas=_deltas(),
                       health=_health_ok(run_date=datetime.date(2026, 7, 31)))
    assert "— 0 findings: every check came back clean" in clean
    assert "finding(s)" not in clean


def test_health_populated_run_breaks_down_sla_and_warn_by_check_never_info():
    health = _health_ok(run_date=datetime.date(2026, 7, 31), sla=2, warn=5, info=14,
                        checks_by_severity={
                            "sla": {"contradiction-sla-open": 1, "contradiction-sla-orphaned": 1},
                            "warn": {"stale-view": 3, "aging-seed": 2}})
    body = build_body(since=SINCE, until=UNTIL, health=health,
                      deltas=_deltas())
    assert "Latest gardener run: 2026-07-31 — 21 findings: 2 sla, 5 warn, 14 info" in body
    assert "• sla: contradiction-sla-open (1), contradiction-sla-orphaned (1)" in body
    assert "• warn: aging-seed (2), stale-view (3)" in body
    assert "• info: 14 (full breakdown: `stigmergy-gardener`)" in body


def test_health_omits_a_severity_bullet_line_when_that_severity_is_zero_but_others_are_not():
    health = _health_ok(run_date=datetime.date(2026, 7, 31), warn=3,
                        checks_by_severity={"warn": {"stale-view": 3}})
    body = build_body(since=SINCE, until=UNTIL, health=health,
                      deltas=_deltas())
    assert "0 sla, 3 warn, 0 info" in body
    assert "• sla:" not in body
    assert "• warn: stale-view (3)" in body
    assert "• info:" not in body


# ── a run whose model sweep failed still has complete, trustworthy deterministic findings
# (status='partial') — the reader must not read a quiet findings section as "the sweep found
# nothing" when it may instead mean "the sweep did not complete" ────────────────────────────────
def test_health_notes_an_incomplete_sweep_alongside_populated_findings():
    health = _health_ok(run_date=datetime.date(2026, 7, 31), warn=3,
                        checks_by_severity={"warn": {"stale-view": 3}},
                        model_passes_incomplete=["sweep"])
    body = build_body(since=SINCE, until=UNTIL, health=health,
                      deltas=_deltas())
    assert "Latest gardener run: 2026-07-31 — 3 findings" in body
    assert "did not complete that run: sweep" in body


def test_health_notes_an_incomplete_sweep_even_with_zero_findings():
    """The note must not depend on `total > 0` — a run can have zero deterministic findings AND a
    failed sweep at the same time, and a reader deserves the same honesty either way."""
    health = _health_ok(run_date=datetime.date(2026, 7, 31),
                        model_passes_incomplete=["empty_body", "sweep"])
    body = build_body(since=SINCE, until=UNTIL, health=health,
                      deltas=_deltas())
    assert "0 findings: every check came back clean" in body
    assert "did not complete that run: empty_body, sweep" in body, (
        "the line must NAME the passes, exactly as the terminal report does")


def test_health_the_benign_twin_a_completed_sweep_prints_no_note():
    health = _health_ok(run_date=datetime.date(2026, 7, 31), warn=1,
                        checks_by_severity={"warn": {"stale-view": 1}})
    body = build_body(since=SINCE, until=UNTIL, health=health,
                      deltas=_deltas())
    assert "did not complete" not in body


# ── corpus deltas ───────────────────────────────────────────────────────────────────────────────
# "N entities born" is exact (ADR 044): `entities_born_count` sums what the window's FILINGS
# wrote, and the librarian is the only writer of an entity page — so every counted birth is a page
# that exists. It is still a COUNT and never a list of names: the report carries the names, but a
# digest that named them would be publishing identities past the destination channel's audiences.
def test_deltas_populated():
    deltas = _deltas(pages_count=3, titles=["Q3 Pricing Floor", "Renewal Terms", "Beta Pilot"],
                     entities=1)
    body = build_body(since=SINCE, until=UNTIL, health=_health_never_run(),
                      deltas=deltas)
    assert ('• 3 pages filed — "Q3 Pricing Floor", "Renewal Terms", "Beta Pilot"') in body
    assert "• 1 entity born" in body
    assert "approved" not in body


def test_deltas_singular_page_for_exactly_one():
    """OLD BEHAVIOUR: `• 1 pages filed`. The pages clause hard-coded the plural while every
    sibling clause in this module is pluralized — `_plural` sits twelve lines above it, the
    stale-run clause uses it, and the entity clause has its own dedicated plural test. A digest
    for a quiet week posted the ungrammatical line next to a correctly singular one."""
    body = build_body(since=SINCE, until=UNTIL, health=_health_never_run(),
                      deltas=_deltas(pages_count=1, titles=["Q3 Pricing Floor"], entities=1))
    assert '• 1 page filed — "Q3 Pricing Floor"' in body
    assert "• 1 entity born" in body


def test_deltas_zero_activity():
    body = build_body(since=SINCE, until=UNTIL, health=_health_never_run(),
                      deltas=_deltas())
    assert "• 0 pages filed" in body
    assert "• 0 entities born" in body


def test_deltas_plural_entities_for_more_than_one():
    body = build_body(since=SINCE, until=UNTIL, health=_health_never_run(),
                      deltas=_deltas(entities=2))
    assert "• 2 entities born" in body


# ── Slack mrkdwn discipline ─────────────────────────────────────────────────────────────────────
def test_no_commonmark_headings_or_bullets_anywhere_in_the_body():
    body = build_body(since=SINCE, until=UNTIL,
                      health=_health_ok(run_date=datetime.date(2026, 7, 31), warn=1,
                                       checks_by_severity={"warn": {"stale-view": 1}}),
                      deltas=_deltas(pages_count=1, titles=["T"], entities=1))
    for line in body.splitlines():
        assert not line.lstrip().startswith("#"), f"a literal CommonMark heading leaked: {line!r}"
        assert not line.lstrip().startswith("- "), f"a literal CommonMark bullet leaked: {line!r}"
    assert "•" in body


def test_corpus_derived_text_is_mrkdwn_escaped():
    """A page title or an area label is client-generated text (`slack.mrkdwn.escape_mrkdwn`'s own
    definition) — an injected `<`/`>`/`&` must never reach the posted body unescaped."""
    deltas = _deltas(pages_count=1, titles=['<script>&"evil"</script>'], entities=0)
    body = build_body(since=SINCE, until=UNTIL, health=_health_never_run(),
                      deltas=deltas)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


# ── --dry-run byte-identity is structural ───────────────────────────────────────────────────────
def test_build_body_never_includes_a_dry_run_wrapper_of_its_own():
    """The two marker lines live OUTSIDE this function entirely (`cli.py`'s own job) — a body this
    function returns must never itself mention "dry run" or carry a marker line, which is what
    makes byte-identity a property of THIS function's return value rather than something `cli.py`
    has to maintain by discipline."""
    body = build_body(since=SINCE, until=UNTIL, health=_health_never_run(),
                      deltas=_deltas())
    assert "dry run" not in body.lower()
    assert "---" not in body
