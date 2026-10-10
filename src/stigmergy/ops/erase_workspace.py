"""Delete a whole workspace: its repository, every row it owns, its indexes, its evidence, and its
checkout, leaving every other workspace as it was.

The operator asks from the master console; the single writer runs the request under its lock. The
repository goes first, so the slug cannot be reused while the old knowledge still exists, and the
rows go in one transaction with the `tenants` row, so the workspace is either whole or gone from
Postgres. Every step is safe to run again after a stop; once the repository is gone the request
keeps trying until the rest is gone too.
"""
from __future__ import annotations

import uuid

import psycopg
from psycopg.types.json import Jsonb

from stigmergy.capture import references, uploads
from stigmergy.index import store
from stigmergy.kernel import tenancy, wake
from stigmergy.server import platform

QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"
OPEN = (QUEUED, RUNNING)
MAX_ATTEMPTS = 8
BACKOFF_BASE_S = 60
BACKOFF_CAP_S = 3600

_COLUMNS = ("id", "workspace_id", "slug", "name", "repo", "status", "requested_by", "requested_at",
            "started_at", "finished_at", "attempts", "next_attempt_at", "repo_deleted",
            "rows_deleted", "receipt", "error")
_SELECT = ", ".join(_COLUMNS)
_OPEN_SQL = "('queued', 'running')"


class WorkspaceErasureRefused(ValueError):
    """A deletion that cannot be asked for; the message is safe to show."""


def _shape(row) -> dict:
    item = dict(zip(_COLUMNS, row, strict=True))
    item["id"], item["workspace_id"] = str(item["id"]), str(item["workspace_id"])
    for key in ("requested_at", "started_at", "finished_at", "next_attempt_at"):
        item[key] = item[key].isoformat() if item[key] is not None else None
    return item


def _ineligible(cursor, workspace_id: str) -> str | None:
    """Why the workspace may not be deleted now, read under a lock on its row; None when it may,
    or when its row is already gone."""
    cursor.execute("SELECT status, slack_team_id FROM tenants WHERE id = %s FOR UPDATE",
                   (workspace_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    status, team = row
    if status != "suspended":
        return "the workspace is no longer suspended; nothing was deleted"
    if team:
        return "the workspace owns the deployment's Slack app; nothing was deleted"
    return None


def request(conn, tenant: platform.Tenant, *, confirm: str, actor: str) -> dict:
    """Queue the deletion of a suspended workspace that does not own the Slack app."""
    if tenant.slack_team_id:
        raise WorkspaceErasureRefused("this workspace owns the deployment's Slack app and cannot be "
                                      "deleted from the console")
    if str(confirm or "").strip() != tenant.slug:
        raise WorkspaceErasureRefused("type the workspace's slug exactly to confirm")
    try:
        with conn.transaction(), conn.cursor() as cursor:
            if _ineligible(cursor, tenant.id) is not None:
                raise WorkspaceErasureRefused("suspend the workspace before deleting it")
            cursor.execute(
                f"INSERT INTO workspace_erasures (id, workspace_id, slug, name, repo, requested_by) "
                f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_SELECT}",
                (str(uuid.uuid4()), tenant.id, tenant.slug, tenant.name, tenant.repo, actor))
            requested = _shape(cursor.fetchone())
    except psycopg.errors.UniqueViolation as error:
        raise WorkspaceErasureRefused("this workspace is already being deleted") from error
    wake.writer()
    return requested


def is_open(conn, workspace_id: str) -> bool:
    with conn.cursor() as cursor:
        if not _exists(cursor, "workspace_erasures"):
            return False
        cursor.execute(f"SELECT 1 FROM workspace_erasures WHERE workspace_id = %s "
                       f"AND status IN {_OPEN_SQL}", (workspace_id,))
        return cursor.fetchone() is not None


def open_requests(conn) -> dict[str, dict]:
    """`{tenant id: its open request}`."""
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT {_SELECT} FROM workspace_erasures WHERE status IN {_OPEN_SQL}")
        return {item["workspace_id"]: item for item in map(_shape, cursor.fetchall())}


def latest(conn, limit: int = 20) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT {_SELECT} FROM workspace_erasures ORDER BY requested_at DESC LIMIT %s",
                       (limit,))
        return [_shape(row) for row in cursor.fetchall()]


