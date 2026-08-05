"""The retention cron exists as a SCHEDULED WORKFLOW — crons never run as a machine — on the same
schedule host as `index-rebuild.yml`.

Pure YAML parsing — no `gh` command, no live run, no secrets. That a run writes a `job_runs` row
is a database fact and lives with `capture.retention.purge`'s own tests
(`tests/capture/test_queue_pg.py`, `ops.job_run`'s contract); this file only pins that the
workflow that is supposed to CALL it, on a schedule, actually exists and is shaped correctly —
mirroring `test_deployment_config.py`'s posture for `fly.toml`.
"""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RETENTION = WORKFLOWS / "retention-purge.yml"
INDEX_REBUILD = WORKFLOWS / "index-rebuild.yml"
CI = WORKFLOWS / "ci.yml"
GARDENER = WORKFLOWS / "gardener.yml"


def _workflow(path: pathlib.Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_retention_purge_workflow_exists_and_parses():
    assert RETENTION.is_file(), "retention-purge.yml is missing — the scheduled retention cron"
    config = _workflow(RETENTION)
    assert "jobs" in config


def test_retention_purge_is_scheduled_never_a_machine():
    """Crons never run as a machine — a GitHub Actions `schedule` trigger, same host as
    `index-rebuild.yml`, not a `fly.toml` process."""
    config = _workflow(RETENTION)
    # YAML parses the bare key `on` as the boolean True unless quoted — same gotcha
    # `index-rebuild.yml` and `ci.yml` both already carry, so this reads it either way.
    triggers = config.get("on") or config.get(True)
    assert "schedule" in triggers
    assert triggers["schedule"], "no cron entry under `schedule`"
    assert "workflow_dispatch" in triggers, "no manual on-demand trigger — an operator cannot re-run it"


def test_retention_purge_invokes_the_stigmergy_queue_purge_cli():
    """The workflow calls the existing, tested `stigmergy-queue purge` CLI — never a hand-rolled
    SQL DELETE that would drift from the library's own retention predicate."""
    text = RETENTION.read_text(encoding="utf-8")
    assert "stigmergy-queue purge" in text


def test_retention_purge_reuses_the_index_rebuild_dsn_secret_not_a_new_one():
    """No NEW secret surface — the same `SUPABASE_DSN` `index-rebuild.yml` already carries, since
    retention operates on the same Postgres the index (and the queue) live in."""
    retention_text = RETENTION.read_text(encoding="utf-8")
    rebuild_text = INDEX_REBUILD.read_text(encoding="utf-8")
    assert "secrets.SUPABASE_DSN" in retention_text
    assert "secrets.SUPABASE_DSN" in rebuild_text


def test_retention_purge_declares_read_only_contents_permission():
    """Least privilege, same posture as `index-rebuild.yml`: this workflow only calls a CLI against
    Postgres, it never needs to write back to the repository."""
    config = _workflow(RETENTION)
    assert config.get("permissions", {}).get("contents") == "read"


def test_ci_workflow_stays_keyless_and_untouched_by_the_new_secret():
    """`ci.yml` (tests/lint) carries no secret at all — retention's `SUPABASE_DSN` must not leak
    into the keyless pipeline."""
    ci_text = CI.read_text(encoding="utf-8")
    assert "SUPABASE_DSN" not in ci_text
    assert "secrets." not in ci_text



def _purge_job_steps() -> list[dict]:
    return _workflow(RETENTION)["jobs"]["purge"]["steps"]


def _step_named(fragment: str) -> dict:
    step = next((s for s in _purge_job_steps() if fragment in (s.get("name") or "")), None)
    assert step is not None, f"no step in retention-purge.yml named like {fragment!r}"
    return step


# ── one failing step must not silently skip the rest of the job ─────────────────────────────────
# GitHub Actions skips every LATER step in a job once one fails, unless that step carries its own
# `if:`. Before this was fixed, the unconditional capture-queue purge silently stopped running
# every night an earlier step stayed stuck. Regression-proofed at the YAML level: a future edit
# that drops that `if:` reintroduces the exact bug, and nothing else in this suite (a database
# fact, not a GitHub Actions control-flow fact) would ever catch it.
def test_capture_purge_step_runs_even_if_an_earlier_step_in_the_job_failed():
    step = _step_named("Purge (writes a job_runs row")
    assert step.get("if") == "${{ !cancelled() }}"


def test_retention_purge_header_restates_the_process_group_ceiling():
    """The ceiling: this is a cron in GitHub Actions, never a process group — nothing in this
    system runs unattended outside Actions. The header restates it the way `fly.toml` does, and
    that is checked the same way this file checks every other header claim (a text-content
    assertion, this file's own convention, never a live run)."""
    text = RETENTION.read_text(encoding="utf-8")
    assert "process group" in text.lower()
    assert "unattended outside actions" in text.lower()


# ── the gardener.yml trial workflow ─────────────────────────────────────────────────────────────
# `gardener.yml` was added WITHOUT the mirroring coverage this file already gives
# `retention-purge.yml`/`index-rebuild.yml`: nothing protected its schedule, its step order, the
# `if: ${{ !cancelled() }}` isolation, or its header's own claims from silent drift — even though
# that header explicitly claims to apply the same lesson about one failing step skipping the rest
# of a job. The tests below are what keeps the claim true.
def test_gardener_workflow_exists_and_parses():
    assert GARDENER.is_file(), (
        "gardener.yml is missing — the daily trial cron")
    config = _workflow(GARDENER)
    assert "jobs" in config


def test_gardener_is_scheduled_with_workflow_dispatch():
    """A daily GitHub Actions `schedule` trigger — crons never run as a machine — plus a manual
    on-demand escape hatch: the same two-trigger shape `retention-purge.yml`/`index-rebuild.yml`
    both already carry."""
    config = _workflow(GARDENER)
    triggers = config.get("on") or config.get(True)   # the same YAML `on:` gotcha this file
                                                       # already reads around, above
    assert "schedule" in triggers
    assert triggers["schedule"], "no cron entry under `schedule`"
    assert "workflow_dispatch" in triggers, "no manual on-demand trigger — an operator cannot re-run it"


def test_gardener_runs_after_index_rebuild_and_retention_purge():
    """The gardener runs daily, AFTER `index-rebuild` and `retention-purge` — pinned as an ordered
    comparison of the three cron entries' own minute-of-day, not merely as three separately
    plausible-looking strings, so a future edit that moves any ONE of the three schedules out of
    order is caught here rather than only in a 3 a.m. reading race."""
    def _minute_of_day(cron_expr: str) -> int:
        minute, hour = cron_expr.split()[:2]
        return int(hour) * 60 + int(minute)

    def _schedule_cron(config: dict) -> str:
        # the same `on:` -> `True` YAML gotcha this file already reads around elsewhere.
        triggers = config.get("on") or config.get(True)
        return triggers["schedule"][0]["cron"]

    gd_cron = _schedule_cron(_workflow(GARDENER))
    retention_cron = _schedule_cron(_workflow(RETENTION))
    rebuild_cron = _schedule_cron(_workflow(INDEX_REBUILD))

    assert _minute_of_day(rebuild_cron) < _minute_of_day(retention_cron) < _minute_of_day(gd_cron), (
        f"expected index-rebuild ({rebuild_cron!r}) < retention-purge ({retention_cron!r}) < "
        f"gardener ({gd_cron!r}) in time-of-day order")


def test_gardener_declares_read_only_contents_permission():
    """Least privilege, same posture as `retention-purge.yml`/`index-rebuild.yml`: this workflow
    only calls two CLIs against Postgres/Slack, it never needs to write back to the repository."""
    config = _workflow(GARDENER)
    assert config.get("permissions", {}).get("contents") == "read"


def test_gardener_serializes_concurrent_runs_without_cancelling_an_in_flight_one():
    """The realistic collision is `schedule` + `workflow_dispatch` (or a retry of one) landing
    close together — `concurrency:` queues the later trigger onto the SAME group
    rather than either racing it or dropping it silently. `cancel-in-progress: false`: a queued
    run WAITS instead of killing an in-flight one — cancelling a gardener run mid-write would
    discard real, already-computed work for no benefit, and queueing is strictly safer than
    cancelling for a job whose whole point is a complete daily read."""
    config = _workflow(GARDENER)
    concurrency = config.get("concurrency")
    assert concurrency is not None, "no top-level concurrency: block"
    assert concurrency.get("group") == "gardener"
    assert concurrency.get("cancel-in-progress") is False


def _gardener_job_steps() -> list[dict]:
    jobs = _workflow(GARDENER)["jobs"]
    assert len(jobs) == 1, "expected exactly one job — one cron, never a process group"
    return next(iter(jobs.values()))["steps"]


def _gd_step_named(fragment: str) -> dict:
    """Matches step NAMES, not `run:` command lines — `fragment` must be specific enough not to
    also match the first checkout step's own name (which mentions both CLIs in prose: "this repo
    — the stigmergy-gardener/stigmergy-digest CLIs + deps"). Proven exactly by the bug this test file
    itself caught: an earlier version of this helper, called with the bare fragment
    "stigmergy-digest", silently matched that checkout step instead of the real "stigmergy-digest —
    ..." step — and `test_digest_step_runs_even_if_the_gardener_step_failed` went red on the FIRST
    run because of it. Before trusting a check, ask whether it can go red, and prove it."""
    step = next((s for s in _gardener_job_steps() if fragment in (s.get("name") or "")),
               None)
    assert step is not None, f"no step in gardener.yml named like {fragment!r}"
    return step


def test_gardener_invokes_the_gardener_cli():
    text = GARDENER.read_text(encoding="utf-8")
    assert "stigmergy-gardener" in text




def test_gardener_step_itself_carries_no_if_its_own_failure_must_still_fail_the_job():
    """The header's own claim, made mechanical: "stigmergy-gardener itself carries no if: — its own
    failure must still fail this JOB (so the cron reports it)". The isolation `if:` protects LATER
    steps from an earlier failure; it must never protect the job from this step's own."""
    step = _gd_step_named("stigmergy-gardener — ")
    assert step.get("if") is None



def test_gardener_reuses_the_existing_dsn_and_openai_secrets_not_new_ones():
    """No NEW secret surface for the DSN/model-call secrets — the same `SUPABASE_DSN`
    `retention-purge.yml` already carries and the same `OPENAI_API_KEY` `index-rebuild.yml` does,
    reused verbatim. `retention-purge.yml` calls no model at all and carries only the DSN, which
    is why it is asserted to be WITHOUT the key rather than with it."""
    gd_text = GARDENER.read_text(encoding="utf-8")
    retention_text = RETENTION.read_text(encoding="utf-8")
    index_text = INDEX_REBUILD.read_text(encoding="utf-8")
    assert "secrets.SUPABASE_DSN" in gd_text
    assert "secrets.SUPABASE_DSN" in retention_text
    assert "secrets.OPENAI_API_KEY" in gd_text
    assert "secrets.OPENAI_API_KEY" in index_text
    assert "secrets.OPENAI_API_KEY" not in retention_text


def test_gardener_carries_its_own_slack_and_channel_configuration():
    """The two names the gardener/digest settings modules read
    (`SLACK_BOT_TOKEN_ENV`/`DIGEST_CHANNEL_ID_ENV`) must actually reach the workflow's env block —
    a real secret (a bot token) and a real repository VARIABLE (a channel id is not a credential),
    matching the header comment's own stated distinction."""
    text = GARDENER.read_text(encoding="utf-8")
    assert "secrets.SLACK_BOT_TOKEN" in text
    assert "vars.STIGMERGY_DIGEST_CHANNEL_ID" in text


def test_gardener_actions_are_sha_pinned_like_its_siblings():
    """Same posture as `retention-purge.yml`/`index-rebuild.yml`: a `uses:` value must be a full
    commit SHA (`@` followed by 40 hex characters), never a floating tag a maintainer (or an
    attacker) could move."""
    import re
    text = GARDENER.read_text(encoding="utf-8")
    uses_lines = re.findall(r"uses:\s*(\S+)", text)
    assert uses_lines, "no `uses:` action reference found — the fixture regex went blind"
    for ref in uses_lines:
        assert re.search(r"@[0-9a-f]{40}\b", ref), f"{ref!r} is not pinned to a full commit SHA"



def test_ci_workflow_stays_keyless_and_untouched_by_the_gardener_secrets():
    """The keyless rule, extended to this workflow's own two new names: `ci.yml` (tests/lint) must
    carry neither the Slack bot token nor the digest channel variable — CI exercises the offline
    `FakeGardenerSweep`/`FakeSlackGateway` (`CLEAN_LLM=fake`) instead, never a real key or a real
    post."""
    ci_text = CI.read_text(encoding="utf-8")
    assert "SLACK_BOT_TOKEN" not in ci_text
    assert "STIGMERGY_DIGEST_CHANNEL_ID" not in ci_text
    assert "STIGMERGY_GARDENER_MODEL" not in ci_text










def test_ci_workflow_stays_keyless_and_untouched_by_the_supervisor_secrets():
    """The keyless rule, extended to two more families of name. The supervisor's own channel and
    model variables name a component that no longer exists, and must not reappear through CI of
    all places; the evidence-store secrets are live, and CI runs against `MemoryEvidenceStore`
    (`CLEAN_LLM=fake`) instead — never a real key, a real bucket or a real post."""
    ci_text = CI.read_text(encoding="utf-8")
    assert "STIGMERGY_SUPERVISOR_CHANNEL_ID" not in ci_text
    assert "STIGMERGY_SUPERVISOR_MODEL" not in ci_text
    assert "STIGMERGY_EVIDENCE_" not in ci_text
