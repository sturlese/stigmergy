"""Postgres storage for the derived index. The one module that owns SQL DDL and writes.

`pages_index` is NEVER migrated: it is dropped and recreated on every rebuild — wipe and rebuild
is the upgrade path, because the index is a CACHE, and the end-to-end idempotency proof keeps that
honest. The only surviving table is `embedding_cache`, keyed by (model, content_hash), so a
rebuild re-embeds ONLY pages whose content changed — which is why a native content_hash is
computed before anything is embedded.

**This layer knows no identity.** `acl` is stored here and enforced ABOVE, by
`stigmergy.server.acl.visible()` at `BrainService`'s read paths — one place decides access, and it
is not the storage layer. `tests/test_architecture.py` lists this module as a named exception to
"every reader of `pages_index` names an ACL predicate" for exactly that reason.
"""
import json
import os

import psycopg
from psycopg.conninfo import conninfo_to_dict

DSN_ENV = "STIGMERGY_INDEX_DSN"
DSN_DEFAULT = "postgresql://stigmergy:stigmergy@localhost:54321/stigmergy"

_PAGES_DDL = """
CREATE TABLE pages_index (
  path text PRIMARY KEY,
  page_id text NOT NULL,
  zone text NOT NULL,
  title text NOT NULL DEFAULT '',
  body text NOT NULL DEFAULT '',
  type text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT '',
  entity text[] NOT NULL DEFAULT '{{}}',   -- a page's aboutness, plural.
  --                                          '{{}}' is BOTH "no entity key at all" and "checked
  --                                          company-wide" — the index does not need to tell them
  --                                          apart, only the page contract does
  --                                          (docs/reference/page-contract.md).
  owner text NOT NULL DEFAULT '',
  tier integer NOT NULL DEFAULT 0,
  as_of text NOT NULL DEFAULT '',
  updated text NOT NULL DEFAULT '',
  superseded_by text NOT NULL DEFAULT '',
  supersedes text NOT NULL DEFAULT '',
  acl text[],                              -- NULL = open, '{{}}' = nobody (labels may contain
  --                                          any character — no lossy CSV). Stored here,
  --                                          ENFORCED above by `server.acl.visible()`.
  inlinks integer NOT NULL DEFAULT 0,
  links text[] NOT NULL DEFAULT '{{}}',    -- resolved OUTBOUND wikilink targets, repo-relative
  --                                          paths (never stems) — a stem resolving to several
  --                                          pages stores every match, the same semantics
  --                                          `inlinks` already counts; a stem resolving to
  --                                          nothing stores nothing. The GIN index below turns
  --                                          the INBOUND view (backlinks) into a containment
  --                                          lookup (`links @> ARRAY[path]`), never a scan —
  --                                          `read_page` serves both directions.
  generated_at text NOT NULL DEFAULT '',   -- a view's own `generated_at` frontmatter
  --                                          (ISO-8601) — the one view-only field
  --                                          `describe_entity`'s view layer needs that no
  --                                          existing column carries (views set neither
  --                                          `updated` nor `as_of`); empty for every other page.
  content_hash text NOT NULL,
  tsv tsvector,
  -- halfvec, not vector — and the reason is a hard ceiling, not a preference.
  -- pgvector refuses to build an HNSW index on a column with more than 2000 dimensions, and the
  -- production embedder (`text-embedding-3-large`) is 3072. `halfvec` raises that ceiling to 4000
  -- by storing 16-bit floats, which is the standard recipe for large-dimension embeddings; the
  -- vectors are cosine-normalized and HNSW is approximate anyway, so the precision the index
  -- loses is well below the noise the approximation already introduces.
  --
  -- Changing the COLUMN rather than casting at the two call sites is deliberate: a cast in the
  -- query that disagrees with the cast in the index fails SILENTLY (the planner just seq-scans),
  -- which is the exact failure mode this build keeps finding. A wrong type here fails loudly.
  embedding halfvec({dim})
)
"""

