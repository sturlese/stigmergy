"""`stigmergy-gardener` — corpus health on demand. One command, no subcommands:

    stigmergy-gardener [--repo PATH] [--dsn DSN] [--json]

Findings present -> exit 0 (findings are data, not errors); a failed run -> nonzero. The only
module in the package that imports `stigmergy.index.store` — every other module takes `conn` as a
plain argument.
"""
import argparse
import sys

from stigmergy.capture import schema as capture_schema
from stigmergy.gardener import report, run
from stigmergy.gardener.errors import GardenerError
from stigmergy.gardener.schema import ensure_gardener_schema
from stigmergy.gardener.settings import GardenerSettings
from stigmergy.index import store
from stigmergy.librarian import config as librarian_config
from stigmergy.server.errors import StartupError

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
    ensure_gardener_schema(conn)                 # gardener_findings — this package's own table
    return conn


def _repo(args) -> str:
    """WHERE only — deliberately NOT paired with `librarian_config.is_repo_checkout`, which is
    what a command that has to write to the checkout adds. The gardener never commits, so it does
    not need a git checkout, only a readable copy of the registry and the pages;
    `run._require_repo` states that weaker requirement, and states it for every caller of
    `run_gardener`, not just this one."""
    return librarian_config.repo_path(args.repo)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stigmergy-gardener",
        description="Deterministic corpus-health checks, run on demand: findings persisted, a "
                    "severity-grouped report printed.")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--repo", default=None,
                    help=f"your clone of the knowledge repo, for the entity registry and the "
                         f"corpus (default: ${librarian_config.REPO_ENV} or "
                         f"{librarian_config.REPO_DEFAULT})")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def _run(conn, args) -> int:
    settings = GardenerSettings.from_args(args)   # may raise StartupError — a bad threshold env
    repo = _repo(args)
    result = run.run_gardener(conn, repo=repo, settings=settings)

    if args.json:
        print(report.render_json(result.findings))
    else:
        print(report.render_report(
            run_id=result.run_id, completed_at=result.completed_at,
            pages_checked=result.pages_checked, entities_checked=result.entities_checked,
            findings=result.findings), end="")

    # Findings are data, not errors: a run that completed exits 0 however much it found. A run
    # that could NOT complete raised, and `main` below is what turns that into a non-zero exit —
    # there is no partial outcome left to report here.
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
