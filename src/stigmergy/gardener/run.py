"""Orchestration: run every check, run the sweep, persist findings + a `job_runs` row in one
transaction — the one function `cli.py` calls.

Calls `ops.record_job_run` directly, not the `job_run` context manager: that manager writes its
row on exit, and every finding needs `run_id` at insert time. The try/except below replicates its
shape so a failed run still gets an honest `status='error'` row.

The three MODEL passes can never make this function raise, for one reason — work already done must
not be lost to a later, optional step. An outage of any one must not cost the operator the
deterministic checks that already ran, nor the other passes' findings, so `_run_sweep_pass`,
`_run_empty_body_pass` and `_run_duplicate_entity_pass` each catch everything and report through
their returned stats. Every OTHER failure aborts the run entirely.

A failure of ANY model pass commits `'partial'`, never `'ok'` — a run's status is the one place an
operator learns a whole model pass did not happen. That status is therefore an aggregate over
three independent passes, and NOTHING may read it as a verdict on one of them:
`sweep.previous_run_watermark` asks `stats.sweep.error` whether the editorial sweep itself
completed, precisely so a failure of either OTHER pass cannot freeze the sweep's `since` and its
sample rotation. There is no price to a `'partial'` beyond the status: each pass's watermark, or
absence of one, follows that pass's own recorded outcome.

The entity zone is walked ONCE per run, here, and the same list goes to the deterministic
placeholder check, to the empty-body pass and to the duplicate-identity pass. Two walks straddling
the editorial sweep's model call would let a page edited in between be reported by both checks or
by neither, and the second pass's exclusion of what the first reported is only exact over one page
set. The registry is loaded once here for the same reason, and the duplicate-identity pass reads
the identity of every page from THAT object rather than from a second read of the same file.
"""
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from stigmergy.capture import ops
from stigmergy.gardener import checks, store, sweep
from stigmergy.gardener.errors import GardenerError
from stigmergy.gardener.schema import JOB_NAME
from stigmergy.gardener.settings import (
    DUPLICATE_ENTITY_CEILING_ENV,
    EMPTY_BODY_CEILING_ENV,
    SWEEP_CHANGED_CEILING_ENV,
    GardenerSettings,
)
from stigmergy.kernel.registry import Registry, load_registry

log = logging.getLogger(__name__)

REGISTRY_RELPATH = os.path.join("ops", "entity-registry.json")


@dataclass
class RunResult:
    run_id: int
    findings: list[dict]
    pages_checked: int
    entities_checked: int
    completed_at: str
    # Set only when the sweep failed (class name only, never `str(ex)`); `""` means it succeeded
    # or had nothing to do.
    sweep_error: str = ""
    sweep_changed_count: int = 0
    sweep_sampled_count: int = 0
    # The SECOND model pass, reported separately from the first: the two fail independently, and
    # one number covering both would tell an operator that a pass failed without saying which.
    empty_body_error: str = ""
    empty_body_judged_count: int = 0
    # What the run ceiling deferred. On `RunResult` rather than only in `job_runs.stats` because
    # `report.py` is the surface an operator actually reads, and a bound that bit only in a stats
    # blob reads as "nothing wrong about the pages it never opened".
    empty_body_deferred_count: int = 0
    # The THIRD model pass, reported separately from the other two for the reason the second is:
    # three passes fail independently, and one number covering all of them would tell an operator
    # that a pass failed without saying which.
    duplicate_entity_error: str = ""
    duplicate_entity_judged_count: int = 0
    duplicate_entity_deferred_count: int = 0
    stats: dict = field(default_factory=dict)


def _require_repo(repo: str) -> str:
    if not repo or not os.path.isdir(repo):
        raise GardenerError(
            f"--repo {repo!r} is not a directory — the gardener reads the entity registry and "
            "view staleness from a checkout of the knowledge repo; point it at one.")
    return repo


