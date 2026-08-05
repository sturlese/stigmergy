"""Fixtures for the capture suite: a real Postgres connection with the capture DDL ensured, and a
real-MinIO helper for the evidence-plane integration tests. Same skip-vs-fail posture as
`tests/index/test_pg_integration.py` and `tests/server/conftest.py`: no service reachable -> skip
cleanly; the relevant env var IS set (CI mode) but the service is unreachable -> FAIL loudly,
never a silent skip that could hide a broken CI composition.

The connection itself comes from `tests.testdb`, the one seam that resolves the test DSN and
refuses every other database — this suite is the one whose `DELETE FROM capture_queue` destroyed
real captures when it shared a database with the dogfood.
"""
import uuid

import pytest

from stigmergy.capture import evidence, schema
from tests import testdb


def connect_or_skip():
    conn = testdb.connect_or_skip("capture")
    schema.ensure_capture_schema(conn)
    return conn


@pytest.fixture(scope="module")
def conn():
    c = connect_or_skip()
    yield c
    c.close()


@pytest.fixture()
def clean_queue(conn):
    """Each test gets an EMPTY `capture_queue`/`job_runs`/`ingest_errors` — mirrors
    `tests/server/test_audit.py::clean_audit_log` (`DELETE FROM audit_log`): these ARE the durable
    production tables (never dropped by `ensure_capture_schema`, which is `CREATE TABLE IF NOT
    EXISTS` only), wiped between tests here purely for test isolation. Never touches `audit_log` or
    `pages_index`.

    Safe to wipe because `conn` can only ever be the test database — `tests.testdb` raises before
    it opens a connection to anything else. These three statements used to run against whatever
    `$STIGMERGY_INDEX_DSN` happened to name, which was once the dogfood database."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM job_runs")
        cur.execute("DELETE FROM ingest_errors")
    return conn


def minio_or_skip():
    """A real `S3EvidenceStore` against the compose `minio` service, proven reachable with a cheap
    call. Same fail-vs-skip posture as `connect_or_skip`: `testdb.required()` (i.e.
    `$STIGMERGY_TEST_DSN` set) signals "this run must have a real stack" even though MinIO has its
    own env var family, since CI always brings the whole composition up together — the suite runs
    against the real composed stack, never a stand-in."""
    st = evidence.store_from_env()
    try:
        st.client().list_buckets()
    except Exception as ex:  # noqa: BLE001 — any failure here means: no local MinIO
        if testdb.required():
            pytest.fail(f"${testdb.DSN_ENV} is set (CI mode) but MinIO at {st.endpoint_url} is "
                        f"unreachable — refusing to skip the evidence suite silently: {ex}")
        pytest.skip(f"no MinIO at {st.endpoint_url} (docker compose up -d --wait): {ex}")
    return st


def unique_material(label: str = "capture") -> str:
    """A distinct-per-call string, safe for content-addressing assertions: every test that counts
    evidence objects (dedup) must never collide with another test's or another suite's bytes on
    the SHARED local bucket."""
    return f"{label} {uuid.uuid4()}\n"
