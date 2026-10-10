"""Hybrid lexical and vector retrieval over the derived index."""
import json
from datetime import date

import httpx

from stigmergy.index import fragments, rank, store
from stigmergy.index.backends.embedder import embedder_for_model
from stigmergy.index.errors import EmptyIndexError, StigmergyIndexError
from stigmergy.index.store import PAGE_COLUMNS, read_meta
from stigmergy.kernel import layers, tenancy

FILTER_COLUMNS = ("zone", "type", "status", "entity")
QUERY_EMBED_TIMEOUT_S = 3

# Build an OR query from escaped normalized lexemes so natural-language questions retain recall.
_FTS_SQL = """
WITH q AS (
    SELECT to_tsquery(%(fts_config)s::regconfig,
                      string_agg('''' || replace(lexeme, '''', '''''') || '''', ' | ')) AS tsq
    FROM unnest(tsvector_to_array(to_tsvector(%(fts_config)s::regconfig, %(query)s))) lexeme)
SELECT vpath FROM pages_index, q
WHERE tsv @@ tsq{filters}
ORDER BY ts_rank_cd(tsv, tsq) DESC, path
LIMIT %(pool)s
"""

# Distance alone orders each scan so an HNSW index serves it; the iterative scan keeps going past
# fragments the page filters exclude. The neighbours found are approximate, as any HNSW scan's are.
_VEC_BRANCH = """(SELECT vpath, path, ordinal, embedding <=> %(embedding)s::halfvec AS distance
FROM page_fragments
WHERE true{audience}{filters}
ORDER BY embedding <=> %(embedding)s::halfvec
LIMIT %(fragments)s)"""
VEC_FRAGMENTS = 160
SNIPPET_FRAGMENT_CHARS = 400


_INTERSECTION_SQL = """
SELECT vpath FROM (
    SELECT vpath, max(updated) AS latest, array_agg(DISTINCT target) AS reached
    FROM pages_index, unnest(links) AS target
    WHERE links && %(entities)s::text[]{filters}
    GROUP BY vpath) AS pages
WHERE reached @> %(entities)s::text[]
ORDER BY latest DESC, vpath
LIMIT %(pool)s
"""

# Seeds vote for the pages they link and the pages linking them. Each direction enumerates at most
# `NEIGHBOR_FANOUT` visible neighbors per seed, a seed's vote is damped by its own visible degree,
# and a neighbor's total by its visible degree, so hub pages neither flood nor win the list. Every
# edge and every degree counts only pages the reader can see, and every limit applies after the
# audience filter.
_NEIGHBOR_SQL = """
WITH vis AS NOT MATERIALIZED (
    SELECT path, vpath, links, updated FROM pages_index WHERE true{audience}),
seeds AS (
    SELECT s.path AS vpath, s.score, coalesce(array_agg(DISTINCT l) FILTER (WHERE l IS NOT NULL), '{{}}') AS links
    FROM unnest(%(seed_paths)s::text[], %(seed_scores)s::float8[]) AS s(path, score)
    JOIN vis sp ON sp.vpath = s.path
    LEFT JOIN LATERAL unnest(sp.links) AS l ON true
    GROUP BY s.path, s.score
),
edges AS (
    SELECT s.vpath AS seed, s.score, n.vpath AS nbr FROM seeds s
    CROSS JOIN LATERAL (
        SELECT v.vpath FROM vis v WHERE v.links @> ARRAY[s.vpath] AND v.vpath <> s.vpath
        GROUP BY v.vpath ORDER BY max(v.updated) DESC, v.vpath LIMIT %(fanout)s) AS n
    UNION ALL
    SELECT s.vpath AS seed, s.score, n.vpath AS nbr FROM seeds s
    CROSS JOIN LATERAL (
        SELECT v.vpath FROM unnest(s.links) AS t(target) JOIN vis v ON v.vpath = t.target
        WHERE v.vpath <> s.vpath GROUP BY v.vpath ORDER BY max(v.updated) DESC, v.vpath
        LIMIT %(fanout)s) AS n
),
seed_degree AS (SELECT seed, count(*) AS degree FROM edges GROUP BY seed),
votes AS (
    SELECT e.nbr, sum(e.score * ln(2) / ln(2 + d.degree)) AS vote
    FROM edges e JOIN seed_degree d ON d.seed = e.seed
    GROUP BY e.nbr
),
cand AS (
    SELECT votes.nbr AS nvpath, votes.vote,
           (SELECT max(p.updated) FROM vis p WHERE p.vpath = votes.nbr) AS nupdated,
           (SELECT coalesce(array_agg(DISTINCT l), '{{}}') FROM vis p, unnest(p.links) AS l
            WHERE p.vpath = votes.nbr) AS nlinks
    FROM votes
    WHERE EXISTS (SELECT 1 FROM pages_index WHERE vpath = votes.nbr{filters})
    ORDER BY votes.vote DESC, votes.nbr
    LIMIT %(cand_cap)s
)
SELECT nvpath FROM cand
ORDER BY cand.vote * ln(2) / ln(2 + (
    (SELECT count(*) FROM (SELECT DISTINCT q.vpath FROM vis q
        WHERE q.links @> ARRAY[cand.nvpath] AND q.vpath <> cand.nvpath LIMIT %(degree_cap)s) AS i)
    + (SELECT count(*) FROM (SELECT DISTINCT tp.vpath FROM unnest(cand.nlinks) AS t(target)
        JOIN vis tp ON tp.vpath = t.target LIMIT %(degree_cap)s) AS o)
)::float8) DESC, nupdated DESC, nvpath
LIMIT %(pool)s
"""

