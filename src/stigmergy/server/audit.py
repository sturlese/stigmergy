"""`audit_log` — the accountability trail: every call attributable to a person. One row per tool
call (`search_brain`, `read_page`, `ask`, ...), written at the service layer
(`BrainService._call` / `.call_async`) so stdio and HTTP share one code path — no per-transport
duplication.

Same Postgres as the index: `ensure_audit_table` is a `CREATE TABLE IF NOT EXISTS` run once at
startup by both transports (`build_service` for stdio, `transport_http.build_http_app` for HTTP) —
no separate migration tool, mirroring how `index_meta`/`pages_index` are already owned by the
index builder rather than a migration framework.

Audit-write failure NEVER fails the serving call: `AuditWriter.write` catches its own errors, logs
loudly at ERROR, and swallows them — the read or answer the caller asked for still ships.

**`result` is a nullable JSONB per-tool outcome SUMMARY, never a transcript.** It rides the exact
same seam every row already goes through (`BrainService._call`/`.call_async`), via an optional
`summarize` callback each call site supplies (`answer.service.audit_summary` for `ask`; a small
inline lambda for `search_brain`). This is what makes `stigmergy-pilot-report` possible without a
schema nobody can read questions/answers out of: `{refused, suppressed, verdict, citations,
retried}` for `ask`, `{hits}` for `search_brain` — facts about the SHAPE of an outcome, never its
content. The column is one schema change away from being a transcript, so a test asserts
NEGATIVELY that nothing writes a question or an answer into it, ever.

**`args` is not content-free for every tool, and this is a NAMED, accepted exemption, not an
oversight.** Every other tool's `args` carries counts, hashes or the caller's OWN structural
choices (a status filter, a limit); `ask`'s `question` and `search_brain`'s `query` are the two
fields where the caller's free-text CONTENT itself is written verbatim (bounded by
`server.service.MAX_ARG_CHARS`, via `_truncate_for_audit` — that module is where the actual write
happens; this file only owns the column). The reason is operator diagnosability: without the
literal question/query text, "why did this answer/search come back empty" is unanswerable from the
database at all. Hashing `question`/`query` instead would close the gap at the cost of that
diagnosability; the trade is recorded here rather than silently taken.
"""
import logging

from psycopg.types.json import Jsonb

from stigmergy.capture.schema import startup_ddl_lock

log = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    identity TEXT NOT NULL,
    tool TEXT NOT NULL,
    args JSONB NOT NULL,
    duration_ms DOUBLE PRECISION NOT NULL,
    outcome TEXT NOT NULL,
    error_class TEXT NOT NULL DEFAULT ''
)
"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS audit_log_identity_ts_idx ON audit_log (identity, ts DESC)
"""
# Additive, nullable — NULL on any row written before this column existed, and on any call whose
# site supplies no `summarize` callback (or whose callback declined to run, e.g. an error
# outcome). Same `ADD COLUMN IF NOT EXISTS` pattern `capture.schema` already uses: no backfill,
# because NULL honestly means "this call predates the per-tool outcome summary" or "this tool
# doesn't have one", not "the summary was empty".
_ADD_RESULT_COLUMN = """
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS result JSONB
"""
_INSERT = """
INSERT INTO audit_log (identity, tool, args, duration_ms, outcome, error_class, result)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


def ensure_audit_table(conn) -> None:
    """Idempotent DDL — safe to call on every startup (both transports), and safe when two of them
    start at once.

    Behind `stigmergy.capture.schema.startup_ddl_lock`, the SAME lock `ensure_capture_schema` takes
    one line later on this same connection (`service.build_service`,
    `transport_http.build_http_app`). Not defensive symmetry: `CREATE INDEX IF NOT EXISTS` is a
    check, not a lock, so two sessions creating `audit_log_identity_ts_idx` on a fresh database can
    both see "does not exist" and the loser dies with `UniqueViolation` on `pg_class`. Read the
    comment above `_STARTUP_DDL_LOCK_KEY` for why an all-`IF NOT EXISTS` migration still needs one,
    and why the key is shared rather than per-table.

    `stigmergy.server` importing `stigmergy.capture` is the allowed direction (`stigmergy/capture/
    __init__.py`, `tests/test_architecture.py`); the lock lives there because that is already where
    this database's cross-package schema facts are written down (`schema.DURABLE_TABLES` names
    `audit_log` for the same reason).
    """
    with startup_ddl_lock(conn) as cur:
        cur.execute(_CREATE_TABLE)
        cur.execute(_CREATE_INDEX)
        cur.execute(_ADD_RESULT_COLUMN)


class AuditWriter:
    """Writes one row per tool call over `conn` — the SAME connection `BrainService` reads
    through. Sharing is safe here: `conn` is opened autocommit (`stigmergy.index.store.connect`),
    so one statement's failure never aborts a later one on the same connection, and FastMCP
    invokes sync tool bodies directly on the event loop, never via a thread pool — but that alone
    isn't the operative safety invariant. The actual invariant is: NO DB helper anywhere in this
    module or `BrainService` holds a cursor open across an `await` (`ask` is async and awaits the
    LLM BETWEEN its own read calls, never mid-cursor) — every DB statement here runs to
    completion, synchronously, before control ever returns to the event loop, so an interleaved
    coroutine can never observe a half-read cursor or share one. See `stigmergy.server.transport_http`
    for the HTTP multi-identity case; anything that adds a connection pool or a concurrent write
    path must preserve this invariant explicitly rather than re-deriving "no thread pool = safe"."""

    def __init__(self, conn):
        self.conn = conn

    def write(self, *, identity: str | None, tool: str, args: dict, duration_ms: float,
              outcome: str, error_class: str = "", result: dict | None = None) -> None:
        try:
            with self.conn.cursor() as cur:
                cur.execute(_INSERT, (identity or "(unknown)", tool, Jsonb(args), duration_ms,
                                       outcome, error_class,
                                       None if result is None else Jsonb(result)))
        except Exception:  # noqa: BLE001 — an audit failure must never fail the serving call
            log.error("audit write failed (tool=%s identity=%s outcome=%s)",
                      tool, identity, outcome, exc_info=True)
