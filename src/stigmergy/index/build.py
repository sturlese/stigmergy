"""Full rebuild: a knowledge-repo checkout -> a fresh `pages_index`. Incremental-on-merge lives
in `stigmergy.server.webhook`; the only incrementality here is the embedding cache, which keeps a
rebuild's API spend proportional to what actually changed.
"""
from stigmergy.index import corpus, store
from stigmergy.index.errors import EmptyCorpusError


def rebuild(conn, repo_dir: str, embedder, fts_config: str = "english") -> dict:
    """Drop + recreate the index from `repo_dir`. Returns build stats (per-zone page counts,
    cache hits vs new embeddings)."""
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
        store.init_schema(conn, dim=dim, model=embedder.model, fts_config=fts_config)
        if fresh:
            store.store_embeddings(conn, embedder.model, fresh)
        store.insert_pages(conn, rows, embeddings, fts_config)
        # after the rows, never before — see `create_search_indexes`' own docstring
        store.create_search_indexes(conn)

    zones: dict[str, int] = {}
    for r in rows:
        zones[r.zone] = zones.get(r.zone, 0) + 1
    return {"pages": len(rows), "zones": zones, "embedded": len(fresh), "cached": len(cached),
            "model": embedder.model, "dim": dim, "fts_config": fts_config}
