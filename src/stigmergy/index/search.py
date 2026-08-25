"""Hybrid lexical and vector retrieval over the derived index."""
import json
from datetime import date

import httpx

from stigmergy.index import rank
from stigmergy.index.backends.embedder import embedder_for_model
from stigmergy.index.errors import EmptyIndexError, StigmergyIndexError
from stigmergy.index.store import PAGE_COLUMNS, read_meta

FILTER_COLUMNS = ("zone", "type", "status", "entity")
QUERY_EMBED_TIMEOUT_S = 10

# Build an OR query from escaped normalized lexemes so natural-language questions retain recall.
_FTS_SQL = """
WITH q AS (
    SELECT to_tsquery(%(fts_config)s::regconfig,
                      string_agg('''' || replace(lexeme, '''', '''''') || '''', ' | ')) AS tsq
    FROM unnest(tsvector_to_array(to_tsvector(%(fts_config)s::regconfig, %(query)s))) lexeme)
SELECT path FROM pages_index, q
WHERE tsv @@ tsq{filters}
ORDER BY ts_rank_cd(tsv, tsq) DESC, path
LIMIT %(pool)s
"""

_VEC_SQL = """
SELECT path FROM pages_index
WHERE true{filters}
ORDER BY embedding <=> %(embedding)s::halfvec, path
LIMIT %(pool)s
"""


def _filter_clause(filters: dict | None) -> tuple[str, dict]:
    """Build validated scalar filters, with membership semantics for entity anchors."""
    if not filters:
        return "", {}
    unknown = sorted(set(filters) - set(FILTER_COLUMNS))
    if unknown:
        raise ValueError(f"unknown filter column(s): {unknown} (allowed: {FILTER_COLUMNS})")
    parts, params = [], {}
    for col in sorted(filters):
        key = f"filter_{col}"
        parts.append(f" AND %({key})s = ANY(entity)" if col == "entity"
                     else f" AND {col} = %({key})s")
        params[key] = str(filters[col])
    return "".join(parts), params


def _audience_clause(audiences: set[str] | None) -> tuple[str, dict]:
    if audiences is None:
        return "", {}
    return " AND (acl IS NULL OR acl && %(reader_audiences)s::text[])", {
        "reader_audiences": sorted(audiences)
    }


def fts_ranking(conn, query: str, fts_config: str, filters: dict | None = None,
                pool: int = rank.CANDIDATE_POOL,
                audiences: set[str] | None = None) -> list[str]:
    clause, params = _filter_clause(filters)
    audience_clause, audience_params = _audience_clause(audiences)
    with conn.cursor() as cur:
        cur.execute(
            _FTS_SQL.format(filters=clause + audience_clause),
            {
                "query": query,
                "fts_config": fts_config,
                "pool": pool,
                **params,
                **audience_params,
            },
        )
        return [r[0] for r in cur.fetchall()]


def vec_ranking(conn, query_embedding: list[float], filters: dict | None = None,
                pool: int = rank.CANDIDATE_POOL,
                audiences: set[str] | None = None) -> list[str]:
    clause, params = _filter_clause(filters)
    audience_clause, audience_params = _audience_clause(audiences)
    with conn.cursor() as cur:
        cur.execute(
            _VEC_SQL.format(filters=clause + audience_clause),
            {
                "embedding": json.dumps(query_embedding),
                "pool": pool,
                **params,
                **audience_params,
            },
        )
        return [r[0] for r in cur.fetchall()]


def fetch_pages(conn, paths: list[str]) -> dict[str, dict]:
    if not paths:
        return {}
    cols = PAGE_COLUMNS
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM pages_index WHERE path = ANY(%s)", (paths,))
        return {row[0]: dict(zip(cols, row, strict=True)) for row in cur.fetchall()}


def search_arms(conn, query: str, *, embedder=None, k: int = rank.TOP_K,
                filters: dict | None = None,
                today: date | None = None, entity_hint: str | None = None,
                fts_expansion: tuple[str, ...] = (),
                audiences: set[str] | None = None) -> dict:
    """Return lexical and vector rankings plus the final ranked hits."""
    if not (query or "").strip():
        raise ValueError("an empty query matches nothing — search for the words the material "
                         "actually uses")
    meta = read_meta(conn)
    if meta is None:
        raise EmptyIndexError("the index is empty — run `stigmergy-index --rebuild --repo <dir>` first")
    embedder = embedder or embedder_for_model(meta["model"])
    # Equal model names on different hosts are not guaranteed to share a vector space.
    recorded_host = (meta.get("host") or "").strip()
    live_host = (getattr(embedder, "host", "") or "").strip()
    if recorded_host and live_host and recorded_host != live_host:
        raise StigmergyIndexError(
            f"this index was built against the embedding host {recorded_host} and the embedder "
            f"is configured for {live_host} — the same model name on two hosts is not provably "
            f"the same vector space. Rebuild the index against the configured OpenRouter "
            f"embedding endpoint (`stigmergy-index --rebuild --repo <dir>`)")
    fts_query = " ".join((query, *fts_expansion)) if fts_expansion else query
    fts = fts_ranking(
        conn,
        fts_query,
        meta["fts_config"],
        filters,
        audiences=audiences,
    )
    try:
        q_emb = embedder.embed([query], timeout_s=QUERY_EMBED_TIMEOUT_S)[0]
    except httpx.TimeoutException:
        vec = []
    else:
        vec = vec_ranking(conn, q_emb, filters, audiences=audiences)
    candidates = fetch_pages(conn, sorted(set(fts) | set(vec)))
    hits = rank.rank(candidates, fts, vec, query, k=k, today=today, entity_hint=entity_hint)
    return {"fts": fts, "vec": vec, "hits": hits,
            "page_ids": {p: c["page_id"] for p, c in candidates.items()}}


def search(conn, query: str, *, embedder=None, k: int = rank.TOP_K,
           filters: dict | None = None,
           today: date | None = None, entity_hint: str | None = None,
           fts_expansion: tuple[str, ...] = (),
           audiences: set[str] | None = None) -> list[dict]:
    """Top-k contract-ranked hits, each carrying `factors`, `arms`, `score` and a snippet."""
    return search_arms(conn, query, embedder=embedder, k=k, filters=filters,
                       today=today, entity_hint=entity_hint,
                       fts_expansion=fts_expansion, audiences=audiences)["hits"]