_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS embedding_cache (
  model text NOT NULL,
  content_hash text NOT NULL,
  embedding jsonb NOT NULL,
  PRIMARY KEY (model, content_hash)
)
"""

_META_DDL = """
CREATE TABLE IF NOT EXISTS index_meta (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  model text NOT NULL,
  dim integer NOT NULL,
  fts_config text NOT NULL,
  built_at timestamptz NOT NULL DEFAULT now()   -- when this index was last rebuilt; the server
  --                                               returns it so staleness is self-diagnosing
)
"""

# The searchable text: title, body, tags, entity, mentions.
# `entity` is a list — folded into the tsvector text the same way `tags` is, via
# `array_to_string`, so a page anchored to several entities is findable by any of them exactly as
# a multi-tag page already is.
# `entity_meta` (an entity page's own `role`/`aliases` frontmatter, empty for every other page
# type — `corpus._entity_meta_text`) joins the same tsv-only channel `tags`/`mentions` are: never
# a stored column, only a source `to_tsvector` reads once here.
_TSV_SQL = ("to_tsvector(%(fts_config)s, %(title)s || ' ' || %(tags)s || ' ' || "
            "array_to_string(%(entity)s::text[], ' ') || ' ' || %(mentions)s || ' ' || "
            "%(entity_meta)s || ' ' || %(body)s)")


def dsn() -> str:
    return os.environ.get(DSN_ENV, DSN_DEFAULT)


def host_of_dsn(conninfo: str | None) -> str:
    """The DSN's host, credential-free — `""` when it names none (a unix socket, PG* defaults, or
    a string libpq cannot read).

    Through libpq's OWN parser, because the keyword form (`host=h port=5432 password=…`) is a DSN
    too and string surgery on it returns the whole connstring, password included. That is not a
    hypothetical: this repo already legislated the rule twice — `tests/testdb.describe` ("never
    the DSN itself. A DSN carries a password") and `mcp_server._dsn_location` — and a third
    hand-rolled parser is how a credential reaches a terminal. Callers that merely need to say
    WHERE the queue is get this; nobody outside this module needs to parse a DSN at all.
    """
    try:
        host = str(conninfo_to_dict(conninfo or "").get("host") or "")
    except psycopg.Error:
        return ""
    return host.split(",", 1)[0].strip().lower()   # multi-host: the first is the one tried first


def connect(conninfo: str | None = None) -> psycopg.Connection:
    """Autocommit ON PURPOSE: a reader must never sit idle-in-transaction holding an
    AccessShareLock on `pages_index` — a concurrent rebuild's DROP TABLE would block behind
    it forever. Writers get atomicity from explicit `conn.transaction()` blocks instead."""
    return psycopg.connect(conninfo or dsn(), autocommit=True)


def init_schema(conn: psycopg.Connection, dim: int, model: str, fts_config: str) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # TARGETED BY NAME, and it must stay that way. This database also holds the DURABLE half
        # of the system — `capture_queue`, `audit_log`, `job_runs`, `ingest_errors`
        # (`stigmergy.capture.schema.DURABLE_TABLES`) — which holds material that exists NOWHERE
        # else until the librarian files it. A "just drop everything and rebuild" shortcut here
        # would silently take the queue with the cache. The index is disposable; its neighbours
        # are not.
        cur.execute("DROP TABLE IF EXISTS pages_index")
        cur.execute(_PAGES_DDL.format(dim=dim))
        # Backlinks are a containment lookup, never a scan. Plain `CREATE INDEX` (no
        # `IF NOT EXISTS`) is correct here and nowhere else in this codebase's DDL — every other
        # `CREATE INDEX` guards a SURVIVING table's idempotent startup DDL; `pages_index` was just
        # dropped and recreated two lines up, so the index cannot already exist.
        cur.execute("CREATE INDEX pages_index_links_gin ON pages_index USING GIN (links)")

        cur.execute(_CACHE_DDL)
        cur.execute(_META_DDL)
        # index_meta is a SURVIVING table (like embedding_cache), so an older database may have it
        # without `built_at`: add the column additively rather than force a wipe (backward
        # compatible — the index stays rebuildable in place; `down -v` is still the clean reset).
        cur.execute("ALTER TABLE index_meta ADD COLUMN IF NOT EXISTS built_at timestamptz"
                    " NOT NULL DEFAULT now()")
        cur.execute("INSERT INTO index_meta (model, dim, fts_config, built_at)"
                    " VALUES (%s, %s, %s, now())"
                    " ON CONFLICT (singleton) DO UPDATE SET model = EXCLUDED.model,"
                    " dim = EXCLUDED.dim, fts_config = EXCLUDED.fts_config,"
                    " built_at = EXCLUDED.built_at",
                    (model, dim, fts_config))



def create_search_indexes(conn) -> None:
    """The two retrieval indexes — built AFTER the bulk load, which is why they are not in
    `init_schema` beside the links GIN.

    `pages_index_tsv_gin` serves `tsv @@ tsq` (the lexical arm) and `pages_index_embedding_hnsw`
    serves `embedding <=> %(embedding)s` (the vector arm, cosine — hence `halfvec_cosine_ops`,
    which must match BOTH the column type and the operator `search.VEC_SQL` uses, or Postgres
    plans a seq scan and the index is decoration. See `_PAGES_DDL` for why the column is
    `halfvec`: plain `vector` caps HNSW at 2000 dimensions and production runs 3072).

    **Why after the insert, and why that is not a micro-optimization.** An HNSW index maintains a
    navigable graph per row inserted. Building it first means every one of 50-100k rows pays graph
    maintenance during a bulk load; building it last is one bulk construction over a finished
    table. The incremental webhook path (one page at a time) then maintains both indexes at
    per-row cost, which is what that path is for. `pages_index` is dropped and recreated on every
    full rebuild, so these are rebuilt with it — there is no migration to write and no `IF NOT
    EXISTS` to guard, exactly like the links GIN above."""
    with conn.cursor() as cur:
        cur.execute("CREATE INDEX pages_index_tsv_gin ON pages_index USING GIN (tsv)")
        cur.execute("CREATE INDEX pages_index_embedding_hnsw ON pages_index "
                    "USING hnsw (embedding halfvec_cosine_ops)")


def read_meta(conn: psycopg.Connection) -> dict | None:
    """None = no index has ever been built here (table absent or empty)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('index_meta')")
        if cur.fetchone()[0] is None:
            return None
        try:
            cur.execute("SELECT model, dim, fts_config, built_at FROM index_meta")
        except psycopg.errors.UndefinedColumn:
            # an older index_meta predates the built_at column (added additively in
            # init_schema): treat it as needing a rebuild rather than crashing the server with a
            # raw error — the caller's empty-index path then surfaces the `--rebuild` hint.
            return None
        row = cur.fetchone()
    if not row:
        return None
    # built_at ships as an ISO-8601 string: the server serializes it into JSON tool output
    # (and read_meta consumers never need a live datetime).
    return {"model": row[0], "dim": row[1], "fts_config": row[2],
            "built_at": row[3].isoformat() if row[3] is not None else None}


def cached_embeddings(conn: psycopg.Connection, model: str, hashes: list[str]) -> dict[str, list[float]]:
    if not hashes:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT content_hash, embedding FROM embedding_cache"
                    " WHERE model = %s AND content_hash = ANY(%s)", (model, hashes))
        return {h: emb for h, emb in cur.fetchall()}


