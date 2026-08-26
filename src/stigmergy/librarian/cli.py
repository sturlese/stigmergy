from __future__ import annotations

import argparse
import sys

from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture import schema
from stigmergy.capture.uploads import ensure_upload_schema
from stigmergy.changes.store import ensure_change_schema
from stigmergy.index import store
from stigmergy.librarian import config, worker
from stigmergy.librarian.errors import GitError, LibrarianConfigError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stigmergy-librarian")
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--repo")
    parser.add_argument("--branch")
    parser.add_argument("--dsn")
    parser.add_argument("--backend", choices=("scripted", "pydantic"))
    parser.add_argument("--poll-interval", type=float)
    parser.add_argument("--visibility-timeout", type=int)
    parser.add_argument("--max-attempts", type=int)
    return parser


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
        settings = config.Settings.from_args(args)
        conn = store.connect(settings.dsn)
        worker.configure_connection(conn)
        schema.ensure_capture_schema(conn)
        ensure_upload_schema(conn)
        ensure_change_schema(conn)
        resolved = worker.startup_checks(settings)
        deps = worker.build_deps(
            settings,
            resolved,
            evidence_plane.store_from_env(),
        )
    except GitError:
        print("stigmergy-librarian: could not validate the knowledge repository", file=sys.stderr)
        return 2
    except (LibrarianConfigError, ValueError, OSError) as error:
        print(f"stigmergy-librarian: {error}", file=sys.stderr)
        return 2

    loop = worker.Worker(conn, deps, on_output=lambda line: print(line, flush=True))
    loop.install_signal_handlers()
    base = resolved["base"]
    print(f"writer ready at {base.describe()}", flush=True)
    processed = loop.run()
    print(f"writer stopped after {processed} operation(s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
