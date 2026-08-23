"""Postgres storage for the derived index — the one module that owns SQL DDL and writes.

`pages_index` is never migrated: dropped and recreated per rebuild; `embedding_cache` and
`index_meta` survive, so a rebuild re-embeds only content whose hash changed.

This layer knows no identity: `acl` is stored here and enforced ABOVE by
`stigmergy.server.acl.visible()` — a named exception in `tests/test_architecture.py`.
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
  content_hash text NOT NULL,
  tsv tsvector,
  -- halfvec, not vector — and the reason is a hard ceiling, not a preference.
  -- pgvector refuses to build an HNSW index on a column with more than 2000 dimensions, and the
  -- default embedder (`text-embedding-3-large`) is 3072. `halfvec` raises that ceiling to 4000
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

# The knowledge repo's `ops/` control files, cached where every process group can see them.
#
# Not an index of pages, and deliberately in this database anyway: these are repo-derived files the
# SERVER reads on the hot path, exactly like `pages_index`, and the deployed `app` and `slack`
# groups hold no checkout at all — they were served copies baked into the image at deploy time,
# which meant an entity minted after the rollout had no name, no type and no aliases until the next
# one (issue #74), and — the sharper half — an identity REVOKED after the rollout kept resolving
# until the next one (issue #79). One writer road they already had (`--rebuild`, from a checkout)
# plus the one the pages already ride (the push webhook) keeps them fresh for both groups at once.
#
# **Caching the identity roster is an INTEGRITY question, not a confidentiality one**, and the
# integrity argument is the webhook's: every ops file is fetched at the BRANCH REF, never at the
# delivery's pushed sha, so no replayed or delayed delivery can install a historical roster — the
# worst any forged-but-signed delivery can do is refresh the cache to what the branch already says.
# What the cache must never weaken is the readers' fail-closed posture: a MISSING snapshot (`None`)
# falls back to the reader's own file, a malformed one refuses exactly as a malformed file does,
# and an EMPTY one (`""`) is malformed — it resolves nobody, never everybody
# (`server.identity.audiences_from_text`).
#
# The TEXT verbatim, never a parsed shape: each file's own reader owns what its bytes mean, and a
# second interpretation of them here is exactly the drift a cache must not introduce. One table
# keyed by the repo-relative path rather than one table per file, so a third file cannot be given
# its own subtly different road.
#
# POSIX paths, because a webhook's changed-path list is POSIX — `server.webhook` matches these
# strings against it verbatim. The reader-side `default_path` helpers re-split them for the local
# filesystem.
ENTITY_REGISTRY_RELPATH = "ops/entity-registry.json"
IDENTITIES_RELPATH = "ops/identities.json"
SLACK_CHANNELS_RELPATH = "ops/slack-channels.json"
# Every file this cache carries, in the order a rebuild reconciles them.
OPS_FILE_RELPATHS = (ENTITY_REGISTRY_RELPATH, IDENTITIES_RELPATH, SLACK_CHANNELS_RELPATH)

# What a rebuild does when its checkout does not carry the file — the one per-file policy in this
# cache, because "no file" means different things for different files. The registry: a repo before
# its first mint genuinely has none, so the snapshot is CLEARED and readers fall back to their own
# file. The two access-scoping files: absence is an anomaly, never an instruction — clearing would
# hand every deployed process back to the roster and scope map baked at the last deploy, silently
# undoing every edit pushed since, by way of a cron. The snapshot stands and the rebuild says so;
# a deployment that genuinely wants "nobody" or "no scoping" pushes an explicit `{}` — a committed,
# reviewable statement, the same line the view sweep's registry refusal draws.
CLEARED_WHEN_CHECKOUT_LACKS = {
    ENTITY_REGISTRY_RELPATH: True,
    IDENTITIES_RELPATH: False,
    SLACK_CHANNELS_RELPATH: False,
}

_OPS_FILE_DDL = """
CREATE TABLE IF NOT EXISTS ops_file_snapshot (
  relpath text PRIMARY KEY,               -- one of OPS_FILE_RELPATHS, the repo-relative POSIX path
  content text NOT NULL,
  source text NOT NULL DEFAULT '',        -- the pushed sha, or 'rebuild' — operator diagnosis only
  refreshed_at timestamptz NOT NULL DEFAULT now()
)
"""

# The bound on what may be installed into one of those rows, applied by BOTH writers (the push
# webhook and `build.rebuild`) so the rebuild road cannot install what the push road refuses.
#
# It is a size, not a count, and it is a bound on the SERVING cost rather than on the ingest: this
# text is read out of Postgres and parsed on every tool call, for every identity, on one small
# single-process machine — so its size is a per-request cost, never the one-off cost of the push
# that wrote it. (`webhook.MAX_BODY_BYTES` bounds the delivery; `webhook_settings.file_cap` bounds
# a COUNT of in-zone files, which an ops-file-only push does not even reach.) Sized to files orders
# of magnitude larger than any this system has served — a registry record or an identity entry is a
# couple of hundred bytes — not to what a text column could hold.
MAX_OPS_FILE_BYTES = 512 * 1024

_META_DDL = """
CREATE TABLE IF NOT EXISTS index_meta (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  model text NOT NULL,
  dim integer NOT NULL,
  fts_config text NOT NULL,
  built_at timestamptz NOT NULL DEFAULT now(),  -- when this index was last rebuilt; the server
  --                                               returns it so staleness is self-diagnosing
  host text NOT NULL DEFAULT ''                 -- WHERE it embedded: the same model name on two
  --                                               hosts is not provably the same vector space
)
"""

# The searchable text: title, tags, entity (folded via array_to_string so any anchor matches),
# mentions, entity_meta and body. `tags`/`mentions`/`entity_meta` are tsv-only — never stored
# columns, only sources `to_tsvector` reads once here.
_TSV_SQL = ("to_tsvector(%(fts_config)s, %(title)s || ' ' || %(tags)s || ' ' || "
            "array_to_string(%(entity)s::text[], ' ') || ' ' || %(mentions)s || ' ' || "
            "%(entity_meta)s || ' ' || %(body)s)")


def dsn() -> str:
    return os.environ.get(DSN_ENV, DSN_DEFAULT)


def host_of_dsn(conninfo: str | None) -> str:
    """The DSN's host, credential-free — `""` when it names none (a unix socket, PG* defaults, or
    a string libpq cannot read). Through libpq's OWN parser: the keyword form
    (`host=h password=…`) is a DSN too, and string surgery on it returns the whole connstring,
    password included. Nobody outside this module needs to parse a DSN at all."""
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


def init_schema(conn: psycopg.Connection, dim: int, model: str, fts_config: str,
                host: str = "") -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # TARGETED BY NAME, and it must stay that way: this database also holds the DURABLE half
        # of the system (`capture_queue`, `audit_log`, `job_runs`, `ingest_errors`) — material
        # that exists nowhere else until the librarian files it. A "drop everything" shortcut
        # would take the queue with the cache.
        cur.execute("DROP TABLE IF EXISTS pages_index")
        cur.execute(_PAGES_DDL.format(dim=dim))
        # Backlinks are a containment lookup, never a scan. Plain `CREATE INDEX` (no
        # `IF NOT EXISTS`) is correct here and nowhere else in this codebase's DDL: the table was
        # just dropped, so the index cannot already exist — every other `CREATE INDEX` in the repo
        # guards a SURVIVING table.
        cur.execute("CREATE INDEX pages_index_links_gin ON pages_index USING GIN (links)")

        cur.execute(_CACHE_DDL)
        cur.execute(_OPS_FILE_DDL)
        # The upgrade path for the SURVIVING side tables is the rebuild, and this is it retiring
        # issue #74's single-purpose predecessor. Its row is not carried forward: this same
        # transaction reconciles every ops file from the checkout a few statements later
        # (`build.rebuild`), so a copy of the old row would be overwritten before anything read it.
        cur.execute("DROP TABLE IF EXISTS entity_registry_snapshot")
        cur.execute(_META_DDL)
        # index_meta is a SURVIVING table, so an older database may lack `built_at` or `host`:
        # add the columns additively rather than force a wipe.
        cur.execute("ALTER TABLE index_meta ADD COLUMN IF NOT EXISTS built_at timestamptz"
                    " NOT NULL DEFAULT now()")
        cur.execute("ALTER TABLE index_meta ADD COLUMN IF NOT EXISTS host text"
                    " NOT NULL DEFAULT ''")
        cur.execute("INSERT INTO index_meta (model, dim, fts_config, built_at, host)"
                    " VALUES (%s, %s, %s, now(), %s)"
                    " ON CONFLICT (singleton) DO UPDATE SET model = EXCLUDED.model,"
                    " dim = EXCLUDED.dim, fts_config = EXCLUDED.fts_config,"
                    " built_at = EXCLUDED.built_at, host = EXCLUDED.host",
                    (model, dim, fts_config, host))


def create_search_indexes(conn) -> None:
    """The two retrieval indexes: `pages_index_tsv_gin` serves `tsv @@ tsq`,
    `pages_index_embedding_hnsw` serves `embedding <=>` with `halfvec_cosine_ops` — which must
    match BOTH the column type and the operator `search._VEC_SQL` uses, or Postgres silently
    seq-scans and the index is decoration (see `_PAGES_DDL` for why the column is `halfvec`).

    Built AFTER the bulk load, not in `init_schema`: an HNSW index maintains a navigable graph
    per inserted row, so building it first makes every row pay graph maintenance and building it
    last is one bulk construction. No `IF NOT EXISTS` — the table was just dropped."""
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
            cur.execute("SELECT model, dim, fts_config, built_at, host FROM index_meta")
        except psycopg.errors.UndefinedColumn:
            # an older index_meta predating built_at/host reads as "needs a rebuild" rather than
            # a raw crash — the caller's empty-index path surfaces the `--rebuild` hint.
            return None
        row = cur.fetchone()
    if not row:
        return None
    # built_at ships as an ISO-8601 string: the server serializes it into JSON tool output.
    return {"model": row[0], "dim": row[1], "fts_config": row[2],
            "built_at": row[3].isoformat() if row[3] is not None else None,
            "host": row[4] or ""}


def read_ops_file(conn: psycopg.Connection, relpath: str) -> str | None:
    """One cached ops file's TEXT, or `None` when this database has no snapshot of it.

    `None` is a real answer with a caller-visible meaning — "no snapshot here, use your file" —
    and it is why the table absence is probed rather than caught: a database whose index predates
    this table must behave exactly like one whose snapshot has not been written yet, and both must
    behave exactly like the server did before any of this existed."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('ops_file_snapshot')")
        if cur.fetchone()[0] is None:
            return None
        cur.execute("SELECT content FROM ops_file_snapshot WHERE relpath = %s", (relpath,))
        row = cur.fetchone()
    return row[0] if row else None