def store_embeddings(conn: psycopg.Connection, model: str, by_hash: dict[str, list[float]]) -> None:
    with conn.transaction(), conn.cursor() as cur:
        for h, emb in by_hash.items():
            cur.execute("INSERT INTO embedding_cache (model, content_hash, embedding)"
                        " VALUES (%s, %s, %s) ON CONFLICT (model, content_hash) DO NOTHING",
                        (model, h, json.dumps(emb)))


# The one column list `insert_pages`/`upsert_pages` both write, and the one source `_INSERT_SQL`
# and `_UPSERT_SET` are both built from. A claim of "ONE INSERT statement between the two" was
# once made while the column list, the VALUES clause and the params dict were each written out
# twice — a `pages_index` column added to one copy and not the other would have diverged
# silently. There is exactly one column list, one params builder (`_page_params`) and one INSERT
# template (`_INSERT_SQL`) that both functions share.
_PAGE_COLUMNS = ("path", "page_id", "zone", "title", "body", "type", "status", "entity",
                 "owner", "tier", "as_of", "updated",
                 "superseded_by", "supersedes", "acl", "inlinks", "links",
                 "generated_at", "content_hash")

_INSERT_SQL = (
    "INSERT INTO pages_index (" + ", ".join(_PAGE_COLUMNS) + ", tsv, embedding)"
    " VALUES (" + ", ".join(f"%({c})s" for c in _PAGE_COLUMNS) + f", {_TSV_SQL}, %(embedding)s::halfvec)"
)