GRAPH_SEEDS = 8
INTERSECTION_POOL = 20
NEIGHBOR_POOL = 40
NEIGHBOR_FANOUT = 200
NEIGHBOR_CANDIDATES = 160
DEGREE_CAP = 256


def _filter_clause(filters: dict | None) -> tuple[str, dict]:
    """Build validated scalar filters; `entity` selects an entity page and every page linking it."""
    if not filters:
        return "", {}
    unknown = sorted(set(filters) - set(FILTER_COLUMNS))
    if unknown:
        raise ValueError(f"unknown filter column(s): {unknown} (allowed: {FILTER_COLUMNS})")
    parts, params = [], {}
    for col in sorted(filters):
        key = f"filter_{col}"
        parts.append(f" AND (%({key})s = ANY(links) OR vpath = %({key})s)" if col == "entity"
                     else f" AND {col} = %({key})s")
        params[key] = str(filters[col])
    return "".join(parts), params


def _audience_clause(audiences: set[str] | None) -> tuple[str, dict]:
    if audiences is None:
        return "", {}
    return " AND (acl IS NULL OR acl && %(reader_audiences)s::text[])", {
        "reader_audiences": sorted(audiences)
    }


def _distinct(paths) -> list[str]:
    """A ranking of virtual paths: a path several layers hold keeps its best position."""
    return list(dict.fromkeys(paths))


def intersection_ranking(conn, entities: tuple[str, ...], filters: dict | None = None,
                         audiences: set[str] | None = None) -> list[str]:
    """Pages linking every resolved entity, newest first: the relation a question naming two
    entities asks about, which neither lexical nor vector similarity can express."""
    if len(entities) < 2:
        return []
    clause, params = _filter_clause(filters)
    audience_clause, audience_params = _audience_clause(audiences)
    with conn.cursor() as cur:
        cur.execute(_INTERSECTION_SQL.format(filters=clause + audience_clause),
                    {"entities": list(entities), "pool": INTERSECTION_POOL, **params,
                     **audience_params})
        return _distinct(r[0] for r in cur.fetchall())


def neighbor_ranking(conn, seeds: list[tuple[str, float]], filters: dict | None = None,
                     audiences: set[str] | None = None) -> list[str]:
    """One hop from the best hits, in one statement, ranked by hub-damped seed votes."""
    if not seeds:
        return []
    clause, params = _filter_clause(filters)
    audience_clause, audience_params = _audience_clause(audiences)
    with conn.cursor() as cur:
        cur.execute(_NEIGHBOR_SQL.format(audience=audience_clause, filters=clause + audience_clause),
                    {"seed_paths": [p for p, _ in seeds], "seed_scores": [s for _, s in seeds],
                     "pool": NEIGHBOR_POOL, "fanout": NEIGHBOR_FANOUT,
                     "cand_cap": NEIGHBOR_CANDIDATES, "degree_cap": DEGREE_CAP,
                     **params, **audience_params})
        return _distinct(r[0] for r in cur.fetchall())


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
        return _distinct(r[0] for r in cur.fetchall())


