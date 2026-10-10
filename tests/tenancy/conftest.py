"""A second tenant beside the suite's own, and helpers to act for either."""
import contextlib

import pytest

from stigmergy.capture import schema, uploads
from stigmergy.changes import store as change_store
from stigmergy.kernel import tenancy
from stigmergy.server import platform, roster
from tests import testdb

TWIN = "00000000-0000-4000-8000-000000000002"
TWIN_SLUG = "twin"
TENANT_TABLES_TO_CLEAR = ("capture_artifacts", "capture_queue", "job_runs", "knowledge_changes",
                          "upload_sessions", "layer_erasures", "pages_index", "embedding_cache",
                          "graph_measures", "index_health")


def ensure_schemas(conn) -> None:
    platform.ensure_platform_schema(conn)
    roster.ensure_roster_schema(conn)
    schema.ensure_capture_schema(conn)
    uploads.ensure_upload_schema(conn)
    change_store.ensure_change_schema(conn)


@contextlib.contextmanager
def acting(conn, tenant: str):
    with tenancy.tenant_scope(conn, tenant):
        yield conn


def clear(conn, tenant: str) -> None:
    with acting(conn, tenant), conn.cursor() as cursor:
        for table in TENANT_TABLES_TO_CLEAR:
            cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
            if cursor.fetchone()[0]:
                cursor.execute(f"DELETE FROM {table}")


@pytest.fixture()
def conn():
    connection = testdb.connect_or_skip("tenancy")
    ensure_schemas(connection)
    yield connection
    connection.close()


@pytest.fixture()
def twin(conn):
    """The second tenant, active for the test and suspended after it, its queue emptied, so no
    later suite claims its work."""
    testdb.ensure_tenant(conn, TWIN, TWIN_SLUG)
    with acting(conn, TWIN):
        roster.seed_tenant(conn)
    clear(conn, TWIN)
    clear(conn, testdb.TEST_TENANT)
    yield TWIN
    clear(conn, TWIN)
    clear(conn, testdb.TEST_TENANT)
    testdb.ensure_tenant(conn, TWIN, TWIN_SLUG, status="suspended")