def read_ops_file_meta(conn: psycopg.Connection, relpath: str) -> dict | None:
    """`{"source", "refreshed_at"}` for one cached ops file, or `None` when there is no snapshot.

    The columns beside the content, read for the operator asking "is what I am serving fresh, and
    from which sha?" — the question issue #74 was found through, and one no other surface can
    answer: what the deployed server serves is a database row nobody holds a checkout of.
    `refreshed_at` ships as an ISO-8601 string, `read_meta`'s own convention, because the console
    serializes it into JSON."""
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
    """Create the ops-file cache if it is not there yet — called once per process at the same
    startup seam `ensure_audit_table` and `ensure_repair_schema` are called from, and by
    `init_schema` on the rebuild road.

    Why startup and not only on the write path: `CREATE TABLE IF NOT EXISTS` is NOT race-free in
    Postgres (two concurrent creations raise a unique violation on `pg_type`), and inside the
    webhook's phase-2 transaction that violation would roll the PAGES back with it — for a push
    GitHub does not redeliver. Created single-threaded at startup, the write path's own
    `IF NOT EXISTS` becomes a no-op that has nothing left to race with.

    CREATE and nothing else — no data migration, ever. Two process groups run this concurrently
    on a rolling deploy, so anything beyond an idempotent create would need the startup DDL lock;
    a create needs nothing. Issue #74's single-purpose `entity_registry_snapshot` is retired by
    `init_schema` on the REBUILD road instead (single-process, the store's own upgrade path); the
    window that leaves — an upgraded process answering from its baked file until the next push or
    nightly rebuild — is bounded by the deploy itself, which bakes those files fresh."""
    with conn.cursor() as cur:
        cur.execute(_OPS_FILE_DDL)