def _page_params(r, *, fts_config: str, embeddings: dict[str, list[float]]) -> dict:
    """One `corpus.PageRow` -> the params dict `_INSERT_SQL` (and its `ON CONFLICT` extension)
    binds against. `tags`/`mentions`/`entity_meta` feed only `_TSV_SQL`, not `_PAGE_COLUMNS` —
    they compute the `tsv` column rather than being stored as columns of their own."""
    return {
        "path": r.path, "page_id": r.page_id, "zone": r.zone, "title": r.title,
        "body": r.body, "type": r.type, "status": r.status, "entity": r.entity,
        "owner": r.owner, "tier": r.tier, "as_of": r.as_of,
        "updated": r.updated, "superseded_by": r.superseded_by,
        "supersedes": r.supersedes, "acl": r.acl, "inlinks": r.inlinks, "links": r.links,
        "generated_at": r.generated_at,
        "content_hash": r.content_hash, "tags": r.tags, "mentions": r.mentions,
        "entity_meta": r.entity_meta,
        "fts_config": fts_config,
        "embedding": json.dumps(embeddings[r.content_hash]),
    }


def insert_pages(conn: psycopg.Connection, rows: list, embeddings: dict[str, list[float]],
                 fts_config: str) -> None:
    """`rows` are corpus.PageRow; `embeddings` maps content_hash -> vector."""
    with conn.transaction(), conn.cursor() as cur:
        for r in rows:
            cur.execute(_INSERT_SQL,
                       _page_params(r, fts_config=fts_config, embeddings=embeddings))


# `inlinks` is excluded here exactly like `path` already is — `path` because it is the conflict
# key, `inlinks` because the incoming row's value (the incremental webhook's `PageRow`, always
# 0 — a single changed file cannot resolve the whole-corpus wikilink
# graph, `corpus.page_row`'s own docstring) is not a fact about the row at all, merely a default
# a single-file parse cannot compute. `EXCLUDED.inlinks` used to clobber whatever the last FULL
# rebuild had computed, demoting every incrementally-edited page in `search.py`'s ranking until
# the next nightly rebuild recomputed it — a retrieval regression the golden set cannot see
# (the golden run does a full rebuild, never an upsert). Excluding it from the SET list means an
# UPDATE simply leaves the column at its current value; a fresh INSERT (no existing row to
# conflict with) still gets the incoming row's own default (0) via the VALUES list, which is the
# honest answer for a page nothing has ever resolved the graph for yet.
#
# `links` is the OPPOSITE case from `inlinks`, and stays IN `_UPSERT_SET` (not
# excluded). A single changed file's own OUTBOUND wikilinks ARE fully resolvable from its own
# text plus `pages_index`'s EXISTING paths — unlike the whole-corpus INBOUND graph `inlinks`
# counts, no single file can compute its own inbound count, but it CAN compute its own outbound
# targets (`server.webhook`'s own one-query resolution, mirroring `corpus.load_pages`'s in-memory
# one — a parity test pins that the two agree). So an UPDATE's incoming `links` value is
# not a stale default to protect against; it is the freshest fact this row can state about its
# own outbound edges, and excluding it would leave a webhook-edited page's `links` frozen at
# whatever the last full rebuild saw.
_UPSERT_SET = ", ".join(f"{col} = EXCLUDED.{col}"
                        for col in _PAGE_COLUMNS if col not in ("path", "inlinks"))


