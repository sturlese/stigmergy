"""`gardener.run.run_gardener` — the orchestrator: runs every check, persists a `job_runs` row
plus this run's findings in one transaction, and returns the durable, re-fetched result.
"""
import asyncio
import datetime
import os

from pydantic_ai.exceptions import AgentRunError

from stigmergy.gardener import checks, run, schema, sweep
from stigmergy.gardener import settings as settings_module
from stigmergy.gardener.errors import GardenerError
from stigmergy.gardener.settings import GardenerSettings
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


def _run(conn, repo, *, settings=None):
    return asyncio.run(run.run_gardener(
        conn, repo=repo, settings=settings or GardenerSettings.from_args()))


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


def test_the_entity_placeholder_check_runs_in_the_pass_and_is_counted_by_name(conn, repo):
    """The wiring, not the rule: a check the runner never calls is a check that does not exist,
    and `stats["findings_by_check"]` is derived from whatever the pass produced — so one assertion
    covers the call site and the aggregate counters at once."""
    _seed_minimal_corpus(conn, repo)
    support.write_page(repo, "wiki", "entities/Meridian Partners.md",
                       frontmatter={"type": "entity", "title": "Meridian Partners",
                                   "entity": ["meridian-partners"], "status": "developing"},
                       body="# Meridian Partners\n\n<One clear paragraph: what this entity is.>\n")
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    placeholder = [f for f in result.findings if f["check"] == "entity-placeholder-body"]
    assert [f["subject"] for f in placeholder] == ["wiki/entities/Meridian Partners.md"]
    with conn.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE id = %s", (result.run_id,))
        (stats,) = cur.fetchone()
    assert stats["findings_by_check"]["entity-placeholder-body"] == 1


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
    # A WRITTEN body, not an empty one: since `model-empty-entity-body` an entity page that says
    # nothing about itself is a finding, so "a clean corpus" now has to include a real body for
    # this fixture to still mean what it says.
    support.write_page(repo, "wiki", "entities/nothing.md",
                       frontmatter={"type": "entity", "title": "Nothing", "entity": [],
                                   "status": "developing", "updated": _days_ago(1)},
                       body=support.written_entity_body("Nothing"))
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



# ── the no-write proof, gardener's own half. The architectural half (nothing in this package
# imports a writer) lives in tests/test_architecture.py ─────────────────────────────────────────
def test_run_gardener_writes_nothing_but_its_own_findings_and_job_runs_row(conn, repo):
    _seed_minimal_corpus(conn, repo)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        capture_before = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM repairs")
        repairs_before = cur.fetchone()[0]

    _run(conn, repo, settings=GardenerSettings())

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == capture_before
        cur.execute("SELECT count(*) FROM repairs")
        assert cur.fetchone()[0] == repairs_before


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
# ── the SECOND model pass: entity bodies that say nothing ───────────────────────────────────────
def _entity_page(repo, stem: str, *, body: str) -> str:
    return support.write_page(
        repo, "wiki", f"entities/{stem}.md",
        frontmatter={"type": "entity", "title": stem, "entity": [stem.lower()],
                     "status": "developing", "updated": _days_ago(1)},
        body=body)


def test_run_gardener_persists_an_empty_body_finding_from_the_second_model_pass(conn, repo):
    """The pass runs inside the real orchestrator, over the CHECKOUT rather than `pages_index`,
    and its finding lands with the fifth slug, `info`, and the run's configured model id."""
    support.write_registry(repo, {})
    _entity_page(repo, "Cofers", body=support.empty_entity_body("Cofers"))
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    empty = [f for f in result.findings if f["check"] == sweep.CHECK_MODEL_EMPTY_ENTITY_BODY]
    assert [f["subject"] for f in empty] == ["wiki/entities/Cofers.md"]
    assert empty[0]["severity"] == schema.SEVERITY_INFO
    assert empty[0]["source"] == schema.SOURCE_MODEL
    assert empty[0]["subjects"] == ["wiki/entities/Cofers.md"]
    assert result.empty_body_error == ""
    assert result.empty_body_judged_count == 1