def write_ops_file(conn: psycopg.Connection, relpath: str, content: str, source: str) -> None:
    """Replace one cached ops file with `content` verbatim.

    The `IF NOT EXISTS` create stays here even though `ensure_ops_file_table` runs at startup:
    without it an incremental refresh would depend on a rebuild (or a restart) having run since
    the upgrade, which is the property it was added for. With the table already created
    single-threaded, it is a no-op — see that function for the race this ordering avoids."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_OPS_FILE_DDL)
        cur.execute("INSERT INTO ops_file_snapshot (relpath, content, source, refreshed_at)"
                    " VALUES (%s, %s, %s, now())"
                    " ON CONFLICT (relpath) DO UPDATE SET content = EXCLUDED.content,"
                    " source = EXCLUDED.source, refreshed_at = EXCLUDED.refreshed_at",
                    (relpath, content, source))


def clear_ops_file(conn: psycopg.Connection, relpath: str) -> bool:
    """Drop one file back to "no snapshot" — the state a fresh database is in, and the state every
    reader falls back to its own baked file from. Returns whether a snapshot was actually removed,
    which is what lets a caller warn about DESTROYING a cached copy without warning about a file
    the repo has simply never had.

    Its production caller is `build.rebuild`'s nightly reconciler: a checkout that carries no copy
    of a file must be able to say so, or a snapshot would outlive every rebuild forever. The
    transaction block and the `to_regclass` probe are its writing sibling's, for the same two
    reasons — one atomic step, and a caught `UndefinedTable` on a SHARED connection would poison an
    open transaction around it."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT to_regclass('ops_file_snapshot')")
        if cur.fetchone()[0] is None:
            return False
        cur.execute("DELETE FROM ops_file_snapshot WHERE relpath = %s RETURNING relpath",
                    (relpath,))
        return cur.fetchone() is not None


