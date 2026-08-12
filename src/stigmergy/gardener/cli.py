"""`stigmergy-gardener` — corpus health on demand. One command, no subcommands:

    stigmergy-gardener [--repo PATH] [--dsn DSN] [--json]

Findings present -> exit 0 (findings are data, not errors); a failed run -> nonzero. The only
module in the package that imports `stigmergy.index.store` or `stigmergy.slack.bolt_gateway` —
every other module takes `conn`/`gateway` as plain arguments.
"""
import argparse
import asyncio
import os
import sys

from stigmergy.capture import schema as capture_schema
from stigmergy.gardener import report, run
from stigmergy.gardener.errors import GardenerError
from stigmergy.gardener.schema import ensure_gardener_schema
from stigmergy.gardener.settings import SLACK_BOT_TOKEN_ENV, GardenerSettings
from stigmergy.index import store
from stigmergy.librarian import config as librarian_config
from stigmergy.server import review
from stigmergy.server.errors import StartupError
from stigmergy.slack import channels

EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130


def _err(message: str) -> None:
    print(f"stigmergy-gardener: {message}", file=sys.stderr)


def _connect(args):
    conn = store.connect(args.dsn)
    # All three schemas, so a FRESH database is never a table short (`UndefinedTable`) — a gap a
    # mature database, and every test fixture, hides. Same shape and order as `digest/cli.py`.
    capture_schema.ensure_capture_schema(conn)   # capture_queue/job_runs
    review.ensure_review_schema(conn)            # review_decisions — read by stigmergy-digest
    ensure_gardener_schema(conn)                 # gardener_findings — this package's own table
    return conn


def _repo(args) -> str:
    return (args.repo or os.environ.get(librarian_config.REPO_ENV)
            or librarian_config.REPO_DEFAULT)


def _gateway():
    """The real Slack gateway, or `None` when no bot token is configured — only a problem if this
    run actually produces an `sla` finding. The SDK-reaching import stays lazy, inside this
    function."""
    token = os.environ.get(SLACK_BOT_TOKEN_ENV)
    if not token:
        return None
    from stigmergy.slack.bolt_gateway import build_gateway
    return build_gateway(token)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stigmergy-gardener",
        description="Eight deterministic corpus-health checks, run on demand: findings persisted, "
                    "a severity-grouped report printed, an SLA notice posted for anything urgent.")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--repo", default=None,
                    help=f"your clone of the knowledge repo, for the entity registry and view "
                         f"staleness (default: ${librarian_config.REPO_ENV} or "
                         f"{librarian_config.REPO_DEFAULT})")
    ap.add_argument("--channels", default=None,
                    help="path to slack-channels.json, for the SLA notice's own channel-scoping "
                         "audiences (default: <repo>/ops/slack-channels.json — mirrors "
                         "stigmergy-digest's identical flag)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def _run(conn, args) -> int:
    settings = GardenerSettings.from_args(args)   # may raise StartupError — a bad threshold env
    repo = _repo(args)
    channels_path = args.channels or channels.default_path(repo)
    gateway = _gateway()
    result = asyncio.run(run.run_gardener(conn, repo=repo, settings=settings,
                                          channels_path=channels_path, gateway=gateway))

    if args.json:
        print(report.render_json(result.findings))
    else:
        print(report.render_report(
            run_id=result.run_id, completed_at=result.completed_at,
            pages_checked=result.pages_checked, entities_checked=result.entities_checked,
            findings=result.findings,
            sweep_summary=report.sweep_summary_text(result.sweep_changed_count,
                                                    result.sweep_sampled_count),
            sweep_failed=bool(result.sweep_error)), end="")

    # A sweep outage and a notice failure are independent; both leave the already-committed
    # report above intact.
    failed = False
    if result.sweep_error:
        _err(f"the model sweep failed ({result.sweep_error}) — the {len(result.findings)} "
            f"finding(s) above are complete and already saved; the sweep pass produced zero "
            f"findings this run and will run again next time. See job_runs for this run's "
            f"recorded outcome.")
        failed = True
    if result.notice_error:
        _err(f"the SLA notice could not be posted: {result.notice_error}")
        failed = True
    return EXIT_ERROR if failed else 0


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
    except (GardenerError, StartupError) as ex:
        _err(str(ex))
        return EXIT_CONFIG
    except KeyboardInterrupt:
        _err("interrupted — nothing was written; re-run when ready.")
        return EXIT_INTERRUPTED
    except Exception as ex:  # noqa: BLE001 — an honest failed run; class name only, never
        # str(ex) — a query failure could echo back a fragment of page/detail content.
        _err(f"the run failed ({ex.__class__.__name__}) — see job_runs for this run's recorded "
            f"outcome.")
        return EXIT_ERROR
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