def _run_all_checks(conn, repo: str, registry: Registry, settings: GardenerSettings,
                    filing_population_stats: dict, age_population_stats: dict,
                    entity_zone_pages: list[dict]) -> list[dict]:
    """`filing_population_stats` and `age_population_stats` are shared sink dicts the checks
    write their exclusion counters into — every excluded row is counted, never silently
    dropped. `entity_zone_pages` is the run's ONE walk of the entity zone (module docstring),
    passed in rather than taken because the two model passes over that zone judge the identical
    list."""
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
    findings += checks.check_entity_placeholder_bodies(entity_zone_pages)
    findings += checks.check_company_wide_fraction(
        conn, window=settings.company_window, share_threshold=settings.company_share,
        population_stats=filing_population_stats)
    findings += checks.check_company_page_names_entity(conn, registry)
    findings += checks.check_anchored_to_superseded_entity(conn)
    findings += checks.check_link_to_narrower_page(conn)
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
        conn, since=since, sample_size=settings.sweep_sample, sample_offset=sample_offset,
        changed_ceiling=settings.sweep_changed_ceiling)

    run_stats = {
        "changed": len(changed), "sampled": len(sampled),
        "unparsed_result_ref": select_stats["unparsed_result_ref"],
        "changed_page_not_indexed": select_stats["changed_page_not_indexed"],
        "excluded_unnameable_path": select_stats["excluded_unnameable_path"],
        "changed_deferred": select_stats["changed_deferred"],
        "next_sample_offset": select_stats["next_sample_offset"],
        "selected_at": selected_at.isoformat(),
        "inserted": 0, "skipped": 0, "skip_reasons": [], "error": "",
    }
    if select_stats["changed_deferred"]:
        run_stats["skip_reasons"].append(sweep.SWEEP_CHANGED_CEILING_REASON.format(
            ceiling=settings.sweep_changed_ceiling, deferred=select_stats["changed_deferred"],
            env=SWEEP_CHANGED_CEILING_ENV))
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
    # APPENDED, not assigned: the changed-ceiling deferral was recorded before the model ran, and
    # a model answer must not be able to erase a selection fact.
    run_stats["skip_reasons"] += skip_reasons
    return findings, run_stats


async def _run_empty_body_pass(zone_pages: list[dict], settings: GardenerSettings, *,
                               walk_exclusions: dict) -> tuple[list[dict], dict]:
    """The second model pass — every entity page from the run's one entity-zone walk that the
    deterministic twin has not already reported, batched — and, like `_run_sweep_pass`, it NEVER
    raises.

    `walk_exclusions` is what that walk REFUSED (unconfined, unreadable, oversized). It is
    recorded here, in the one stats block that describes the entity zone, even though every
    consumer of the walk lost those pages: a page missing from the checks and from the
    population count would let the pass report full coverage of a population it silently
    excluded.

    Three decisions this shape records:

    · **The judge is built only when there is something to judge.** A corpus with no entity pages
      must not pay a model-stack construction, and — the real reason — a missing API key would
      otherwise turn a run with nothing to do into a failed pass.
    · **A batch failure stops the pass, and the batches already done are KEPT.** Stopping, because
      a failing batch is a fact about the judge or about its answers, not about page 57, so the
      remaining batches would most likely just repeat the bill. Keeping, because validated
      findings are validated however the next batch went — the same reasoning that lets a failed
      sweep leave the deterministic findings standing.
    · **What was not judged is COUNTED, never dropped** — `unjudged` for a pass that stopped
      early, `deferred` for the run ceiling, and the ceiling also speaks as a skip reason and a
      log warning, because a bound that bit in silence reads as "nothing wrong here".
    """
    pages, select_stats = sweep.select_empty_body_pages(
        zone_pages, ceiling=settings.empty_body_ceiling)
    run_stats = {**select_stats, "walk_exclusions": dict(walk_exclusions),
                 "batch": settings.empty_body_batch, "batches": 0,
                 "inserted": 0, "skipped": 0, "skip_reasons": [], "unjudged": 0, "error": ""}
    if select_stats["deferred"]:
        reason = sweep.EMPTY_BODY_CEILING_REASON.format(
            ceiling=settings.empty_body_ceiling, deferred=select_stats["deferred"],
            env=EMPTY_BODY_CEILING_ENV)
        run_stats["skip_reasons"].append(reason)
        log.warning("gardener: %s", reason)
    if not pages:
        return [], run_stats

    findings: list[dict] = []
    batches = sweep.in_batches(pages, settings.empty_body_batch)
    judged = 0
    # Validation rejections ONLY, kept apart from `skip_reasons` (which also carries the ceiling
    # sentence): `stats[*]["skipped"]` has to mean the same thing in both passes or a dashboard
    # comparing them compares two different questions. The ceiling has `deferred` for its count.
    rejected = 0
    try:
        judge = sweep.build_empty_body_judge(settings.model)
        for batch in batches:
            accepted, skip_reasons = await sweep.run_empty_body_sweep(judge, batch)
            run_stats["batches"] += 1
            judged += len(batch)
            run_stats["skip_reasons"] += skip_reasons
            rejected += len(skip_reasons)
            findings += [sweep.to_finding(spec, model_name=settings.model) for spec in accepted]
    except Exception as ex:  # noqa: BLE001 — ANY failure of this pass must leave the same run's
        # deterministic findings, and the editorial sweep's, intact (module docstring).
        run_stats["error"] = ex.__class__.__name__
        run_stats["unjudged"] = len(pages) - judged
    run_stats["judged"] = judged
    run_stats["inserted"] = len(findings)
    run_stats["skipped"] = rejected
    return findings, run_stats


