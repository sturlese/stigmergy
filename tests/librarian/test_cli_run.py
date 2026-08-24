"""Writer CLI and autonomous loop contract."""

import datetime as dt
from types import SimpleNamespace

import pytest

from stigmergy.capture import schema
from stigmergy.librarian import cli, config, worker
from stigmergy.librarian.errors import LibrarianConfigError


def test_cli_exposes_only_the_long_running_writer():
    assert cli.build_parser().parse_args(["run"]).command == "run"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["once"])


@pytest.mark.parametrize(
    ("field", "value"),
    (("poll_interval_s", 0), ("max_attempts", 0), ("visibility_timeout_s", 1)),
)
def test_worker_limits_fail_closed(field, value):
    settings = config.Settings(backend="scripted", **{field: value})
    with pytest.raises(LibrarianConfigError):
        settings.check_domains()


def test_worker_reports_each_operation_and_stops_after_the_active_one(monkeypatch):
    settings = SimpleNamespace(
        visibility_timeout_s=600,
        max_attempts=3,
        poll_interval_s=0.01,
        garden_at="off",
    )
    loop = worker.Worker(object(), SimpleNamespace(settings=settings, evidence=object()))
    output = []
    loop.on_output = output.append

    monkeypatch.setattr(worker.ops, "heartbeat", lambda *args: None)
    monkeypatch.setattr(worker.queue, "release_expired", lambda *args, **kwargs: None)

    def process_once(*args):
        loop.stopping = True
        return (
            {"id": "00000000-0000-4000-8000-000000000001"},
            worker.ProcessOutcome(status=schema.LANDED, report={}),
        )

    monkeypatch.setattr(worker, "process_next", process_once)

    assert loop.run() == 1
    assert output == ["#00000000-0000-4000-8000-000000000001 -> landed"]


def test_daily_garden_enters_the_same_queue_once(monkeypatch):
    now = dt.datetime(2026, 8, 24, 5, 8, tzinfo=dt.UTC)
    settings = SimpleNamespace(garden_at="05:07")
    loop = worker.Worker(
        object(),
        SimpleNamespace(settings=settings),
        utcnow=lambda: now,
        on_output=lambda line: None,
    )
    captured = []

    monkeypatch.setattr(worker.schedule, "last_run_at", lambda *args: None)

    def enqueue(_conn, request):
        captured.append(request)
        return {"id": "00000000-0000-4000-8000-000000000002", "created": True}

    monkeypatch.setattr(worker.queue, "enqueue_garden", enqueue)

    assert loop._maybe_garden() is True
    assert captured[0].actor.subject == "system:garden"
    assert captured[0].idempotency_key == "garden:scheduled:2026-08-24"
