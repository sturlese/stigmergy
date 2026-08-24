"""Best-effort tool audit shared by the stdio and HTTP service paths."""
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
    error_class TEXT NOT NULL DEFAULT '',
    result JSONB
)
"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS audit_log_identity_ts_idx ON audit_log (identity, ts DESC)
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
        except Exception as error:  # noqa: BLE001 — audit failure must not fail the serving call
            log.error(
                "audit write failed (tool=%s identity=%s outcome=%s error=%s)",
                tool,
                identity,
                outcome,
                error.__class__.__name__,
            )
