"""Privacy-minimized, bounded tool audit shared by every service path."""

import hashlib
import json
import logging
import re
import threading
import time

from psycopg.types.json import Jsonb

from stigmergy.capture.schema import startup_ddl_lock

log = logging.getLogger(__name__)

AUDIT_RETENTION_DAYS = 30
_PURGE_INTERVAL_SECONDS = 24 * 60 * 60
_OPAQUE_ID = re.compile(
    r"(?:[a-z][a-z0-9]*_)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\Z"
)
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_SAFE_ARG_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_purge_lock = threading.Lock()
_next_purge_at = 0.0

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
_CREATE_RETENTION_INDEX = """
CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (ts)
"""
_INSERT = """
INSERT INTO audit_log (identity, tool, args, duration_ms, outcome, error_class, result)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""
_DELETE_EXPIRED = """
DELETE FROM audit_log WHERE ts < now() - make_interval(days => %s)
"""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fingerprint(value: str) -> dict[str, int | str]:
    return {"chars": len(value), "sha256": _sha256(value)}


def _container_fingerprint(value, *, count_key: str) -> dict[str, int | str]:
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: f"<{type(item).__name__}>",
        )
    except Exception:  # noqa: BLE001 -- unfamiliar audit containers still need a safe digest
        canonical = f"<{type(value).__name__}>"
    return {count_key: len(value), "sha256": _sha256(canonical)}


def _minimize_value(key: str, value):
    """Keep useful cardinality and correlation without retaining human-readable content."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        is_identifier = (key.endswith("_id") or key == "resolution_of") and _OPAQUE_ID.fullmatch(value)
        is_digest = key.endswith("_sha256") and _SHA256.fullmatch(value)
        return value if is_identifier or is_digest else _fingerprint(value)
    if isinstance(value, dict):
        return _container_fingerprint(value, count_key="fields")
    if isinstance(value, (list, tuple)):
        return _container_fingerprint(value, count_key="items")
    return {"type": type(value).__name__}


def minimize_audit_args(args: dict) -> dict:
    """Remove free text from arguments while preserving their operational shape."""
    minimized = {}
    for key, value in args.items():
        key_text = str(key)
        safe_key = key_text if _SAFE_ARG_KEY.fullmatch(key_text) else f"field_{_sha256(key_text)[:12]}"
        minimized[safe_key] = _minimize_value(key_text, value)
    return minimized


def _purge_expired_if_due(conn) -> None:
    """Bound storage without a scheduler: at most one cleanup attempt per process and day."""
    global _next_purge_at

    now = time.monotonic()
    if now < _next_purge_at:
        return
    with _purge_lock:
        now = time.monotonic()
        if now < _next_purge_at:
            return
        _next_purge_at = now + _PURGE_INTERVAL_SECONDS
        try:
            with conn.cursor() as cur:
                cur.execute(_DELETE_EXPIRED, (AUDIT_RETENTION_DAYS,))
        except Exception as error:  # noqa: BLE001 -- retention must not fail a served call
            log.error(
                "expired audit row cleanup failed (%s)",
                error.__class__.__name__,
                exc_info=log.isEnabledFor(logging.DEBUG),
            )


def ensure_audit_table(conn) -> None:
    """Idempotent DDL, safe from two startups at once: behind `startup_ddl_lock` because
    `CREATE INDEX IF NOT EXISTS` is a check, not a lock — two fresh-database startups can both
    see "does not exist" and the loser dies with `UniqueViolation` on `pg_class`."""
    with startup_ddl_lock(conn) as cur:
        cur.execute(_CREATE_TABLE)
        cur.execute(_CREATE_INDEX)
        cur.execute(_CREATE_RETENTION_INDEX)


class AuditWriter:
    """One privacy-minimized row per tool call on `BrainService`'s autocommit connection.

    Safe because no DB helper in this module or `BrainService` holds a cursor open across an
    `await` — every statement runs to completion before control returns to the event loop.
    Anything adding a connection pool or a concurrent write path must preserve that explicitly."""

    def __init__(self, conn):
        self.conn = conn

    def write(self, *, identity: str | None, tool: str, args: dict, duration_ms: float,
              outcome: str, error_class: str = "", result: dict | None = None) -> None:
        try:
            minimized_args = minimize_audit_args(args)
        except Exception as error:  # noqa: BLE001 -- audit shaping must not fail a served call
            log.error(
                "audit argument minimization failed (%s)",
                error.__class__.__name__,
                exc_info=log.isEnabledFor(logging.DEBUG),
            )
            minimized_args = {"args_unavailable": True}
        try:
            with self.conn.cursor() as cur:
                cur.execute(_INSERT, (identity or "(unknown)", tool, Jsonb(minimized_args),
                                       duration_ms,
                                       outcome, error_class,
                                       None if result is None else Jsonb(result)))
        except Exception as error:  # noqa: BLE001 — audit failure must not fail the serving call
            log.error(
                "audit write failed (tool=%s outcome=%s error=%s)",
                tool,
                outcome,
                error.__class__.__name__,
            )
        _purge_expired_if_due(self.conn)
