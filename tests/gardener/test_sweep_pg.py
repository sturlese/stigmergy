"""`gardener.sweep.select_pages`/`previous_run_watermark` against real Postgres: "changed since
watermark" resolution, the rotating sample, and the exclusion counters — the DB half
`test_sweep.py`'s pure tests cannot exercise (no `capture_queue`/`pages_index`/`job_runs` there
at all).
"""
import datetime

from stigmergy.gardener import sweep
from tests.gardener import support


def _file_page(conn, repo, relpath: str, *, body: str = "") -> str:
    path = support.write_page(repo, "wiki", relpath,
                              frontmatter={"type": "note", "title": relpath, "entity": [],
                                          "status": "developing", "updated": "2026-07-01"},
                              body=body)
    support.rebuild_index(conn, repo)
    return path


# ── "changed since watermark" resolution ─────────────────────────────────────────────────────
def test_changed_is_every_page_resolved_from_a_filed_capture_since_none(conn, repo):
    """`since=None` (a genuine first run, `previous_run_watermark`'s own "no prior run" case):
    every currently-filed, currently-indexed page counts as changed — there is no earlier
    baseline to compare against."""
    filed = _file_page(conn, repo, "notes/filed.md", body="filed body")
    unfiled = _file_page(conn, repo, "notes/unfiled.md", body="unfiled body")
    support.seed_filed_capture(conn, result_ref=f"{filed}@sha0")

    changed, sampled, stats = sweep.select_pages(conn, since=None, sample_size=10,
                                                 sample_offset=0, changed_ceiling=30)

    assert [p["path"] for p in changed] == [filed]
    assert "filed body" in changed[0]["body"]
    assert [p["path"] for p in sampled] == [unfiled]
    assert stats["unparsed_result_ref"] == 0
    assert stats["changed_page_not_indexed"] == 0


def test_changed_excludes_a_filing_older_than_the_given_since(conn, repo):
    p = _file_page(conn, repo, "notes/old.md")
    support.seed_filed_capture(conn, result_ref=f"{p}@sha0", finished_days_ago=10)

    recent_since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)
    changed, _sampled, _stats = sweep.select_pages(conn, since=recent_since, sample_size=10,
                                                    sample_offset=0, changed_ceiling=30)
    assert changed == []

    old_since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=20)
    changed2, _sampled2, _stats2 = sweep.select_pages(conn, since=old_since, sample_size=10,
                                                       sample_offset=0, changed_ceiling=30)
    assert [pg["path"] for pg in changed2] == [p]


def test_changed_counts_an_unparseable_result_ref_never_guesses_at_it(conn, repo):
    _file_page(conn, repo, "notes/x.md")
    support.seed_filed_capture(conn, result_ref="not-a-parseable-ref")

    changed, _sampled, stats = sweep.select_pages(conn, since=None, sample_size=10,
                                                   sample_offset=0, changed_ceiling=30)

    assert changed == []
    assert stats["unparsed_result_ref"] == 1


def test_changed_counts_a_filing_whose_page_is_no_longer_indexed(conn, repo):
    support.seed_filed_capture(conn, result_ref="wiki/notes/gone.md@sha0")

    changed, _sampled, stats = sweep.select_pages(conn, since=None, sample_size=10,
                                                   sample_offset=0, changed_ceiling=30)

    assert changed == []
    assert stats["changed_page_not_indexed"] == 1


def test_changed_deduplicates_a_page_filed_more_than_once(conn, repo):
    p = _file_page(conn, repo, "notes/refiled.md")
    support.seed_filed_capture(conn, result_ref=f"{p}@sha0")
    support.seed_filed_capture(conn, result_ref=f"{p}@sha1")

    changed, _sampled, _stats = sweep.select_pages(conn, since=None, sample_size=10,
                                                    sample_offset=0, changed_ceiling=30)

    assert [pg["path"] for pg in changed] == [p]


# ── the rotating sample ───────────────────────────────────────────────────────────────────────
def test_sample_excludes_pages_already_counted_as_changed(conn, repo):
    changed_page = _file_page(conn, repo, "notes/changed.md")
    _file_page(conn, repo, "notes/unchanged.md")
    support.seed_filed_capture(conn, result_ref=f"{changed_page}@sha0")

    _changed, sampled, _stats = sweep.select_pages(conn, since=None, sample_size=10,
                                                    sample_offset=0, changed_ceiling=30)

    assert changed_page not in {p["path"] for p in sampled}