def vec_statement(conn, filters: dict | None, audiences: set[str] | None) -> tuple[str, dict]:
    """The vector statement and its filter parameters: one nearest-neighbour scan per audience the
    reader holds (an unscoped reader: every audience with a page), each served by that audience's
    own HNSW index, or scanned exactly when it has none."""
    clause, params = _filter_clause(filters)
    page_filter = f" AND path IN (SELECT path FROM pages_index WHERE true{clause})" if clause else ""
    held = (store.page_audiences(conn) if audiences is None
            else [layers.PUBLIC, *sorted(layer for layer in audiences if layers.is_layer_name(layer))])
    tenant = tenancy.require_tenant(conn)
    scopes = [f" AND {store.audience_predicate(tenant, layer)}" for layer in held]
    return "\nUNION ALL\n".join(_VEC_BRANCH.format(audience=scope, filters=page_filter) for scope in scopes), params


def vec_fragments(conn, query_embedding: list[float], filters: dict | None = None,
                  audiences: set[str] | None = None,
                  limit: int = VEC_FRAGMENTS) -> list[tuple[str, str, int, float]]:
    """The nearest visible fragments, `(virtual path, path, ordinal, distance)`, nearest first.
    Page filters apply through the fragment's page row; the audience applies to the fragment."""
    statement, params = vec_statement(conn, filters, audiences)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true),"
                    " set_config('hnsw.ef_search', %s, true)", (str(limit),))
        cur.execute(statement, {"embedding": json.dumps(query_embedding), "fragments": limit, **params})
        found = cur.fetchall()
    return sorted(((vpath, path, ordinal, float(distance)) for vpath, path, ordinal, distance in found),
                  key=lambda item: (item[3], item[0], item[2]))[:limit]


def best_fragments(found, pool: int = rank.CANDIDATE_POOL) -> dict[str, tuple[str, int]]:
    """`{virtual path: (path, ordinal)}` of each page's nearest fragment, for the `pool` nearest
    pages, nearest first."""
    best: dict[str, tuple[str, int]] = {}
    for vpath, path, ordinal, _distance in found:
        if vpath not in best:
            if len(best) == pool:
                break
            best[vpath] = (path, ordinal)
    return best


# A fragment's number is its 1-based position among the page's visible fragments: `public`'s
# first, then each layer's, in repository-path and ordinal order.
_NUMBERED_SQL = """
WITH numbered AS (
  SELECT vpath, path, ordinal,
         row_number() OVER (PARTITION BY vpath ORDER BY (acl IS NOT NULL), path, ordinal) AS number,
         count(*) OVER (PARTITION BY vpath) AS total
  FROM page_fragments WHERE vpath = ANY(%s){audience})
SELECT n.vpath, n.path, n.ordinal, n.number, n.total, f.acl, f.trail, f.text
FROM numbered n JOIN page_fragments f USING (path, ordinal)
WHERE {where}
ORDER BY n.vpath, n.number
"""


def _numbered(conn, vpaths, where: str, params: tuple, *, audiences: set[str] | None) -> list[dict]:
    audience_clause, audience_params = positional_audience_clause(audiences)
    columns = ("vpath", "path", "ordinal", "number", "total", "acl", "trail", "text")
    with conn.cursor() as cur:
        cur.execute(_NUMBERED_SQL.format(audience=audience_clause, where=where),
                    (sorted(vpaths), *audience_params, *params))
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def fragment_refs(conn, best: dict[str, tuple[str, int]], *, audiences: set[str] | None) -> dict[str, dict]:
    """`{virtual path: {"number", "of", "text"}}` for each page's best fragment: its number, how
    many fragments the reader sees on that page, and its text."""
    if not best:
        return {}
    rows = _numbered(conn, best, "(n.path, n.ordinal) IN (SELECT * FROM unnest(%s::text[], %s::int[]))",
                     ([path for path, _o in best.values()], [ordinal for _p, ordinal in best.values()]),
                     audiences=audiences)
    return {row["vpath"]: {"number": row["number"], "of": row["total"], "text": row["text"]}
            for row in rows if best.get(row["vpath"]) == (row["path"], row["ordinal"])}


def fragment_window(conn, vpath: str, number: int, *,
                    audiences: set[str] | None) -> tuple[list[dict], int] | None:
    """Fragment `number` of a page and its neighbours, `({"number", "layer", "trail", "text"}...,
    how many the reader sees)`, or None when the reader sees no such fragment."""
    rows = _numbered(conn, [vpath], "n.number BETWEEN %s AND %s", (number - 1, number + 1), audiences=audiences)
    if not any(row["number"] == number for row in rows):
        return None
    return ([{"number": row["number"], "layer": layer_of(row["acl"]), "trail": row["trail"], "text": row["text"]}
             for row in rows], rows[0]["total"])


