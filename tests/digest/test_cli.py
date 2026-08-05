"""`stigmergy-digest` — the CLI entrypoint, driven in-process through `cli.main(argv)` against real
Postgres, same posture as `tests/gardener/test_cli.py`. `$STIGMERGY_INDEX_DSN` is already pinned to
the test database for the whole session (`tests/conftest.py::pytest_configure`), so no test here
needs to pass `--dsn` explicitly.
"""
from stigmergy.capture import ops as capture_ops
from stigmergy.digest import cli
from stigmergy.digest.settings import DIGEST_CHANNEL_ID_ENV, SLACK_BOT_TOKEN_ENV, WINDOW_DAYS_ENV
from stigmergy.slack.bolt_gateway import BoltSlackGateway
from stigmergy.slack.gateway import FakeSlackGateway


# ── _gateway: the real-vs-none construction, without ever posting ─────────────────────────────
def test_gateway_is_none_when_no_bot_token_is_configured(monkeypatch):
    monkeypatch.delenv(SLACK_BOT_TOKEN_ENV, raising=False)
    assert cli._gateway() is None


def test_gateway_constructs_a_real_gateway_when_a_bot_token_is_configured(monkeypatch):
    monkeypatch.setenv(SLACK_BOT_TOKEN_ENV, "xoxb-test-token")
    gateway = cli._gateway()
    assert isinstance(gateway, BoltSlackGateway)


# ── connection failure: local and specific, mirroring `gardener.cli`'s own posture ──────────────
def test_unreachable_database_prints_a_clean_message_and_exits_config(capsys):
    rc = cli.main(["--dsn", "postgresql://stigmergy:stigmergy@127.0.0.1:1/nope"])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_CONFIG
    assert "stigmergy-digest:" in err
    assert "cannot reach the queue database" in err
    assert "make db-up" in err


