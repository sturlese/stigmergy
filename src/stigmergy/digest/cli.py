"""`stigmergy-digest` — the week's learning in one Slack post. One command, no subcommands:

    stigmergy-digest [--repo PATH] [--channels PATH] [--dsn DSN] [--since YYYY-MM-DD] [--dry-run]

`--dry-run` prints byte-identically what a real run would send and posts nothing; a real run
posts and records `job_runs` + the watermark. The only module in the package that imports
`stigmergy.index.store` or `stigmergy.slack.bolt_gateway` — every other module takes
`conn`/`gateway` as plain arguments.
"""
import argparse
import asyncio
import os
import sys

from stigmergy.capture import decisions
from stigmergy.capture import schema as capture_schema
from stigmergy.digest import run
from stigmergy.digest.errors import DigestError
from stigmergy.digest.settings import SLACK_BOT_TOKEN_ENV, DigestSettings
from stigmergy.gardener.schema import ensure_gardener_schema
from stigmergy.index import store
from stigmergy.librarian import config as librarian_config
from stigmergy.server.errors import StartupError
from stigmergy.slack import channels

EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130


def _err(message: str) -> None:
    print(f"stigmergy-digest: {message}", file=sys.stderr)


def _connect(args):
    conn = store.connect(args.dsn)
    # Every table the two sections read, or this run's own job_runs row needs.
    capture_schema.ensure_capture_schema(conn)
    decisions.ensure_decisions_schema(conn)
    ensure_gardener_schema(conn)
    return conn


def _repo(args) -> str:
    """WHERE only — deliberately not the checkout predicate. The digest never commits; it needs a
    path to resolve `ops/slack-channels.json` against, not a git checkout. `gardener/cli.py`'s
    twin says the same thing (this package's index.md: change both or neither)."""
    return librarian_config.repo_path(args.repo)


def _gateway():
    """The real Slack gateway, or `None` when no bot token is configured — only a problem for a
    non-`--dry-run` invocation. The SDK-reaching import stays lazy, inside this function."""
    token = os.environ.get(SLACK_BOT_TOKEN_ENV)
    if not token:
        return None
    from stigmergy.slack.bolt_gateway import build_gateway
    return build_gateway(token)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stigmergy-digest",
        description="The week in one Slack post: corpus "
                    "health, corpus deltas. --dry-run previews the exact body and posts nothing.")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--repo", default=None,
                    help=f"your clone of the knowledge repo, for ops/slack-channels.json "
                         f"(default: ${librarian_config.REPO_ENV} or "
                         f"{librarian_config.REPO_DEFAULT})")
    ap.add_argument("--channels", default=None,
                    help="path to slack-channels.json (default: <repo>/ops/slack-channels.json)")
    ap.add_argument("--since", default=None,
                    help="YYYY-MM-DD — override the window start (default: since the last "
                         "digest post, or 7 days back on a first run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print exactly what would post; post nothing")
    return ap


def _run(conn, args) -> int:
    settings = DigestSettings.from_args(args)                    # may raise StartupError
    since_override = run.parse_since(args.since) if args.since else None  # may raise DigestError
    repo = _repo(args)
    channels_path = args.channels or channels.default_path(repo)
    gateway = _gateway()

    result = asyncio.run(run.run_digest(
        conn, settings=settings, channels_path=channels_path, gateway=gateway,
        dry_run=args.dry_run, since_override=since_override))

    if args.dry_run:
        # The marker lines are printed HERE, outside `render.build_body`: the body between them
        # is the literal candidate for `text=`, byte for byte.
        print("--- would post to the configured channel (dry run — nothing sent) ---")
        print(result.body)
        print("--- end of dry-run body ---")
    elif result.posted and result.run_id is None:
        # `record_job_run` swallows a write failure and returns `None` — right for the post,
        # wrong to then exit 0: the watermark never advanced, so the next run may re-post this
        # window as a duplicate with no operator signal.
        _err(f"posted to {settings.digest_channel_id}, but the watermark could not be recorded "
            f"(the job_runs write failed) — window {result.since.date().isoformat()} to "
            f"{result.until.date().isoformat()} was NOT saved as covered, so the next run may "
            f"re-post it as a duplicate. Check job_runs for a 'digest' row for this run before "
            f"re-running.")
        return EXIT_ERROR
    else:
        print(f"posted to {settings.digest_channel_id} (job_runs #{result.run_id}) — window "
              f"{result.since.date().isoformat()} to {result.until.date().isoformat()}")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    conn = None
    try:
        conn = _connect(args)
    except KeyboardInterrupt:
        _err("interrupted while connecting to the queue database")
        return EXIT_INTERRUPTED
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason
        _err(f"cannot reach the queue database ({ex}); is Postgres up (`make db-up`)?")
        return EXIT_CONFIG

    try:
        return _run(conn, args)
    except (DigestError, StartupError) as ex:
        _err(str(ex))
        return EXIT_CONFIG
    except KeyboardInterrupt:
        _err("interrupted — if the message had already posted, the watermark update may not "
            "have committed yet; check job_runs for a 'digest' row before re-running, and check "
            "the configured channel for a duplicate post. If nothing had posted yet, nothing "
            "happened at all; re-run when ready.")
        return EXIT_INTERRUPTED
    except Exception as ex:  # noqa: BLE001 — an honest failed run; class name only, never
        # str(ex) — a query failure could echo back a fragment of page/title content.
        _err(f"the run failed ({ex.__class__.__name__}) — see job_runs for this run's recorded "
            f"outcome.")
        return EXIT_ERROR
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
