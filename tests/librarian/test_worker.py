import contextlib
import datetime as dt
import time
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from stigmergy.capture import schema
from stigmergy.capture.errors import EvidenceError, ExtractionError, QueueStateError
from stigmergy.knowledge import writer as knowledge_writer
from stigmergy.knowledge.writer import WriterDeadline, WriteResult
from stigmergy.librarian import config, gitcmd, worker
from stigmergy.librarian.errors import GitError, LibrarianConfigError, TransientGitError


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


def test_writer_recovers_a_published_commit_before_checking_the_spent_budget(monkeypatch):
    settings = config.Settings(timeout_s=180)
    deadlines = []

    @contextlib.contextmanager
    def record_deadline(seconds, _error_factory):
        deadlines.append(seconds)
        yield

    @contextlib.contextmanager
    def writer_lock(*_args, **_kwargs):
        yield True

    recovered = WriteResult(commit_sha="abc", change_id=None)
    monkeypatch.setattr(knowledge_writer.ops, "try_advisory_lock", writer_lock)
    monkeypatch.setattr(
        knowledge_writer.gitcmd,
        "base_ref",
        lambda *_args: SimpleNamespace(sha="base"),
    )
    monkeypatch.setattr(
        knowledge_writer,
        "_recover",
        lambda *_args: recovered,
    )
    monkeypatch.setattr(
        knowledge_writer,
        "_remaining_operation_budget_s",
        lambda *_args, **_kwargs: pytest.fail("a landed commit must recover before budget checks"),
    )
    monkeypatch.setattr(knowledge_writer, "hard_deadline", record_deadline)
    assert knowledge_writer.process(
        object(),
        {"id": "operation"},
        SimpleNamespace(settings=settings, repo="repo"),
    ).commit_sha == "abc"

    assert deadlines == [settings.timeout_s]


def test_writer_and_terminal_transition_fit_inside_the_queue_lease(monkeypatch):
    settings = config.Settings(timeout_s=180)
    deadlines = []

    @contextlib.contextmanager
    def record_deadline(seconds, _error_factory):
        deadlines.append(seconds)
        yield

    item = {
        "id": "00000000-0000-4000-8000-000000000001",
        "attempts": 1,
        "lease_started_at": "2026-09-24T10:00:00+00:00",
    }
    monkeypatch.setattr(worker, "_release_expired_if_writer_idle", lambda *_args: {})
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
    assert deadlines == [settings.visibility_timeout_s - worker.LEASE_ABORT_MARGIN_S]


def test_writer_budget_is_anchored_to_the_first_processing_start():
    started_at = dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.UTC)
    deadline_at = started_at + dt.timedelta(seconds=420)
    item = {"budget_deadline_at": deadline_at.isoformat()}

    remaining = knowledge_writer._remaining_operation_budget_s(
        item,
        now=started_at + dt.timedelta(seconds=75),
    )

    assert remaining == 345
    assert knowledge_writer._remaining_operation_budget_s(
        item,
        now=started_at + dt.timedelta(hours=1),
    ) == 0


def test_lease_headroom_covers_bounded_cleanup_and_terminal_database_work():
    cleanup_s = (
        2 * gitcmd.DEFAULT_GIT_TIMEOUT_S
        + 3 * worker.STATEMENT_TIMEOUT_MS / 1000
    )

    assert cleanup_s < config.VISIBILITY_HEADROOM_S - worker.LEASE_ABORT_MARGIN_S


def test_terminal_queue_transition_is_interrupted_before_lease_expiry(monkeypatch):
    settings = SimpleNamespace(
        timeout_s=180,
        visibility_timeout_s=worker.LEASE_ABORT_MARGIN_S + 0.02,
        max_attempts=3,
    )
    item = {
        "id": "00000000-0000-4000-8000-000000000001",
        "attempts": 1,
        "lease_started_at": "2026-09-24T10:00:00+00:00",
    }
    monkeypatch.setattr(worker, "_release_expired_if_writer_idle", lambda *_args: {})
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
    ("error", "expected"),
    (
        (UnexpectedModelBehavior("invalid structured output"), False),
        (ModelHTTPError(503, "openrouter:deepseek/deepseek-v4-flash"), True),
        (WriterDeadline("capture budget exceeded"), False),
        (ExtractionError("invalid document"), False),
        (EvidenceError("object store unavailable"), True),
        (httpx.ReadTimeout("provider timed out"), True),
        (httpx.ConnectError("provider unavailable"), True),
        (GitError("git rejected the update"), False),
        (TransientGitError("git transport timed out"), True),
    ),
)
def test_only_explicit_transient_failures_are_retryable(error, expected):
    assert worker._retryable(error) is expected


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


def test_writer_refuses_to_publish_after_losing_the_exact_lease(monkeypatch):
    monkeypatch.setattr(knowledge_writer.queue, "holds_lease", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        knowledge_writer.gitcmd,
        "commit",
        lambda *_args, **_kwargs: pytest.fail("a stale worker must not create a commit"),
    )

    with pytest.raises(QueueStateError, match="redelivered before publication"):
        knowledge_writer._commit_and_record(
            object(),
            SimpleNamespace(),
            worktree="repo",
            base_sha="a" * 40,
            entries=(),
            item_id="00000000-0000-4000-8000-000000000001",
            expected_attempts=1,
            expected_lease_started_at="2026-09-24T10:00:00+00:00",
            trigger="capture",
            actor="alice",
            summary="Test",
            reasons={},
        )


def test_expired_leases_are_not_reclaimed_while_a_writer_holds_the_lock(monkeypatch):
    @contextlib.contextmanager
    def busy_writer(*_args, **_kwargs):
        yield False

    monkeypatch.setattr(worker.ops, "try_advisory_lock", busy_writer)
    monkeypatch.setattr(
        worker.queue,
        "release_expired",
        lambda *_args, **_kwargs: pytest.fail("an active writer's lease must not be invalidated"),
    )

    assert worker._release_expired_if_writer_idle(object(), 900) == {
        "released": 0,
        "failed": 0,
    }
