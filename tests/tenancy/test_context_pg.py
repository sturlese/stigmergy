"""TEN-02: a connection that acts for no tenant reads no tenant row and cannot write one; a pooled
connection goes back to the pool acting for none."""
import psycopg
import pytest

from stigmergy.index import store
from stigmergy.kernel import tenancy
from stigmergy.server import roster
from tests import testdb
from tests.roster import seed_roster

TABLES = ("roster_principals", "roster_groups", "roster_tokens", "capture_queue", "job_runs",
          "knowledge_changes", "audit_log", "index_health")


def test_a_connection_without_a_tenant_sees_and_writes_nothing(conn):
    seed_roster(conn, {"someone@example.com": {"display_name": "Someone", "groups": [],
                                               "default_audience": ["public"]}})
    bare = store.connect(testdb.dsn())
    try:
        assert tenancy.current_tenant(bare) is None
        with bare.cursor() as cur:
            for table in TABLES:
                cur.execute(f"SELECT count(*) FROM {table}")
                assert cur.fetchone()[0] == 0, table
        refused = (psycopg.errors.NotNullViolation, psycopg.errors.InsufficientPrivilege)
        with pytest.raises(refused), bare.cursor() as cur:
            cur.execute("INSERT INTO roster_groups (name) VALUES ('stray')")
    finally:
        bare.close()
    assert roster.principal(conn, "someone@example.com").tenant == testdb.TEST_TENANT


def test_platform_scope_sees_every_tenant_and_ends_with_its_block(conn, twin):
    with tenancy.tenant_scope(conn, twin):
        seed_roster(conn, {"other@example.com": {"display_name": "Other", "groups": [],
                                                 "default_audience": ["public"]}})
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM roster_principals WHERE subject = 'other@example.com'")
        assert cur.fetchone()[0] == 0
        with tenancy.platform_scope(conn):
            cur.execute("SELECT count(*) FROM roster_principals WHERE subject = 'other@example.com'")
            assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM roster_principals WHERE subject = 'other@example.com'")
        assert cur.fetchone()[0] == 0


def test_a_pooled_connection_returns_acting_for_no_tenant():
    pool = store.serving_pool(testdb.dsn(), max_size=1)
    try:
        first = pool.getconn()
        testdb.bind_tenant(first)
        assert tenancy.current_tenant(first) == testdb.TEST_TENANT
        first.close()
        again = pool.getconn()
        try:
            assert tenancy.current_tenant(again) is None
        finally:
            again.close()
    finally:
        pool.close()


def test_a_tenant_id_that_is_not_a_uuid_is_refused_before_sql(conn):
    with pytest.raises(tenancy.TenancyError):
        tenancy.set_tenant(conn, "x'; DROP TABLE tenants; --")


class _BypassingCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        assert "rolbypassrls" in sql

    def fetchone(self):
        return (True,)


class _Bypassing:
    """A connection whose role is exempt from row-level security, as `postgres` is on Supabase."""

    closed = False

    def cursor(self):
        return _BypassingCursor()

    def close(self):
        self.closed = True


def test_a_role_that_bypasses_row_level_security_is_refused(monkeypatch):
    bypassing = _Bypassing()
    monkeypatch.setattr(store.psycopg, "connect", lambda *args, **kwargs: bypassing)
    with pytest.raises(tenancy.TenancyError, match="bypasses row-level security"):
        store.connect("postgresql://owner@db.invalid/brain")
    assert bypassing.closed


def test_the_service_role_is_accepted(conn):
    tenancy.assert_isolating(conn)


def test_a_tenant_scope_inside_platform_scope_sees_only_that_tenant(conn, twin):
    with tenancy.tenant_scope(conn, twin):
        seed_roster(conn, {"other@example.com": {"display_name": "Other", "groups": [],
                                                 "default_audience": ["public"]}})
    seed_roster(conn, {"own@example.com": {"display_name": "Own", "groups": [],
                                           "default_audience": ["public"]}})
    with tenancy.platform_scope(conn), conn.cursor() as cur:
        with tenancy.tenant_scope(conn, twin):
            cur.execute("SELECT subject FROM roster_principals "
                        "WHERE subject IN ('other@example.com', 'own@example.com')")
            assert [row[0] for row in cur.fetchall()] == ["other@example.com"]
        cur.execute("SELECT count(*) FROM roster_principals "
                    "WHERE subject IN ('other@example.com', 'own@example.com')")
        assert cur.fetchone()[0] == 2


def test_protecting_a_protected_table_takes_no_lock_that_blocks_other_tenants(conn):
    """A rebuild re-asserts the policy inside its transaction; it must not hold a lock that
    stops another tenant reading or writing until it commits."""
    with conn.transaction(), conn.cursor() as cur:
        tenancy.protect(cur, "job_runs")
        cur.execute("SELECT mode FROM pg_locks WHERE pid = pg_backend_pid() "
                    "AND relation = 'job_runs'::regclass")
        held = {row[0] for row in cur.fetchall()}
    assert not held & {"ShareLock", "ShareRowExclusiveLock", "ExclusiveLock", "AccessExclusiveLock"}


def test_a_suspended_workspaces_people_resolve_as_unknown(conn, twin):
    """Slack resolves each event's author by email; a suspended workspace answers and queues
    nothing for anyone."""
    from stigmergy.server.identity import IdentityError

    with tenancy.tenant_scope(conn, twin):
        seed_roster(conn, {"member@example.com": {"display_name": "Member", "groups": [],
                                                  "default_audience": ["public"]}})
        assert roster.principal(conn, "member@example.com").tenant == twin
        testdb.ensure_tenant(conn, twin, "twin", status="suspended")
        try:
            with pytest.raises(IdentityError):
                roster.principal(conn, "member@example.com")
        finally:
            testdb.ensure_tenant(conn, twin, "twin", status="active")
