"""Requests to erase a layer and their state (specs/data-erasure.md §3).

An administrator asks; the writer runs the request before its next operation (`ops.erase_layer`).
While a request is open the layer is closed (`capture.queue.layer_closed`). Once the rewritten
history is pushed a request never fails: it is retried with backoff until every later step is done.
"""
from __future__ import annotations

import uuid

from psycopg.types.json import Jsonb

from stigmergy.admin import schema as admin_schema
from stigmergy.capture import queue
from stigmergy.capture.schema import ERASURE_OPEN, ERASURE_RUNNING, ERASURE_SUCCEEDED, FAILED, QUEUED
from stigmergy.kernel import layers, wake

RUNNING, SUCCEEDED = ERASURE_RUNNING, ERASURE_SUCCEEDED
MAX_ATTEMPTS = 3
BACKOFF_BASE_S, BACKOFF_CAP_S = 30, 900

_COLUMNS = ("id", "tenant_id", "layer", "requested_by", "requested_at", "status", "attempts", "next_attempt_at",
            "started_at", "finished_at", "names", "old_head", "new_head", "rewrite", "pushed",
            "garbage", "counts", "receipt", "error")
_SELECT = ", ".join(_COLUMNS)
# What only the writer needs; the backoffice never shows it.
_INTERNAL = ("tenant_id", "names", "rewrite", "garbage", "counts")
_OPEN_SQL = f"('{QUEUED}', '{RUNNING}')"
# Due, and never while another request of the same tenant's repository has a rewrite that may be
# pushed (it is recorded) but unfinished: a second rewrite would move the history that request's
# stored commit map points into.
_DUE = (f"status IN {_OPEN_SQL} AND next_attempt_at <= now() AND (pushed OR new_head <> '' "
        f"OR NOT EXISTS (SELECT 1 FROM layer_erasures AS other "
        f"WHERE other.tenant_id = layer_erasures.tenant_id "
        f"AND (other.pushed OR other.new_head <> '') AND other.status IN {_OPEN_SQL}))")

__all__ = ["FAILED", "QUEUED", "RUNNING", "SUCCEEDED"]


class ErasureRefused(ValueError):
    """A request that cannot be accepted; the message says why and is safe to show."""


def prefix(layer: str) -> str:
    """The repository folder that holds everything filed in `layer`."""
    return f"{layers.LAYER_ROOT}/{layer}/"


def is_erasing(conn, layer: str) -> bool:
    return queue.layer_closed(conn, layer)


def request(conn, layer: str, *, actor: str) -> dict:
    """Queue the erasure of `layer`, a roster layer other than `public`."""
    name = str(layer or "").strip()
    if name == layers.PUBLIC or not layers.is_layer_name(name):
        raise ErasureRefused("only a restricted layer can be erased")
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM roster_groups WHERE name = %s", (name,))
        if cursor.fetchone() is None:
            raise ErasureRefused("that layer does not exist")
        cursor.execute(
            "INSERT INTO layer_erasures (id, layer, requested_by, status) VALUES (%s, %s, %s, %s) "
            f"ON CONFLICT (tenant_id, layer) WHERE status IN ('{QUEUED}', '{RUNNING}') DO NOTHING "
            f"RETURNING {_SELECT}",
            (uuid.uuid4(), name, actor, QUEUED))
        row = cursor.fetchone()
    if row is None:
        raise ErasureRefused("an erasure of this layer is already under way")
    wake.writer()
    return _shape(row)


def latest(conn, layer: str) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT {_SELECT} FROM layer_erasures WHERE layer = %s "
                       "ORDER BY requested_at DESC LIMIT 1", (layer,))
        row = cursor.fetchone()
    return None if row is None else _shape(row)


def public(item: dict | None) -> dict | None:
    """`item` without what only the writer needs."""
    return None if item is None else {k: v for k, v in item.items() if k not in _INTERNAL}


def open_layers(conn) -> set[str]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT layer FROM layer_erasures WHERE status = ANY(%s)", (list(ERASURE_OPEN),))
        return {row[0] for row in cursor.fetchall()}


def pending(conn) -> bool:
    """Whether a request is due now."""
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT 1 FROM layer_erasures WHERE {_DUE} LIMIT 1")
        return cursor.fetchone() is not None


def claim(conn) -> dict | None:
    """The oldest due request, marked running, with its next attempt already pushed back so a
    writer that dies mid-run does not retry it at once. One left running by a stopped writer is
    resumed; one that keeps stopping before it ever recorded a rewrite is given up."""
    with conn.cursor() as cursor:
        cursor.execute(
            f"""UPDATE layer_erasures SET status = '{FAILED}', finished_at = now(),
                    error = CASE WHEN error = '' THEN 'the writer stopped during every attempt'
                                 ELSE error END, names = '[]'
                WHERE status = '{RUNNING}' AND NOT pushed AND new_head = '' AND attempts >= %s""",
            (MAX_ATTEMPTS,))
        cursor.execute(
            f"""UPDATE layer_erasures SET status = '{RUNNING}', attempts = attempts + 1,
                    started_at = coalesce(started_at, now()), error = '',
                    next_attempt_at = now() + make_interval(
                        secs => least(%s, %s * power(2, least(attempts, 16))))
                WHERE id = (SELECT id FROM layer_erasures WHERE {_DUE}
                            ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                RETURNING {_SELECT}""",
            (BACKOFF_CAP_S, BACKOFF_BASE_S))
        row = cursor.fetchone()
    return None if row is None else _shape(row)