async def _run_duplicate_entity_pass(zone_pages: list[dict], registry: Registry,
                                     settings: GardenerSettings) -> tuple[list[dict], dict]:
    """The third model pass — the registry entries behind the run's one entity-zone walk, judged
    for two entries that are one entity — and, like the other two, it NEVER raises.

    ONE call, never batched, and that is this pass's defining property rather than an oversight:
    the question is about PAIRS, and a pair whose two halves fell in different batches would be
    invisible to every batch. So there is no `batch` counter here and no per-batch loop; what
    bounds the spend is the population ceiling and the per-entry character cap in the prompt.

    The population FLOOR is enforced before the judge is built, `proposer.MIN_ANCHORED_PAGES`'
    posture: a registry holding one registered entity cannot hold a pair, and a run that asked
    anyway would reach the same answer and pay for it every night. The skip is RECORDED, because
    "no model was asked" and "the model found nothing" are different facts and only one of them is
    a clean bill of health.
    """
    pages, select_stats = sweep.select_duplicate_entity_pages(
        zone_pages, registry, ceiling=settings.duplicate_entity_ceiling)
    run_stats = {**select_stats, "inserted": 0, "skipped": 0, "skip_reasons": [], "error": ""}
    if select_stats["deferred"]:
        reason = sweep.DUPLICATE_ENTITY_CEILING_REASON.format(
            ceiling=settings.duplicate_entity_ceiling, deferred=select_stats["deferred"],
            env=DUPLICATE_ENTITY_CEILING_ENV)
        run_stats["skip_reasons"].append(reason)
        log.warning("gardener: %s", reason)
    if len(pages) < sweep.MIN_DUPLICATE_ENTITY_POPULATION:
        # Not an error and not a finding: a corpus with fewer than two registered entities is a
        # corpus with no pair to judge. `judged` is corrected to zero so the stats never claim a
        # comparison that no call made.
        if pages:
            run_stats["skip_reasons"].append(sweep.TOO_SMALL_POPULATION_REASON.format(
                population=len(pages), floor=sweep.MIN_DUPLICATE_ENTITY_POPULATION))
        run_stats["judged"] = 0
        return [], run_stats

    findings: list[dict] = []
    try:
        judge = sweep.build_duplicate_entity_judge(settings.model)
        accepted, skip_reasons = await sweep.run_duplicate_entity_sweep(judge, pages)
        run_stats["skip_reasons"] += skip_reasons
        run_stats["skipped"] = len(skip_reasons)
        findings = [sweep.to_finding(spec, model_name=settings.model) for spec in accepted]
    except Exception as ex:  # noqa: BLE001 — ANY failure of this pass must leave the same run's
        # deterministic findings, and both other model passes', intact (module docstring).
        run_stats["error"] = ex.__class__.__name__
        # The whole population went unjudged: this pass is ONE call, so there is no half of it that
        # survived a failure the way a batched pass's earlier batches do.
        run_stats["judged"] = 0
        return [], run_stats
    run_stats["inserted"] = len(findings)
    return findings, run_stats


