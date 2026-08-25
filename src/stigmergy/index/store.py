"""Postgres storage for the derived search index and repository control snapshots."""
import json
import os
import time

import psycopg
from psycopg.conninfo import conninfo_to_dict

DSN_ENV = "STIGMERGY_INDEX_DSN"
DSN_DEFAULT = "postgresql://stigmergy:stigmergy@localhost:54321/stigmergy"
_CONNECT_RETRY_DELAYS = (0.1, 0.5)

_PAGES_DDL = """
CREATE TABLE pages_index (
  path text PRIMARY KEY,
  page_id text NOT NULL,
  zone text NOT NULL,
  title text NOT NULL DEFAULT '',
  body text NOT NULL DEFAULT '',
  type text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT '',
  entity text[] NOT NULL DEFAULT '{{}}',
  updated text NOT NULL DEFAULT '',
  acl text[],
  inlinks integer NOT NULL DEFAULT 0,
  links text[] NOT NULL DEFAULT '{{}}',
  sources text[] NOT NULL DEFAULT '{{}}',
  content_hash text NOT NULL,
  tsv tsvector,
  -- halfvec supports the 2560-dimensional default model within pgvector's HNSW limits.
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

# Repository control files are cached verbatim so process groups without a checkout stay current.
ENTITY_REGISTRY_RELPATH = "ops/entity-registry.json"
IDENTITIES_RELPATH = "ops/identities.json"
SLACK_CHANNELS_RELPATH = "ops/slack-channels.json"
OPS_FILE_RELPATHS = (ENTITY_REGISTRY_RELPATH, IDENTITIES_RELPATH, SLACK_CHANNELS_RELPATH)

_OPS_FILE_DDL = """
CREATE TABLE IF NOT EXISTS ops_file_snapshot (
  relpath text PRIMARY KEY,               -- one of OPS_FILE_RELPATHS, the repo-relative POSIX path
  content text NOT NULL,
  source text NOT NULL DEFAULT '',        -- the pushed sha, or 'rebuild' — operator diagnosis only
  refreshed_at timestamptz NOT NULL DEFAULT now()
)
"""

# Parsed on every request, so each snapshot has a serving-cost bound.
MAX_OPS_FILE_BYTES = 512 * 1024

_META_DDL = """
CREATE TABLE IF NOT EXISTS index_meta (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  model text NOT NULL,
  dim integer NOT NULL,
  fts_config text NOT NULL,
  built_at timestamptz NOT NULL DEFAULT now(),
  host text NOT NULL DEFAULT ''
)
"""

_TSV_SQL = (
    "to_tsvector(%(fts_config)s, %(title)s || ' ' || "
    "array_to_string(%(entity)s::text[], ' ') || ' ' || %(body)s)"
)


def dsn() -> str:
    return os.environ.get(DSN_ENV, DSN_DEFAULT)


def host_of_dsn(conninfo: str | None) -> str:
    """Return the first DSN host without exposing credentials."""
    try:
        host = str(conninfo_to_dict(conninfo or "").get("host") or "")
    except psycopg.Error:
        return ""
    return host.split(",", 1)[0].strip().lower()


def connect(conninfo: str | None = None) -> psycopg.Connection:
    """Use autocommit so readers do not block a concurrent full rebuild."""
    target = conninfo or dsn()
    attempt = 0
    while True:
        try:
            return psycopg.connect(target, autocommit=True)
        except psycopg.OperationalError:
            if attempt == len(_CONNECT_RETRY_DELAYS):
                raise
            time.sleep(_CONNECT_RETRY_DELAYS[attempt])
            attempt += 1


# Serving connections answer inside an event loop, so one stuck statement must fail rather than
# hold the process; workers and rebuilds keep the database default and run to completion.
SERVING_STATEMENT_TIMEOUT_MS = 30_000


def bound_statements(conn: psycopg.Connection,
                     timeout_ms: int = SERVING_STATEMENT_TIMEOUT_MS) -> psycopg.Connection:
    """Bound every statement on one connection. `connect` is autocommit, so `is_local=false`
    applies for the whole session rather than one transaction."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('statement_timeout', %s, false)", (f"{timeout_ms}ms",))
    return conn


