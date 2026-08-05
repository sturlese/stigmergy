"""`digest.run` against real Postgres: the watermark's priority chain, `--dry-run` never advancing
it, post-then-record ordering, and the honest failure/refusal paths — offline
(`FakeSlackGateway`, no real Slack credentials), the same posture `tests/gardener/test_notice.py`
takes one package over.
"""
import asyncio
import datetime

import pytest

from stigmergy.capture import ops
from stigmergy.digest import run
from stigmergy.digest.errors import DigestError
from stigmergy.digest.settings import DigestSettings
from stigmergy.slack.gateway import FakeSlackGateway, SlackApiError

UTC = datetime.UTC


def _now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


# ── parse_since ───────────────────────────────────────────────────────────────────────────────
def test_parse_since_accepts_iso_date():
    assert run.parse_since("2026-07-24") == datetime.datetime(2026, 7, 24, tzinfo=UTC)


@pytest.mark.parametrize("bad", ["next-tuesday", "2026/07/24", "24-07-2026", "", "2026-13-40"])
def test_parse_since_refuses_a_malformed_date(bad):
    with pytest.raises(DigestError, match="YYYY-MM-DD"):
        run.parse_since(bad)


# ── _resolve_since priority: --since > watermark > default window ───────────────────────────────
def test_resolve_since_prefers_an_explicit_override(conn):
    now = _now()
    override = now - datetime.timedelta(days=2)
    since = run._resolve_since(conn, since_override=override, window_days=7, now=now)
    assert since == override


def test_resolve_since_falls_back_to_the_watermark_when_no_override_is_given(conn):
    """The watermark is the prior run's own `stats['until']`, never its `started_at` — seeded here
    as a value CLEARLY DIFFERENT from `started_at` (which `record_job_run` stamps at real insert
    time, effectively "now") so the assertion cannot pass by accident, which is what it did when
    the two happened to land close together in a fast test."""
    watermark = _now() - datetime.timedelta(days=3)
    # a completed prior 'digest' run — job_runs written directly, mirroring
    # `tests.gardener.support.seed_gardener_job_run`'s own shape one job name over.
    ops.record_job_run(conn, run.JOB_NAME, status="ok", stats={"until": watermark.isoformat()})
    since = run._resolve_since(conn, since_override=None, window_days=7, now=_now())
    assert since == watermark


def test_resolve_since_ignores_a_watermark_row_with_no_until_in_its_stats(conn):
    """Defensive fallback: a 'digest' 'ok' row with no `until` key at all (which no row THIS
    package's own `run_digest` writes ever lacks — but a read must not crash on one either) falls
    all the way back to the default window, the same posture
    `gardener.sweep.previous_run_watermark` takes for a row with no `sweep` stats."""
    now = _now()
    ops.record_job_run(conn, run.JOB_NAME, status="ok", stats={})
    since = run._resolve_since(conn, since_override=None, window_days=7, now=now)
    assert abs((since - (now - datetime.timedelta(days=7))).total_seconds()) < 5


def test_resolve_since_ignores_a_dry_run_watermark(conn):
    ops.record_job_run(conn, run.JOB_NAME_DRY_RUN, status="ok", stats={})
    now = _now()
    since = run._resolve_since(conn, since_override=None, window_days=7, now=now)
    # no REAL 'digest' watermark exists — falls all the way back to the default window, never the
    # dry-run job's own row.
    assert abs((since - (now - datetime.timedelta(days=7))).total_seconds()) < 5


def test_resolve_since_defaults_to_the_window_on_a_genuine_first_run(conn):
    now = _now()
    since = run._resolve_since(conn, since_override=None, window_days=7, now=now)
    assert since == now - datetime.timedelta(days=7)


def test_resolve_since_ignores_a_failed_prior_run(conn):
    ops.record_job_run(conn, run.JOB_NAME, status="error", stats={})
    now = _now()
    since = run._resolve_since(conn, since_override=None, window_days=14, now=now)
    assert since == now - datetime.timedelta(days=14)


# ── --dry-run: never posts, never advances the watermark ────────────────────────────────────────
def test_dry_run_posts_nothing_and_returns_the_exact_body(conn):
    gateway = FakeSlackGateway()
    result = asyncio.run(run.run_digest(
        conn, settings=DigestSettings(digest_channel_id=""), channels_path="", gateway=gateway,
        dry_run=True))

    assert gateway.posted == []
    assert result.posted is False
    assert "*Stigmergy digest —" in result.body
    assert result.run_id is not None


