import argparse
import os
import re
import subprocess

from stigmergy.index import build, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.kernel import tenancy
from stigmergy.server import platform
from stigmergy.server.settings import TENANT_ENV

_GITHUB_REMOTE = re.compile(r"^(?:https://(?:[^@/\s]+@)?github\.com/|git@github\.com:|"
                            r"ssh://git@github\.com/)"
                            r"([^/\s]+/[^/\s]+?)(?:\.git)?/?$")


def github_repository(remote: str) -> str | None:
    """`owner/name` of a GitHub remote URL in any of its spellings, or None for anything else."""
    match = _GITHUB_REMOTE.match(remote.strip())
    return match.group(1).lower() if match else None


def _origin(repo: str) -> str:
    result = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def index_main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="stigmergy-index", description="Rebuild the derived index.")
    ap.add_argument("--rebuild", action="store_true", required=True)
    ap.add_argument("--repo", required=True, help="knowledge-repository checkout")
    ap.add_argument("--tenant", default=None,
                    help=f"workspace slug the checkout belongs to (fallback: ${TENANT_ENV}; the "
                         "only active workspace when there is one)")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--embedder", choices=["openrouter", "fake"], default="openrouter",
                    help="fake = deterministic offline double (tests/CI only)")
    ap.add_argument("--fts-config", default="english", help="Postgres text-search config")
    args = ap.parse_args(argv)

    embedder = build_embedder(args.embedder)
    try:
        with store.connect(args.dsn) as conn:
            platform.ensure_platform_schema(conn)
            chosen = platform.resolve_tenant(conn, args.tenant or os.environ.get(TENANT_ENV))
            origin = github_repository(_origin(args.repo))
            if origin is not None and origin != chosen.repo.lower():
                raise SystemExit(f"the checkout belongs to {origin}, not to the {chosen.slug} "
                                 f"workspace's repository {chosen.repo}; refusing to index it")
            tenancy.set_tenant(conn, chosen.id)
            stats = build.rebuild(
                conn,
                args.repo,
                embedder,
                fts_config=args.fts_config,
                require_repository_head=True,
            )
    except (StigmergyIndexError, platform.PlatformRefused, tenancy.TenancyError) as ex:
        raise SystemExit(str(ex)) from ex
    zones = ", ".join(f"{z}={n}" for z, n in sorted(stats["zones"].items()))
    print(f"indexed {stats['pages']} pages ({zones}) · embeddings: {stats['embedded']} new, "
          f"{stats['cached']} cached · model={stats['model']} dim={stats['dim']} "
          f"fts={stats['fts_config']}")