def record_names(conn, erasure_id, names: list[str]) -> list[str]:
    """Keep the layer's page names from before the rewrite; a rerun redacts by the same names."""
    with conn.cursor() as cursor:
        cursor.execute("UPDATE layer_erasures SET names = %s WHERE id = %s", (Jsonb(names), erasure_id))
    return names


def record_rewrite(conn, erasure_id, *, old_head: str, new_head: str, rewrite: dict) -> None:
    """Keep what the history rewrite produced before it is pushed, so a resumed run remaps rows
    with the same commit map."""
    with conn.cursor() as cursor:
        cursor.execute("UPDATE layer_erasures SET old_head = %s, new_head = %s, rewrite = %s "
                       "WHERE id = %s", (old_head, new_head, Jsonb(rewrite), erasure_id))


def mark_pushed(conn, erasure_id) -> None:
    with conn.cursor() as cursor:
        cursor.execute("UPDATE layer_erasures SET pushed = true WHERE id = %s", (erasure_id,))


def add_garbage(conn, erasure_id, refs: set[str]) -> None:
    """Evidence objects to delete once the rows that pointed at them are gone; written in the
    same transaction as those rows."""
    with conn.cursor() as cursor:
        cursor.execute(
            "UPDATE layer_erasures SET garbage = (SELECT coalesce(jsonb_agg(DISTINCT ref), '[]') "
            "FROM jsonb_array_elements_text(garbage || %s::jsonb) AS ref) WHERE id = %s",
            (Jsonb(sorted(refs)), erasure_id))


def add_counts(conn, erasure_id, counts: dict) -> dict:
    """Add one attempt's counts to those of earlier attempts; returns the totals."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT counts FROM layer_erasures WHERE id = %s FOR UPDATE", (erasure_id,))
        totals = dict(cursor.fetchone()[0])
        for key, value in counts.items():
            totals[key] = int(totals.get(key, 0)) + int(value)
        cursor.execute("UPDATE layer_erasures SET counts = %s WHERE id = %s",
                       (Jsonb(totals), erasure_id))
    return totals


def garbage(conn, erasure_id) -> list[str]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT garbage FROM layer_erasures WHERE id = %s", (erasure_id,))
        return list(cursor.fetchone()[0])


def clear_garbage(conn, erasure_id) -> None:
    with conn.cursor() as cursor:
        cursor.execute("UPDATE layer_erasures SET garbage = '[]' WHERE id = %s", (erasure_id,))


def finish(conn, erasure_id, receipt: dict, *, actor: str) -> None:
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(f"UPDATE layer_erasures SET status = '{SUCCEEDED}', finished_at = now(), "
                       "receipt = %s, rewrite = '{}', names = '[]' WHERE id = %s",
                       (Jsonb(receipt), erasure_id))
        admin_schema.record_action(conn, actor=actor, action="layer.erased", args=receipt,
                                   outcome="ok", strict=True)


def fail(conn, erasure_id, error: str, *, final: bool) -> None:
    """Record a failed attempt: retried after the backoff `claim` set, or failed for good. A request
    fails for good only when nothing was pushed, so it keeps nothing of the layer."""
    with conn.cursor() as cursor:
        if final:
            cursor.execute(f"UPDATE layer_erasures SET status = '{FAILED}', error = %s, "
                           "finished_at = now(), names = '[]', rewrite = '{}', new_head = '', "
                           "garbage = '[]' WHERE id = %s", (error[:500], erasure_id))
        else:
            cursor.execute(f"UPDATE layer_erasures SET status = '{QUEUED}', error = %s WHERE id = %s",
                           (error[:500], erasure_id))


def inventory(conn, layer: str) -> dict:
    """What an erasure of `layer` would remove, counted from the index, the queue and the ledger."""
    counts = {"layer": layer, "pages": {}, "pages_total": 0, "captures": 0, "originals": 0,
              "changes": 0}
    acl = [layer]
    with conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('pages_index') IS NOT NULL, "
                       "to_regclass('knowledge_changes') IS NOT NULL")
        has_index, has_ledger = cursor.fetchone()
        if has_index:
            cursor.execute("SELECT type, count(*) FROM pages_index WHERE acl = %s::text[] "
                           "GROUP BY type ORDER BY type", (acl,))
            counts["pages"] = {kind: int(total) for kind, total in cursor.fetchall()}
            counts["pages_total"] = sum(counts["pages"].values())
        cursor.execute("SELECT count(*) FROM capture_queue WHERE acl = %s::text[]", (acl,))
        counts["captures"] = int(cursor.fetchone()[0])
        cursor.execute(
            "SELECT count(DISTINCT ref) FROM capture_artifacts a "
            "CROSS JOIN LATERAL (VALUES (a.original_ref), (a.readable_ref)) AS r(ref) "
            "WHERE a.capture_id IN (SELECT id FROM capture_queue WHERE acl = %s::text[])", (acl,))
        counts["originals"] = int(cursor.fetchone()[0])
        if has_ledger:
            cursor.execute(
                "SELECT count(*) FROM knowledge_changes c WHERE EXISTS ("
                "SELECT 1 FROM jsonb_array_elements(c.manifest) m WHERE m->>'path' LIKE %s)",
                (prefix(layer) + "%",))
            counts["changes"] = int(cursor.fetchone()[0])
    return counts


def _shape(row) -> dict:
    data = dict(zip(_COLUMNS, row, strict=True))
    data["id"] = str(data["id"])
    data["tenant_id"] = str(data["tenant_id"])
    for key in ("requested_at", "next_attempt_at", "started_at", "finished_at"):
        data[key] = data[key].isoformat() if data[key] is not None else None
    return data