def pending(conn) -> bool:
    with conn.cursor() as cursor:
        if not _exists(cursor, "workspace_erasures"):
            return False
        cursor.execute(f"SELECT 1 FROM workspace_erasures WHERE status IN {_OPEN_SQL} "
                       "AND next_attempt_at <= now() LIMIT 1")
        return cursor.fetchone() is not None


def claim(conn) -> dict | None:
    """The oldest due request, marked running, its next attempt already pushed back so a writer
    that dies mid-run does not retry it at once."""
    with conn.cursor() as cursor:
        cursor.execute(
            f"""UPDATE workspace_erasures SET status = '{RUNNING}', attempts = attempts + 1,
                    started_at = coalesce(started_at, now()),
                    next_attempt_at = now() + make_interval(
                        secs => least(%s, %s * power(2, least(attempts, 16))))
                WHERE id = (SELECT id FROM workspace_erasures
                            WHERE status IN {_OPEN_SQL} AND next_attempt_at <= now()
                            ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                RETURNING {_SELECT}""",
            (BACKOFF_CAP_S, BACKOFF_BASE_S))
        row = cursor.fetchone()
    return None if row is None else _shape(row)


def _record(conn, erasure_id: str, **values) -> None:
    sets = ", ".join(f"{key} = %s" for key in values)
    params = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in values.values()]
    with conn.cursor() as cursor:
        cursor.execute(f"UPDATE workspace_erasures SET {sets} WHERE id = %s", (*params, erasure_id))


def _tenant_tables(cursor) -> list[str]:
    """Every table under the isolation policy, a table that references another before it."""
    cursor.execute("SELECT tablename FROM pg_policies WHERE schemaname = current_schema() "
                   "AND policyname = %s", (tenancy.POLICY,))
    tables = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint "
                   "WHERE contype = 'f' AND connamespace = current_schema()::regnamespace")
    parents: dict[str, set[str]] = {table: set() for table in tables}
    for child, parent in cursor.fetchall():
        if child in tables and parent in tables and child != parent:
            parents[child].add(parent)
    ordered: list[str] = []
    while parents:
        referenced = {parent for owned in parents.values() for parent in owned}
        leaves = sorted(table for table in parents if table not in referenced)
        if not leaves:
            raise RuntimeError("the tenant tables reference each other in a cycle")
        ordered.extend(leaves)
        for table in leaves:
            del parents[table]
    return ordered


def _exists(cursor, table: str) -> bool:
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    return cursor.fetchone()[0]


def _tenant_refs(cursor) -> set[str]:
    """Every evidence object the bound tenant's rows point at."""
    queries = (
        ("capture_artifacts", "SELECT ref FROM capture_artifacts a CROSS JOIN LATERAL "
                              "(VALUES (a.original_ref), (a.readable_ref)) AS r(ref)"),
        ("capture_queue", "SELECT artifact->>'blob_ref' FROM capture_queue q, "
                          "jsonb_array_elements(coalesce(q.request->'artifacts', '[]')) AS artifact"),
        ("capture_queue", "SELECT artifact->>'readable_ref' FROM capture_queue q, "
                          "jsonb_array_elements(coalesce(q.extraction->'artifacts', '[]')) AS artifact"),
        ("upload_sessions", "SELECT blob_ref FROM upload_sessions"),
        ("knowledge_changes", "SELECT exact_patch_ref FROM knowledge_changes"),
        ("layer_erasures", "SELECT jsonb_array_elements_text(garbage) FROM layer_erasures"),
    )
    refs: set[str] = set()
    for table, sql in queries:
        if _exists(cursor, table):
            cursor.execute(sql)
            refs |= {row[0] for row in cursor.fetchall() if row[0]}
    if _exists(cursor, "upload_sessions"):
        cursor.execute("SELECT id FROM upload_sessions")
        refs |= {uploads.staging_ref(row[0]) for row in cursor.fetchall()}
    return refs


