"""`gardener.run.run_gardener` — the orchestrator: runs every check, persists a `job_runs` row
plus this run's findings in one transaction, posts the SLA notice when warranted, and returns the
durable, re-fetched result.
"""
import asyncio
import datetime
import os

from pydantic_ai.exceptions import AgentRunError

from stigmergy.gardener import run, schema, sweep
from stigmergy.gardener.errors import GardenerError
from stigmergy.gardener.settings import GardenerSettings
from stigmergy.slack.gateway import FakeSlackGateway
from tests.gardener import support


def _days_ago(n: int) -> str:
    # UTC, never local time: the checks this orchestrator runs age off Postgres's own `now()`/
    # `current_date` (Etc/UTC in this stack), so a fixture backdated from the MACHINE's local
    # calendar day drifts by one during the nightly window where local has already rolled to a new
    # day and UTC has not (e.g. 00:00-02:00 CEST) — an off-by-one age mismatch with nothing wrong
    # in the code. Do not simplify this back to `date.today()`.
    return (datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=n)).isoformat()


def _seed_minimal_corpus(conn, repo) -> None:
    """A small corpus producing exactly one `info` and one `warn` finding — enough to prove
    aggregation/counting without re-testing each check's own logic (that is `test_checks_*`'s
    job). No registered entity at all, so `dead-vocabulary` never adds noise to the count.

    The `warn` comes from `aging-seed` over a long-untouched `developing` page. It used to come
    from `stale-canon` over a `status: canonical` page; that check went with the canon lane, so
    this is the same shape built out of a check that exists."""
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "notes/orphan.md",
                       frontmatter={"type": "note", "title": "Orphan", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.write_page(repo, "wiki", "product/stale.md",
                       frontmatter={"type": "note", "title": "Stale", "entity": [],
                                   "status": "seed", "updated": _days_ago(151)})
    support.rebuild_index(conn, repo)


def _run(conn, repo, *, settings=None, gateway=None, channels_path=""):
    return asyncio.run(run.run_gardener(
        conn, repo=repo, settings=settings or GardenerSettings.from_args(),
        channels_path=channels_path, gateway=gateway))


# ── run + persist + report shape ────────────────────────────────────────────────────────────────
def test_run_gardener_persists_findings_and_a_job_runs_row(conn, repo):
    _seed_minimal_corpus(conn, repo)

    result = _run(conn, repo)

    assert result.run_id > 0
    assert result.pages_checked == 2
    assert result.entities_checked == 0
    # Both pages are orphans (neither links to the other); "product/stale.md" is ALSO an aging seed.
    assert {f["check"] for f in result.findings} == {"orphan-page", "aging-seed"}
    assert len(result.findings) == 3
    assert result.completed_at  # a real timestamp, not the empty-string fallback

    with conn.cursor() as cur:
        cur.execute("SELECT job, status, stats FROM job_runs WHERE id = %s", (result.run_id,))
        job, status, stats = cur.fetchone()
    assert job == schema.JOB_NAME
    assert status == "ok"
    assert stats["findings_total"] == 3
    assert stats["findings_by_severity"] == {"info": 2, "warn": 1}
    assert stats["findings_by_check"] == {"orphan-page": 2, "aging-seed": 1}
    assert stats["pages_checked"] == 2
    assert stats["entities_checked"] == 0


def test_run_gardener_returned_findings_match_what_is_actually_persisted(conn, repo):
    """`store.py`'s own "what a reader sees is never allowed to drift from what is stored" rule,
    proven end to end: the returned findings are the RE-FETCHED rows, not the in-memory list."""
    _seed_minimal_corpus(conn, repo)

    result = _run(conn, repo)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM gardener_findings WHERE run_id = %s", (result.run_id,))
        stored_count = cur.fetchone()[0]
    assert stored_count == len(result.findings)
    assert all(f.get("id") is not None for f in result.findings)


def test_run_gardener_on_a_clean_corpus_persists_zero_findings(conn, repo):
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "entities/nothing.md",
                       frontmatter={"type": "entity", "title": "Nothing", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    assert result.findings == []
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM job_runs WHERE id = %s", (result.run_id,))
        assert cur.fetchone()[0] == "ok"


# ── --repo validation ─────────────────────────────────────────────────────────────────────────
def test_run_gardener_refuses_a_repo_that_is_not_a_directory(conn, tmp_path):
    missing = str(tmp_path / "does-not-exist")
    try:
        _run(conn, missing)
        raise AssertionError("expected GardenerError")
    except GardenerError as ex:
        assert missing in str(ex)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_runs WHERE job = %s", (schema.JOB_NAME,))
        assert cur.fetchone()[0] == 0   # a bad --repo never even opens a job_runs row


# ── a run-level failure still records an honest error row (no partial insert) ─────────────────
def test_run_gardener_a_registry_failure_records_an_error_job_run_and_inserts_no_findings(conn, repo):
    """A malformed `ops/entity-registry.json` is a real, deterministic run-level failure
    (`load_registry` raises `ValueError`/`json.JSONDecodeError`) — used here specifically because
    it fails regardless of what an EARLIER test in this session already did to `pages_index`
    (unlike "query a table that does not exist yet", which only fails on a session's very first
    Postgres-backed test)."""
    os.makedirs(os.path.join(repo, "ops"), exist_ok=True)
    with open(os.path.join(repo, "ops", "entity-registry.json"), "w", encoding="utf-8") as f:
        f.write("{not valid json")
    support.write_page(repo, "wiki", "notes/x.md",
                       frontmatter={"type": "note", "title": "X", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)

    try:
        _run(conn, repo)
        raise AssertionError("expected an exception from the malformed registry")
    except Exception:  # noqa: BLE001 — the exact exception class is json's, not ours to name
        pass

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1",
                   (schema.JOB_NAME,))
        row = cur.fetchone()
        cur.execute("SELECT count(*) FROM gardener_findings")
        findings_count = cur.fetchone()[0]
    assert row is not None
    assert row[0] == "error"
    assert findings_count == 0   # never a partial insert


# ── threshold override via env, end to end ──────────────────────────────────────────────────────
def test_run_gardener_honors_a_threshold_override_read_through_settings(conn, repo, monkeypatch):
    support.write_registry(repo, {
        "acme-corp": {"name": "Acme Corp", "type": "organization", "aliases": []},
    })
    support.write_page(repo, "wiki", "product/stale.md",
                       frontmatter={"type": "note", "title": "Stale", "entity": [],
                                   "status": "seed", "updated": _days_ago(20)})
    support.rebuild_index(conn, repo)

    default_settings = GardenerSettings.from_args()
    default_result = _run(conn, repo, settings=default_settings)
    assert "aging-seed" not in {f["check"] for f in default_result.findings}

    monkeypatch.setenv("STIGMERGY_GARDENER_AGING_SEED_DAYS", "10")
    tightened_settings = GardenerSettings.from_args()
    tightened_result = _run(conn, repo, settings=tightened_settings)
    assert "aging-seed" in {f["check"] for f in tightened_result.findings}


def test_run_gardener_anchor_concentration_honors_its_own_env_override(conn, repo, monkeypatch):
    """The full chain: env var -> `GardenerSettings.concentration_share` ->
    `check_anchor_concentration` -> the finding actually firing, through `run_gardener` end to
    end."""
    support.write_registry(repo, {
        "acme-corp": {"name": "Acme Corp", "type": "organization", "aliases": []},
        "beta-robotics": {"name": "Beta Robotics", "type": "organization", "aliases": []},
    })
    for i, entity in enumerate(["acme-corp", "beta-robotics", "acme-corp", "beta-robotics"]):
        p = support.write_page(repo, "wiki", f"notes/anchor-{i}.md",
                               frontmatter={"type": "note", "title": f"Anchor {i}",
                                           "entity": [entity], "status": "developing",
                                           "updated": _days_ago(1)})
        support.rebuild_index(conn, repo)
        support.seed_filed_capture(conn, result_ref=f"{p}@sha{i}")

    default_settings = GardenerSettings.from_args()
    default_result = _run(conn, repo, settings=default_settings)
    assert "anchor-concentration" not in {f["check"] for f in default_result.findings}

    monkeypatch.setenv("STIGMERGY_GARDENER_CONCENTRATION_SHARE", "0.4")
    monkeypatch.setenv("STIGMERGY_GARDENER_CONCENTRATION_WINDOW", "4")
    tightened_settings = GardenerSettings.from_args()
    tightened_result = _run(conn, repo, settings=tightened_settings)
    concentration = [f for f in tightened_result.findings if f["check"] == "anchor-concentration"]
    assert len(concentration) == 1
    assert concentration[0]["subject"] == "acme-corp"



def test_run_gardener_posts_nothing_when_only_info_and_warn_findings_fire(conn, repo):
    _seed_minimal_corpus(conn, repo)   # produces info + warn, no sla
    gateway = FakeSlackGateway()
    settings = GardenerSettings(digest_channel_id="C0123456789")

    result = _run(conn, repo, settings=settings, gateway=gateway)

    assert result.notice_posted is False
    assert result.notice_error == ""
    assert gateway.posted == []


def test_run_gardener_records_a_notice_error_but_still_returns_the_full_report(conn, repo, monkeypatch):
    """The findings are already committed before the notice is attempted — a missing bot token
    (`gateway=None`) must never make the run withhold an already-successful result."""
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "notes/x.md",
                       frontmatter={"type": "note", "title": "X", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)
    support.force_one_sla_finding(monkeypatch)
    settings = GardenerSettings(digest_channel_id="C0123456789")

    result = _run(conn, repo, settings=settings, gateway=None)

    assert result.notice_posted is False
    assert "SLACK_BOT_TOKEN" in result.notice_error
    assert any(f["severity"] == "sla" for f in result.findings)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM gardener_findings WHERE run_id = %s", (result.run_id,))
        assert cur.fetchone()[0] == len(result.findings)   # persisted regardless


def test_run_gardener_records_a_notice_error_when_the_channel_is_unset(conn, repo, monkeypatch):
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "notes/x.md",
                       frontmatter={"type": "note", "title": "X", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)
    support.force_one_sla_finding(monkeypatch)
    gateway = FakeSlackGateway()

    result = _run(conn, repo, settings=GardenerSettings(digest_channel_id=""), gateway=gateway)

    assert result.notice_posted is False
    assert "STIGMERGY_DIGEST_CHANNEL_ID" in result.notice_error
    assert gateway.posted == []



def test_run_gardener_the_benign_twin_a_channel_carrying_the_label_posts_full_detail(conn, repo, monkeypatch):
    """The identical scenario as above, this time with a channel that DOES carry `leadership` —
    the benign twin every defense needs, because a redaction test alone measures the gate's
    sensitivity and never its specificity."""
    support.write_registry(repo, {})
    path = support.write_page(repo, "wiki", "leadership/pricing-floor.md",
                              frontmatter={"type": "note", "title": "t", "entity": [],
                                          "status": "developing", "updated": _days_ago(1),
                                          "acl": ["leadership"]})
    support.rebuild_index(conn, repo)
    support.force_one_sla_finding(monkeypatch, subject=path)
    channels_path = support.write_channels_file(repo, {"C0123456789": ["leadership"]})
    gateway = FakeSlackGateway()
    settings = GardenerSettings(digest_channel_id="C0123456789")

    result = _run(conn, repo, settings=settings, gateway=gateway, channels_path=channels_path)

    assert result.notice_posted is True
    text = gateway.posted[0].text
    assert path in text
    assert "redacted" not in text


# ── a malformed channels file must not fail a run with nothing to post, and must fail CLEANLY
# (report intact, `notice_error` set) when it does ──────────────────────────────────────────────
def test_run_gardener_survives_a_malformed_channels_file_when_nothing_needs_to_post(conn, repo):
    """`channels.channel_audiences` used to be resolved unconditionally, before the SLA
    short-circuit — an info/warn-only run (which never touches Slack at all) failed anyway on a
    malformed `ops/slack-channels.json`, contradicting `post_sla_notice`'s own docstring ("an
    info/warn-only run never needs Slack configured"). No `digest_channel_id`/gateway configured
    either, on purpose — this run needs NONE of it."""
    _seed_minimal_corpus(conn, repo)   # info + warn only, no sla
    channels_path = support.write_malformed_channels_file(repo)

    result = _run(conn, repo, channels_path=channels_path)

    assert result.notice_posted is False
    assert result.notice_error == ""
    assert len(result.findings) == 3



# ── the no-write proof, gardener's own half. The architectural half (nothing in this package
# imports a writer) lives in tests/test_architecture.py ─────────────────────────────────────────
def test_run_gardener_writes_nothing_but_its_own_findings_and_job_runs_row(conn, repo, monkeypatch):
    _seed_minimal_corpus(conn, repo)
    support.force_one_sla_finding(monkeypatch)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        capture_before = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM review_decisions")
        decisions_before = cur.fetchone()[0]

    _run(conn, repo, settings=GardenerSettings())   # no channel configured; sla finding present

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == capture_before
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == decisions_before


# ── the model sweep, wired end to end ───────────────────────────────────────────────────────────
def _file_one_changed_page(conn, repo, relpath: str = "notes/changed.md") -> str:
    p = support.write_page(repo, "wiki", relpath,
                           frontmatter={"type": "note", "title": relpath, "entity": [],
                                       "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p}@sha0")
    return p


def test_run_gardener_persists_a_model_sourced_finding_alongside_deterministic_ones(conn, repo):
    """A sweep finding lands with `source=model` plus the configured model id, a deterministic
    one with `source=deterministic` — both in the SAME run's persisted findings."""
    support.write_registry(repo, {})
    p = _file_one_changed_page(conn, repo)

    result = _run(conn, repo)

    by_source = {f["source"] for f in result.findings}
    assert "deterministic" in by_source    # the filed page is itself an orphan (info)
    assert "model" in by_source
    model_finding = next(f for f in result.findings if f["source"] == "model")
    assert model_finding["model_id"] == GardenerSettings.from_args().model
    assert model_finding["check"] in sweep.ALL_MODEL_CHECK_SLUGS
    assert model_finding["subject"] == p
    assert result.sweep_error == ""
    assert result.sweep_changed_count == 1


def test_run_gardener_sweep_findings_are_re_fetched_from_the_database_too(conn, repo):
    """`store.py`'s own "what a reader sees is never allowed to drift from what is stored" rule,
    proven for a model-sourced row specifically — the returned finding carries a real `id` and
    round-tripped `model_id`, not the in-memory spec `sweep.to_finding` built."""
    support.write_registry(repo, {})
    _file_one_changed_page(conn, repo)

    result = _run(conn, repo)

    model_finding = next(f for f in result.findings if f["source"] == "model")
    assert model_finding["id"] is not None
    with conn.cursor() as cur:
        cur.execute("SELECT source, model_id FROM gardener_findings WHERE id = %s",
                    (model_finding["id"],))
        source, model_id = cur.fetchone()
    assert source == "model"
    assert model_id == model_finding["model_id"]


def test_run_gardener_sweep_stats_land_in_job_runs(conn, repo):
    support.write_registry(repo, {})
    _file_one_changed_page(conn, repo)

    result = _run(conn, repo)

    with conn.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE id = %s", (result.run_id,))
        stats = cur.fetchone()[0]
    sweep_stats = stats["sweep"]
    assert sweep_stats["changed"] == 1
    assert sweep_stats["error"] == ""
    assert sweep_stats["inserted"] == 1
    assert "next_sample_offset" in sweep_stats
    # the honest page-selection boundary, captured before select_pages ran — a real, parseable
    # timestamp, not merely present.
    datetime.datetime.fromisoformat(sweep_stats["selected_at"])


def test_run_gardener_a_batch_with_only_sampled_unchanged_pages_produces_no_model_finding(conn, repo):
    """The offline double's own, deliberate restraint (`FakeGardenerSweep`'s docstring) — no
    capture filed at all -> `changed` is empty -> the double contributes nothing, proven end to
    end through the real orchestrator, not only through `test_sweep.py`'s direct calls."""
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "notes/never-filed.md",
                       frontmatter={"type": "note", "title": "t", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    assert "model" not in {f["source"] for f in result.findings}
    assert result.sweep_changed_count == 0


# ── a sweep failure never costs the deterministic findings from the SAME run ────────────────────
def test_run_gardener_survives_a_garbage_sweep_output_deterministic_findings_still_persist(
        conn, repo, monkeypatch):
    """`CLEAN_LLM=fake-flawed` (the shipped switch every offline double answers to) makes
    `FakeGardenerSweep` return deliberately-invalid output on every call — `_run_sweep_pass`
    catches the resulting `SweepGarbage` itself, so the run as a whole still succeeds and the
    deterministic findings (an orphan page here) are still complete and persisted."""
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    support.write_registry(repo, {})
    _file_one_changed_page(conn, repo)

    result = _run(conn, repo)

    assert result.sweep_error == "SweepGarbage"
    assert "model" not in {f["source"] for f in result.findings}
    assert "deterministic" in {f["source"] for f in result.findings}
    with conn.cursor() as cur:
        cur.execute("SELECT status, stats FROM job_runs WHERE id = %s", (result.run_id,))
        status, stats = cur.fetchone()
    # 'partial', never 'ok' — the deterministic findings are complete and
    # committed, but the run's own status must say honestly that an auxiliary pass (the sweep)
    # failed, or `gardener.sweep.previous_run_watermark` (which reads 'ok' runs only) would have
    # no way to tell "this run's sweep baseline did not move" from "everything about this run is
    # trustworthy" — see `capture.ops`'s module docstring for the full status vocabulary.
    assert status == "partial"
    assert stats["sweep"]["error"] == "SweepGarbage"
    assert stats["sweep"]["inserted"] == 0
    # the offset must not have silently advanced past pages nothing was actually judged for.
    assert stats["sweep"]["next_sample_offset"] == 0   # sample_offset this run started from


def test_run_gardener_survives_an_agent_run_error_deterministic_findings_still_persist(
        conn, repo, monkeypatch):
    """The other failure mode: a hard model-call failure. `sweep.build_judge` is replaced with one
    that raises `AgentRunError` the moment it is awaited — exercising the REAL
    `_run_sweep_pass`/`run_gardener` integration, not merely `run_sweep`'s own unit test."""
    class _FlakyJudge:
        async def run(self, prompt, *, deps=None, usage_limits=None):
            raise AgentRunError("simulated model outage")

    monkeypatch.setattr(sweep, "build_judge", lambda model_name=None: _FlakyJudge())
    support.write_registry(repo, {})
    _file_one_changed_page(conn, repo)

    result = _run(conn, repo)

    assert result.sweep_error == "AgentRunError"
    assert "model" not in {f["source"] for f in result.findings}
    assert "deterministic" in {f["source"] for f in result.findings}
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM job_runs WHERE id = %s", (result.run_id,))
        # 'partial', not 'ok' — see the sibling garbage-sweep test above for the full reasoning;
        # identical status posture for this, the OTHER failure mode.
        assert cur.fetchone()[0] == "partial"


def test_run_gardener_survives_a_bare_misconfiguration_like_a_missing_api_key(conn, repo, monkeypatch):
    """Not one of the two failure modes above, but the SAME posture must hold: `build_judge`
    itself can raise a bare exception (a missing `OPENAI_API_KEY`, for real, raises plain
    `RuntimeError` — `kernel.llm.build_model`) before `run_sweep` is ever reached, and it must not
    cost the deterministic findings either."""
    def _boom(model_name=None):
        raise RuntimeError("OPENAI_API_KEY is required (set it in the environment / .env)")

    monkeypatch.setattr(sweep, "build_judge", _boom)
    support.write_registry(repo, {})
    _file_one_changed_page(conn, repo)

    result = _run(conn, repo)

    assert result.sweep_error == "RuntimeError"
    assert "deterministic" in {f["source"] for f in result.findings}
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM job_runs WHERE id = %s", (result.run_id,))
        # the same 'partial' posture as the two failure modes above — a bare misconfiguration is
        # still a sweep failure, not a run-level one.
        assert cur.fetchone()[0] == "partial"


# ── filing-population exclusion counters ────────────────────────────────────────────────────────
def test_run_gardener_surfaces_filing_population_exclusions_in_job_runs_stats(conn, repo):
    """`_recent_filed_pages`'s exclusion counters (unparsed refs, unindexed pages, provenance
    exclusions) used to be computed and never surfaced anywhere — `checks.py`'s `population_stats`
    sink, threaded through `run._run_all_checks`, is what puts them in `job_runs.stats`."""
    support.write_registry(repo, {"acme-corp": {"name": "Acme Corp", "type": "organization",
                                                "aliases": []}})
    p = support.write_page(repo, "wiki", "notes/anchored.md",
                           frontmatter={"type": "note", "title": "t", "entity": ["acme-corp"],
                                       "status": "developing", "updated": _days_ago(1)})
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p}@sha0")
    support.seed_filed_capture(conn, result_ref="garbage-ref-with-no-at-sign")

    result = _run(conn, repo)

    with conn.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE id = %s", (result.run_id,))
        stats = cur.fetchone()[0]
    exclusions = stats["filing_population_exclusions"]
    assert exclusions["anchor_concentration"]["unparsed_result_ref"] == 1
    assert exclusions["company_wide_fraction"]["unparsed_result_ref"] == 1


# ── a malformed `updated` date must not abort the whole run ─────────────────────────────────────
def test_run_gardener_survives_a_malformed_updated_date_the_whole_run_does_not_abort(conn, repo):
    """`updated::date` used to cast a hand-edited value like "next week" unconditionally —
    Postgres raised mid-query, the age-based checks (and, since `_run_all_checks` runs them
    inline, the WHOLE run) aborted with `status='error'` and zero findings, daily, until a human
    diagnosed a cast error. The health surface must not be takeable-down by the exact data defect
    it exists to surface."""
    support.write_registry(repo, {})
    support.write_page(repo, "wiki", "product/garbled-date.md",
                       frontmatter={"type": "note", "title": "Garbled Date", "entity": [],
                                   "status": "seed", "updated": "next week"})
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    assert "aging-seed" not in {f["check"] for f in result.findings}
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM job_runs WHERE id = %s", (result.run_id,))
        assert cur.fetchone()[0] == "ok"   # NOT 'error' — the run survives the bad data
    assert result.stats["age_population_exclusions"]["aging_seed"]["malformed_updated"] == 1