def test_run_gardener_the_benign_twin_a_written_entity_body_produces_no_finding(conn, repo):
    """A steward's real work rides the same run and comes back untouched — the pass was asked
    about the page (it is in the population) and answered no."""
    support.write_registry(repo, {})
    _entity_page(repo, "Cofers", body=support.written_entity_body("Cofers"))
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    assert result.empty_body_judged_count == 1, "the page must have been JUDGED, not skipped"
    assert [f for f in result.findings
            if f["check"] == sweep.CHECK_MODEL_EMPTY_ENTITY_BODY] == []


def test_run_gardener_a_page_with_placeholders_produces_exactly_one_finding_across_both_checks(
        conn, repo):
    """The interaction, pinned on a page that would satisfy BOTH: the deterministic check reports
    it and the model pass never sees it, so the repair proposer is handed one question about that
    page rather than two."""
    support.write_registry(repo, {})
    _entity_page(repo, "Cofers", body="# Cofers\n\n<One clear paragraph: what this entity is.>\n")
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    about_the_page = [f["check"] for f in result.findings
                      if f["subject"] == "wiki/entities/Cofers.md"]
    assert about_the_page == [checks.CHECK_ENTITY_PLACEHOLDER_BODY]
    assert result.stats["empty_body"]["excluded_placeholder"] == 1
    assert result.empty_body_judged_count == 0


def test_run_gardener_empty_body_stats_land_in_job_runs(conn, repo):
    support.write_registry(repo, {})
    _entity_page(repo, "Cofers", body=support.empty_entity_body("Cofers"))
    _entity_page(repo, "Meridian", body="# Meridian\n\n<a placeholder>\n")
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    with conn.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE id = %s", (result.run_id,))
        stats = cur.fetchone()[0]
    empty_body = stats["empty_body"]
    assert empty_body["population"] == 2
    assert empty_body["excluded_placeholder"] == 1
    assert empty_body["judged"] == 1
    assert empty_body["batches"] == 1
    assert empty_body["inserted"] == 1
    assert empty_body["deferred"] == 0
    assert empty_body["error"] == ""


def test_run_gardener_the_empty_body_ceiling_stops_the_pass_and_records_what_it_deferred(
        conn, repo, monkeypatch):
    """The bound is LOUD when it binds: the pages it did not look at are counted and named as a
    skip reason, because a ceiling that truncated in silence would read as "nothing wrong about
    them" — the exact failure this check exists to end."""
    monkeypatch.setenv("STIGMERGY_GARDENER_EMPTY_BODY_CEILING", "1")
    support.write_registry(repo, {})
    for stem in ("Alpha", "Beta", "Gamma"):
        _entity_page(repo, stem, body=support.empty_entity_body(stem))
    support.rebuild_index(conn, repo)

    result = _run(conn, repo, settings=GardenerSettings.from_args())

    empty_body = result.stats["empty_body"]
    assert empty_body["considered"] == 3
    assert empty_body["judged"] == 1
    assert empty_body["deferred"] == 2
    assert any("2 entity page(s)" in reason for reason in empty_body["skip_reasons"])
    # The reason's "$VARIABLE raises this" promise, pinned at the CALL SITE: the unit tests format
    # the template with a name they themselves supply, which proves only that the template writes
    # a `$` — a call site passing the wrong constant would leave them green.
    assert any(f"${settings_module.EMPTY_BODY_CEILING_ENV}" in reason
               for reason in empty_body["skip_reasons"])
    assert len([f for f in result.findings
                if f["check"] == sweep.CHECK_MODEL_EMPTY_ENTITY_BODY]) == 1


def test_run_gardener_the_empty_body_batch_size_is_honoured_from_settings(conn, repo, monkeypatch):
    monkeypatch.setenv("STIGMERGY_GARDENER_EMPTY_BODY_BATCH", "1")
    support.write_registry(repo, {})
    for stem in ("Alpha", "Beta", "Gamma"):
        _entity_page(repo, stem, body=support.empty_entity_body(stem))
    support.rebuild_index(conn, repo)

    result = _run(conn, repo, settings=GardenerSettings.from_args())

    assert result.stats["empty_body"]["batches"] == 3
    assert result.stats["empty_body"]["judged"] == 3


def test_run_gardener_builds_no_empty_body_judge_when_there_is_nothing_to_judge(conn, repo,
                                                                                monkeypatch):
    """A corpus with no entity page must not pay a model-stack construction — and, the real
    reason, a missing API key must not turn a run with nothing to do into a failed pass."""
    def refuse(*args, **kwargs):
        raise AssertionError("the empty-body judge was built for an empty population")

    monkeypatch.setattr(sweep, "build_empty_body_judge", refuse)
    _seed_minimal_corpus(conn, repo)

    result = _run(conn, repo)

    assert result.empty_body_error == ""
    assert result.stats["empty_body"]["population"] == 0