def fetch_pages(conn, paths: list[str]) -> dict[str, dict]:
    if not paths:
        return {}
    cols = PAGE_COLUMNS
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM pages_index WHERE path = ANY(%s)", (paths,))
        return {row[0]: dict(zip(cols, row, strict=True)) for row in cur.fetchall()}


def fetch_link_targets(conn, paths, *, audiences: set[str] | None) -> dict[str, dict]:
    """`{virtual path: {title, layers}}` for the visible link targets a result's navigation needs."""
    paths = sorted({p for p in paths if isinstance(p, str)})
    if not paths:
        return {}
    audience_clause, audience_params = positional_audience_clause(audiences)
    with conn.cursor() as cur:
        cur.execute(f"SELECT vpath, title, acl FROM pages_index WHERE vpath = ANY(%s){audience_clause} "
                    "ORDER BY vpath, (acl IS NOT NULL), path", (paths, *audience_params))
        rows = cur.fetchall()
    targets: dict[str, dict] = {}
    for vpath, title, acl in rows:
        target = targets.setdefault(vpath, {"title": title, "layers": []})
        target["layers"].append(layer_of(acl))
    return targets


def layer_of(acl) -> str:
    try:
        return layers.layer_of_acl(acl)
    except layers.LayerError:
        return ""


def fused_pages(conn, paths, *, audiences: set[str] | None, columns: tuple[str, ...] = PAGE_COLUMNS) -> dict[str, dict]:
    """`{virtual path: one fused record}` over the rows of each path the reader may see, in the
    caller's order; a path the reader cannot see is absent."""
    paths = list(dict.fromkeys(p for p in paths if isinstance(p, str)))
    if not paths:
        return {}
    cols = tuple(dict.fromkeys(("path", "vpath", "acl", *columns)))
    audience_clause, audience_params = positional_audience_clause(audiences)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM pages_index WHERE vpath = ANY(%s){audience_clause} "
                    "ORDER BY path", (paths, *audience_params))
        rows = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        row["layer"] = layer_of(row["acl"])
        if row["layer"]:
            grouped.setdefault(row["vpath"], []).append(row)
    return {path: layers.fuse_rows(grouped[path]) for path in paths if path in grouped}


def bounded_unique_paths(paths, *, limit: int) -> tuple[list[str], bool]:
    """Deduplicate and cap caller-supplied paths before they can become a SQL array."""
    selected = []
    seen = set()
    for path in paths:
        if not isinstance(path, str) or path in seen:
            continue
        seen.add(path)
        if len(selected) >= limit:
            return selected, True
        selected.append(path)
    return selected, False


