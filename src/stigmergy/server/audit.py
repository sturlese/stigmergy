"""`audit_log` — one row per tool call, written at the service seam (`BrainService._call` /
`.call_async`) so stdio and HTTP share one code path. Idempotent DDL, run at startup by both
transports. Audit-write failure NEVER fails the serving call: `AuditWriter.write` swallows its
own errors and logs at ERROR.

`result` is a nullable JSONB per-tool outcome SUMMARY, never a transcript — facts about an
outcome's SHAPE, from an optional per-call-site `summarize` callback; a test asserts NEGATIVELY
that nothing writes a question or an answer into it, ever.

`args` carries caller CONTENT verbatim for exactly two fields — a NAMED, accepted exemption, not
an oversight: `ask`'s `question` and `search_brain`'s `query` are written as-is (bounded by
`server.service.MAX_ARG_CHARS`) for operator diagnosability — without the literal text, "why did
this come back empty" is unanswerable from the database at all.
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
# Additive, nullable, no backfill: NULL honestly means "no per-tool summary for this row",
# never "the summary was empty".
_ADD_RESULT_COLUMN = """
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS result JSONB
"""
_INSERT = """
INSERT INTO audit_log (identity, tool, args, duration_ms, outcome, error_class, result)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


def ensure_audit_table(conn) -> None:
    """Idempotent DDL, safe from two startups at once: behind `startup_ddl_lock` because
    `CREATE INDEX IF NOT EXISTS` is a check, not a lock — two fresh-database startups can both
    see "does not exist" and the loser dies with `UniqueViolation` on `pg_class`."""
    with startup_ddl_lock(conn) as cur:
        cur.execute(_CREATE_TABLE)
        cur.execute(_CREATE_INDEX)
        cur.execute(_ADD_RESULT_COLUMN)


class AuditWriter:
    """One row per tool call, over the SAME autocommit connection `BrainService` reads through.
    Safe because no DB helper in this module or `BrainService` holds a cursor open across an
    `await` — every statement runs to completion before control returns to the event loop.
    Anything adding a connection pool or a concurrent write path must preserve that explicitly."""

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