# ── how the two model passes' failures combine ──────────────────────────────────────────────────
def test_run_gardener_an_empty_body_pass_failure_alone_still_commits_partial(conn, repo,
                                                                             monkeypatch):
    """The existing rule, now with a SECOND model pass that can fail: the deterministic findings
    and the editorial sweep's are intact and committed, and the run's status still says honestly
    that a model pass did not happen. `'ok'` here would be a clean bill of health for entity pages
    nothing looked at."""
    class _FlakyJudge:
        async def run(self, prompt, *, deps=None, usage_limits=None):
            raise AgentRunError("the provider went away")

    monkeypatch.setattr(sweep, "build_empty_body_judge",
                        lambda model_name=None: _FlakyJudge())
    support.write_registry(repo, {})
    _file_one_changed_page(conn, repo)
    _entity_page(repo, "Cofers", body=support.empty_entity_body("Cofers"))
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    assert result.empty_body_error == "AgentRunError"
    assert result.sweep_error == "", "the editorial sweep is untouched by the other pass failing"
    assert "deterministic" in {f["source"] for f in result.findings}
    assert [f for f in result.findings
            if f["check"] == sweep.CHECK_MODEL_EMPTY_ENTITY_BODY] == []
    with conn.cursor() as cur:
        cur.execute("SELECT status, stats FROM job_runs WHERE id = %s", (result.run_id,))
        status, stats = cur.fetchone()
    assert status == "partial"
    assert stats["empty_body"]["error"] == "AgentRunError"
    assert stats["empty_body"]["unjudged"] == 1
    assert stats["sweep"]["error"] == ""


def test_run_gardener_both_model_passes_failing_is_still_one_partial_run(conn, repo, monkeypatch):
    """`CLEAN_LLM=fake-flawed` fails BOTH doubles at once. Two failures, one status — the run is
    `partial`, not `error`, because the deterministic checks completed and their findings are
    exactly as trustworthy as on any other night."""
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    support.write_registry(repo, {})
    _file_one_changed_page(conn, repo)
    _entity_page(repo, "Cofers", body=support.empty_entity_body("Cofers"))
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    assert result.sweep_error == "SweepGarbage"
    assert result.empty_body_error == "SweepGarbage"
    assert "model" not in {f["source"] for f in result.findings}
    assert "deterministic" in {f["source"] for f in result.findings}
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM job_runs WHERE id = %s", (result.run_id,))
        assert cur.fetchone()[0] == "partial"


def test_run_gardener_an_empty_body_batch_failure_keeps_the_batches_already_judged(conn, repo,
                                                                                    monkeypatch):
    """A pass that dies on batch two keeps batch one's validated findings: they are validated
    however the next batch went, and discarding them would cost the operator a real finding to
    make an error message tidier."""
    monkeypatch.setenv("STIGMERGY_GARDENER_EMPTY_BODY_BATCH", "1")
    support.write_registry(repo, {})
    for stem in ("Alpha", "Beta", "Gamma"):
        _entity_page(repo, stem, body=support.empty_entity_body(stem))
    support.rebuild_index(conn, repo)

    real = sweep.run_empty_body_sweep
    calls = {"n": 0}

    async def _fail_on_the_second(judge, pages):
        calls["n"] += 1
        if calls["n"] == 2:
            raise AgentRunError("the provider went away mid-pass")
        return await real(judge, pages)

    monkeypatch.setattr(sweep, "run_empty_body_sweep", _fail_on_the_second)

    result = _run(conn, repo, settings=GardenerSettings.from_args())

    assert result.empty_body_error == "AgentRunError"
    assert calls["n"] == 2, "the pass stops at the failing batch rather than trying the rest"
    assert len([f for f in result.findings
                if f["check"] == sweep.CHECK_MODEL_EMPTY_ENTITY_BODY]) == 1
    assert result.stats["empty_body"]["judged"] == 1
    assert result.stats["empty_body"]["unjudged"] == 2