def connect_serving(conninfo: str | None = None) -> psycopg.Connection:
    """The connection every request-scoped reader opens."""
    return bound_statements(connect(conninfo))


def init_schema(conn: psycopg.Connection, dim: int, model: str, fts_config: str,
                host: str = "") -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # A rebuild replaces only the derived page index; operational tables are durable.
        cur.execute("DROP TABLE IF EXISTS pages_index")
        cur.execute(_PAGES_DDL.format(dim=dim))
        cur.execute("CREATE INDEX pages_index_links_gin ON pages_index USING GIN (links)")

        cur.execute(_CACHE_DDL)
        cur.execute(_OPS_FILE_DDL)
        cur.execute(_META_DDL)
        cur.execute("INSERT INTO index_meta (model, dim, fts_config, built_at, host)"
                    " VALUES (%s, %s, %s, now(), %s)"
                    " ON CONFLICT (singleton) DO UPDATE SET model = EXCLUDED.model,"
                    " dim = EXCLUDED.dim, fts_config = EXCLUDED.fts_config,"
                    " built_at = EXCLUDED.built_at, host = EXCLUDED.host",
                    (model, dim, fts_config, host))


def create_search_indexes(conn) -> None:
    """Build lexical and vector indexes after the bulk page load."""
    with conn.cursor() as cur:
        cur.execute("CREATE INDEX pages_index_tsv_gin ON pages_index USING GIN (tsv)")
        cur.execute("CREATE INDEX pages_index_embedding_hnsw ON pages_index "
                    "USING hnsw (embedding halfvec_cosine_ops)")


def read_meta(conn: psycopg.Connection) -> dict | None:
    """Return index metadata, or ``None`` before the first rebuild."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('index_meta')")
        if cur.fetchone()[0] is None:
            return None
        cur.execute("SELECT model, dim, fts_config, built_at, host FROM index_meta")
        row = cur.fetchone()
    if not row:
        return None
    return {"model": row[0], "dim": row[1], "fts_config": row[2],
            "built_at": row[3].isoformat() if row[3] is not None else None,
            "host": row[4] or ""}


def read_ops_file(conn: psycopg.Connection, relpath: str) -> str | None:
    """Return a cached control file or ``None`` when no snapshot exists."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('ops_file_snapshot')")
        if cur.fetchone()[0] is None:
            return None
        cur.execute("SELECT content FROM ops_file_snapshot WHERE relpath = %s", (relpath,))
        row = cur.fetchone()
    return row[0] if row else None


def read_ops_file_meta(conn: psycopg.Connection, relpath: str) -> dict | None:
    """Return source and refresh time for a cached control file."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('ops_file_snapshot')")
        if cur.fetchone()[0] is None:
            return None
        cur.execute("SELECT source, refreshed_at FROM ops_file_snapshot WHERE relpath = %s",
                    (relpath,))
        row = cur.fetchone()
    if not row:
        return None
    return {"source": row[0], "refreshed_at": row[1].isoformat() if row[1] is not None else None}


def ensure_ops_file_table(conn: psycopg.Connection) -> None:
    """Create the repository control snapshot table."""
    with conn.cursor() as cur:
        cur.execute(_OPS_FILE_DDL)


def write_ops_file(conn: psycopg.Connection, relpath: str, content: str, source: str) -> None:
    """Replace one cached control file with its verbatim repository bytes."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_OPS_FILE_DDL)
        cur.execute("INSERT INTO ops_file_snapshot (relpath, content, source, refreshed_at)"
                    " VALUES (%s, %s, %s, now())"
                    " ON CONFLICT (relpath) DO UPDATE SET content = EXCLUDED.content,"
                    " source = EXCLUDED.source, refreshed_at = EXCLUDED.refreshed_at",
                    (relpath, content, source))


def clear_ops_file(conn: psycopg.Connection, relpath: str) -> bool:
    """Remove one snapshot and report whether it existed."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT to_regclass('ops_file_snapshot')")
        if cur.fetchone()[0] is None:
            return False
        cur.execute("DELETE FROM ops_file_snapshot WHERE relpath = %s RETURNING relpath",
                    (relpath,))
        return cur.fetchone() is not None


# Successful webhook delivery IDs are recorded atomically with their index changes.
_WEBHOOK_DELIVERIES_DDL = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  delivery_id text PRIMARY KEY,
  received_at timestamptz NOT NULL DEFAULT now()
)
"""

