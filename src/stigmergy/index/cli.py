"""The two index CLIs: `stigmergy-index` (builder/lint) and `stigmergy-search` (query) — thin
argument parsing over the library; the server consumes the same seams, nothing here is CLI-only.
"""
import argparse
import json
from datetime import date

from stigmergy.index import build, check, search, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.text import sanitize


def _parse_filters(pairs: list[str]) -> dict:
    filters = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--filter expects key=value, got {pair!r} "
                             f"(allowed keys: {', '.join(search.FILTER_COLUMNS)})")
        key, value = pair.split("=", 1)
        filters[key.strip()] = value.strip()
    return filters


def index_main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="stigmergy-index",
        description="Full rebuild of the hybrid derived index — or the substrate lint over it.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rebuild", action="store_true",
                      help="drop and rebuild pages_index (full rebuild only)")
    mode.add_argument("--check", action="store_true",
                      help="lint the LIVE index: duplicate page_ids, orphan continuation "
                           "parts, arm-invisible pages, dangling supersessions, unregistered "
                           "anchors — exit 1 on any ERROR finding")
    ap.add_argument("--repo", default=None,
                    help="knowledge-repo checkout (required with --rebuild; with --check it "
                         "locates ops/entity-registry.json for the registry-coverage warning)")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--embedder", choices=["openai", "fake"], default="openai",
                    help="fake = deterministic offline double (tests/CI only)")
    ap.add_argument("--model", default=None, help="embedding model (default per embedder)")
    ap.add_argument("--fts-config", default="english", help="Postgres text-search config")
    args = ap.parse_args(argv)

    if args.check:
        registry_file = build.registry_path(args.repo) if args.repo else None
        try:
            with store.connect(args.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM pages_index")
                    pages = cur.fetchone()[0]
                # The copy the SERVER serves, never `--repo`'s file by default: an index carrying a
                # snapshot is answering from it, and linting the working tree instead would report
                # coverage nobody's server has.
                findings = check.run_checks(
                    conn, registry=check.served_registry(conn, registry_file))
        except StigmergyIndexError as ex:
            raise SystemExit(str(ex)) from ex
        print(check.render(findings, pages))
        if any(f["severity"] == "error" for f in findings):
            raise SystemExit(1)
        return

    if not args.repo:
        ap.error("--rebuild requires --repo")
    embedder = build_embedder(args.embedder, args.model)
    try:
        with store.connect(args.dsn) as conn:
            stats = build.rebuild(conn, args.repo, embedder, fts_config=args.fts_config)
    except StigmergyIndexError as ex:
        raise SystemExit(str(ex)) from ex   # domain error -> exit code; the library never exits
    zones = ", ".join(f"{z}={n}" for z, n in sorted(stats["zones"].items()))
    print(f"indexed {stats['pages']} pages ({zones}) · embeddings: {stats['embedded']} new, "
          f"{stats['cached']} cached · model={stats['model']} dim={stats['dim']} "
          f"fts={stats['fts_config']}")


def _render_hit(i: int, h: dict) -> str:
    # titles/snippets are untrusted corpus content headed for a terminal: control characters are
    # stripped (snippets already arrive sanitized from rank._snippet; belt and braces)
    factors = ", ".join(h["factors"]) if h["factors"] else "none"
    lines = [f"{i}. {sanitize(h['title'])}  ({sanitize(h['path'])})",
             f"   zone={h['zone']} score={h['score']:.5f} arms={'+'.join(h['arms'])} "
             f"factors: {factors}"]
    if h.get("snippet"):
        lines.append(f"   {sanitize(h['snippet'])}")
    return "\n".join(lines)


def search_main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="stigmergy-search",
        description="Query the hybrid index (ES/EN); every hit shows its ranking factors.")
    ap.add_argument("query", help="the question, in natural language")
    ap.add_argument("-k", type=int, default=5, help="hits to return (default 5)")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--filter", action="append", metavar="KEY=VALUE",
                    help=f"frontmatter filter, repeatable ({', '.join(search.FILTER_COLUMNS)})")
    ap.add_argument("--current-only", action="store_true",
                    help="drop superseded pages instead of demoting them")
    ap.add_argument("--json", action="store_true", help="machine-readable hits")
    args = ap.parse_args(argv)

    try:
        with store.connect(args.dsn) as conn:
            hits = search.search(conn, args.query, k=args.k, filters=_parse_filters(args.filter),
                                 include_superseded=not args.current_only, today=date.today())
    except StigmergyIndexError as ex:
        raise SystemExit(str(ex)) from ex   # domain error -> exit code; the library never exits
    if args.json:
        print(json.dumps([{k: v for k, v in h.items() if k != "tsv"} for h in hits],
                         ensure_ascii=False, indent=2))
        return
    if not hits:
        print("no hits")
        return
    for i, h in enumerate(hits, start=1):
        print(_render_hit(i, h))
