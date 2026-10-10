"""Postgres storage for the derived search index and repository control snapshots."""
import hashlib
import json
import os
import time

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool

from stigmergy.index.errors import StigmergyIndexError
from stigmergy.kernel import layers, tenancy

DSN_ENV = "STIGMERGY_INDEX_DSN"
DSN_DEFAULT = "postgresql://stigmergy_app:stigmergy_app@localhost:54321/stigmergy"
_CONNECT_RETRY_DELAYS = (0.1, 0.5)

_PAGES_DDL = f"""
CREATE TABLE IF NOT EXISTS pages_index (
  {tenancy.TENANT_COLUMN},
  path text NOT NULL,
  vpath text NOT NULL,
  page_id text NOT NULL,
  zone text NOT NULL,
  title text NOT NULL DEFAULT '',
  body text NOT NULL DEFAULT '',
  type text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT '',
  entity_type text NOT NULL DEFAULT '',
  aliases text[] NOT NULL DEFAULT '{{}}',
  updated text NOT NULL DEFAULT '',
  acl text[],
  inlinks integer NOT NULL DEFAULT 0,
  links text[] NOT NULL DEFAULT '{{}}',
  sources text[] NOT NULL DEFAULT '{{}}',
  source_date text NOT NULL DEFAULT '',
  source_dates text[] NOT NULL DEFAULT '{{}}',
  content_hash text NOT NULL,
  tsv tsvector,
  PRIMARY KEY (tenant_id, path)
)
"""

# A fragment is an exact slice of one stored page and lives and dies with its page row; it carries
# that page's audience so the vector arm filters it before any limit. halfvec keeps the
# 2560-dimensional model within HNSW limits.
_FRAGMENTS_DDL = f"""
CREATE TABLE IF NOT EXISTS page_fragments (
  {tenancy.TENANT_COLUMN},
  path text NOT NULL,
  vpath text NOT NULL,
  ordinal integer NOT NULL,
  acl text[],
  trail text NOT NULL DEFAULT '',
  text text NOT NULL,
  embedding halfvec({{dim}}) NOT NULL,
  PRIMARY KEY (tenant_id, path, ordinal),
  FOREIGN KEY (tenant_id, path) REFERENCES pages_index (tenant_id, path) ON DELETE CASCADE
)
"""

_CACHE_DDL = f"""
CREATE TABLE IF NOT EXISTS embedding_cache (
  {tenancy.TENANT_COLUMN},
  model text NOT NULL,
  content_hash text NOT NULL,
  embedding jsonb NOT NULL,
  PRIMARY KEY (tenant_id, model, content_hash)
)
"""

INDEX_TABLES = ("pages_index", "page_fragments", "embedding_cache")

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
    "array_to_string(%(aliases)s::text[], ' ') || ' ' || %(body)s)"
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


_ISOLATING: set[str] = set()


def _require_isolating(conn: psycopg.Connection, conninfo: str) -> psycopg.Connection:
    """Check once per process and DSN that its role cannot bypass row-level security."""
    if conninfo not in _ISOLATING:
        try:
            tenancy.assert_isolating(conn)
        except tenancy.TenancyError:
            conn.close()
            raise
        _ISOLATING.add(conninfo)
    return conn


def connect(conninfo: str | None = None) -> psycopg.Connection:
    """Use autocommit so readers do not block a concurrent full rebuild."""
    target = conninfo or dsn()
    attempt = 0
    while True:
        try:
            return _require_isolating(psycopg.connect(target, autocommit=True), target)
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


# Kept connections outlive requests, so a silently dropped pooler socket must fail fast.
_SERVING_LIVENESS = {
    "connect_timeout": 5,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}


def serving_pool(conninfo: str | None = None, *, max_size: int) -> ConnectionPool:
    """Bounded serving connections. `getconn()` hands one request an exclusive checked
    connection and its `close()` returns it, so request code keeps the open/close contract."""
    target = conninfo or dsn()

    def configure(conn: psycopg.Connection) -> None:
        bound_statements(_require_isolating(conn, target))

    return ConnectionPool(
        target,
        min_size=1,
        max_size=max_size,
        open=True,
        close_returns=True,
        kwargs={"autocommit": True, **_SERVING_LIVENESS},
        configure=configure,
        # A request binds its connection to one tenant; it goes back to the pool bound to none.
        reset=tenancy.clear,
        check=ConnectionPool.check_connection,
        max_idle=120,
        timeout=10,
        name="serving",
    )