def entity_timeline(conn, entity_path: str, *, audiences: set[str] | None, limit: int) -> tuple[list[dict], bool]:
    """The visible pages linking an entity, one fused record per virtual path, newest first.

    ACL filtering happens before the cap; one extra page makes truncation truthful."""
    audience_clause, audience_params = positional_audience_clause(audiences)
    sql = (
        "SELECT vpath, max(updated) AS latest FROM pages_index"
        " WHERE %s = ANY(links) AND type <> 'source' AND vpath <> %s"
        f"{audience_clause}"
        " GROUP BY vpath ORDER BY (max(updated) = ''), max(updated) DESC, vpath ASC LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (entity_path, entity_path, *audience_params, limit + 1))
        paths = [row[0] for row in cur.fetchall()]
    cols = ("path", "title", "type", "status", "updated", "sources", "body", "links", "acl")
    pages = fused_pages(conn, paths[:limit], audiences=audiences, columns=cols)
    return [pages[path] for path in paths[:limit] if path in pages], len(paths) > limit


def fetch_visible_pages_limited(
    conn,
    paths: list[str],
    *,
    audiences: set[str] | None,
    limit: int,
) -> tuple[dict[str, dict], bool]:
    """Return at most ``limit`` fused visible pages in caller order and a bounded has-more flag."""
    if not paths or limit < 1:
        return {}, False
    paths, candidates_truncated = bounded_unique_paths(paths, limit=limit + 1)
    pages = fused_pages(conn, paths, audiences=audiences)
    ordered = [path for path in paths if path in pages]
    has_more = candidates_truncated or len(ordered) > limit
    return {path: pages[path] for path in ordered[:limit]}, has_more


LIST_SORTS = {
    # sort: (key expression, direction); a page without a dated source sorts last either way
    "recent": ("coalesce(last_source, '')", "DESC"),
    "oldest": ("coalesce(first_source, '9999-12-31')", "ASC"),
    "title": ("lower(title)", "ASC"),
}

_LIST_SQL = """
WITH rows AS NOT MATERIALIZED (
    SELECT vpath, path, acl, title, type, entity_type, links, source_dates,
           (SELECT min(d) FROM unnest(source_dates) AS d) AS first_d,
           (SELECT max(d) FROM unnest(source_dates) AS d) AS last_d
    FROM pages_index WHERE true{audience}{linking}),
pages AS (
    SELECT vpath,
           (array_agg(title ORDER BY (acl IS NOT NULL), path))[1] AS title,
           (array_agg(entity_type ORDER BY (acl IS NOT NULL), path)
                FILTER (WHERE entity_type <> ''))[1] AS entity_type,
           min(first_d) AS first_source, max(last_d) AS last_source
    FROM rows GROUP BY vpath
    HAVING true{having}),
ranked AS (SELECT vpath, {key} AS sort_key FROM pages WHERE true{fused})
SELECT shown.vpath, shown.sort_key, counted.total
FROM (SELECT count(*) AS total FROM ranked) AS counted
LEFT JOIN LATERAL (
    SELECT vpath, sort_key FROM ranked WHERE true{after}
    ORDER BY sort_key {direction}, vpath ASC LIMIT %(limit)s) AS shown ON true
ORDER BY shown.sort_key {direction}, shown.vpath ASC
"""


def list_pages(conn, *, audiences: set[str] | None, sort: str = "recent", after=None,
               limit: int, page_type: str | None = None, entity_type: str | None = None,
               layer: str | None = None, links_to: str | None = None, since: str | None = None,
               until: str | None = None) -> tuple[list[tuple[str, str]], int]:
    """Every visible virtual path matching the filters, one per path, in `sort` order after the
    keyset `after` (`(sort key, virtual path)`): `([(path, sort key)], total)`. Filters apply to
    the fused page: its type and entity type as readers see them, a row in `layer`, a link from any
    of its rows, a cited source in the period. The audience applies before grouping, counting, and
    the limit."""
    key, direction = LIST_SORTS[sort]
    audience_clause, params = _audience_clause(audiences)
    linking, having, fused = "", [], []
    if page_type:
        having.append("bool_or(type = %(page_type)s)")
        params["page_type"] = page_type
    if entity_type:
        fused.append("entity_type = %(entity_type)s")
        params["entity_type"] = entity_type
    if layer is not None:
        acl = layers.acl_of_layer(layer)
        having.append("bool_or(acl IS NULL)" if acl is None else "bool_or(acl = %(layer_acl)s::text[])")
        if acl is not None:
            params["layer_acl"] = list(acl)
    if links_to:
        linking = (" AND vpath IN (SELECT vpath FROM pages_index"
                   f" WHERE links @> ARRAY[%(links_to)s]::text[]{audience_clause})")
        having.append("bool_or(%(links_to)s = ANY(links))")
        params["links_to"] = links_to
    if since or until:
        having.append("bool_or(EXISTS (SELECT 1 FROM unnest(source_dates) AS d"
                      " WHERE d >= %(since)s AND d <= %(until)s))")
        params["since"] = since or "0000-01-01"
        params["until"] = until or "9999-12-31"
    after_clause = ""
    if after is not None:
        comparison = "<" if direction == "DESC" else ">"
        after_clause = (f" AND (sort_key {comparison} %(after_key)s"
                        " OR (sort_key = %(after_key)s AND vpath > %(after_path)s))")
        params["after_key"], params["after_path"] = after
    sql = _LIST_SQL.format(audience=audience_clause, linking=linking,
                           having="".join(f" AND {item}" for item in having),
                           fused="".join(f" AND {item}" for item in fused),
                           key=key, direction=direction, after=after_clause)
    with conn.cursor() as cur:
        cur.execute(sql, {**params, "limit": limit})
        rows = cur.fetchall()
    return [(path, sort_key) for path, sort_key, _total in rows if path is not None], int(rows[0][2])


def listing_records(conn, paths, *, audiences: set[str] | None) -> dict[str, dict]:
    """`{virtual path: fused listing record}` for visible `paths`: catalog fields, the head of the
    body a one-line summary is read from, and the cited sources and their dates over every visible
    row of the path."""
    paths = list(dict.fromkeys(p for p in paths if isinstance(p, str)))
    if not paths:
        return {}
    audience_clause, audience_params = positional_audience_clause(audiences)
    with conn.cursor() as cur:
        cur.execute("SELECT path, vpath, acl, title, type, entity_type, aliases, sources, source_dates,"
                    f" left(body, %s) FROM pages_index WHERE vpath = ANY(%s){audience_clause}"
                    " ORDER BY path", (rank.SUMMARY_SCAN_CHARS, paths, *audience_params))
        found = cur.fetchall()
    grouped: dict[str, list[dict]] = {}
    for path, vpath, acl, title, kind, entity_type, aliases, sources, dates, head in found:
        layer = layer_of(acl)
        if layer:
            grouped.setdefault(vpath, []).append({
                "path": path, "vpath": vpath, "acl": acl, "layer": layer, "title": title, "type": kind,
                "entity_type": entity_type or "", "aliases": list(aliases or ()),
                "sources": list(sources or ()), "source_dates": list(dates or ()), "head": head or ""})
    return {vpath: layers.fuse_rows(rows) for vpath, rows in grouped.items()}


def visible_sources(conn, paths, *, audiences: set[str] | None) -> set[str]:
    """The source virtual paths among `paths` the reader may see."""
    paths = sorted({p for p in paths if isinstance(p, str)})
    if not paths:
        return set()
    audience_clause, audience_params = positional_audience_clause(audiences)
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT vpath FROM pages_index WHERE type = 'source' AND vpath = ANY(%s)"
                    f"{audience_clause}", (paths, *audience_params))
        return {row[0] for row in cur.fetchall()}