def test_sample_is_bounded_by_sample_size(conn, repo):
    for i in range(5):
        _file_page(conn, repo, f"notes/p{i}.md")

    _changed, sampled, _stats = sweep.select_pages(conn, since=None, sample_size=2,
                                                    sample_offset=0, changed_ceiling=30)

    assert len(sampled) == 2


def test_sample_size_zero_yields_no_sampled_pages(conn, repo):
    _file_page(conn, repo, "notes/only.md")

    _changed, sampled, stats = sweep.select_pages(conn, since=None, sample_size=0,
                                                   sample_offset=0, changed_ceiling=30)

    assert sampled == []
    assert stats["next_sample_offset"] == 0


def test_consecutive_calls_rotate_through_disjoint_pages(conn, repo):
    """"a rotating sample" — two consecutive runs (offset chained through `next_sample_offset`,
    exactly as `run.py`'s own `_run_sweep_pass` chains them across REAL runs) must not sample the
    SAME pages twice before every page has had a turn."""
    for i in range(4):
        _file_page(conn, repo, f"notes/p{i}.md")

    _changed1, sampled1, stats1 = sweep.select_pages(conn, since=None, sample_size=2,
                                                      sample_offset=0, changed_ceiling=30)
    _changed2, sampled2, _stats2 = sweep.select_pages(
        conn, since=None, sample_size=2, sample_offset=stats1["next_sample_offset"], changed_ceiling=30)

    paths1 = {p["path"] for p in sampled1}
    paths2 = {p["path"] for p in sampled2}
    assert len(paths1) == 2
    assert len(paths2) == 2
    assert paths1.isdisjoint(paths2)
    assert paths1 | paths2 == {f"wiki/notes/p{i}.md" for i in range(4)}


def test_the_rotation_wraps_around_when_the_offset_runs_past_the_end(conn, repo):
    paths = [_file_page(conn, repo, f"notes/w{i}.md") for i in range(3)]

    # offset=2 into a 3-page pool, sample_size=2: page[2], then WRAPS to page[0].
    _changed, sampled, stats = sweep.select_pages(conn, since=None, sample_size=2,
                                                   sample_offset=2, changed_ceiling=30)

    assert [p["path"] for p in sampled] == [paths[2], paths[0]]
    assert stats["next_sample_offset"] == 1   # (2 + 2) % 3


def test_an_offset_past_the_current_total_still_resolves_via_modulo(conn, repo):
    """The corpus can SHRINK between runs (a page superseded/removed) — an offset computed
    against a larger pool must not crash or go out of range against a smaller one now."""
    paths = [_file_page(conn, repo, f"notes/m{i}.md") for i in range(2)]

    _changed, sampled, _stats = sweep.select_pages(conn, since=None, sample_size=1,
                                                    sample_offset=99, changed_ceiling=30)
    assert sampled[0]["path"] in paths


def test_an_empty_corpus_selects_nothing_and_never_divides_by_zero(conn, repo):
    # `pages_index` is NOT cleared between tests by `support.clean` (only `rebuild_index` — which
    # drops and recreates the whole table — actually resets it, `support.py`'s own module
    # docstring), and `index_build.rebuild` itself REFUSES a truly page-less repo
    # (`EmptyCorpusError`) — so a direct delete is the only way to force this table into the
    # genuinely empty state this test is actually about, rather than inheriting whatever the
    # previous test in this session happened to leave behind.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pages_index")

    changed, sampled, stats = sweep.select_pages(conn, since=None, sample_size=10,
                                                 sample_offset=0, changed_ceiling=30)
    assert changed == []
    assert sampled == []
    assert stats["next_sample_offset"] == 0


# ── previous_run_watermark ───────────────────────────────────────────────────────────────────
def test_previous_run_watermark_with_no_prior_run_is_none_and_zero(conn):
    since, offset = sweep.previous_run_watermark(conn)
    assert since is None
    assert offset == 0