def _fragment_dim(cur) -> int | None:
    cur.execute("SELECT atttypmod FROM pg_attribute WHERE attrelid = to_regclass('page_fragments')"
                " AND attname = 'embedding' AND NOT attisdropped")
    row = cur.fetchone()
    return int(row[0]) if row else None


def init_schema(conn: psycopg.Connection, dim: int, model: str, fts_config: str,
                host: str = "") -> None:
    """Ensure the index tables every tenant shares exist for embeddings of `dim`. A dimension change
    drops them only while no other tenant holds a page; the index is derived and rebuildable."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        tenancy.ensure_tenants(cur)
        existing = _fragment_dim(cur)
        if existing is not None and existing != dim:
            with tenancy.platform_scope(conn):
                cur.execute("SELECT count(DISTINCT tenant_id) FROM pages_index")
                holders = cur.fetchone()[0]
                cur.execute("SELECT EXISTS (SELECT 1 FROM pages_index WHERE tenant_id IS DISTINCT FROM"
                            " nullif(current_setting('app.tenant_id', true), '')::uuid)")
                others = cur.fetchone()[0]
            if others:
                raise RuntimeError(f"the index holds {holders} tenants' {existing}-dimension "
                                   f"embeddings; a {dim}-dimension model needs a platform reset")
            cur.execute("DROP TABLE IF EXISTS page_fragments")
            cur.execute("DROP TABLE IF EXISTS pages_index")
        cur.execute(_PAGES_DDL)
        cur.execute(_FRAGMENTS_DDL.format(dim=dim))
        for name, definition in (
                ("pages_index_links_gin", "ON pages_index USING GIN (links)"),
                ("pages_index_vpath", "ON pages_index (tenant_id, vpath)"),
                ("pages_index_tsv_gin", "ON pages_index USING GIN (tsv)"),
                ("page_fragments_vpath", "ON page_fragments (tenant_id, vpath)"),
                ("page_fragments_acl", "ON page_fragments (tenant_id, acl)")):
            tenancy.ensure_index(cur, name, definition)

        cur.execute(_CACHE_DDL)
        for table in INDEX_TABLES:
            tenancy.protect(cur, table)
        cur.execute(_META_DDL)
        cur.execute("INSERT INTO index_meta (model, dim, fts_config, built_at, host)"
                    " VALUES (%s, %s, %s, now(), %s)"
                    " ON CONFLICT (singleton) DO UPDATE SET model = EXCLUDED.model,"
                    " dim = EXCLUDED.dim, fts_config = EXCLUDED.fts_config,"
                    " built_at = EXCLUDED.built_at, host = EXCLUDED.host",
                    (model, dim, fts_config, host))


def clear_tenant_index(conn) -> None:
    """Remove the connection's tenant's pages and, through them, its fragments."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pages_index WHERE tenant_id = %s", (tenancy.require_tenant(conn),))


def create_search_indexes(conn) -> None:
    """One HNSW index per audience of the connection's tenant after the bulk page load, and the
    statistics the planner chooses between an audience's index and an exact scan by."""
    audiences = page_audiences(conn)
    drop_stale_audience_indexes(conn, audiences)
    ensure_audience_indexes(conn, audiences)
    with conn.cursor() as cur:
        cur.execute("ANALYZE pages_index, page_fragments")


