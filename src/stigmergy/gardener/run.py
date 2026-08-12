"""Orchestration: run every check, run the sweep, persist findings + a `job_runs` row in one
transaction, post the SLA notice — the one function `cli.py` calls.

Calls `ops.record_job_run` directly, not the `job_run` context manager: that manager writes its
row on exit, and every finding needs `run_id` at insert time. The try/except below replicates its
shape so a failed run still gets an honest `status='error'` row.

The sweep pass can never make this function raise: a sweep outage must not cost the operator the
deterministic checks that already ran, so `_run_sweep_pass` catches everything and reports
through its returned stats. Every OTHER failure aborts the run entirely.

A failed sweep commits `'partial'`, never `'ok'`: `sweep.previous_run_watermark` reads only
`'ok'` rows, and an `'ok'` here would advance the sweep watermark past pages nothing judged.
"""
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from stigmergy.capture import ops
from stigmergy.gardener import checks, notice, store, sweep
from stigmergy.gardener.errors import GardenerError
from stigmergy.gardener.schema import JOB_NAME
from stigmergy.gardener.settings import GardenerSettings
from stigmergy.kernel.registry import Registry, load_registry
from stigmergy.server.errors import IdentityError
from stigmergy.slack import channels
from stigmergy.slack.gateway import SlackApiError

log = logging.getLogger(__name__)

REGISTRY_RELPATH = os.path.join("ops", "entity-registry.json")


@dataclass
class RunResult:
    run_id: int
    findings: list[dict]
    pages_checked: int
    entities_checked: int
    completed_at: str
    notice_posted: bool = False
    # Set only when an `sla` finding fired but the notice could not post; the findings are
    # already persisted.
    notice_error: str = ""
    # Set only when the sweep failed (class name only, never `str(ex)`); `""` means it succeeded
    # or had nothing to do.
    sweep_error: str = ""
    sweep_changed_count: int = 0
    sweep_sampled_count: int = 0
    stats: dict = field(default_factory=dict)


def _require_repo(repo: str) -> str:
    if not repo or not os.path.isdir(repo):
        raise GardenerError(
            f"--repo {repo!r} is not a directory — the gardener reads the entity registry and "
            "view staleness from a checkout of the knowledge repo; point it at one.")
    return repo


def _run_all_checks(conn, repo: str, registry: Registry, settings: GardenerSettings,
                    filing_population_stats: dict, age_population_stats: dict) -> list[dict]:
    """`filing_population_stats` and `age_population_stats` are shared sink dicts the checks
    write their exclusion counters into — every excluded row is counted, never silently
    dropped."""
    findings: list[dict] = []
    findings += checks.check_orphans(conn)
    aging_seed_stats: dict = {}
    findings += checks.check_aging_seeds(conn, threshold_days=settings.aging_seed_days,
                                         population_stats=aging_seed_stats)
    age_population_stats["aging_seed"] = aging_seed_stats
    findings += checks.check_stale_views(repo)
    findings += checks.check_anchor_concentration(
        conn, registry, window=settings.concentration_window,
        share_threshold=settings.concentration_share,
        population_stats=filing_population_stats)
    findings += checks.check_dead_vocabulary(repo, registry)
    findings += checks.check_date_bearing_body_links(repo)
    findings += checks.check_company_wide_fraction(
        conn, window=settings.company_window, share_threshold=settings.company_share,
        population_stats=filing_population_stats)
    findings += checks.check_company_page_names_entity(conn, registry)
    return findings


async def _run_sweep_pass(conn, settings: GardenerSettings) -> tuple[list[dict], dict]:
    """Runs the sweep as one self-contained unit and NEVER raises: `stats["error"]` is `""` on
    success or the failing exception's CLASS NAME — never `str(ex)`, which can carry page content.

    Page selection stays OUTSIDE the try/except: a bug in pure-SQL selection is a defect, not a
    sweep outage, and should fail the run loudly.

    `selected_at` is captured immediately before `select_pages` — the boundary this sweep
    actually read up to — for `previous_run_watermark` to prefer over `job_runs.started_at`,
    which is written later; a page filed between the two would otherwise fall in NO sweep window
    ever.
    """
    selected_at = datetime.now(UTC)
    since, sample_offset = sweep.previous_run_watermark(conn)
    changed, sampled, select_stats = sweep.select_pages(
        conn, since=since, sample_size=settings.sweep_sample, sample_offset=sample_offset)

    run_stats = {
        "changed": len(changed), "sampled": len(sampled),
        "unparsed_result_ref": select_stats["unparsed_result_ref"],
        "changed_page_not_indexed": select_stats["changed_page_not_indexed"],
        "next_sample_offset": select_stats["next_sample_offset"],
        "selected_at": selected_at.isoformat(),
        "inserted": 0, "skipped": 0, "skip_reasons": [], "error": "",
    }
    try:
        judge = sweep.build_judge(settings.model)
        accepted, skip_reasons = await sweep.run_sweep(
            judge, sweep.tag_selected_pages(changed, sampled))
    except Exception as ex:  # noqa: BLE001 — ANY sweep-pass failure must leave the same run's
        # deterministic findings intact (module docstring).
        run_stats["error"] = ex.__class__.__name__
        # Reset to the offset this run STARTED from — a `job_runs` row must not claim a rotation
        # advanced when nothing was actually swept.
        run_stats["next_sample_offset"] = sample_offset
        return [], run_stats

    findings = [sweep.to_finding(spec, model_name=settings.model) for spec in accepted]
    run_stats["inserted"] = len(findings)
    run_stats["skipped"] = len(skip_reasons)
    run_stats["skip_reasons"] = skip_reasons
    return findings, run_stats


