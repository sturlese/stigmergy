"""`gardener.run.run_gardener` — the orchestrator: runs every check, persists a `job_runs` row
plus this run's findings in one transaction, and returns the durable, re-fetched result.
"""
import datetime
import os

from stigmergy.gardener import run, schema
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
    return run.run_gardener(conn, repo=repo, settings=settings or GardenerSettings.from_args())


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
    # A WRITTEN body: an entity page blank below its title, or still carrying the template, is
    # itself a finding (`entity-placeholder-body`), so "a clean corpus" has to include a real body
    # for this fixture to mean what it says.
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