def page_audiences(conn) -> list[str]:
    """Every audience that holds a page: `public` and each layer with a row."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT acl FROM pages_index")
        held = {layers.layer_of_acl(acl) for (acl,) in cur.fetchall()}
    return sorted(held | {layers.PUBLIC})


def audience_predicate(tenant: str, layer: str) -> str:
    """The rows of one tenant's audience, as its partial HNSW index and the vector statement both
    spell it: the planner uses that index only for a statement that repeats the predicate."""
    tenant = tenancy.checked_uuid(tenant)
    own = f"tenant_id = '{tenant}'::uuid"
    if layer == layers.PUBLIC:
        return f"{own} AND acl IS NULL"
    if not layers.is_layer_name(layer):
        raise ValueError(f"not a layer name: {layer!r}")
    return f"{own} AND acl = '{{" + layer + "}'::text[]"


def _tenant_index_key(tenant: str) -> str:
    return hashlib.sha256(tenancy.checked_uuid(tenant).encode("ascii")).hexdigest()[:12]


def audience_index(tenant: str, layer: str) -> str:
    return f"pfh_{_tenant_index_key(tenant)}_{layer.replace('-', '_')}"


def drop_stale_audience_indexes(conn, audiences) -> None:
    """Drop the connection's tenant's HNSW indexes of audiences that no longer hold a page."""
    tenant = tenancy.require_tenant(conn)
    wanted = {audience_index(tenant, layer) for layer in audiences}
    with conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
                    "AND tablename = 'page_fragments' AND indexname LIKE %s",
                    (f"pfh_{_tenant_index_key(tenant)}_%",))
        for (name,) in cur.fetchall():
            if name not in wanted:
                cur.execute(f'DROP INDEX IF EXISTS "{name}"')


def drop_tenant_indexes(conn, tenant: str) -> int:
    """Drop every HNSW index of `tenant` without blocking other tenants' reads: outside any
    transaction, concurrently."""
    with conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
                    "AND tablename = 'page_fragments' AND indexname LIKE %s",
                    (f"pfh_{_tenant_index_key(tenant)}_%",))
        names = [row[0] for row in cur.fetchall()]
        for name in names:
            cur.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
    return len(names)


def ensure_audience_indexes(conn, audiences) -> None:
    """One HNSW index per audience of the connection's tenant, over that audience's fragments
    alone; no index spans two audiences or two tenants, so no reader's neighbours depend on content
    it cannot see. The rebuild and each incremental update create the index of every audience they
    write."""
    tenant = tenancy.require_tenant(conn)
    with conn.cursor() as cur:
        for layer in sorted(audiences):
            tenancy.ensure_index(cur, audience_index(tenant, layer),
                                 f"ON page_fragments USING hnsw (embedding halfvec_cosine_ops)"
                                 f" WHERE {audience_predicate(tenant, layer)}")


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
        built_at = row[3]
        # The shared row moves with every tenant's rebuild; a tenant is told when its own index
        # was last built, never when another's was.
        if tenancy.current_tenant(conn) is not None:
            cur.execute("SELECT to_regclass('index_health') IS NOT NULL")
            built_at = None
            if cur.fetchone()[0]:
                cur.execute("SELECT last_full_rebuild_at FROM index_health WHERE singleton")
                own = cur.fetchone()
                built_at = own[0] if own else None
    return {"model": row[0], "dim": row[1], "fts_config": row[2],
            "built_at": built_at.isoformat() if built_at is not None else None,
            "host": row[4] or ""}


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


def cached_hashes(conn: psycopg.Connection, model: str, hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT content_hash FROM embedding_cache"
                    " WHERE model = %s AND content_hash = ANY(%s)", (model, hashes))
        return {row[0] for row in cur.fetchall()}


def cached_dimension(conn: psycopg.Connection, model: str, content_hash: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT jsonb_array_length(embedding) FROM embedding_cache"
                    " WHERE model = %s AND content_hash = %s", (model, content_hash))
        return cur.fetchone()[0]


def store_embeddings(conn: psycopg.Connection, model: str, by_hash: dict[str, list[float]]) -> None:
    with conn.transaction(), conn.cursor() as cur:
        for h, emb in by_hash.items():
            cur.execute("INSERT INTO embedding_cache (model, content_hash, embedding)"
                        " VALUES (%s, %s, %s) ON CONFLICT (tenant_id, model, content_hash) DO NOTHING",
                        (model, h, json.dumps(emb)))


# Shared by rebuild, incremental upsert, and page fetch shaping.
PAGE_COLUMNS = (
    "path",
    "vpath",
    "page_id",
    "zone",
    "title",
    "body",
    "type",
    "status",
    "entity_type",
    "aliases",
    "updated",
    "acl",
    "inlinks",
    "links",
    "sources",
    "source_date",
    "source_dates",
    "content_hash",
)

_INSERT_SQL = (
    "INSERT INTO pages_index (" + ", ".join(PAGE_COLUMNS) + ", tsv)"
    " VALUES (" + ", ".join(f"%({c})s" for c in PAGE_COLUMNS) + f", {_TSV_SQL})"
)

FRAGMENT_COLUMNS = ("path", "vpath", "ordinal", "acl", "trail", "text")
# A fragment's vector is copied from the embedding cache inside Postgres; the caller caches every
# vector first. The explicit tenant lets the lookup use the cache's primary key.
_FRAGMENT_INSERT_SQL = (
    "INSERT INTO page_fragments (" + ", ".join(FRAGMENT_COLUMNS) + ", embedding)"
    " SELECT " + ", ".join(f"%({c})s" for c in FRAGMENT_COLUMNS) + ", embedding::text::halfvec"
    " FROM embedding_cache WHERE tenant_id = %(tenant)s AND model = %(model)s"
    " AND content_hash = %(content_hash)s"
)


def _page_params(r, *, fts_config: str) -> dict:
    return {
        "path": r.path, "vpath": r.vpath, "page_id": r.page_id, "zone": r.zone, "title": r.title,
        "body": r.body, "type": r.type, "status": r.status, "entity_type": r.entity_type,
        "aliases": r.aliases, "updated": r.updated, "acl": r.acl, "inlinks": r.inlinks, "links": r.links,
        "sources": r.sources,
        "source_date": r.source_date,
        "source_dates": r.source_dates,
        "content_hash": r.content_hash,
        "fts_config": fts_config,
    }


def insert_pages(conn: psycopg.Connection, rows: list, fts_config: str) -> None:
    """`rows` are corpus.PageRow."""
    with conn.transaction(), conn.cursor() as cur:
        for r in rows:
            cur.execute(_INSERT_SQL, _page_params(r, fts_config=fts_config))


def replace_fragments(conn: psycopg.Connection, paths, fragments: list, model: str) -> None:
    """Replace the fragments of every repository path in `paths` with `fragments` (each a
    `fragments.FragmentRow`), each with its content's vector from `model`'s embedding cache."""
    tenant = tenancy.require_tenant(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM page_fragments WHERE path = ANY(%s)", (sorted(set(paths)),))
        for fragment in fragments:
            params = {column: getattr(fragment, column) for column in FRAGMENT_COLUMNS}
            cur.execute(_FRAGMENT_INSERT_SQL, {**params, "tenant": tenant, "model": model,
                                               "content_hash": fragment.content_hash})
            if cur.rowcount != 1:
                raise StigmergyIndexError(f"a fragment has no cached {model} vector")


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


def source_dates(conn: psycopg.Connection, vpaths, *, exclude=()) -> dict[tuple[str, str], str]:
    """`{(layer, source virtual path): its date}` for the indexed sources among `vpaths`, leaving
    out the repository paths in `exclude`."""
    vpaths = sorted(set(vpaths))
    if not vpaths:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT path, vpath, source_date FROM pages_index WHERE type = 'source'"
                    " AND source_date <> '' AND vpath = ANY(%s) AND NOT path = ANY(%s)",
                    (vpaths, sorted(set(exclude))))
        return {(layers.split_path(path)[0], vpath): day for path, vpath, day in cur.fetchall()}


def existing_paths(conn: psycopg.Connection) -> list[str]:
    """Return every indexed path for incremental link resolution."""
    with conn.cursor() as cur:
        cur.execute("SELECT path FROM pages_index")
        return [row[0] for row in cur.fetchall()]


_UPSERT_SQL = (_INSERT_SQL + f" ON CONFLICT (tenant_id, path) DO UPDATE SET {_UPSERT_SET}, tsv = {_TSV_SQL}")


def upsert_pages(conn: psycopg.Connection, rows: list, fts_config: str) -> None:
    """Insert or update pages using the same row shape as a full rebuild."""
    with conn.transaction(), conn.cursor() as cur:
        for r in rows:
            cur.execute(_UPSERT_SQL, _page_params(r, fts_config=fts_config))


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