def _run_completed_at(conn, run_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT finished_at FROM job_runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    finished_at = row[0] if row else None
    return finished_at.isoformat() if finished_at is not None else ""


async def run_gardener(conn, *, repo: str, settings: GardenerSettings) -> RunResult:
    """Run every check and the three model passes, persist findings + a `job_runs` row in ONE
    transaction, and return the durable result. `RunResult.findings` is the RE-FETCHED, persisted
    list, never the in-memory one, so the report renders what is durably true."""
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
        # THE walk of the entity zone for this run — every consumer judges this exact list.
        walk_exclusions: dict = {}
        entity_zone_pages = checks.entity_zone_pages(repo, walk_stats=walk_exclusions)
        findings = _run_all_checks(conn, repo, registry, settings, filing_population_stats,
                                   age_population_stats, entity_zone_pages)
        run_stats["filing_population_exclusions"] = filing_population_stats
        run_stats["age_population_exclusions"] = age_population_stats

        # All three model passes' findings join the deterministic ones BEFORE the aggregate
        # counts, so the counts always describe exactly what got persisted.
        sweep_findings, sweep_stats = await _run_sweep_pass(conn, settings)
        run_stats["sweep"] = sweep_stats
        findings += sweep_findings

        empty_body_findings, empty_body_stats = await _run_empty_body_pass(
            entity_zone_pages, settings, walk_exclusions=walk_exclusions)
        run_stats["empty_body"] = empty_body_stats
        findings += empty_body_findings

        duplicate_findings, duplicate_stats = await _run_duplicate_entity_pass(
            entity_zone_pages, registry, settings)
        run_stats["duplicate_entity"] = duplicate_stats
        findings += duplicate_findings

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
        # 'partial', never 'ok', when ANY model pass failed: a run whose second or third model pass
        # never happened must not read as a clean bill of health for the pages it never looked at.
        # This status is an AGGREGATE and no watermark may be derived from it — that is
        # `previous_run_watermark`'s job, off `stats.sweep.error` (module docstring), so a failed
        # empty-body or duplicate-identity pass costs the editorial sweep's `since` and rotation
        # nothing.
        run_status = "partial" if (sweep_stats["error"] or empty_body_stats["error"]
                                   or duplicate_stats["error"]) else "ok"
        run_id = ops.record_job_run(conn, JOB_NAME, status=run_status, stats=run_stats)
        store.insert_findings(conn, run_id, findings)

    persisted = store.findings_for_run(conn, run_id)
    completed_at = _run_completed_at(conn, run_id)

    return RunResult(run_id=run_id, findings=persisted, pages_checked=pages_checked,
                     entities_checked=entities_checked, completed_at=completed_at,
                     stats=run_stats, sweep_error=sweep_stats["error"],
                     sweep_changed_count=sweep_stats["changed"],
                     sweep_sampled_count=sweep_stats["sampled"],
                     empty_body_error=empty_body_stats["error"],
                     empty_body_judged_count=empty_body_stats["judged"],
                     empty_body_deferred_count=empty_body_stats["deferred"],
                     duplicate_entity_error=duplicate_stats["error"],
                     duplicate_entity_judged_count=duplicate_stats["judged"],
                     duplicate_entity_deferred_count=duplicate_stats["deferred"])
