"""The master console: the platform operator's view of every tenant and the only way to create one.

Every method needs an operator principal. Cross-tenant reads run in platform scope and return
counts and states only, never a tenant's knowledge; a tenant's own screens are reached by making it
the session's acting tenant.
"""
from __future__ import annotations

import os

from stigmergy.kernel import layers, tenancy, wake
from stigmergy.ops import erase_workspace
from stigmergy.server import platform, roster, usage
from stigmergy.server.identity import UNRESTRICTED_GROUP, Principal

ORG_ENV = "STIGMERGY_GITHUB_ORG"
REPO_SUFFIX = "-brain"


class ConsoleRefused(ValueError):
    """A console request that cannot be done; the message is safe to show."""


_OVERVIEW = """
SELECT t.id,
       (SELECT count(*) FROM roster_principals p WHERE p.tenant_id = t.id AND p.disabled_at IS NULL),
       (SELECT count(*) FROM capture_queue q WHERE q.tenant_id = t.id
            AND q.status IN ('queued', 'processing')),
       (SELECT count(*) FROM capture_queue q WHERE q.tenant_id = t.id AND q.status = 'failed'),
       (SELECT max(q.finished_at) FROM capture_queue q WHERE q.tenant_id = t.id
            AND q.status = 'landed'),
       (SELECT h.dirty FROM index_health h WHERE h.tenant_id = t.id LIMIT 1),
       (SELECT max(j.started_at) FROM job_runs j WHERE j.tenant_id = t.id AND j.job = 'garden'
            AND j.status = 'succeeded'),
       t.created_at
FROM tenants t
"""


