"""Orchestration: run every check, persist findings + a `job_runs` row in one transaction — the
one function `cli.py` calls.

Calls `ops.record_job_run` directly, not the `job_run` context manager: that manager writes its
row on exit, and every finding needs `run_id` at insert time. The try/except below replicates its
shape so a failed run still gets an honest `status='error'` row.

Every check here is deterministic, so this function asks no model and holds no model budget: a
run either completes or fails, and there is no third outcome where some of the corpus was looked
at and some was not. `job_runs.status` is `'ok'` or `'error'` for a run this code writes —
`'partial'` is a value only rows written by the retired model passes carry, and `store.py` still
reads it for exactly that reason.

The entity zone is walked ONCE per run, here, and the same list goes to the deterministic
placeholder check. The registry is loaded once for the same reason: two walks of one zone in one
run can disagree about what the corpus contained.
"""
import logging
import os
from dataclasses import dataclass, field

from stigmergy.capture import ops
from stigmergy.gardener import checks, store
from stigmergy.gardener.errors import GardenerError
from stigmergy.gardener.schema import JOB_NAME
from stigmergy.gardener.settings import GardenerSettings
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
    stats: dict = field(default_factory=dict)


def _require_repo(repo: str) -> str:
    if not repo or not os.path.isdir(repo):
        raise GardenerError(
            f"--repo {repo!r} is not a directory — the gardener reads the entity registry and "
            "the corpus from a checkout of the knowledge repo; point it at one.")
    return repo


def _run_all_checks(conn, repo: str, registry: Registry, settings: GardenerSettings,
                    filing_population_stats: dict, age_population_stats: dict,
                    entity_zone_pages: list[dict]) -> list[dict]:
    """`filing_population_stats` and `age_population_stats` are shared sink dicts the checks
    write their exclusion counters into — every excluded row is counted, never silently
    dropped. `entity_zone_pages` is the run's ONE walk of the entity zone (module docstring),
    passed in rather than taken so a later second reader of that zone judges the same list."""
    findings: list[dict] = []
    findings += checks.check_orphans(conn)
    aging_seed_stats: dict = {}
    findings += checks.check_aging_seeds(conn, threshold_days=settings.aging_seed_days,
                                         population_stats=aging_seed_stats)
    age_population_stats["aging_seed"] = aging_seed_stats
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


def _run_completed_at(conn, run_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT finished_at FROM job_runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    finished_at = row[0] if row else None
    return finished_at.isoformat() if finished_at is not None else ""


def run_gardener(conn, *, repo: str, settings: GardenerSettings) -> RunResult:
    """Run every check, persist findings + a `job_runs` row in ONE transaction, and return the
    durable result. `RunResult.findings` is the RE-FETCHED, persisted list, never the in-memory
    one, so the report renders what is durably true."""
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
        # THE walk of the entity zone for this run — every consumer judges this exact list. The
        # walk's own refusals (unconfined, unreadable, oversized) are counted rather than dropped:
        # a page missing from the checks AND from the exclusion count would let the run report
        # full coverage of a population it silently shrank.
        walk_exclusions: dict = {}
        entity_zone_pages = checks.entity_zone_pages(repo, walk_stats=walk_exclusions)
        findings = _run_all_checks(conn, repo, registry, settings, filing_population_stats,
                                   age_population_stats, entity_zone_pages)
        run_stats["filing_population_exclusions"] = filing_population_stats
        run_stats["age_population_exclusions"] = age_population_stats
        run_stats["entity_zone_walk_exclusions"] = dict(walk_exclusions)

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
        run_id = ops.record_job_run(conn, JOB_NAME, status="ok", stats=run_stats)
        store.insert_findings(conn, run_id, findings)

    persisted = store.findings_for_run(conn, run_id)
    completed_at = _run_completed_at(conn, run_id)

    return RunResult(run_id=run_id, findings=persisted, pages_checked=pages_checked,
                     entities_checked=entities_checked, completed_at=completed_at,
                     stats=run_stats)