def test_run_gardener_an_editorial_sweep_failure_alone_leaves_the_empty_body_pass_standing(
        conn, repo, monkeypatch):
    """The MIRROR of the test above, and the half that says the two passes are independent in both
    directions: the editorial sweep dies, the empty-body pass runs to completion and its finding is
    persisted, and the run is still `partial` because one model pass did not happen. Without this,
    "an outage of either must not cost the other" is asserted in one direction and assumed in the
    other."""
    class _FlakyJudge:
        async def run(self, prompt, *, deps=None, usage_limits=None):
            raise AgentRunError("the provider went away")

    monkeypatch.setattr(sweep, "build_judge", lambda model_name=None: _FlakyJudge())
    support.write_registry(repo, {})
    _file_one_changed_page(conn, repo)
    _entity_page(repo, "Cofers", body=support.empty_entity_body("Cofers"))
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    assert result.sweep_error == "AgentRunError"
    assert result.empty_body_error == "", "the second pass is untouched by the first failing"
    assert [f["subject"] for f in result.findings
            if f["check"] == sweep.CHECK_MODEL_EMPTY_ENTITY_BODY] == ["wiki/entities/Cofers.md"]
    with conn.cursor() as cur:
        cur.execute("SELECT status, stats FROM job_runs WHERE id = %s", (result.run_id,))
        status, stats = cur.fetchone()
    assert status == "partial"
    assert stats["empty_body"]["error"] == ""
    assert stats["empty_body"]["inserted"] == 1


def test_run_gardener_a_ceiling_that_binds_and_a_batch_that_fails_are_counted_separately(
        conn, repo, monkeypatch):
    """Two DIFFERENT reasons a page went unjudged in one run, and neither may absorb the other: a
    page nobody looked at because the run ceiling bound (`deferred`) and a page nobody looked at
    because the pass died mid-way (`unjudged`). An operator reading one number would raise the
    ceiling for pages that were in fact lost to an outage, or wait out an outage that was really a
    bound — so both counts are non-zero here and both are on the row."""
    monkeypatch.setenv("STIGMERGY_GARDENER_EMPTY_BODY_CEILING", "3")
    monkeypatch.setenv("STIGMERGY_GARDENER_EMPTY_BODY_BATCH", "1")
    support.write_registry(repo, {})
    for stem in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon"):
        _entity_page(repo, stem, body=support.empty_entity_body(stem))
    support.rebuild_index(conn, repo)

    real = sweep.run_empty_body_sweep
    calls = {"n": 0}

    async def _fail_on_the_second(judge, pages):
        calls["n"] += 1
        if calls["n"] == 2:
            raise AgentRunError("the provider went away mid-pass")
        return await real(judge, pages)

    monkeypatch.setattr(sweep, "run_empty_body_sweep", _fail_on_the_second)

    result = _run(conn, repo, settings=GardenerSettings.from_args())

    empty_body = result.stats["empty_body"]
    assert empty_body["considered"] == 5
    assert empty_body["deferred"] == 2, "the ceiling's own two, never folded into the outage"
    assert empty_body["unjudged"] == 2, "the pages the dead pass never reached"
    assert empty_body["judged"] == 1
    assert empty_body["error"] == "AgentRunError"
    assert any("2 entity page(s)" in reason and "EMPTY_BODY_CEILING" in reason
               for reason in empty_body["skip_reasons"])
    assert len([f for f in result.findings
                if f["check"] == sweep.CHECK_MODEL_EMPTY_ENTITY_BODY]) == 1


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


# ── the THIRD model pass: two registry entries that are one entity ──────────────────────────────
# Every test here uses `Cofers` beside `Cofers SL`, the one pair the offline double can see (it
# folds two registered names to a single `normalize()` key). What that proves is the ORCHESTRATION
# — which population reached the pass, what landed in `stats`, and what a failure costs the rest of
# the run — never the rubric; `test_sweep_duplicate_entity.py` says the same thing about itself.
_DUPLICATE_REGISTRY = {"cofers": {"name": "Cofers", "type": "organization", "aliases": []},
                       "cofers-sl": {"name": "Cofers SL", "type": "organization", "aliases": []}}


def _duplicate_pair(repo) -> None:
    support.write_registry(repo, _DUPLICATE_REGISTRY)
    _entity_page(repo, "Cofers", body=support.written_entity_body("Cofers"))
    _entity_page(repo, "Cofers SL", body=support.written_entity_body("Cofers SL"))