def test_previous_run_watermark_reads_the_latest_ok_runs_started_at_and_offset(conn):
    support.seed_gardener_job_run(conn, stats={"sweep": {"next_sample_offset": 3}},
                                  started_days_ago=2)

    since, offset = sweep.previous_run_watermark(conn)

    assert since is not None
    age = datetime.datetime.now(datetime.UTC) - since
    assert datetime.timedelta(days=1, hours=12) < age < datetime.timedelta(days=2, hours=12)
    assert offset == 3


def test_previous_run_watermark_ignores_a_run_with_no_sweep_stats_at_all(conn):
    """A `job_runs` row written before the sweep existed (no `"sweep"` key at all) must not crash
    this read — `previous_run_watermark` reads `since` and defaults `offset` to 0."""
    support.seed_gardener_job_run(conn, stats={"pages_checked": 5}, started_days_ago=1)

    since, offset = sweep.previous_run_watermark(conn)

    assert since is not None
    assert offset == 0


def test_previous_run_watermark_prefers_stats_selected_at_over_started_at(conn):
    """`started_at` is written at `job_runs` INSERT time — after `select_pages`
    ran, the model call, and the deterministic checks' own commit — so it is always LATER than the
    moment page selection actually happened. `selected_at`, captured immediately before
    `select_pages` runs (`run._run_sweep_pass`), is the honest boundary and must win even when the
    two clearly disagree, as they do here (a `started_at` 1 day ago vs. a `selected_at` fixed 20
    days in the past)."""
    fixed_selected_at = "2020-01-01T00:00:00+00:00"
    support.seed_gardener_job_run(
        conn, stats={"sweep": {"next_sample_offset": 3, "selected_at": fixed_selected_at}},
        started_days_ago=1)

    since, offset = sweep.previous_run_watermark(conn)

    assert since == datetime.datetime.fromisoformat(fixed_selected_at)
    assert offset == 3


def test_previous_run_watermark_ignores_a_more_recent_error_status_run(conn):
    """Only a COMPLETED (`status='ok'`) run is a real watermark — an errored run's own `stats`
    may be partial or absent, and `run_gardener`'s own posture is that an error-status run never
    reaches the point of committing anything trustworthy."""
    support.seed_gardener_job_run(conn, status="ok",
                                  stats={"sweep": {"next_sample_offset": 7}},
                                  started_days_ago=5)
    support.seed_gardener_job_run(conn, status="error", stats={}, started_days_ago=1)

    since, offset = sweep.previous_run_watermark(conn)

    assert offset == 7
    age = datetime.datetime.now(datetime.UTC) - since
    assert age > datetime.timedelta(days=4)


def test_previous_run_watermark_a_failed_sweep_leaves_the_next_runs_since_unchanged(conn):
    """A run whose SWEEP failed commits `status='partial'` (`gardener.run.run_gardener`), never
    `'ok'`; this read (`status='ok'` only) must keep returning the OLDER completed run's own
    watermark, exactly as if the partial run had never happened. A sweep-failed run used to commit
    `'ok'`, and this read would then pick up ITS `next_sample_offset` (99 here) instead of the good
    run's (7) — a week of daily sweep failures under the cron would have silently advanced the
    watermark, daily, past a week of pages nothing had actually judged, while
    `job_runs WHERE status='error'` reported nothing wrong the whole time."""
    support.seed_gardener_job_run(conn, status="ok",
                                  stats={"sweep": {"next_sample_offset": 7}},
                                  started_days_ago=5)
    # the failed-sweep run: committed AFTER the good run, carrying a DIFFERENT, advanced offset
    # the pre-fix code would have wrongly treated as the new baseline.
    support.seed_gardener_job_run(
        conn, status="partial",
        stats={"sweep": {"next_sample_offset": 99, "error": "SweepGarbage"}},
        started_days_ago=1)

    since, offset = sweep.previous_run_watermark(conn)

    assert offset == 7   # the OLDER 'ok' run's offset — the partial run never became a baseline
    age = datetime.datetime.now(datetime.UTC) - since
    assert age > datetime.timedelta(days=4)   # since == the OLD run's started_at