# One row per webhook delivery already APPLIED — replay protection for the page road
# (issue #79 item 1). GitHub stamps every delivery with a unique `X-GitHub-Delivery` id, replays
# included, so a captured-and-replayed delivery presents an id this table already holds and is
# acknowledged without being re-applied. The ops files never needed this (they are fetched at the
# branch ref, so a replay re-installs current content); the PAGES did: they are fetched at the
# delivery's own sha, so a replay re-installed old page bytes — and re-performed old DELETIONS —
# until the nightly rebuild. Manual redelivery of a FAILED delivery still works, because the id is
# recorded inside phase 2's transaction: a delivery that failed never recorded itself.
_WEBHOOK_DELIVERIES_DDL = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  delivery_id text PRIMARY KEY,
  received_at timestamptz NOT NULL DEFAULT now()
)
"""

# How long a delivery id is remembered. A bound on the table, not a security parameter: a replay
# of a delivery older than this is already a no-op for the ops files (branch ref) and reinstalls
# page bytes the nightly rebuild corrects within a day — the window this table exists to close is
# the fresh one, where a replayed delete or downgrade could sit unnoticed between rebuilds.
WEBHOOK_DELIVERY_RETENTION_DAYS = 30


def ensure_webhook_dedupe_table(conn: psycopg.Connection) -> None:
    """Create the delivery-id table if it is not there yet — the same startup seam and the same
    create-only rule as `ensure_ops_file_table` above, for the same rolling-deploy race."""
    with conn.cursor() as cur:
        cur.execute(_WEBHOOK_DELIVERIES_DDL)


def delivery_already_applied(conn: psycopg.Connection, delivery_id: str) -> bool:
    """Has this delivery id already been recorded by a SUCCESSFUL apply? `False` for an empty id
    (an origin that stamps no id gets no dedupe, never a collision on `""`) and for a database
    whose index predates the table."""
    if not delivery_id:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('webhook_deliveries')")
        if cur.fetchone()[0] is None:
            return False
        cur.execute("SELECT 1 FROM webhook_deliveries WHERE delivery_id = %s", (delivery_id,))
        return cur.fetchone() is not None


def record_delivery(cur, delivery_id: str) -> None:
    """Record one applied delivery id — called with the caller's own CURSOR, inside the webhook's
    phase-2 transaction, so the id lands atomically with the writes it de-duplicates: a delivery
    whose apply rolled back never recorded itself, and GitHub's manual redelivery still works.
    `ON CONFLICT DO NOTHING` because two identical concurrent deliveries may both pass the
    read-side check; both apply idempotent writes and one records. Pruning rides along — one
    bounded DELETE per recorded delivery keeps the table a window, not a ledger."""
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


# The one column list both writers AND `search.fetch_pages` share, and the one source
# `_INSERT_SQL` and `_UPSERT_SET` are built from — exactly one column list, one params builder
# (`_page_params`) and one INSERT template, so a `pages_index` column added anywhere else would
# diverge silently.
PAGE_COLUMNS = ("path", "page_id", "zone", "title", "body", "type", "status", "entity",
                "owner", "tier", "as_of", "updated",
                "superseded_by", "supersedes", "acl", "inlinks", "links", "content_hash")

_INSERT_SQL = (
    "INSERT INTO pages_index (" + ", ".join(PAGE_COLUMNS) + ", tsv, embedding)"
    " VALUES (" + ", ".join(f"%({c})s" for c in PAGE_COLUMNS) + f", {_TSV_SQL}, %(embedding)s::halfvec)"
)


def _page_params(r, *, fts_config: str, embeddings: dict[str, list[float]]) -> dict:
    """One `corpus.PageRow` -> the params dict `_INSERT_SQL` (and its `ON CONFLICT` extension)
    binds against. `tags`/`mentions`/`entity_meta` feed only `_TSV_SQL`, not `PAGE_COLUMNS` —
    they compute the `tsv` column rather than being stored as columns of their own."""
    return {
        "path": r.path, "page_id": r.page_id, "zone": r.zone, "title": r.title,
        "body": r.body, "type": r.type, "status": r.status, "entity": r.entity,
        "owner": r.owner, "tier": r.tier, "as_of": r.as_of,
        "updated": r.updated, "superseded_by": r.superseded_by,
        "supersedes": r.supersedes, "acl": r.acl, "inlinks": r.inlinks, "links": r.links,
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


# `inlinks` is excluded like `path` (the conflict key): the webhook's incoming row always carries
# 0 — a single changed file cannot resolve the whole-corpus INBOUND graph — and letting
# `EXCLUDED.inlinks` clobber the last full rebuild's count demotes every incrementally-edited
# page until the next nightly rebuild. A fresh INSERT still gets 0 via VALUES, the honest answer
# for a page nothing has resolved the graph for yet. `links` is the OPPOSITE case and stays IN
# the SET list: a file CAN compute its own outbound targets from its text plus existing paths, so
# the incoming value is the freshest fact available — excluding it would freeze a webhook-edited
# page's `links` at whatever the last full rebuild saw.
_UPSERT_SET = ", ".join(f"{col} = EXCLUDED.{col}"
                        for col in PAGE_COLUMNS if col not in ("path", "inlinks"))


def current_content_hashes(conn: psycopg.Connection, paths: list[str]) -> dict[str, str]:
    """`path -> content_hash` for whichever of `paths` are already indexed — the webhook's
    idempotency check, so the same content pushed twice embeds once. An absent path is simply not
    indexed yet."""
    if not paths:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT path, content_hash FROM pages_index WHERE path = ANY(%s)", (paths,))
        return dict(cur.fetchall())


def existing_paths(conn: psycopg.Connection) -> list[str]:
    """Every path currently in `pages_index`, one query — the webhook builds its
    `corpus.by_stem_index` from this, so its link resolution shares `corpus.resolve_links` with
    the full rebuild's in-memory one (parity-tested) instead of growing a second algorithm."""
    with conn.cursor() as cur:
        cur.execute("SELECT path FROM pages_index")
        return [row[0] for row in cur.fetchall()]


