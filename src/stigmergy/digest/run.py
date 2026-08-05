"""Orchestration: resolve the window, gather the two sections, assemble the body, post (or
preview), record `job_runs` + the watermark — the one function `cli.py` calls, mirroring
`gardener.run.run_gardener`'s own "the library is what's tested, the CLI is a thin wrapper"
posture.

**Watermark.** `job_runs.stats` is already how `gardener` records a run's own bookkeeping (a
dedicated tiny table would buy nothing the JSONB blob does not already give); the digest reuses
the SAME shape, one job name over (`JOB_NAME = "digest"`). The window start, resolved in this
priority order (`_resolve_since`): an explicit `--since` override; else the latest COMPLETED
(`status='ok'`) `job='digest'` run's own `stats['until']` (the existing `(job, started_at DESC)`
index finds the ROW; the WATERMARK VALUE itself comes from its `stats`, not the row's own
`started_at` — see the next paragraph for why); else `now - settings.window_days`, a genuine
first-ever run.

**`stats['until']`, never `job_runs.started_at`, is the watermark.**
`capture.ops.record_job_run`'s own `_INSERT_JOB_RUN` writes `started_at = now()` at INSERT time —
after every section query below AND the Slack post that follows them. An event landing between
"the queries ran" (bounded at THIS run's own `now`, captured at the top of this function, before
any query) and "the row committed" would, under a `started_at`-based watermark, fall into no
digest window at all: this run's own queries were already bounded above `now`, and the NEXT run's
`since` would start from a strictly LATER instant (`started_at`, always > `now`). `stats['until']`
is `now` itself, persisted below — the honest boundary every section query was ACTUALLY bounded
by — so consecutive windows are exactly contiguous rather than approximately so.

**A `--dry-run` never advances the watermark.** It writes its OWN `job_runs` row, under
`job='digest-dry-run'` — the same `job`/`job-dry-run` naming convention `capture.retention.purge`
uses, for the exact same reason: `_watermark_since` below reads ONLY `job='digest'`, so previewing
a window and later actually posting it stay two independent acts, and a preview can never silently
consume a window a real post has not covered yet.

**Post, then record — in that order.** The interrupt copy in `cli.py` depends on this exact
sequence: the message reaches Slack BEFORE the `job_runs` row commits, so an interrupt landing
between the two leaves a posted message with no recorded watermark. That is a real, accepted,
and NAMED risk (the honest alternative to a distributed transaction this system does not have),
never silently assumed away — see `cli.py`'s own interrupted-message handling.
"""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from stigmergy.capture import ops
from stigmergy.digest import sections
from stigmergy.digest.errors import DigestError
from stigmergy.digest.render import build_body
from stigmergy.digest.settings import DIGEST_CHANNEL_ID_ENV, DigestSettings
from stigmergy.slack import channels
from stigmergy.slack.gateway import SlackGateway

JOB_NAME = "digest"
JOB_NAME_DRY_RUN = "digest-dry-run"

_WATERMARK_SQL = (
    "SELECT stats ->> 'until' FROM job_runs WHERE job = %s AND status = 'ok' "
    "ORDER BY started_at DESC LIMIT 1")


@dataclass
class DigestResult:
    run_id: int | None
    body: str
    posted: bool
    since: datetime
    until: datetime


def parse_since(value: str) -> datetime:
    """`--since YYYY-MM-DD` -> midnight UTC that day. Fail-closed: unlike `server.pilot_report`'s
    own UNGUARDED `datetime.strptime`, a malformed value raises a named, actionable `DigestError`
    rather than a raw traceback reaching the operator."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise DigestError(
            f"--since {value!r} is not a valid date (expected YYYY-MM-DD) — nothing was read or "
            f"posted") from None


def _watermark_since(conn) -> datetime | None:
    """The last completed run's own `stats['until']`, parsed back from the JSON string
    `stats ->> 'until'` extracts. `None` when no completed run exists yet, OR when one exists but
    its `stats` carries no `until` at all (defensive only — every 'ok' `job='digest'` row this
    package writes stores `until`, see `run_digest` below; a plain `None` here safely falls through
    to `_resolve_since`'s own default-window branch, the same posture
    `gardener.sweep.previous_run_watermark` takes for a row with no `sweep` stats at all)."""
    with conn.cursor() as cur:
        cur.execute(_WATERMARK_SQL, (JOB_NAME,))
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    return datetime.fromisoformat(row[0])


def _resolve_since(conn, *, since_override: datetime | None, window_days: int,
                   now: datetime) -> datetime:
    if since_override is not None:
        return since_override
    watermark = _watermark_since(conn)
    if watermark is not None:
        return watermark
    return now - timedelta(days=window_days)


def _require_channel(digest_channel_id: str) -> str:
    """Fail-closed, mirroring `gardener.notice.require_channel`'s exact "name the var, name the
    consequence, name the fix" shape — checked ONLY when a REAL post is about to happen, so a dry
    run, whose whole point is to preview the body without posting, never needs it."""
    if not digest_channel_id:
        raise DigestError(
            f"${DIGEST_CHANNEL_ID_ENV} is not set — there is nowhere configured to post this "
            f"to. Set it to a Slack channel id (e.g. C0123456789), or run with --dry-run to "
            f"preview the body without posting.")
    return digest_channel_id


async def run_digest(conn, *, settings: DigestSettings, channels_path: str,
                     gateway: SlackGateway | None, dry_run: bool,
                     since_override: datetime | None = None,
                     now: datetime | None = None) -> DigestResult:
    """Gather, render, and either post (recording the watermark) or preview (never advancing it).

    `now`/`since_override` are injectable so a test drives a seeded window without ever touching
    the wall clock — that determinism is what this function's testability rests on, and `cli.py`
    never passes `now`, letting it default to the real clock exactly once, at the top of a real
    run.

    Any failure — a query error, a missing channel/token on a real run, a `SlackApiError` while
    posting — records an honest `status='error'` `job_runs` row (the exception's CLASS NAME only,
    never `str(ex)`, which could echo back a fragment of page content) before re-raising, the same
    posture `gardener.run.run_gardener` already takes for its own run-level failures."""
    now = now or datetime.now(UTC)
    job = JOB_NAME_DRY_RUN if dry_run else JOB_NAME
    stats: dict = {"dry_run": dry_run}
    posted = False
    try:
        since = _resolve_since(conn, since_override=since_override,
                               window_days=settings.window_days, now=now)
        audiences = channels.channel_audiences(channels_path, settings.digest_channel_id)

        health = sections.gather_corpus_health(conn, since=since)
        deltas = sections.gather_corpus_deltas(conn, since=since, until=now, audiences=audiences)
        body = build_body(since=since, until=now, health=health, deltas=deltas)
        stats.update({"since": since.isoformat(), "until": now.isoformat()})

        if not dry_run:
            channel = _require_channel(settings.digest_channel_id)
            if gateway is None:
                raise DigestError(
                    "no Slack bot token is configured ($SLACK_BOT_TOKEN) — the digest cannot "
                    "post. Set the token and re-run, or preview it with --dry-run instead.")
            await gateway.chat_post_message(channel, text=body)
            posted = True
    except Exception as ex:
        ops.record_job_run(conn, job, status="error", stats=stats, error=ex.__class__.__name__)
        raise

    run_id = ops.record_job_run(conn, job, status="ok", stats=stats)
    return DigestResult(run_id=run_id, body=body, posted=posted, since=since, until=now)
