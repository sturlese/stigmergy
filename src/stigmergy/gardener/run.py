"""Orchestration: run every deterministic check, run the model sweep, persist findings + a
`job_runs` row, post the SLA notice if any `sla` finding fired — the one function `cli.py` calls,
and what a test drives directly (mirrors `views.regenerate.run`'s "the library is what's tested,
the CLI is a thin wrapper" posture).

**Bypasses `capture.ops.job_run`'s context manager on purpose.** That contextmanager only writes
the `job_runs` row on exit, so its own id is never available to code INSIDE the block — and every
finding needs `run_id` at insert time. This calls `ops.record_job_run` directly instead,
replicating the identical try/except/re-raise shape `job_run`'s own implementation has (it is a
thin wrapper over the same function), so a run that fails still gets an honest `status='error'`
row carrying whatever stats were gathered before the fault.

**The sweep pass (`_run_sweep_pass`) can never make this function raise.** Every OTHER failure
inside the `try` block below (a malformed registry, a broken query) aborts the run entirely —
`status='error'`, zero findings, by design, because nothing computed so far can be trusted either.
The sweep is different: a sweep outage (a hard model-call failure, a `SweepGarbage` retry
exhaustion, even a bare misconfiguration like a missing API key) must NOT cost the operator the
eight deterministic checks that already ran successfully in the SAME run. So `_run_sweep_pass`
catches everything itself and reports failure through its own returned stats (`error`, class name
only) instead of raising — the deterministic findings still commit, in the SAME transaction,
regardless.

**The run's overall `job_runs.status` becomes `'partial'`, never `'ok'`, when the sweep failed.**
The deterministic findings being complete is not enough to call the run `'ok'`, and the reason
matters beyond honesty: `gardener.sweep.previous_run_watermark` reads the latest `status='ok'`
run's own watermark to decide what "changed since last time" means for the NEXT sweep. Committing
`'ok'` on a sweep failure makes that watermark advance anyway — a week of model outage under the
daily cron would mean seven `'ok'` rows, a watermark advancing daily, and every page filed during
the outage permanently excluded from the "changed" set (recoverable only through the rotating
sample, whose own offset would also have silently advanced past pages nothing had judged) — while
`job_runs WHERE status='error'` reported zero failures the whole time. `'partial'` avoids that
without touching the report or the exit code: the deterministic findings persist and render
exactly as they would otherwise, and only the DB status changes, so a watermark reader can tell
"the sweep's own baseline did not move this run" from "everything about this run is trustworthy."
See `capture.ops`'s module docstring for the full three-value vocabulary this status column
carries.
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
    # Set only when an `sla` finding fired but the notice itself could not be posted (no bot
    # token, no channel configured, or a real `SlackApiError`) — the findings above are still
    # complete and already persisted; see `run_gardener`'s own comment on why this is captured
    # rather than raised.
    notice_error: str = ""
    # Set only when the model sweep itself failed this run (class name only, never `str(ex)`) —
    # the deterministic findings above are still complete and already persisted regardless (module
    # docstring). `""` means the sweep either succeeded or found nothing to do.
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
    """`filing_population_stats` is a shared sink dict — `check_anchor_concentration` and
    `check_company_wide_fraction` each write their own `_recent_filed_pages` exclusion counters
    into it (keyed `anchor_concentration`/`company_wide_fraction`), so an operator can see how many
    of a window's filings were dropped (an unparsed `result_ref`, a page no longer indexed, a
    provenance page) before either check ever got to judge a share. A counter that is computed and
    never surfaced is indistinguishable from no counter at all.

    `age_population_stats` is the identical pattern, one section over: `check_aging_seeds` writes
    its own malformed-`updated` count into it (keyed `aging_seed`) — a page whose `updated` is not
    empty but does not parse as a date is excluded from the age comparison rather than crashing the
    query, and counted rather than silently dropped."""
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
    """Runs the model sweep as ONE self-contained unit and NEVER raises (see this module's own
    docstring for why): `stats["error"]` is `""` on success (including the honest "nothing to
    sweep" case), or the failing exception's CLASS NAME — never `str(ex)`, which can carry
    fragments of page content the model read.

    Page selection (`sweep.select_pages`) happens OUTSIDE the try/except below on purpose: a bug
    in that pure-SQL selection is a real defect in this package's own code, not a "sweep outage"
    (a model-call failure or unusable model output) — it should fail the run loudly like any other
    `checks.py` defect would, not be swallowed here.

    `selected_at` is captured HERE, immediately before `select_pages` runs — the honest boundary
    this sweep actually read up to — and persisted in `stats["selected_at"]` for
    `previous_run_watermark` to prefer over `job_runs.started_at` (written later, at INSERT time,
    after this whole function returns and the deterministic checks' own findings commit too). A
    page filed between `selected_at` and `started_at` would otherwise fall in NO sweep window
    ever: THIS run's own `since` was already resolved before it existed, and a `started_at`-based
    read of the NEXT run's `since` would start strictly AFTER it existed too.
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
    except Exception as ex:  # noqa: BLE001 — ANY sweep-pass failure (a hard model-call error via
        # `AgentRunError`, a `SweepGarbage` retry exhaustion, or a bare misconfiguration like a
        # missing API key raised straight out of `build_judge`) must leave the deterministic
        # findings computed in the SAME run intact — see this module's own docstring.
        run_stats["error"] = ex.__class__.__name__
        # The rotation must not silently advance past pages nothing actually judged. Reset to the
        # OFFSET THIS RUN STARTED FROM (`sample_offset`, captured before `select_pages` ran the
        # rotation forward), not the post-rotation value already written into `run_stats` above.
        # Functionally this row's own `next_sample_offset` is never read as a baseline anyway
        # (`previous_run_watermark` only reads `status='ok'` rows, and this run commits `'partial'`
        # — see `run_gardener`) — recorded honestly regardless, per this package's own "counted,
        # never silently dropped" discipline: a `job_runs` row must not claim a rotation advanced
        # when nothing was actually swept.
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
    """Run every deterministic check, run the model sweep, persist a `job_runs` row + this run's
    findings (one transaction — never a partial insert), post the SLA notice if any `sla` finding
    fired, and return the durable result. `findings` on the returned `RunResult` is the
    RE-FETCHED, persisted list (`store.py`'s own "what a reader sees is never allowed to drift
    from what is stored" rule), not the in-memory list the checks/sweep built — the notice itself
    is the one exception (below).

    `channels_path` — threaded down the way `digest.run.run_digest` already takes it, resolved by
    `cli.py` the identical way `digest/cli.py` does (`args.channels or
    channels.default_path(repo)`) — is this run's own source for the SLA notice's channel-scoping
    audiences; see the notice-posting block below.

    `gateway=None` is a legitimate value (no `$SLACK_BOT_TOKEN` configured) and is only a problem
    if an `sla` finding actually fires this run — see `notice.post_sla_notice`.

    **`channels_path` is resolved to audiences no earlier than `notice.post_sla_notice`'s own SLA
    short-circuit would resolve them** — mirroring, in this caller, the identical "nothing to post
    -> touch nothing Slack-shaped" precondition ordering `post_sla_notice`'s own docstring promises
    for `gateway`/`channel`. Resolving `channels.channel_audiences` unconditionally instead lets a
    malformed `ops/slack-channels.json` abort even an info/warn-only run that was never going to
    post anything — contradicting that exact docstring promise, losing the run's own report in the
    process (the exception propagates past `cli.py`'s own print statements), and reporting "the run
    failed" when the run had in fact succeeded and only a notice nothing needed went unresolved.
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

        # The sweep never raises out of this block (see `_run_sweep_pass`'s own docstring) — its
        # (possibly empty) findings join the deterministic ones BEFORE the aggregate counts below
        # are computed, so `findings_total`/`findings_by_check`/`findings_by_severity` always
        # describe exactly what actually got persisted, sweep included, in ONE pass.
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
        # 'partial', never 'ok', when the sweep failed — see this module's own docstring for why
        # an honest status here (rather than the deterministic findings' own completeness) is what
        # `gardener.sweep.previous_run_watermark` needs to stay correct across a sweep outage.
        # Bare string literals, matching every other `job_runs.status` write in this codebase
        # (including `status="error"` a few lines above) — see `capture.ops`'s module docstring
        # for the vocabulary these literals are drawn from.
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
        # The PRE-INSERT, in-memory `findings` — never `persisted`. `insert_findings`/
        # `findings_for_run` round-trip only the columns `gardener_findings` actually has
        # (`store.py`'s own comment), so `persisted` has already lost every `_notice_*` key by the
        # time it reaches here: the notice's own dedicated wording, and the `_notice_page_paths`
        # ACL scoping below, would otherwise be dead code on the real path (the DB content is
        # identical either way; only these ephemeral, never-persisted keys differ).
        #
        # `channels.channel_audiences` — and, with it, the ACL scoping that needs its result — is
        # resolved ONLY when this run actually has an `sla` finding to post, through
        # `notice.sla_findings` rather than a re-derived copy of that filter: it is the identical
        # predicate `post_sla_notice` itself short-circuits on. An info/warn-only run therefore
        # never reads `channels_path` at all, exactly as `post_sla_notice`'s own docstring
        # promises for `gateway`/`channel`; resolving it unconditionally would let a malformed
        # `ops/slack-channels.json` raise `IdentityError` and abort a run that was never going to
        # post anything.
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
        # The findings above are ALREADY committed — a notice failure must never make the report
        # withhold a successfully-computed, already-persisted result (the same "one pass's outage
        # must not take an independent, already-computed pass down with it" posture the model sweep
        # takes, applied here one seam over). `IdentityError` means an `sla` finding fired and the
        # posting channel's own audiences could not be resolved (a malformed
        # `ops/slack-channels.json`) — the notice genuinely cannot be scoped, so it is caught here
        # exactly like a missing bot token or a real `SlackApiError` rather than left to escape and
        # take the whole run's report down with it.
        result.notice_error = str(ex)
    return result
