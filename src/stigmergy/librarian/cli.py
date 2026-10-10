from __future__ import annotations

import argparse
import sys

from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture import schema
from stigmergy.capture.uploads import ensure_upload_schema
from stigmergy.changes.store import ensure_change_schema
from stigmergy.index import store
from stigmergy.librarian import config, provision, worker, workspaces
from stigmergy.librarian.errors import GitError, LibrarianConfigError
from stigmergy.server import platform, roster, usage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stigmergy-librarian")
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--repo")
    parser.add_argument("--branch")
    parser.add_argument("--dsn")
    parser.add_argument("--backend", choices=("scripted", "pydantic"))
    parser.add_argument("--poll-interval", type=float)
    parser.add_argument("--visibility-timeout", type=int)
    return parser


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
        settings = config.Settings.from_args(args)
        conn = store.connect(settings.dsn)
        worker.configure_connection(conn)
        platform.ensure_platform_schema(conn)
        roster.ensure_roster_schema(conn)
        schema.ensure_capture_schema(conn)
        ensure_upload_schema(conn)
        ensure_change_schema(conn)
        usage.ensure_schema(conn)
        worker.check_settings(settings)
        evidence = evidence_plane.store_from_env()
        places = workspaces.Workspaces(conn, settings, evidence)
    except GitError:
        print("stigmergy-librarian: could not validate the knowledge repository", file=sys.stderr)
        return 2
    except (LibrarianConfigError, ValueError, OSError) as error:
        print(f"stigmergy-librarian: {error}", file=sys.stderr)
        return 2

    loop = worker.Worker(conn, places, on_output=lambda line: print(line, flush=True),
                         provisioner=provision.provisioner_from_env(),
                         repo_remover=provision.remover_from_env())
    loop.install_signal_handlers()
    print(f"writer ready for {len(platform.list_tenants(conn, statuses=('active',)))} "
          f"workspace(s) under {places.root}", flush=True)
    processed = loop.run()
    print(f"writer stopped after {processed} operation(s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
