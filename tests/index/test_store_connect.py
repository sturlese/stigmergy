from types import SimpleNamespace

import psycopg
import pytest

from stigmergy.index import store


def test_connect_retries_transient_operational_errors(monkeypatch):
    connection = object()
    attempts = []
    sleeps = []

    def connect(conninfo, *, autocommit):
        attempts.append((conninfo, autocommit))
        if len(attempts) < 3:
            raise psycopg.OperationalError("temporary DNS failure")
        return connection

    monkeypatch.setattr(store.psycopg, "connect", connect)
    monkeypatch.setattr(store, "time", SimpleNamespace(sleep=sleeps.append), raising=False)

    assert store.connect("postgresql://db.example/stigmergy") is connection
    assert attempts == [("postgresql://db.example/stigmergy", True)] * 3
    assert sleeps == [0.1, 0.5]


def test_connect_stops_after_the_bounded_retry_budget(monkeypatch):
    attempts = 0
    sleeps = []

    def connect(_conninfo, *, autocommit):
        nonlocal attempts
        assert autocommit is True
        attempts += 1
        raise psycopg.OperationalError("database unavailable")

    monkeypatch.setattr(store.psycopg, "connect", connect)
    monkeypatch.setattr(store, "time", SimpleNamespace(sleep=sleeps.append), raising=False)

    with pytest.raises(psycopg.OperationalError, match="database unavailable"):
        store.connect("postgresql://db.example/stigmergy")

    assert attempts == 3
    assert sleeps == [0.1, 0.5]
