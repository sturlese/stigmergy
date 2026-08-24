import argparse

from stigmergy.index import build, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.errors import StigmergyIndexError


def index_main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="stigmergy-index", description="Rebuild the derived index.")
    ap.add_argument("--rebuild", action="store_true", required=True)
    ap.add_argument("--repo", required=True, help="knowledge-repository checkout")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--embedder", choices=["openrouter", "fake"], default="openrouter",
                    help="fake = deterministic offline double (tests/CI only)")
    ap.add_argument("--fts-config", default="english", help="Postgres text-search config")
    args = ap.parse_args(argv)

    embedder = build_embedder(args.embedder)
    try:
        with store.connect(args.dsn) as conn:
            stats = build.rebuild(
                conn,
                args.repo,
                embedder,
                fts_config=args.fts_config,
                require_repository_head=True,
            )
    except StigmergyIndexError as ex:
        raise SystemExit(str(ex)) from ex
    zones = ", ".join(f"{z}={n}" for z, n in sorted(stats["zones"].items()))
    print(f"indexed {stats['pages']} pages ({zones}) · embeddings: {stats['embedded']} new, "
          f"{stats['cached']} cached · model={stats['model']} dim={stats['dim']} "
          f"fts={stats['fts_config']}")
