"""Full rebuild: a knowledge-repo checkout -> a fresh `pages_index` and the entity-registry
snapshot beside it. Incremental-on-merge lives in `stigmergy.server.webhook`; the only
incrementality here is the embedding cache, which keeps a rebuild's API spend proportional to what
actually changed.
"""
import logging
import os

from stigmergy.index import corpus, store
from stigmergy.index.errors import EmptyCorpusError

log = logging.getLogger(__name__)

# `server.entity_aliases.ENTITY_REGISTRY_RELPATH`'s spelling of the same file. Spelled here rather
# than imported: `stigmergy.index` sits BELOW `stigmergy.server` and may not import it, so the
# duplication is declared instead of discovered.
ENTITY_REGISTRY_RELPATH = "ops/entity-registry.json"


def registry_path(repo_dir: str) -> str:
    """`<repo_dir>/ops/entity-registry.json`, the ONE spelling in this package — `index/cli.py`
    builds the `--check` path through it rather than re-joining the parts."""
    return os.path.join(repo_dir, *ENTITY_REGISTRY_RELPATH.split("/"))


def _read_entity_registry_file(repo_dir: str) -> str | None:
    """The checkout's registry TEXT, or `None` when there is nothing installable to read.

    A repo with NO registry is a real state (a knowledge repo before its first mint), never an
    error: a missing registry is an empty one everywhere else in this codebase, and a rebuild that
    refused it would make the index unbuildable for exactly the repos with nothing to serve yet.

    A registry ABOVE `store.MAX_ENTITY_REGISTRY_BYTES` is refused rather than installed, and the
    cap is the webhook's: the two roads write one row, so a rebuild that installed what a push
    refuses would just move the per-request parse cost to whichever road ran last."""
    path = registry_path(repo_dir)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        text = f.read()
    size = len(text.encode("utf-8"))
    if size > store.MAX_ENTITY_REGISTRY_BYTES:
        log.error("index rebuild: %s is %d bytes, above the %d-byte snapshot cap — NOT installed; "
                  "every server on this index falls back to its own --entity-registry file",
                  path, size, store.MAX_ENTITY_REGISTRY_BYTES)
        return None
    return text


def rebuild(conn, repo_dir: str, embedder, fts_config: str = "english") -> dict:
    """Drop + recreate the index from `repo_dir`. Returns build stats (per-zone page counts, cache
    hits vs new embeddings, and `entity_registry`: whether the snapshot was `written` or
    `cleared`)."""
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
    registry_text = _read_entity_registry_file(repo_dir)
    # ONE transaction for drop+create+cache+insert (the store's own transaction blocks nest
    # as savepoints): a failure mid-rebuild must leave the previous index, never an
    # empty-but-valid one a concurrent reader would answer from with silent zero hits.
    with conn.transaction():
        store.init_schema(conn, dim=dim, model=embedder.model, fts_config=fts_config)
        if fresh:
            store.store_embeddings(conn, embedder.model, fresh)
        store.insert_pages(conn, rows, embeddings, fts_config)
        # after the rows, never before — see `create_search_indexes`' own docstring
        store.create_search_indexes(conn)
        # The nightly reconciler for what the push webhook refreshes incrementally. A repo with no
        # registry CLEARS the snapshot rather than leaving the last one standing: this is the run
        # that makes the index match the checkout, and a snapshot answering from a registry the
        # repo no longer has is the same deploy-time staleness the snapshot exists to end. Cleared,
        # the server falls back to its own `--entity-registry` file — the pre-snapshot behaviour,
        # and the honest floor.
        if registry_text is None:
            store.clear_entity_registry(conn)
        else:
            store.write_entity_registry(conn, registry_text, "rebuild")

    if registry_text is None:
        # NEVER silent: this branch destroys state the push webhook may have refreshed seconds ago
        # and hands every reader back to the copy baked at deploy time, which is issue #74. The
        # same function refuses an empty CORPUS loudly (`EmptyCorpusError`); an empty registry is
        # not an error, but it must be as visible — in the log, and in the stats `job_runs` keeps.
        log.warning("index rebuild: nothing installable at %s/%s — the entity-registry snapshot is "
                    "CLEARED, and every server on this index falls back to its own "
                    "--entity-registry file until a registry lands again",
                    repo_dir, ENTITY_REGISTRY_RELPATH)

    zones: dict[str, int] = {}
    for r in rows:
        zones[r.zone] = zones.get(r.zone, 0) + 1
    return {"pages": len(rows), "zones": zones, "embedded": len(fresh), "cached": len(cached),
            "model": embedder.model, "dim": dim, "fts_config": fts_config,
            "entity_registry": "cleared" if registry_text is None else "written"}
