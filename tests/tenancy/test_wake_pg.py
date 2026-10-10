"""The writer sleeps only when no workspace has work it must finish, anything queued for it wakes
it, and a sleeping writer reads as asleep rather than down."""
import uuid

from stigmergy.admin.service import AdminService
from stigmergy.capture import ops, queue, schema
from stigmergy.librarian import worker
from stigmergy.server.settings import Settings
from tests import testdb
from tests.roster import seed_roster

ADMIN = {"admin@example.com": {"display_name": "Admin", "groups": ["brain-admins"],
                               "default_audience": ["public"]}}


def _garden(conn) -> dict:
    return queue.enqueue_garden(conn, schema.GardenRequest(
        idempotency_key=f"test:{uuid.uuid4()}", actor=schema.Actor(subject="admin@example.com", display_name="Admin"),
        rationale="test"))


def test_queuing_work_wakes_the_writer_and_keeps_it_awake(conn, monkeypatch):
    woken = []
    monkeypatch.setattr("stigmergy.kernel.wake.writer", lambda env=None: woken.append(1))
    item = _garden(conn)
    try:
        assert woken == [1]
        assert worker._pending_work(conn) is True
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM capture_queue WHERE id = %s", (item["id"],))


def test_a_sleeping_writer_reads_as_asleep_not_stale(conn):
    seed_roster(conn, ADMIN, admins=set(ADMIN))
    ops.heartbeat(conn, "sleeping")
    with conn.cursor() as cur:
        cur.execute("UPDATE worker_heartbeat SET heartbeat_at = now() - interval '3 hours' WHERE singleton")
    from stigmergy.server import roster

    service = AdminService(conn, principal=roster.principal(conn, "admin@example.com"),
                           server_settings=Settings(dsn=testdb.dsn(), embedder="fake", llm="fake"))
    status = service.worker_status()
    assert status["stale"] is False
    assert status["heartbeat"]["state"] == "sleeping"
    ops.heartbeat(conn, "idle")