WEBHOOK_DELIVERY_RETENTION_DAYS = 30


def ensure_webhook_dedupe_table(conn: psycopg.Connection) -> None:
    """Create the webhook replay-protection table."""
    with conn.cursor() as cur:
        cur.execute(_WEBHOOK_DELIVERIES_DDL)


def delivery_already_applied(conn: psycopg.Connection, delivery_id: str) -> bool:
    """Return whether a non-empty delivery ID was applied successfully."""
    if not delivery_id:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('webhook_deliveries')")
        if cur.fetchone()[0] is None:
            return False
        cur.execute("SELECT 1 FROM webhook_deliveries WHERE delivery_id = %s", (delivery_id,))
        return cur.fetchone() is not None


def record_delivery(cur, delivery_id: str) -> None:
    """Record and prune delivery IDs inside the caller's apply transaction."""
    if not delivery_id:
        return
    cur.execute(_WEBHOOK_DELIVERIES_DDL)
    cur.execute("INSERT INTO webhook_deliveries (delivery_id) VALUES (%s)"
                " ON CONFLICT (delivery_id) DO NOTHING", (delivery_id,))
    cur.execute("DELETE FROM webhook_deliveries WHERE received_at < now() - make_interval("
                "days => %s)", (WEBHOOK_DELIVERY_RETENTION_DAYS,))


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


# Shared by rebuild, incremental upsert, and page fetch shaping.
PAGE_COLUMNS = (
    "path",
    "page_id",
    "zone",
    "title",
    "body",
    "type",
    "status",
    "entity",
    "updated",
    "acl",
    "inlinks",
    "links",
    "sources",
    "content_hash",
)

_INSERT_SQL = (
    "INSERT INTO pages_index (" + ", ".join(PAGE_COLUMNS) + ", tsv, embedding)"
    " VALUES (" + ", ".join(f"%({c})s" for c in PAGE_COLUMNS) + f", {_TSV_SQL}, %(embedding)s::halfvec)"
)


def _page_params(r, *, fts_config: str, embeddings: dict[str, list[float]]) -> dict:
    return {
        "path": r.path, "page_id": r.page_id, "zone": r.zone, "title": r.title,
        "body": r.body, "type": r.type, "status": r.status, "entity": r.entity,
        "updated": r.updated, "acl": r.acl, "inlinks": r.inlinks, "links": r.links,
        "sources": r.sources,
        "content_hash": r.content_hash,
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


# Incremental updates cannot recompute whole-corpus inbound-link counts.
_UPSERT_SET = ", ".join(f"{col} = EXCLUDED.{col}"
                        for col in PAGE_COLUMNS if col not in ("path", "inlinks"))


def current_content_hashes(conn: psycopg.Connection, paths: list[str]) -> dict[str, str]:
    """Return current hashes for the requested indexed paths."""
    if not paths:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT path, content_hash FROM pages_index WHERE path = ANY(%s)", (paths,))
        return dict(cur.fetchall())


def existing_paths(conn: psycopg.Connection) -> list[str]:
    """Return every indexed path for incremental link resolution."""
    with conn.cursor() as cur:
        cur.execute("SELECT path FROM pages_index")
        return [row[0] for row in cur.fetchall()]


_UPSERT_SQL = (_INSERT_SQL + f" ON CONFLICT (path) DO UPDATE SET {_UPSERT_SET},"
              f" tsv = {_TSV_SQL}, embedding = %(embedding)s::halfvec")


def upsert_pages(conn: psycopg.Connection, rows: list, embeddings: dict[str, list[float]],
                fts_config: str) -> None:
    """Insert or update pages using the same row shape as a full rebuild."""
    with conn.transaction(), conn.cursor() as cur:
        for r in rows:
            cur.execute(_UPSERT_SQL,
                       _page_params(r, fts_config=fts_config, embeddings=embeddings))


def delete_pages(conn: psycopg.Connection, paths: list[str]) -> int:
    """Delete indexed paths and return the number removed."""
    if not paths:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM pages_index WHERE path = ANY(%s)", (paths,))
        return cur.rowcount


def page_count(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index")
        return cur.fetchone()[0]