def test_connect_interrupted_exits_130_with_the_generic_message(capsys, monkeypatch):
    def boom(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_connect", boom)

    rc = cli.main([])

    captured = capsys.readouterr()
    assert rc == cli.EXIT_INTERRUPTED
    assert captured.out == ""
    assert "stigmergy-digest: interrupted while connecting to the queue database" in captured.err
    assert "Traceback" not in captured.err


def test_generic_interrupt_during_the_run_exits_130_naming_the_watermark_risk(
        conn, capsys, monkeypatch):
    def boom(_conn, _args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run", boom)

    rc = cli.main([])

    captured = capsys.readouterr()
    assert rc == cli.EXIT_INTERRUPTED
    assert captured.out == ""
    assert "stigmergy-digest: interrupted" in captured.err
    assert "watermark update may not" in captured.err
    assert "check job_runs for a 'digest' row" in captured.err
    assert "Traceback" not in captured.err


# ── --since validation ──────────────────────────────────────────────────────────────────────────
def test_bad_since_prints_the_exact_copy_and_exits_config(conn, capsys):
    rc = cli.main(["--since", "next-tuesday"])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_CONFIG
    assert ("stigmergy-digest: --since 'next-tuesday' is not a valid date (expected YYYY-MM-DD) "
           "— nothing was read or posted") in err


# ── a malformed threshold env var (StartupError) ─────────────────────────────────────────────
def test_bad_window_days_env_prints_a_clean_message_and_exits_config(conn, capsys, monkeypatch):
    monkeypatch.setenv(WINDOW_DAYS_ENV, "not-a-number")
    rc = cli.main([])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_CONFIG
    assert "stigmergy-digest:" in err
    assert WINDOW_DAYS_ENV in err


# ── missing channel ─────────────────────────────────────────────────────────────────────────────
def test_missing_channel_on_a_real_run_prints_the_exact_copy_and_exits_config(
        conn, capsys, monkeypatch):
    monkeypatch.delenv(DIGEST_CHANNEL_ID_ENV, raising=False)
    rc = cli.main([])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_CONFIG
    assert (f"stigmergy-digest: ${DIGEST_CHANNEL_ID_ENV} is not set — there is nowhere configured "
           f"to post this to. Set it to a Slack channel id (e.g. C0123456789), or run with "
           f"--dry-run to preview the body without posting.") in err


def test_missing_channel_never_blocks_a_dry_run(conn, capsys, monkeypatch):
    monkeypatch.delenv(DIGEST_CHANNEL_ID_ENV, raising=False)
    rc = cli.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--- would post to the configured channel (dry run — nothing sent) ---" in out


# ── --dry-run wrapper: the two marker lines live OUTSIDE the postable body ──────────────────────
def test_dry_run_prints_the_exact_wrapper_and_the_body_between_the_markers(conn, capsys):
    rc = cli.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0

    lines = out.splitlines()
    assert lines[0] == "--- would post to the configured channel (dry run — nothing sent) ---"
    assert lines[-1] == "--- end of dry-run body ---"
    body = "\n".join(lines[1:-1])
    assert body.startswith("*Stigmergy digest —")
    # the literal, unrendered mrkdwn: a printed preview shows literal *asterisks* and •
    # characters, which is the correct, honest behaviour for a body that Slack has not rendered.
    assert "•" in body


# ── a real, successful run's own confirmation line ───────────────────────────────────────────
def test_a_real_run_prints_a_short_confirmation_naming_the_channel_and_the_run(
        conn, capsys, monkeypatch):
    monkeypatch.setenv(DIGEST_CHANNEL_ID_ENV, "C0123456789")
    fake_gateway = FakeSlackGateway()
    monkeypatch.setattr(cli, "_gateway", lambda: fake_gateway)

    rc = cli.main([])

    out = capsys.readouterr().out
    assert rc == 0
    assert "posted to C0123456789" in out
    assert "job_runs #" in out
    assert len(fake_gateway.posted) == 1
    assert fake_gateway.posted[0].channel_id == "C0123456789"


# ── a silent job_runs write failure must not look like a clean run ──────────────────────────────
def test_a_real_run_that_posts_but_cannot_record_the_watermark_warns_and_exits_error(
        conn, capsys, monkeypatch):
    """`capture.ops.record_job_run` swallows its own write failure and returns `None` (its own
    docstring: bookkeeping must never fail the work it records) — a real posture for the WRITE,
    wrong for the CLI to then print a cheerful "posted ... (job_runs #None)" and exit 0. The
    message has ALREADY reached Slack by the time this is discovered, so silence would leave an
    operator with no signal that tomorrow's cron may re-post this same window as a duplicate."""
    monkeypatch.setenv(DIGEST_CHANNEL_ID_ENV, "C0123456789")
    fake_gateway = FakeSlackGateway()
    monkeypatch.setattr(cli, "_gateway", lambda: fake_gateway)
    monkeypatch.setattr(capture_ops, "record_job_run", lambda *a, **k: None)

    rc = cli.main([])

    captured = capsys.readouterr()
    assert rc == cli.EXIT_ERROR
    assert len(fake_gateway.posted) == 1   # the message DID reach Slack — this is not a no-op
    assert "stigmergy-digest:" in captured.err
    assert "posted to C0123456789" in captured.err
    assert "watermark could not be recorded" in captured.err
    assert "duplicate" in captured.err
    assert "job_runs #" not in captured.out   # the happy-path confirmation must not ALSO print


def test_a_dry_run_is_unaffected_by_a_job_runs_write_failure(conn, capsys, monkeypatch):
    """The benign twin: `result.posted` is `False` for a `--dry-run`, so the SAME `record_job_run`
    failure must not trip the check above at all — a preview posts nothing and has no watermark to
    lose."""
    monkeypatch.setattr(capture_ops, "record_job_run", lambda *a, **k: None)

    rc = cli.main(["--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "--- would post to the configured channel (dry run — nothing sent) ---" in out