def _delete_rows(conn, erasure_id: str, tenant_id: str, receipt: dict) -> dict:
    """Every row of the tenant and its `tenants` row, with the evidence they pointed at recorded on
    the request, in one transaction."""
    counts: dict[str, int] = {}
    with conn.transaction(), conn.cursor() as cursor:
        with tenancy.tenant_scope(conn, tenant_id):
            refs = _tenant_refs(cursor)
            for table in _tenant_tables(cursor):
                cursor.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,))
                if cursor.rowcount:
                    counts[table] = cursor.rowcount
        with tenancy.platform_scope(conn):
            cursor.execute("UPDATE platform_sessions SET acting_tenant_id = NULL "
                           "WHERE acting_tenant_id = %s", (tenant_id,))
            cursor.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        receipt = {**receipt, "rows": counts}
        _record(conn, erasure_id, rows_deleted=True, garbage=sorted(refs), receipt=receipt)
    return receipt


def _garbage(conn, erasure_id: str) -> list[str]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT garbage FROM workspace_erasures WHERE id = %s", (erasure_id,))
        return list(cursor.fetchone()[0])


def run(conn, item: dict, *, places, evidence, remove_repo) -> dict:
    """Carry one claimed request through every step not yet done and return its receipt."""
    receipt = dict(item.get("receipt") or {})
    if not item["repo_deleted"]:
        with conn.transaction(), conn.cursor() as cursor:
            reason = _ineligible(cursor, item["workspace_id"])
        if reason is not None:
            raise WorkspaceErasureRefused(reason)
        if remove_repo is None:
            raise RuntimeError("the repository cannot be deleted without the librarian GitHub App")
        receipt["repository"] = remove_repo(item["repo"])
        _record(conn, item["id"], repo_deleted=True, receipt=receipt)
    if not item["rows_deleted"]:
        receipt = _delete_rows(conn, item["id"], item["workspace_id"], receipt)
    receipt["search_indexes_dropped"] = (receipt.get("search_indexes_dropped", 0)
                                         + store.drop_tenant_indexes(conn, item["workspace_id"]))
    if evidence is not None:
        deleted = sum(1 for ref in _garbage(conn, item["id"])
                      if not references.is_live(conn, ref) and evidence.delete(ref))
        receipt["evidence_deleted"] = receipt.get("evidence_deleted", 0) + deleted
        _record(conn, item["id"], garbage=[], receipt=receipt)
    receipt["checkout_removed"] = places.forget(item["workspace_id"]) or receipt.get("checkout_removed", False)
    return receipt


def finish(conn, item: dict, receipt: dict) -> None:
    _record(conn, item["id"], status=SUCCEEDED, finished_at=_now(conn), receipt=receipt, error="")


def fail(conn, item: dict, error: Exception) -> str:
    """Record why the attempt stopped. A refused deletion ends at once, having removed nothing; one
    whose repository is already gone keeps trying; any other that keeps stopping ends failed."""
    reason = (str(error) if isinstance(error, WorkspaceErasureRefused)
              else f"{error.__class__.__name__}: {str(error)[:300]}")
    with conn.cursor() as cursor:
        cursor.execute("SELECT repo_deleted FROM workspace_erasures WHERE id = %s", (item["id"],))
        repo_gone = cursor.fetchone()[0]
    if repo_gone:
        status = QUEUED
    elif isinstance(error, WorkspaceErasureRefused) or item["attempts"] >= MAX_ATTEMPTS:
        status = FAILED
    else:
        status = QUEUED
    values = {"status": status, "error": reason}
    if status == FAILED:
        values["finished_at"] = _now(conn)
    _record(conn, item["id"], **values)
    return status


def _now(conn):
    with conn.cursor() as cursor:
        cursor.execute("SELECT now()")
        return cursor.fetchone()[0]