def test_dry_run_needs_neither_a_channel_nor_a_gateway(conn):
    """The escape hatch a missing channel points an operator at — "run with --dry-run to preview
    the body without posting" — so no configured channel and `gateway=None` must both be fine."""
    result = asyncio.run(run.run_digest(
        conn, settings=DigestSettings(digest_channel_id=""), channels_path="", gateway=None,
        dry_run=True))
    assert result.posted is False


def test_dry_run_does_not_advance_the_real_watermark(conn):
    now = _now()
    asyncio.run(run.run_digest(
        conn, settings=DigestSettings(digest_channel_id=""), channels_path="", gateway=None,
        dry_run=True, now=now))

    since = run._resolve_since(conn, since_override=None, window_days=7, now=now)
    # still the default window — a real 'digest' watermark was never written.
    assert since == now - datetime.timedelta(days=7)


def test_dry_run_records_its_own_job_name_never_the_real_one(conn):
    asyncio.run(run.run_digest(
        conn, settings=DigestSettings(digest_channel_id=""), channels_path="", gateway=None,
        dry_run=True))
    with conn.cursor() as cur:
        cur.execute("SELECT job, status FROM job_runs ORDER BY id DESC LIMIT 1")
        job, status = cur.fetchone()
    assert job == "digest-dry-run"
    assert status == "ok"


# ── a real run: posts, records job_runs + the watermark ─────────────────────────────────────────
def test_real_run_posts_records_job_runs_and_advances_the_watermark(conn):
    gateway = FakeSlackGateway()
    now = _now()

    result = asyncio.run(run.run_digest(
        conn, settings=DigestSettings(digest_channel_id="C0123456789"), channels_path="",
        gateway=gateway, dry_run=False, now=now))

    assert result.posted is True
    assert len(gateway.posted) == 1
    assert gateway.posted[0].channel_id == "C0123456789"
    assert gateway.posted[0].text == result.body

    with conn.cursor() as cur:
        cur.execute("SELECT job, status FROM job_runs ORDER BY id DESC LIMIT 1")
        job, status = cur.fetchone()
    assert job == "digest"
    assert status == "ok"

    # the NEXT default run's window starts at this run's watermark.
    later = now + datetime.timedelta(hours=1)
    since = run._resolve_since(conn, since_override=None, window_days=7, now=later)
    assert abs((since - now).total_seconds()) < 5


def test_real_run_refuses_when_no_channel_is_configured(conn):
    gateway = FakeSlackGateway()
    with pytest.raises(DigestError, match="STIGMERGY_DIGEST_CHANNEL_ID"):
        asyncio.run(run.run_digest(
            conn, settings=DigestSettings(digest_channel_id=""), channels_path="", gateway=gateway,
            dry_run=False))
    assert gateway.posted == []
    with conn.cursor() as cur:
        cur.execute("SELECT job, status, error FROM job_runs ORDER BY id DESC LIMIT 1")
        job, status, error = cur.fetchone()
    assert (job, status, error) == ("digest", "error", "DigestError")


def test_real_run_refuses_when_no_gateway_is_configured(conn):
    with pytest.raises(DigestError, match="SLACK_BOT_TOKEN"):
        asyncio.run(run.run_digest(
            conn, settings=DigestSettings(digest_channel_id="C0123456789"), channels_path="",
            gateway=None, dry_run=False))


def test_a_posting_failure_records_an_error_row_and_propagates(conn):
    gateway = FakeSlackGateway()
    gateway.fail_post_count = 99   # every post fails

    with pytest.raises(SlackApiError):
        asyncio.run(run.run_digest(
            conn, settings=DigestSettings(digest_channel_id="C0123456789"), channels_path="",
            gateway=gateway, dry_run=False))

    with conn.cursor() as cur:
        cur.execute("SELECT job, status, error FROM job_runs ORDER BY id DESC LIMIT 1")
        job, status, error = cur.fetchone()
    assert (job, status, error) == ("digest", "error", "SlackApiError")
    # the failed attempt must never have advanced the watermark.
    later = _now()
    since = run._resolve_since(conn, since_override=None, window_days=7, now=later)
    assert since == later - datetime.timedelta(days=7)


# ── determinism / injectability ─────────────────────────────────────────────────────────────────
def test_now_and_since_override_are_injectable_never_read_from_the_wall_clock_by_default(conn):
    fixed_now = datetime.datetime(2026, 7, 31, 5, 7, tzinfo=UTC)
    fixed_since = datetime.datetime(2026, 7, 24, tzinfo=UTC)

    result = asyncio.run(run.run_digest(
        conn, settings=DigestSettings(digest_channel_id=""), channels_path="", gateway=None,
        dry_run=True, since_override=fixed_since, now=fixed_now))

    assert result.since == fixed_since
    assert result.until == fixed_now
    assert "2026-07-24 to 2026-07-31" in result.body
