"""Which tenant a database connection acts for, and the row-level isolation of tenant tables.

A tenant-owned table carries `tenant_id`, defaulting to the connection's tenant, and forces one
policy: a row is visible and writable when it belongs to the connection's tenant, or when the
connection is in platform scope. A connection with neither reads no tenant row and cannot insert one.
This module is the only code that sets either setting.
"""
from __future__ import annotations

import re
import uuid
from contextlib import contextmanager

TENANT_SETTING = "app.tenant_id"
SCOPE_SETTING = "app.scope"
PLATFORM = "platform"
POLICY = "tenant_isolation"

_CURRENT_TENANT = f"nullif(current_setting('{TENANT_SETTING}', true), '')::uuid"
VISIBLE = f"(current_setting('{SCOPE_SETTING}', true) = '{PLATFORM}' OR tenant_id = {_CURRENT_TENANT})"
TENANT_COLUMN = f"tenant_id UUID NOT NULL DEFAULT {_CURRENT_TENANT} REFERENCES tenants (id)"

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
RESERVED_SLUGS = frozenset({"admin", "api", "app", "mcp", "platform", "status", "www"})
STATUSES = ("provisioning", "active", "suspended")
_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

_TENANTS_DDL = f"""
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{{1,31}}$'),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'provisioning'
        CHECK (status IN ({", ".join(f"'{status}'" for status in STATUSES)})),
    repo TEXT NOT NULL UNIQUE,
    slack_team_id TEXT UNIQUE,
    last_claimed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL DEFAULT ''
)
"""


class TenancyError(RuntimeError):
    pass


def ensure_tenants(cursor) -> None:
    """The table every tenant-owned row references; create it before any of them."""
    cursor.execute(_TENANTS_DDL)


def checked_uuid(value) -> str:
    """A tenant id in canonical form; anything else is refused before it can reach SQL text."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as error:
        raise TenancyError("a tenant id must be a UUID") from error


def _set(conn, name: str, value: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, false)", (name, value))


def _get(conn, name: str) -> str:
    with conn.cursor() as cursor:
        cursor.execute("SELECT coalesce(current_setting(%s, true), '')", (name,))
        return cursor.fetchone()[0]


def set_tenant(conn, tenant_id) -> None:
    """Bind the session to one tenant until it is cleared or rebound."""
    _set(conn, TENANT_SETTING, checked_uuid(tenant_id))


def clear(conn) -> None:
    """Forget the tenant and leave platform scope; a pooled connection is returned this way."""
    _set(conn, TENANT_SETTING, "")
    _set(conn, SCOPE_SETTING, "")


def current_tenant(conn) -> str | None:
    value = _get(conn, TENANT_SETTING)
    return value or None


def require_tenant(conn) -> str:
    tenant = current_tenant(conn)
    if tenant is None:
        raise TenancyError("this operation needs a tenant")
    return tenant


@contextmanager
def tenant_scope(conn, tenant_id):
    """Act for `tenant_id` alone inside the block, even within platform scope, then restore the
    previous binding and scope."""
    previous, scope = _get(conn, TENANT_SETTING), _get(conn, SCOPE_SETTING)
    set_tenant(conn, tenant_id)
    if scope:
        _set(conn, SCOPE_SETTING, "")
    try:
        yield
    finally:
        if scope:
            _restore(conn, SCOPE_SETTING, scope)
        _restore(conn, TENANT_SETTING, previous)


@contextmanager
def platform_scope(conn):
    """See and write every tenant's rows inside the block; its uses are listed in
    `specs/multi-tenancy.md` §3.1."""
    previous = _get(conn, SCOPE_SETTING)
    _set(conn, SCOPE_SETTING, PLATFORM)
    try:
        yield
    finally:
        _restore(conn, SCOPE_SETTING, previous)


def _restore(conn, name: str, value: str) -> None:
    try:
        _set(conn, name, value)
    except Exception:  # noqa: BLE001 — an aborted transaction rolls the setting back itself
        if not _in_failed_transaction(conn):
            raise


def _in_failed_transaction(conn) -> bool:
    from psycopg.pq import TransactionStatus

    return conn.info.transaction_status == TransactionStatus.INERROR


def protect(cursor, table: str) -> None:
    """Force the isolation policy on one tenant table, and index its tenant column. Each statement
    runs only when its catalog entry is missing: the row-security statements and `CREATE INDEX`
    lock the table even when they change nothing, and a rebuild calls this inside its transaction."""
    if not _TABLE_RE.fullmatch(table):
        raise TenancyError(f"not a table name: {table!r}")
    cursor.execute("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                   "WHERE oid = to_regclass(%s)", (table,))
    row = cursor.fetchone()
    if row is None or not row[0]:
        cursor.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        cursor.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    cursor.execute("SELECT 1 FROM pg_policies WHERE schemaname = current_schema() "
                   "AND tablename = %s AND policyname = %s", (table, POLICY))
    if cursor.fetchone() is None:
        cursor.execute(f"CREATE POLICY {POLICY} ON {table} USING {VISIBLE} WITH CHECK {VISIBLE}")
    ensure_index(cursor, f"{table}_tenant_idx", f"ON {table} (tenant_id)")


def ensure_index(cursor, name: str, definition: str) -> None:
    """`CREATE INDEX name definition` unless it exists, without locking the table when it does."""
    if not _TABLE_RE.fullmatch(name):
        raise TenancyError(f"not an index name: {name!r}")
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    if not cursor.fetchone()[0]:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {name} {definition}")


_BYPASSING_ROLE = ("SELECT coalesce((SELECT rolsuper OR rolbypassrls FROM pg_roles "
                   "WHERE rolname = current_user), true)")


def assert_isolating(conn) -> None:
    """Refuse a connection whose role is exempt from row-level security: it would read and write
    every tenant's rows whatever the session is bound to."""
    with conn.cursor() as cursor:
        cursor.execute(_BYPASSING_ROLE)
        bypasses = cursor.fetchone()[0]
    if bypasses:
        raise TenancyError("the database role bypasses row-level security; connect as a role "
                           "that does not (stigmergy_app)")