def test_run_gardener_persists_a_duplicate_identity_finding_carrying_BOTH_ids(conn, repo):
    """One finding, `warn`, both entity pages in `subjects` — the LIST, which is what the repair
    loop reads to know which pair a merge would be about."""
    _duplicate_pair(repo)
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    dupes = [f for f in result.findings if f["check"] == sweep.CHECK_MODEL_DUPLICATE_ENTITY]
    assert len(dupes) == 1
    assert dupes[0]["subjects"] == ["wiki/entities/Cofers SL.md", "wiki/entities/Cofers.md"]
    assert dupes[0]["severity"] == schema.SEVERITY_WARN
    assert dupes[0]["source"] == schema.SOURCE_MODEL
    assert result.duplicate_entity_error == ""
    assert result.duplicate_entity_judged_count == 2


def test_run_gardener_the_benign_twin_two_unrelated_entities_produce_no_finding(conn, repo):
    """`Cofers` beside `Cofers Legal` — a parent and its law firm — ride the same run, are both
    JUDGED, and come back unmerged. A pass that flagged this would be rewriting what somebody's
    pages are about, which is the failure that turns a health check into noise nobody reads."""
    support.write_registry(repo, {
        "cofers": {"name": "Cofers", "type": "organization", "aliases": []},
        "cofers-legal": {"name": "Cofers Legal", "type": "organization", "aliases": []}})
    _entity_page(repo, "Cofers", body=support.written_entity_body("Cofers"))
    _entity_page(repo, "Cofers Legal", body=support.written_entity_body("Cofers Legal"))
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    assert result.duplicate_entity_judged_count == 2, "both must have been JUDGED, not skipped"
    assert [f for f in result.findings
            if f["check"] == sweep.CHECK_MODEL_DUPLICATE_ENTITY] == []


def test_run_gardener_duplicate_identity_stats_land_in_job_runs(conn, repo):
    _duplicate_pair(repo)
    _entity_page(repo, "Never Minted", body=support.written_entity_body("Never Minted"))
    support.rebuild_index(conn, repo)

    result = _run(conn, repo)

    with conn.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE id = %s", (result.run_id,))
        stats = cur.fetchone()[0]
    duplicate = stats["duplicate_entity"]
    assert duplicate["population"] == 3
    assert duplicate["excluded_unregistered"] == 1
    assert duplicate["judged"] == 2
    assert duplicate["inserted"] == 1
    assert duplicate["deferred"] == 0
    assert duplicate["error"] == ""
    # No `batch`/`batches` key at all: this pass is ONE call by construction, and a counter
    # implying otherwise would invite somebody to batch a question about pairs.
    assert "batches" not in duplicate


def test_run_gardener_asks_no_model_about_a_registry_that_cannot_hold_a_pair(conn, repo,
                                                                             monkeypatch):
    """The floor, enforced BEFORE the judge is built: one registered entity cannot be half of a
    pair, so a run that asked would reach the same answer and pay for it every night. The skip is
    RECORDED — "no model was asked" and "the model found nothing" are different facts."""
    support.write_registry(repo, {"cofers": {"name": "Cofers", "type": "organization",
                                             "aliases": []}})
    _entity_page(repo, "Cofers", body=support.written_entity_body("Cofers"))
    support.rebuild_index(conn, repo)

    def refuse(*a, **kw):
        raise AssertionError("no judge may be built for a population that cannot hold a pair")

    monkeypatch.setattr(sweep, "build_duplicate_entity_judge", refuse)

    result = _run(conn, repo)

    assert result.duplicate_entity_error == ""
    assert result.duplicate_entity_judged_count == 0
    assert any("population-below-floor" in reason
               for reason in result.stats["duplicate_entity"]["skip_reasons"])


