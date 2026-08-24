import uuid

import pytest

from stigmergy.capture import evidence, schema, uploads
from stigmergy.changes import store as change_store
from stigmergy.index import store as index_store
from tests import testdb


def connect_or_skip():
    conn = testdb.connect_or_skip("capture")
    schema.ensure_capture_schema(conn)
    uploads.ensure_upload_schema(conn)
    change_store.ensure_change_schema(conn)
    index_store.ensure_ops_file_table(conn)
    return conn


@pytest.fixture(scope="module")
def conn():
    connection = connect_or_skip()
    yield connection
    connection.close()


@pytest.fixture()
def clean_queue(conn):
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM capture_artifacts")
        cursor.execute("DELETE FROM capture_queue")
        cursor.execute("DELETE FROM job_runs")
        cursor.execute("DELETE FROM knowledge_changes")
        cursor.execute("DELETE FROM upload_sessions")
        cursor.execute("DELETE FROM ops_file_snapshot")
        cursor.execute(
            "UPDATE index_health SET repository_head_sha = '', indexed_commit_sha = '', "
            "dirty = TRUE, last_incremental_at = NULL, last_full_rebuild_at = NULL, "
            "indexed_rows = 0, updated_at = now() WHERE singleton"
        )
    return conn


def minio_or_skip():
    store = evidence.store_from_env()
    try:
        store.client().list_buckets()
    except Exception as error:
        if testdb.required():
            pytest.fail(f"configured MinIO is unreachable: {error}")
        pytest.skip(f"MinIO is unavailable: {error}")
    return store


def unique_material(label: str = "capture") -> str:
    return f"{label} {uuid.uuid4()}\n"
