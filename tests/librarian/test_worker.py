import contextlib
import time
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from stigmergy.capture import schema
from stigmergy.knowledge import writer as knowledge_writer
from stigmergy.knowledge.writer import WriterDeadline, WriteResult
from stigmergy.librarian import config, gitcmd, worker
from stigmergy.librarian.errors import LibrarianConfigError


def test_pydantic_worker_requires_openrouter_before_reading_the_repository(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        worker.gitcmd,
        "ensure_repo",
        lambda *_args: pytest.fail("repository access must happen after credential validation"),
    )

    with pytest.raises(LibrarianConfigError, match="OPENROUTER_API_KEY"):
        worker.startup_checks(config.Settings())


def test_worker_connection_bounds_database_statements():
    executed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, parameters):
            executed.append((statement, parameters))

    worker.configure_connection(SimpleNamespace(cursor=Cursor))

    assert executed == [
        (
            "SELECT set_config('statement_timeout', %s, false)",
            (f"{worker.STATEMENT_TIMEOUT_MS}ms",),
        )
    ]


def test_writer_and_terminal_transition_fit_inside_the_queue_lease(monkeypatch):
    settings = config.Settings(timeout_s=180)
    deadlines = []

    @contextlib.contextmanager
    def record_deadline(seconds, _error_factory):
        deadlines.append(seconds)
        yield

    monkeypatch.setattr(knowledge_writer, "hard_deadline", record_deadline)
    monkeypatch.setattr(
        knowledge_writer,
        "_process_with_lock",
        lambda *_args: WriteResult(commit_sha="abc", change_id=None),
    )
    assert knowledge_writer.process(
        object(),
        {},
        SimpleNamespace(settings=settings),
    ).commit_sha == "abc"

    item = {"id": "00000000-0000-4000-8000-000000000001", "attempts": 1}
    monkeypatch.setattr(worker, "hard_deadline", record_deadline)
    monkeypatch.setattr(worker.queue, "claim_next", lambda *_args, **_kwargs: item)
    monkeypatch.setattr(worker.ops, "heartbeat", lambda *_args: None)
    monkeypatch.setattr(
        worker,
        "process",
        lambda *_args: WriteResult(commit_sha="abc", change_id=None),
    )
    monkeypatch.setattr(
        worker.queue,
        "finish_landed",
        lambda *_args, **_kwargs: {"status": schema.LANDED, "report": {}},
    )

    _row, outcome = worker.process_next(
        object(),
        SimpleNamespace(settings=settings),
    )

    assert outcome.status == schema.LANDED
    assert deadlines == [
        config.operation_budget_s(timeout_s=180),
        settings.visibility_timeout_s - worker.LEASE_ABORT_MARGIN_S,
    ]


def test_lease_headroom_covers_bounded_cleanup_and_terminal_database_work():
    cleanup_s = (
        2 * gitcmd.DEFAULT_GIT_TIMEOUT_S
        + 3 * worker.STATEMENT_TIMEOUT_MS / 1000
    )

    assert cleanup_s < config.VISIBILITY_HEADROOM_S - worker.LEASE_ABORT_MARGIN_S


def test_terminal_queue_transition_is_interrupted_before_lease_expiry(monkeypatch):
    settings = SimpleNamespace(
        visibility_timeout_s=worker.LEASE_ABORT_MARGIN_S + 0.02,
        max_attempts=3,
    )
    item = {"id": "00000000-0000-4000-8000-000000000001", "attempts": 1}
    monkeypatch.setattr(worker.queue, "claim_next", lambda *_args, **_kwargs: item)
    monkeypatch.setattr(worker.ops, "heartbeat", lambda *_args: None)
    monkeypatch.setattr(
        worker,
        "process",
        lambda *_args: WriteResult(commit_sha="abc", change_id=None),
    )
    monkeypatch.setattr(worker.queue, "finish_landed", lambda *_args, **_kwargs: time.sleep(1))
    monkeypatch.setattr(
        worker.queue,
        "fail_or_retry",
        lambda *_args, **_kwargs: pytest.fail("lease abort must not start another queue write"),
    )
    started = time.monotonic()

    with pytest.raises(worker.LeaseAbort):
        worker.process_next(object(), SimpleNamespace(settings=settings))

    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize(
    "error",
    (
        UnexpectedModelBehavior("invalid structured output"),
        ModelHTTPError(503, "openrouter:deepseek/deepseek-v4-flash"),
        WriterDeadline("lease budget exceeded"),
    ),
)
def test_model_run_failures_are_retryable(error):
    assert worker._retryable(error) is True


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (409, True),
        (425, True),
        (429, True),
        (500, True),
        (503, True),
    ),
)
def test_model_http_retry_matrix(status, expected):
    error = ModelHTTPError(status, "openrouter:deepseek/deepseek-v4-flash")

    assert worker._retryable(error) is expected