def test_run_gardener_the_duplicate_identity_ceiling_records_what_it_deferred(conn, repo,
                                                                              monkeypatch):
    _duplicate_pair(repo)
    _entity_page(repo, "Globex", body=support.written_entity_body("Globex"))
    support.write_registry(repo, {**_DUPLICATE_REGISTRY,
                                  "globex": {"name": "Globex", "type": "organization",
                                             "aliases": []}})
    support.rebuild_index(conn, repo)
    monkeypatch.setenv("STIGMERGY_GARDENER_DUPLICATE_ENTITY_CEILING", "2")

    result = _run(conn, repo, settings=GardenerSettings.from_args())

    duplicate = result.stats["duplicate_entity"]
    assert duplicate["considered"] == 3
    assert duplicate["judged"] == 2
    assert duplicate["deferred"] == 1
    assert result.duplicate_entity_deferred_count == 1
    assert any("1 registered entity page(s)" in reason for reason in duplicate["skip_reasons"])
    # The same call-site pin as the empty-body twin above.
    assert any(f"${settings_module.DUPLICATE_ENTITY_CEILING_ENV}" in reason
               for reason in duplicate["skip_reasons"])


def test_run_gardener_a_duplicate_identity_failure_alone_still_commits_partial(conn, repo,
                                                                               monkeypatch):
    """The third pass fails independently of the other two: the deterministic findings and both
    other passes' findings stand, the run is `'partial'`, and the WHOLE population is recorded as
    unjudged — this pass is one call, so there is no half of it that survived."""
    _duplicate_pair(repo)
    support.rebuild_index(conn, repo)

    class _Boom:
        async def run(self, prompt, *, deps=None, usage_limits=None):
            raise AgentRunError("the identity pass is down")

    monkeypatch.setattr(sweep, "build_duplicate_entity_judge", lambda *a, **kw: _Boom())

    result = _run(conn, repo)

    assert result.duplicate_entity_error == "AgentRunError"
    assert result.duplicate_entity_judged_count == 0
    assert result.sweep_error == ""
    assert result.empty_body_error == ""
    assert [f for f in result.findings
            if f["check"] == sweep.CHECK_MODEL_DUPLICATE_ENTITY] == []
    with conn.cursor() as cur:
        cur.execute("SELECT status, stats FROM job_runs WHERE id = %s", (result.run_id,))
        status, stats = cur.fetchone()
    assert status == "partial"
    assert stats["duplicate_entity"]["error"] == "AgentRunError"
    assert stats["duplicate_entity"]["judged"] == 0


def test_run_gardener_a_duplicate_identity_failure_does_not_freeze_the_editorial_watermark(
        conn, repo, monkeypatch):
    """`previous_run_watermark` asks `stats.sweep.error`, never the run's aggregate status — so a
    `'partial'` run whose EDITORIAL sweep completed still advances that sweep's `since`."""
    _duplicate_pair(repo)
    support.rebuild_index(conn, repo)

    class _Boom:
        async def run(self, prompt, *, deps=None, usage_limits=None):
            raise AgentRunError("the identity pass is down")

    monkeypatch.setattr(sweep, "build_duplicate_entity_judge", lambda *a, **kw: _Boom())
    result = _run(conn, repo)

    assert result.stats["sweep"]["error"] == ""
    since, _offset = sweep.previous_run_watermark(conn)
    assert since is not None, ("a failed identity pass must not cost the editorial sweep its "
                              "watermark")


def test_run_gardener_the_changed_ceiling_defers_to_the_rotation_and_names_its_knob(
        conn, repo, monkeypatch):
    """The editorial sweep's own bound, end to end and with the call-site env pin its two sibling
    ceilings carry: a catch-up night (here: a first run over five filings) judges the newest
    `$STIGMERGY_GARDENER_SWEEP_CHANGED_CEILING` and records what it deferred — into stats AND into
    a skip reason naming the knob, because a bound that binds in silence reads as a small night."""
    monkeypatch.setenv(settings_module.SWEEP_CHANGED_CEILING_ENV, "2")
    support.write_registry(repo, {})
    paths = []
    for i in range(5):
        paths.append(support.write_page(
            repo, "wiki", f"notes/burst-{i}.md",
            frontmatter={"type": "note", "title": f"Burst {i}", "entity": [],
                        "status": "developing", "updated": "2026-07-01"},
            body=f"burst body {i}"))
    support.rebuild_index(conn, repo)
    for p in paths:
        support.seed_filed_capture(conn, result_ref=f"{p}@sha0")

    result = _run(conn, repo, settings=GardenerSettings.from_args())

    sweep_stats = result.stats["sweep"]
    assert sweep_stats["changed"] == 2
    assert sweep_stats["changed_deferred"] == 3
    assert any(f"${settings_module.SWEEP_CHANGED_CEILING_ENV}" in reason
               for reason in sweep_stats["skip_reasons"])
