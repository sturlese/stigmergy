"""Full rebuild: a knowledge-repo checkout -> a fresh `pages_index` and the ops-file snapshots
beside it. Incremental-on-merge lives in `stigmergy.server.webhook`; the only incrementality here
is the embedding cache, which keeps a rebuild's API spend proportional to what actually changed.
"""
import logging
import os

from stigmergy.index import corpus, store
from stigmergy.index.errors import EmptyCorpusError

log = logging.getLogger(__name__)

# `store.ENTITY_REGISTRY_RELPATH` re-exported under the name `index/cli.py` and one architecture
# pin already know. The store owns the ONE spelling of every cached ops file's relpath.
ENTITY_REGISTRY_RELPATH = store.ENTITY_REGISTRY_RELPATH


def registry_path(repo_dir: str) -> str:
    """`<repo_dir>/ops/entity-registry.json`, resolved through the store's one spelling —
    `index/cli.py` builds the `--check` path through it rather than re-joining the parts."""
    return os.path.join(repo_dir, *ENTITY_REGISTRY_RELPATH.split("/"))


def _read_ops_file(repo_dir: str, relpath: str) -> tuple[str | None, bool]:
    """`(TEXT, oversized)` for one checkout ops file — `(None, False)` when the checkout has no
    such file, `(None, True)` when it has one too big to install.

    The two `None`s are different decisions and must not collapse: an ABSENT file goes to
    `store.CLEARED_WHEN_CHECKOUT_LACKS`'s per-file posture, while an OVERSIZED one always leaves
    the previous snapshot standing — the same answer the push webhook gives it, because two roads
    writing one row must not disagree about the same fault, and the honest floor is a snapshot
    that is stale, not one that costs every identity a multi-megabyte parse per tool call.

    The cap is the webhook's (`store.MAX_OPS_FILE_BYTES`), for the same one-row reason."""
    path = os.path.join(repo_dir, *relpath.split("/"))
    if not os.path.exists(path):
        return None, False
    with open(path, encoding="utf-8") as f:
        text = f.read()
    size = len(text.encode("utf-8"))
    if size > store.MAX_OPS_FILE_BYTES:
        log.error("index rebuild: %s is %d bytes, above the %d-byte snapshot cap — NOT installed; "
                  "the previous snapshot of %s stands", path, size, store.MAX_OPS_FILE_BYTES,
                  relpath)
        return None, True
    return text, False


def _reconcile_ops_files(conn, repo_dir: str) -> dict[str, str]:
    """Make the snapshots match the checkout, one decision per file — the nightly counterpart of
    the push webhook's incremental refresh. Returns `{relpath: "written" | "cleared" | "kept"}`,
    the same words the returned stats carry.

    "kept" is the access files' absent-in-checkout posture (`store.CLEARED_WHEN_CHECKOUT_LACKS`):
    clearing would hand every deployed process back to the copy baked at the last deploy —
    a revocation silently undone by a cron — so the snapshot stands and this run says so.

    "absent" is the quiet fourth outcome: the checkout has no such file AND the cache holds no
    snapshot of it, so there is nothing to destroy, nothing to keep, and nothing to warn about —
    the nightly log of a deployment that simply never scoped its channels must not cry wolf."""
    outcomes: dict[str, str] = {}
    for relpath in store.OPS_FILE_RELPATHS:
        text, oversized = _read_ops_file(repo_dir, relpath)
        if text is not None:
            store.write_ops_file(conn, relpath, text, "rebuild")
            outcomes[relpath] = "written"
        elif not oversized and store.CLEARED_WHEN_CHECKOUT_LACKS[relpath]:
            outcomes[relpath] = "cleared" if store.clear_ops_file(conn, relpath) else "absent"
        else:
            snapshot_exists = store.read_ops_file(conn, relpath) is not None
            outcomes[relpath] = "kept" if snapshot_exists else "absent"
    return outcomes


def rebuild(conn, repo_dir: str, embedder, fts_config: str = "english") -> dict:
    """Drop + recreate the index from `repo_dir`. Returns build stats: per-zone page counts,
    cache hits vs new embeddings, and `ops_files` — each cached ops file's reconcile outcome
    (`written`/`cleared`/`kept`/`absent`), with the registry's repeated under `entity_registry`
    because that is the key `job_runs` history already carries."""
    rows = corpus.load_pages(repo_dir)
    if not rows:
        raise EmptyCorpusError(f"no pages found under {repo_dir!r} zones {corpus.ZONES}")

    # consult the cache only if it exists already (first build on an empty database)
    hashes = [r.content_hash for r in rows]
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('embedding_cache')")
        cache_exists = cur.fetchone()[0] is not None
    cached = store.cached_embeddings(conn, embedder.model, hashes) if cache_exists else {}

    to_embed = [r for r in rows if r.content_hash not in cached]
    # one embedding per distinct content_hash (identical pages embed once)
    unique: dict[str, str] = {}
    for r in to_embed:
        unique.setdefault(r.content_hash, r.embed_text)
    fresh: dict[str, list[float]] = {}
    if unique:
        keys = list(unique)
        vectors = embedder.embed([unique[h] for h in keys])
        fresh = dict(zip(keys, vectors, strict=True))

    embeddings = {**cached, **fresh}
    dim = len(next(iter(embeddings.values())))
    # ONE transaction for drop+create+cache+insert (the store's own transaction blocks nest
    # as savepoints): a failure mid-rebuild must leave the previous index, never an
    # empty-but-valid one a concurrent reader would answer from with silent zero hits.
    with conn.transaction():
        store.init_schema(conn, dim=dim, model=embedder.model, fts_config=fts_config,
                          host=getattr(embedder, "host", ""))
        if fresh:
            store.store_embeddings(conn, embedder.model, fresh)
        store.insert_pages(conn, rows, embeddings, fts_config)
        # after the rows, never before — see `create_search_indexes`' own docstring
        store.create_search_indexes(conn)
        ops_files = _reconcile_ops_files(conn, repo_dir)

    # NEVER silent, either way a snapshot stops matching the checkout: a CLEAR destroys state the
    # push webhook may have refreshed seconds ago (the registry's absent-in-checkout posture), and
    # a KEEP means the checkout and the snapshot now disagree about an access-scoping file. The
    # same function refuses an empty CORPUS loudly (`EmptyCorpusError`); these are not errors, but
    # they must be as visible — in the log, and in the stats `job_runs` keeps.
    for relpath, outcome in ops_files.items():
        if outcome == "cleared":
            log.warning("index rebuild: nothing installable at %s/%s — the snapshot is CLEARED "
                        "and every reader falls back to its own copy until the file lands again",
                        repo_dir, relpath)
        elif outcome == "kept":
            log.error("index rebuild: %s/%s is missing from the checkout and its snapshot STANDS "
                      "— an access-scoping file's absence is an anomaly, never an instruction to "
                      "fall back to the deploy-time copy. Push the file (an explicit {} is a "
                      "committed, reviewable statement), or clear the row by hand",
                      repo_dir, relpath)

    zones: dict[str, int] = {}
    for r in rows:
        zones[r.zone] = zones.get(r.zone, 0) + 1
    return {"pages": len(rows), "zones": zones, "embedded": len(fresh), "cached": len(cached),
            "model": embedder.model, "dim": dim, "fts_config": fts_config,
            "entity_registry": ops_files[store.ENTITY_REGISTRY_RELPATH],
            "ops_files": ops_files}
