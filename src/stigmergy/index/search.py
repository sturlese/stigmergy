"""Hybrid retrieval over `pages_index`: the two arms, fused, then the contract ranking.

One shared base query feeds every consumer (CLI, golden runner, the MCP server): the same
frontmatter filters and candidate pools apply to both arms, so no caller can ask the lexical and
semantic arms structurally different questions. `include_superseded` is the one operational
filter — superseded pages are DEMOTED by default (history stays reachable) and only dropped when
a caller explicitly asks for current-only.

This layer knows no identity: `acl` is returned as a column, and access is decided ABOVE, by
`stigmergy.server.acl.visible()`.
"""
import json
from datetime import date

from stigmergy.index import rank
from stigmergy.index.backends.embedder import embedder_for_model
from stigmergy.index.errors import EmptyIndexError
from stigmergy.index.store import PAGE_COLUMNS, read_meta

# The only columns a caller may filter on.
FILTER_COLUMNS = ("zone", "type", "status", "entity", "owner", "tier", "as_of")

# OR of the query's own lexemes: websearch_to_tsquery ANDs every term, so a whole natural-language
# question would match nothing by construction. Each lexeme is single-quoted (inner quotes
# doubled) before to_tsquery: normalized lexemes can still contain tsquery syntax (':' in URLs,
# '/'), which would otherwise let a hostile or merely URL-bearing query crash the arm.
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
    """The frontmatter filters, AND-ed onto the base query: ONE value per column. `entity` is the
    one `text[]` column, matched by MEMBERSHIP (`%s = ANY(entity)`) so a page anchored to several
    entities is found by any one; every other column is scalar equality.

    `filters={"entity": ""}` matches NOTHING, by contract: `entity` is `NOT NULL DEFAULT '{}'`
    and `corpus.entity_list` never produces `['']`, so `'' = ANY(entity)` matches only a row that
    would itself be a contract violation.
    """
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


def fts_ranking(conn, query: str, fts_config: str, filters: dict | None = None,
                pool: int = rank.CANDIDATE_POOL) -> list[str]:
    clause, params = _filter_clause(filters)
    with conn.cursor() as cur:
        cur.execute(_FTS_SQL.format(filters=clause),
                    {"query": query, "fts_config": fts_config, "pool": pool, **params})
        return [r[0] for r in cur.fetchall()]


def vec_ranking(conn, query_embedding: list[float], filters: dict | None = None,
                pool: int = rank.CANDIDATE_POOL) -> list[str]:
    clause, params = _filter_clause(filters)
    with conn.cursor() as cur:
        cur.execute(_VEC_SQL.format(filters=clause),
                    {"embedding": json.dumps(query_embedding), "pool": pool, **params})
        return [r[0] for r in cur.fetchall()]


def fetch_pages(conn, paths: list[str]) -> dict[str, dict]:
    if not paths:
        return {}
    # `links`/`generated_at` are fetched here so the serving surfaces (`read_page`,
    # `describe_entity`) share this ONE fetch; `rank.rank` ignores the extra keys — they change
    # what is SERVED, not what SCORES.
    cols = PAGE_COLUMNS
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM pages_index WHERE path = ANY(%s)", (paths,))
        return {row[0]: dict(zip(cols, row, strict=True)) for row in cur.fetchall()}


def search_arms(conn, query: str, *, embedder=None, k: int = rank.TOP_K,
                filters: dict | None = None, include_superseded: bool = True,
                today: date | None = None, entity_hint: str | None = None,
                fts_expansion: tuple[str, ...] = ()) -> dict:
    """The full picture, one query: per-arm rankings plus the final contract-ranked hits.
    `embedder` defaults to the one the index was built with (index_meta) — queries must embed
    in the same space the documents did.

    Two facts the registry-owning service may TELL this module (it resolves nothing itself):
    `entity_hint`, the resolved entity id handed to `rank` for the entity boost; and
    `fts_expansion`, the registry's other names for it, appended to the LEXICAL arm only (an OR
    of lexemes can only ADD candidates) — the vector arm embeds the raw query untouched, because
    expansion is a lexical repair, not a semantic one."""
    # BEFORE the meta read, so the refusal is pure and reaches every caller: the ask agent's
    # search tool turns a ValueError into an error string the model repairs from. OLD BEHAVIOUR:
    # an empty model-chosen query reached the embedding PROVIDER, whose 400 (OpenAI and
    # OpenRouter both refuse empty input) crashed the whole ask instead of repairing one tool
    # call — surfaced by the qa golden's first DeepSeek run.
    if not (query or "").strip():
        raise ValueError("an empty query matches nothing — search for the words the material "
                         "actually uses")
    meta = read_meta(conn)
    if meta is None:
        raise EmptyIndexError("the index is empty — run `stigmergy-index --rebuild --repo <dir>` first")
    embedder = embedder or embedder_for_model(meta["model"])
    q_emb = embedder.embed([query])[0]
    fts_query = " ".join((query, *fts_expansion)) if fts_expansion else query
    fts = fts_ranking(conn, fts_query, meta["fts_config"], filters)
    vec = vec_ranking(conn, q_emb, filters)
    candidates = fetch_pages(conn, sorted(set(fts) | set(vec)))
    hits = rank.rank(candidates, fts, vec, query, k=k, today=today,
                     include_superseded=include_superseded, entity_hint=entity_hint)
    return {"fts": fts, "vec": vec, "hits": hits,
            "page_ids": {p: c["page_id"] for p, c in candidates.items()}}


def search(conn, query: str, *, embedder=None, k: int = rank.TOP_K,
           filters: dict | None = None, include_superseded: bool = True,
           today: date | None = None, entity_hint: str | None = None,
           fts_expansion: tuple[str, ...] = ()) -> list[dict]:
    """Top-k contract-ranked hits, each carrying `factors`, `arms`, `score` and a snippet."""
    return search_arms(conn, query, embedder=embedder, k=k, filters=filters,
                       include_superseded=include_superseded, today=today,
                       entity_hint=entity_hint, fts_expansion=fts_expansion)["hits"]