def pages_with_page_id_prefix(conn: psycopg.Connection, prefix: str) -> list[tuple[str, str]]:
    """`(path, page_id)` for every row whose `page_id` starts with `prefix` — a cheap LIKE filter
    narrowing candidates before `corpus.chain_part_pattern` decides which are real (the webhook's
    incremental `superseded_by` propagation). Escaped for `%`/`_`/`\\` so a `page_id` containing
    them is never read as a wildcard."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with conn.cursor() as cur:
        cur.execute("SELECT path, page_id FROM pages_index WHERE page_id LIKE %s",
                   (f"{escaped}%",))
        return cur.fetchall()


def set_superseded_by(conn: psycopg.Connection, paths: list[str], value: str) -> None:
    """Targeted `superseded_by` UPDATE for exactly `paths` — once a push upserts a chain's
    PRIMARY, its already-indexed siblings must not wait for the nightly rebuild. Stamped or
    cleared symmetrically: `value` is used verbatim, empty string included."""
    if not paths:
        return
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE pages_index SET superseded_by = %s WHERE path = ANY(%s)",
                   (value, paths))


_UPSERT_SQL = (_INSERT_SQL + f" ON CONFLICT (path) DO UPDATE SET {_UPSERT_SET},"
              f" tsv = {_TSV_SQL}, embedding = %(embedding)s::halfvec")


def upsert_pages(conn: psycopg.Connection, rows: list, embeddings: dict[str, list[float]],
                fts_config: str) -> None:
    """Insert-or-update keyed on `path` — the incremental path. `_UPSERT_SQL` extends
    `_INSERT_SQL` with one `ON CONFLICT` clause, so an upserted row and a rebuilt one can never
    disagree about columns or search-text derivation. `rows`/`embeddings` are exactly
    `insert_pages`'s shapes; cached embeddings pass through unchanged, so a same-content upsert
    never re-embeds."""
    with conn.transaction(), conn.cursor() as cur:
        for r in rows:
            cur.execute(_UPSERT_SQL,
                       _page_params(r, fts_config=fts_config, embeddings=embeddings))


def delete_pages(conn: psycopg.Connection, paths: list[str]) -> int:
    """Remove rows by path; returns how many existed to delete. An already-absent path deletes
    zero rows, not an error — pushes arrive out of order and get redelivered, and neither may
    corrupt the index."""
    if not paths:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM pages_index WHERE path = ANY(%s)", (paths,))
        return cur.rowcount


def page_count(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index")
        return cur.fetchone()[0]