def _run_completed_at(conn, run_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT finished_at FROM job_runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    finished_at = row[0] if row else None
    return finished_at.isoformat() if finished_at is not None else ""


async def run_gardener(conn, *, repo: str, settings: GardenerSettings, channels_path: str,
                       gateway=None) -> RunResult:
    """Run every check and the sweep, persist findings + a `job_runs` row in ONE transaction,
    post the SLA notice if one fired, and return the durable result. `RunResult.findings` is the
    RE-FETCHED, persisted list, never the in-memory one — the notice is the one exception
    (below). `gateway=None` is legitimate and only a problem if an `sla` finding fires.

    `channels_path` is resolved to audiences only when an `sla` finding exists — resolving it
    unconditionally would let a malformed channels file abort an info/warn-only run that was
    never going to post anything.
    """
    repo = _require_repo(repo)

    run_stats: dict = {}
    try:
        registry = load_registry(os.path.join(repo, REGISTRY_RELPATH))
        pages_checked = checks.count_indexed_pages(conn)
        entities_checked = len(registry.entities)
        run_stats["pages_checked"] = pages_checked
        run_stats["entities_checked"] = entities_checked

        filing_population_stats: dict = {}
        age_population_stats: dict = {}
        findings = _run_all_checks(conn, repo, registry, settings, filing_population_stats,
                                   age_population_stats)
        run_stats["filing_population_exclusions"] = filing_population_stats
        run_stats["age_population_exclusions"] = age_population_stats

        # Sweep findings join the deterministic ones BEFORE the aggregate counts, so the counts
        # always describe exactly what got persisted.
        sweep_findings, sweep_stats = await _run_sweep_pass(conn, settings)
        run_stats["sweep"] = sweep_stats
        findings += sweep_findings

        by_check: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for f in findings:
            by_check[f["check"]] = by_check.get(f["check"], 0) + 1
            by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        run_stats["findings_total"] = len(findings)
        run_stats["findings_by_check"] = by_check
        run_stats["findings_by_severity"] = by_severity
    except Exception as ex:
        ops.record_job_run(conn, JOB_NAME, status="error", stats=run_stats,
                           error=ex.__class__.__name__)
        raise

    with conn.transaction():
        # 'partial', never 'ok', when the sweep failed — what `previous_run_watermark` needs to
        # stay correct across a sweep outage (module docstring).
        run_status = "partial" if sweep_stats["error"] else "ok"
        run_id = ops.record_job_run(conn, JOB_NAME, status=run_status, stats=run_stats)
        store.insert_findings(conn, run_id, findings)

    persisted = store.findings_for_run(conn, run_id)
    completed_at = _run_completed_at(conn, run_id)

    result = RunResult(run_id=run_id, findings=persisted, pages_checked=pages_checked,
                       entities_checked=entities_checked, completed_at=completed_at,
                       stats=run_stats, sweep_error=sweep_stats["error"],
                       sweep_changed_count=sweep_stats["changed"],
                       sweep_sampled_count=sweep_stats["sampled"])

    try:
        # The PRE-INSERT, in-memory `findings`, never `persisted`: the round trip through the
        # table drops every `_notice_*` key, so the notice wording and its ACL scoping only exist
        # on this list. Audiences are resolved only when an `sla` finding exists (docstring).
        if notice.sla_findings(findings):
            audiences = channels.channel_audiences(channels_path, settings.digest_channel_id)
            findings_to_post = notice.scope_findings_to_channel(conn, findings, audiences=audiences)
        else:
            findings_to_post = findings
        posted = await notice.post_sla_notice(
            gateway, channel=settings.digest_channel_id, findings=findings_to_post,
            run_date=completed_at[:10] if completed_at else "")
        result.notice_posted = posted is not None
    except (GardenerError, SlackApiError, IdentityError) as ex:
        # The findings are already committed — a notice failure must never withhold the report.
        # `IdentityError` = the posting channel's audiences could not be resolved (malformed
        # channels file); caught like a missing token rather than taking the run down.
        result.notice_error = str(ex)
    return result