def current_content_hashes(conn: psycopg.Connection, paths: list[str]) -> dict[str, str]:
    """`path -> content_hash` for whichever of `paths` are already indexed — the incremental
    webhook's idempotency check, so that the same content pushed twice embeds once. A path absent
    from the return value is not indexed yet at all (a genuine new page, or one outside the built
    corpus)."""
    if not paths:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT path, content_hash FROM pages_index WHERE path = ANY(%s)", (paths,))
        return dict(cur.fetchall())


def existing_paths(conn: psycopg.Connection) -> list[str]:
    """Every path currently in `pages_index` — ONE query: the webhook resolves a single file's
    stems against `pages_index`'s own paths. The incremental webhook builds its own
    `corpus.by_stem_index` from this, so its outbound-link resolution shares `corpus.resolve_links`
    with the full rebuild's in-memory one rather than growing a second algorithm that could drift
    (a parity test pins the two agree)."""
    with conn.cursor() as cur:
        cur.execute("SELECT path FROM pages_index")
        return [row[0] for row in cur.fetchall()]


def pages_with_page_id_prefix(conn: psycopg.Connection, prefix: str) -> list[tuple[str, str]]:
    """`(path, page_id)` for every row whose `page_id` STARTS WITH `prefix` — a cheap SQL prefix
    filter (`LIKE`, backslash-escaped) narrowing candidates BEFORE an exact Python-side pattern
    decides which are real (`server.webhook`'s incremental `superseded_by`
    propagation onto split-chain siblings, marker-gated exactly like `corpus.load_pages`'s
    build-time rule — `corpus.chain_part_pattern` is the exact matcher this narrows for). `prefix`
    is escaped for `%`/`_`/`\\` so a `page_id` containing those characters is never read as a
    wildcard."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with conn.cursor() as cur:
        cur.execute("SELECT path, page_id FROM pages_index WHERE page_id LIKE %s",
                   (f"{escaped}%",))
        return cur.fetchall()


def set_superseded_by(conn: psycopg.Connection, paths: list[str], value: str) -> None:
    """Targeted `superseded_by` UPDATE for exactly `paths` — the webhook's own supersession
    window: once a push upserts a split
    chain's PRIMARY, its already-indexed `#p<n>` siblings must not wait for the nightly rebuild to
    learn the new value, stamped or cleared symmetrically (`value` is used verbatim, empty string
    included)."""
    if not paths:
        return
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE pages_index SET superseded_by = %s WHERE path = ANY(%s)",
                   (value, paths))


_UPSERT_SQL = (_INSERT_SQL + f" ON CONFLICT (path) DO UPDATE SET {_UPSERT_SET},"
              f" tsv = {_TSV_SQL}, embedding = %(embedding)s::halfvec")


def upsert_pages(conn: psycopg.Connection, rows: list, embeddings: dict[str, list[float]],
                fts_config: str) -> None:
    """Insert-or-update, keyed on `path` (the table's own primary key) — the incremental path.
    `_UPSERT_SQL` extends `_INSERT_SQL` with one `ON CONFLICT` clause, so an upserted
    row and a freshly-rebuilt one can never disagree about which columns exist or how the search
    text is derived — there is exactly one column list and one params builder
    (`_page_params`) behind both, not two copies that could drift.

    `rows`/`embeddings` are exactly `insert_pages`'s shapes (`corpus.PageRow` list;
    content_hash -> vector map) — a caller that already has cached embeddings for an unchanged
    row's content_hash passes them here unchanged, so a same-content upsert never re-embeds.
    """
    with conn.transaction(), conn.cursor() as cur:
        for r in rows:
            cur.execute(_UPSERT_SQL,
                       _page_params(r, fts_config=fts_config, embeddings=embeddings))


def delete_pages(conn: psycopg.Connection, paths: list[str]) -> int:
    """Remove rows by path — a deleted page's row is removed from the index. Returns how many
    rows actually existed to delete: a path already absent (never indexed, or already removed by an
    earlier delivery of the same event) deletes zero rows, not an error, which is what makes this
    safe to call on a REDELIVERED push. Pushes can arrive out of order and be redelivered, and
    neither may corrupt the index."""
    if not paths:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM pages_index WHERE path = ANY(%s)", (paths,))
        return cur.rowcount


def page_count(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index")
        return cur.fetchone()[0]
