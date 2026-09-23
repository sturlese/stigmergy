import uuid
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "ops"
    / "migrations"
    / "20260924_capture_deadlines.sql"
)


def test_deadline_cutover_migrates_a_drained_prechange_queue(conn):
    namespace = f"deadline_cutover_{uuid.uuid4().hex}"
    with conn.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{namespace}"')
        cursor.execute(f'SET search_path TO "{namespace}"')
        cursor.execute("CREATE TABLE capture_queue (status TEXT NOT NULL)")
        cursor.execute(MIGRATION.read_text(encoding="utf-8"))
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'capture_queue' "
            "ORDER BY column_name",
            (namespace,),
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "budget_deadline_at",
            "lease_started_at",
            "status",
        ]
        cursor.execute("SET search_path TO public")
        cursor.execute(f'DROP SCHEMA "{namespace}" CASCADE')