def test_previous_run_watermark_advances_past_a_run_whose_OTHER_model_pass_failed(conn):
    """A run whose editorial sweep SUCCEEDED and whose empty-body pass failed commits
    `status='partial'` — that status is an aggregate over the run's model passes. Reading `'ok'`
    only, this watermark stayed pinned at the last flawless run: `since` never advanced, so
    `select_pages` put every page filed since into ONE unbatched prompt that grew every night
    until it killed the editorial sweep too, and `next_sample_offset` re-judged the same rotating
    sample forever — coverage halting silently, which is the exact failure the second pass was
    added to end, reintroduced on the other one."""
    fixed_selected_at = "2026-01-01T00:00:00+00:00"
    support.seed_gardener_job_run(conn, status="ok",
                                  stats={"sweep": {"next_sample_offset": 7}},
                                  started_days_ago=5)
    support.seed_gardener_job_run(
        conn, status="partial",
        stats={"sweep": {"next_sample_offset": 42, "selected_at": fixed_selected_at, "error": ""},
               "empty_body": {"error": "SweepGarbage"}},
        started_days_ago=1)

    since, offset = sweep.previous_run_watermark(conn)

    assert offset == 42, "the sweep that ran IS the next run's baseline, whatever the other pass did"
    assert since == datetime.datetime.fromisoformat(fixed_selected_at)


def test_previous_run_watermark_skips_a_run_whose_own_sweep_failed_whatever_the_status(conn):
    """The twin, and the property that survives the change above: what disqualifies a baseline is
    the SWEEP's own recorded outcome, never the run's aggregate status. A row that somehow carries
    `status='ok'` beside a failed sweep is still not a baseline."""
    support.seed_gardener_job_run(conn, status="ok",
                                  stats={"sweep": {"next_sample_offset": 7}},
                                  started_days_ago=5)
    support.seed_gardener_job_run(
        conn, status="ok",
        stats={"sweep": {"next_sample_offset": 99, "error": "AgentRunError"}},
        started_days_ago=1)

    _since, offset = sweep.previous_run_watermark(conn)

    assert offset == 7


def test_the_watermark_status_pair_is_the_one_the_stores_completed_run_reads():
    """`('ok', 'partial')` is spelled as a SQL literal in `store._LATEST_COMPLETED_RUN` and as a
    Python list in `sweep.WATERMARK_STATUSES` — two different questions that happen to share one
    pair, which #92's comment rewrite turned from a declared divergence into an undeclared
    duplication. Pinned equal, so a status added to one cannot silently miss the other."""
    from stigmergy.gardener import store as gardener_store

    assert sweep.WATERMARK_STATUSES == ["ok", "partial"]
    for status in sweep.WATERMARK_STATUSES:
        assert f"'{status}'" in gardener_store._LATEST_COMPLETED_RUN


# ── the changed half is bounded, and the overflow falls to the rotation ────────────────────────
def test_changed_is_capped_at_the_ceiling_keeping_the_newest(conn, repo):
    """Red before the fix: the editorial sweep put EVERY page filed since the watermark into one
    unbatched prompt — on a first run or after a cron outage, the whole corpus. The ceiling keeps
    the NEWEST filings (the pages most likely to contradict the current corpus); the overflow is
    counted, and it is not lost: it joins the unchanged pool, where tonight's sample can pick it
    and the rotation reaches the rest."""
    paths = [_file_page(conn, repo, f"notes/changed-{i}.md", body=f"body {i}") for i in range(5)]
    for age, p in enumerate(reversed(paths)):
        support.seed_filed_capture(conn, result_ref=f"{p}@sha0", finished_days_ago=age)

    changed, sampled, stats = sweep.select_pages(conn, since=None, sample_size=10,
                                                 sample_offset=0, changed_ceiling=2)

    assert [p["path"] for p in changed] == [paths[4], paths[3]], "the newest two"
    assert stats["changed_deferred"] == 3
    # The overflow is in the SAMPLE pool this very run — deferred to the rotation, never dropped.
    assert set(paths[:3]) <= {p["path"] for p in sampled}


def test_a_night_under_the_ceiling_defers_nothing(conn, repo):
    """The benign twin: an ordinary night's handful of filings is untouched, and the counter says
    zero rather than being absent — a bound that binds must be tellable from one that never ran."""
    p = _file_page(conn, repo, "notes/only.md")
    support.seed_filed_capture(conn, result_ref=f"{p}@sha0")

    changed, _sampled, stats = sweep.select_pages(conn, since=None, sample_size=10,
                                                  sample_offset=0, changed_ceiling=30)

    assert [c["path"] for c in changed] == [p]
    assert stats["changed_deferred"] == 0