def positional_audience_clause(audiences: set[str] | None) -> tuple[str, tuple[list[str], ...]]:
    if audiences is None:
        return "", ()
    return " AND (acl IS NULL OR acl && %s::text[])", (sorted(audiences),)


def search_arms(conn, query: str, *, embedder=None, k: int = rank.TOP_K,
                filters: dict | None = None,
                today: date | None = None, entity_hints: tuple[str, ...] = (),
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
    best: dict[str, tuple[str, int]] = {}
    try:
        q_emb = embedder.embed([query], timeout_s=QUERY_EMBED_TIMEOUT_S)[0]
    except httpx.TimeoutException:
        vector_search = "timed_out"
    else:
        best, vector_search = best_fragments(vec_fragments(conn, q_emb, filters, audiences)), "ok"
    vec = list(best)
    extra = {"entity": intersection_ranking(conn, entity_hints, filters, audiences)}
    candidates = fused_pages(conn, sorted(set(fts) | set(vec) | set(extra["entity"])), audiences=audiences)
    seeds = rank.rank(candidates, fts, vec, query, k=GRAPH_SEEDS, today=today,
                      entity_hints=entity_hints, extra_rankings=extra)
    extra["graph"] = neighbor_ranking(conn, [(h["path"], h["score"]) for h in seeds],
                                      filters, audiences)
    missing = sorted(set(extra["graph"]) - set(candidates))
    candidates.update(fused_pages(conn, missing, audiences=audiences))
    hits = rank.rank(candidates, fts, vec, query, k=k, today=today,
                     entity_hints=entity_hints, extra_rankings=extra)
    refs = fragment_refs(conn, {hit["path"]: best[hit["path"]] for hit in hits if hit["path"] in best},
                         audiences=audiences)
    words = rank.query_tokens(query)
    for hit in hits:
        ref = refs.get(hit["path"])
        if ref is not None and ref["of"] > 1:
            hit["fragment"] = fragments.reference(hit["path"], ref["number"])
            hit["fragments"] = ref["of"]
            hit["snippet"] = rank.snippet(ref["text"], words, width=SNIPPET_FRAGMENT_CHARS)
    return {"fts": fts, "vec": vec, **extra, "hits": hits, "vector_search": vector_search,
            "ranked": len(candidates),
            "page_ids": {p: c["page_id"] for p, c in candidates.items()}}


def search(conn, query: str, *, embedder=None, k: int = rank.TOP_K,
           filters: dict | None = None,
           today: date | None = None, entity_hints: tuple[str, ...] = (),
           fts_expansion: tuple[str, ...] = (),
           audiences: set[str] | None = None) -> list[dict]:
    """Top-k contract-ranked hits, each carrying `factors`, `arms`, `score` and a snippet."""
    return search_arms(conn, query, embedder=embedder, k=k, filters=filters,
                       today=today, entity_hints=entity_hints,
                       fts_expansion=fts_expansion, audiences=audiences)["hits"]