class ConsoleService:
    def __init__(self, conn, *, operator: Principal):
        if not operator.operator:
            raise ConsoleRefused("the master console is for platform operators")
        self._conn = conn
        self._operator = operator

    # ── tenants ──────────────────────────────────────────────────────────────────────────────
    def tenants(self) -> dict:
        tenants = platform.list_tenants(self._conn)
        with tenancy.platform_scope(self._conn), self._conn.cursor() as cur:
            cur.execute(_OVERVIEW)
            stats = {str(row[0]): row[1:] for row in cur.fetchall()}
        spend = usage.spend_by_tenant(self._conn)
        deleting = erase_workspace.open_requests(self._conn)
        rows = []
        for tenant in tenants:
            people, pending, failed, landed, dirty, garden, created = stats.get(
                tenant.id, (0, 0, 0, None, None, None, None))
            rows.append({**tenant.as_json(), "people": people, "pending": pending, "failed": failed,
                         "last_landed_at": _iso(landed), "index_dirty": dirty,
                         "last_garden_at": _iso(garden), "created_at": _iso(created),
                         "spend_7d": spend.get(tenant.id, {"input_tokens": 0, "output_tokens": 0,
                                                           "usd": 0.0}),
                         "erasure": _erasure_view(deleting.get(tenant.id))})
        return {"tenants": rows, "acting": self._operator.tenant or None}

    def create_tenant(self, *, name: str, slug: str, admin_email: str, admin_name: str) -> dict:
        org = os.environ.get(ORG_ENV, "").strip()
        if not org:
            raise ConsoleRefused(f"set ${ORG_ENV} to the GitHub organization that holds workspaces")
        clean = platform.check_slug(slug)
        with self._conn.transaction():
            tenant = platform.create_tenant(self._conn, slug=clean, name=name,
                                            repo=f"{org}/{clean}{REPO_SUFFIX}",
                                            actor=self._operator.subject)
            with tenancy.tenant_scope(self._conn, tenant.id):
                admin = roster.create_principal(self._conn, {
                    "subject": admin_email, "display_name": admin_name, "kind": "person",
                    "is_admin": True, "groups": [UNRESTRICTED_GROUP],
                    "default_audience": [layers.PUBLIC]}, actor=self._operator.subject)
                grant = roster.issue_token(self._conn, admin.subject, "first administrator",
                                           actor=self._operator.subject)
            platform.record_action(self._conn, actor=self._operator.subject, action="tenant.create",
                                   tenant_slug=tenant.slug, outcome="ok",
                                   args={"repo": tenant.repo, "admin": admin.subject})
        wake.writer()
        return {"tenant": tenant.as_json(), "admin": admin.subject,
                "token": {"token": grant.token, "prefix": grant.prefix, "subject": grant.subject}}

    def set_status(self, tenant_id: str, status: str) -> dict:
        if status not in ("active", "suspended"):
            raise ConsoleRefused("a workspace is active or suspended")
        current = platform.tenant_by_id(self._conn, tenant_id)
        if current is None:
            raise ConsoleRefused("that workspace does not exist")
        if current.status == "provisioning":
            raise ConsoleRefused("a workspace becomes active once its repository exists")
        # A deletion request locks the same row, so the two can never interleave.
        with self._conn.transaction(), self._conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM tenants WHERE id = %s FOR UPDATE", (current.id,))
            if erase_workspace.is_open(self._conn, current.id):
                raise ConsoleRefused("this workspace is being deleted")
            updated = platform.set_tenant_status(self._conn, current.id, status)
        platform.record_action(self._conn, actor=self._operator.subject, action=f"tenant.{status}",
                               tenant_slug=updated.slug, outcome="ok")
        if status == "active":
            wake.writer()
        return {"tenant": updated.as_json()}

    def rebuild(self, tenant_id: str) -> dict:
        """Ask the writer to rebuild a workspace's index on its next pass."""
        from stigmergy.index import health

        tenant = platform.tenant_by_id(self._conn, tenant_id)
        if tenant is None:
            raise ConsoleRefused("that workspace does not exist")
        with tenancy.tenant_scope(self._conn, tenant.id):
            health.ensure_schema(self._conn)
            with self._conn.cursor() as cur:
                cur.execute("UPDATE index_health SET last_full_rebuild_at = NULL WHERE singleton")
        platform.record_action(self._conn, actor=self._operator.subject, action="tenant.rebuild",
                               tenant_slug=tenant.slug, outcome="ok")
        wake.writer()
        return {"queued": True}

    def erase(self, tenant_id: str, confirm: str) -> dict:
        """Queue the deletion of a whole suspended workspace; the writer carries it out."""
        tenant = platform.tenant_by_id(self._conn, tenant_id)
        if tenant is None:
            raise ConsoleRefused("that workspace does not exist")
        try:
            requested = erase_workspace.request(self._conn, tenant, confirm=confirm,
                                                actor=self._operator.subject)
        except erase_workspace.WorkspaceErasureRefused as error:
            raise ConsoleRefused(str(error)) from error
        platform.record_action(self._conn, actor=self._operator.subject, action="tenant.erase",
                               tenant_slug=tenant.slug, outcome="queued", args={"repo": tenant.repo})
        return {"erasure": _erasure_view(requested)}

    def act(self, session_value: str, tenant_id: str | None) -> platform.Tenant | None:
        """Make `tenant_id` the session's acting tenant, or leave every tenant with None."""
        tenant = None
        if tenant_id:
            tenant = platform.tenant_by_id(self._conn, tenant_id)
            if tenant is None:
                raise ConsoleRefused("that workspace does not exist")
        platform.set_acting_tenant(self._conn, session_value, tenant.id if tenant else None)
        platform.record_action(self._conn, actor=self._operator.subject, action="tenant.act",
                               tenant_slug=tenant.slug if tenant else "", outcome="ok")
        return tenant

    # ── access requests and the worker ───────────────────────────────────────────────────────
    def access_requests(self) -> dict:
        return {"requests": platform.list_access_requests(self._conn)}

    def set_access_request(self, request_id: str, status: str) -> dict:
        platform.set_access_request_status(self._conn, request_id, status,
                                           actor=self._operator.subject)
        return {"ok": True}

    def worker(self) -> dict:
        from stigmergy.capture import ops

        with tenancy.platform_scope(self._conn), self._conn.cursor() as cur:
            cur.execute("SELECT t.slug, q.id, q.operation, q.finished_at, q.error_category "
                        "FROM capture_queue q JOIN tenants t ON t.id = q.tenant_id "
                        "WHERE q.status = 'failed' ORDER BY q.finished_at DESC NULLS LAST LIMIT 20")
            failed = [{"tenant": row[0], "id": str(row[1]), "operation": row[2],
                       "finished_at": _iso(row[3]), "error_category": row[4]}
                      for row in cur.fetchall()]
        return {"heartbeat": ops.read_heartbeat(self._conn), "failed": failed,
                "actions": platform.recent_actions(self._conn, limit=30)}


def _erasure_view(item: dict | None) -> dict | None:
    if item is None:
        return None
    return {key: item[key] for key in ("id", "status", "attempts", "error", "requested_at",
                                       "next_attempt_at", "repo_deleted", "rows_deleted")}


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
