"""Orchestration: resolve the window, gather the sections, build the body, post (or preview),
record `job_runs` + the watermark — the one function `cli.py` calls.

Window start (`_resolve_since`): `--since` override, else the latest completed `job='digest'`
run's `stats['until']`, else `now - window_days`. The watermark is `stats['until']`, never
`job_runs.started_at`: `started_at` is written at INSERT time, after the queries AND the post, so
an event landing between the two would fall into no window at all — `stats['until']` is the
boundary the queries were actually bounded by, keeping consecutive windows exactly contiguous.

A `--dry-run` writes its own row under `job='digest-dry-run'`; `_watermark_since` reads only
`job='digest'`, so a preview can never consume a window a real post has not covered.

Post, then record — in that order. An interrupt between the two leaves a posted message with no
recorded watermark: an accepted, NAMED risk `cli.py`'s interrupt copy warns about.
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
    """`--since YYYY-MM-DD` -> midnight UTC that day; a malformed value raises a named
    `DigestError`, never a raw traceback."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise DigestError(
            f"--since {value!r} is not a valid date (expected YYYY-MM-DD) — nothing was read or "
            f"posted") from None


def last_window_until(conn) -> str | None:
    """The last completed `job='digest'` run's `stats['until']`, as the ISO string it is stored as
    — `None` when no such run exists. Public because the admin console reports the same fact and
    must never re-type this query."""
    with conn.cursor() as cur:
        cur.execute(_WATERMARK_SQL, (JOB_NAME,))
        row = cur.fetchone()
    return row[0] if row else None


def _watermark_since(conn) -> datetime | None:
    """The last completed run's `stats['until']`; `None` when no completed run exists or its
    `stats` carries no `until` (defensive — falls through to the default-window branch)."""
    raw = last_window_until(conn)
    return datetime.fromisoformat(raw) if raw else None


def _resolve_since(conn, *, since_override: datetime | None, window_days: int,
                   now: datetime) -> datetime:
    if since_override is not None:
        return since_override
    watermark = _watermark_since(conn)
    if watermark is not None:
        return watermark
    return now - timedelta(days=window_days)


def _require_channel(digest_channel_id: str) -> str:
    """Fail closed — checked only when a REAL post is about to happen; a dry run never needs
    it."""
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

    `now`/`since_override` are injectable so a test drives a seeded window without the wall
    clock; the real clock is read exactly once, at the top of a real run. Any failure records an
    honest `status='error'` row — the exception's CLASS NAME only, never `str(ex)`, which could
    echo page content — before re-raising."""
    now = now or datetime.now(UTC)
    job = JOB_NAME_DRY_RUN if dry_run else JOB_NAME
    stats: dict = {"dry_run": dry_run}
    posted = False
    try:
        since = _resolve_since(conn, since_override=since_override,
                               window_days=settings.window_days, now=now)
        # The LIVE road, like every other consumer of this fact (`slack.mention`): the
        # baked file is the copy from the last deploy, so a channel NARROWED in the
        # knowledge repo would keep its wider groups here and the digest would broadcast
        # page titles at that wider scope until the next rollout — staleness failing open,
        # on the one surface whose whole job is to post into a room (issue #79's shape).
        audiences = channels.channel_audiences_live(
            conn, channels_path, settings.digest_channel_id)

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
