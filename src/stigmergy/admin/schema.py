"""`admin_actions` — the console's own bookkeeping, the only state this package owns: one row
per attempted mutation, the web equivalent of `--by`. The writer swallows and logs — bookkeeping
must never fail the work it records. DDL runs behind `startup_ddl_lock`, and only from
`routes.compose` when the console is configured: an inert console executes no DDL.
"""
import logging

from psycopg.types.json import Jsonb

from stigmergy.capture.schema import startup_ddl_lock

log = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS admin_actions (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    args JSONB NOT NULL,
    outcome TEXT NOT NULL,
    error_class TEXT NOT NULL DEFAULT ''
)
"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS admin_actions_ts_idx ON admin_actions (ts DESC)
"""
_INSERT = """
INSERT INTO admin_actions (actor, action, args, outcome, error_class)
VALUES (%s, %s, %s, %s, %s)
RETURNING id
"""
_RECENT = """
SELECT id, ts, actor, action, args, outcome, error_class
FROM admin_actions ORDER BY ts DESC, id DESC LIMIT %s
"""


def ensure_admin_schema(conn) -> None:
    with startup_ddl_lock(conn) as cur:
        cur.execute(_CREATE_TABLE)
        cur.execute(_CREATE_INDEX)


def record_action(conn, *, actor: str, action: str, args: dict, outcome: str,
                  error_class: str = "") -> int | None:
    """One row per attempted mutation; returns the id, or None when the write itself failed —
    logged loudly, never raised."""
    try:
        with conn.cursor() as cur:
            cur.execute(_INSERT, (actor, action, Jsonb(args or {}), outcome, error_class))
            return cur.fetchone()[0]
    except Exception:  # noqa: BLE001 — bookkeeping must never fail the work it records
        log.error("admin_actions write failed (action=%s outcome=%s)", action, outcome,
                  exc_info=True)
        return None


def recent_actions(conn, *, limit: int = 50) -> list[dict]:
    """The console's own audit trail, newest first."""
    with conn.cursor() as cur:
        cur.execute(_RECENT, (max(1, min(int(limit), 500)),))
        rows = cur.fetchall()
    return [
        {"id": r[0], "ts": r[1].isoformat() if r[1] is not None else None, "actor": r[2],
         "action": r[3], "args": r[4], "outcome": r[5], "error_class": r[6]}
        for r in rows
    ]
