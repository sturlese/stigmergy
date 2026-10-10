"""Postgres contract for Slack capture deduplication and terminal reports."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import MagicMock

import psycopg
import pytest

from stigmergy.capture import queue, schema
from stigmergy.slack import store
from tests import testdb


def connect_or_skip():
    conn = testdb.connect_or_skip("slack_store")
    store.ensure_write_path_schema(conn)
    return conn


@pytest.fixture()
def conn():
    connection = connect_or_skip()
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM slack_submissions")
        cursor.execute("DELETE FROM capture_artifacts")
        cursor.execute("DELETE FROM capture_queue")
    yield connection
    connection.close()


def enqueue_work(conn, key: str, *, status: str = schema.QUEUED) -> uuid.UUID:
    request = schema.GardenRequest(
        idempotency_key=key,
        actor=schema.Actor(subject="ana@example.com", display_name="Ana"),
        rationale="Test Slack delivery",
    )
    item = queue.enqueue_garden(conn, request)
    if status != schema.QUEUED:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE capture_queue SET status = %s, finished_at = now() WHERE id = %s",
                (status, item["id"]),
            )
    return uuid.UUID(item["id"])


def reserve(conn, *, message="1.1", thread="1.1", user="U1"):
    return store.reserve(
        conn,
        team_id="T1",
        channel_id="C1",
        message_ts=message,
        thread_ts=thread,
        slack_user_id=user,
        submitted_by="ana@example.com",
    )


def test_ensure_write_path_schema_is_idempotent(conn):
    store.ensure_write_path_schema(conn)
    with conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('slack_submissions')")
        assert cursor.fetchone()[0] == "slack_submissions"


def test_reserve_is_exactly_once_per_thread_and_reactor(conn):
    assert reserve(conn, message="1.1", thread="9.1") is not None
    assert reserve(conn, message="1.2", thread="9.1") is None


def test_different_reactors_get_independent_reservations(conn):
    first = reserve(conn, thread="9.1", user="U1")
    second = reserve(conn, thread="9.1", user="U2")
    assert first is not None
    assert second is not None
    assert first != second


def test_release_allows_retry_only_before_submission_is_attached(conn):
    reservation = reserve(conn)
    store.release_reservation(conn, reservation)
    retried = reserve(conn)
    assert retried is not None

    submission_id = enqueue_work(conn, "attached")
    store.attach_submission(conn, retried, submission_id)
    store.release_reservation(conn, retried)
    assert reserve(conn) is None


@pytest.mark.parametrize("status", [schema.LANDED, schema.FAILED])
def test_due_for_report_returns_terminal_status_once(conn, status):
    reservation = reserve(conn)
    submission_id = enqueue_work(conn, f"terminal-{status}", status=status)
    store.attach_submission(conn, reservation, submission_id)

    due = store.due_for_report(conn)
    assert len(due) == 1
    assert due[0]["submission_id"] == submission_id
    assert due[0]["status"] == status

    store.mark_reported(conn, reservation, status)
    assert store.due_for_report(conn) == []


def test_non_terminal_work_is_not_reportable(conn):
    reservation = reserve(conn)
    submission_id = enqueue_work(conn, "queued")
    store.attach_submission(conn, reservation, submission_id)
    assert store.due_for_report(conn) == []


def test_reaction_to_the_same_reply_is_idempotent_after_thread_binding(conn):
    """A reply reservation becomes a root-thread reservation after acquisition."""
    first = store.reserve_reaction(
        conn,
        team_id="T1",
        channel_id="C1",
        message_ts="100.2",
        slack_user_id="U1",
        submitted_by="ana@example.com",
    )
    assert first is not None
    assert store.bind_thread(
        conn,
        first,
        team_id="T1",
        channel_id="C1",
        thread_ts="100.1",
        slack_user_id="U1",
    )

    assert store.reserve_reaction(
        conn,
        team_id="T1",
        channel_id="C1",
        message_ts="100.2",
        slack_user_id="U1",
        submitted_by="ana@example.com",
    ) is None


def test_reserve_reaction_does_not_hide_unrelated_database_errors(monkeypatch):
    conn = MagicMock()
    expected = psycopg.OperationalError("connection lost")
    monkeypatch.setattr(store, "reserve", MagicMock(side_effect=expected))

    with pytest.raises(psycopg.OperationalError, match="connection lost"):
        store.reserve_reaction(
            conn,
            team_id="T1",
            channel_id="C1",
            message_ts="100.2",
            slack_user_id="U1",
            submitted_by="ana@example.com",
        )


def test_root_and_reply_reservations_converge_during_concurrent_thread_binding(conn):
    root = store.reserve_reaction(
        conn,
        team_id="T1",
        channel_id="C1",
        message_ts="100.1",
        slack_user_id="U1",
        submitted_by="ana@example.com",
    )
    assert root is not None
    conn.commit()

    reply_conn = connect_or_skip()
    try:
        reply = store.reserve_reaction(
            reply_conn,
            team_id="T1",
            channel_id="C1",
            message_ts="100.2",
            slack_user_id="U1",
            submitted_by="ana@example.com",
        )
        assert reply is not None
        reply_conn.commit()
    finally:
        reply_conn.close()

    barrier = Barrier(2)

    def bind(reservation_id):
        binding_conn = connect_or_skip()
        try:
            barrier.wait()
            bound = store.bind_thread(
                binding_conn,
                reservation_id,
                team_id="T1",
                channel_id="C1",
                thread_ts="100.1",
                slack_user_id="U1",
            )
            binding_conn.commit()
            return bound
        finally:
            binding_conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(bind, (root, reply)))

    assert sorted(results) == [False, True]
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT message_ts, thread_ts FROM slack_submissions "
            "WHERE team_id = 'T1' AND channel_id = 'C1' AND slack_user_id = 'U1'"
        )
        assert len(cursor.fetchall()) == 1
