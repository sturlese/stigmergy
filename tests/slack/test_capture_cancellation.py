"""Cancellation safety for an unbound Slack reaction reservation."""

import asyncio
import contextlib
import threading
from types import SimpleNamespace

import pytest

from stigmergy.slack import capture
from stigmergy.slack.identity import Resolved


def test_cancellation_after_reservation_releases_only_the_unbound_reservation(monkeypatch):
    acquisition_started = threading.Event()
    released = []

    class Connection:
        def transaction(self):
            return contextlib.nullcontext()

    class Gateway:
        async def conversations_info(self, _channel_id):
            return {"channel": {}}

        async def conversations_replies(self, _channel_id, _message_ts):
            acquisition_started.set()
            await asyncio.Event().wait()

    class Service:
        conn = Connection()

        def check_submit_audience(self, _audience):
            return ["finance"]

    class Context:
        conn = Connection()
        gateway = Gateway()
        settings = SimpleNamespace(channels_path="ignored")
        cache = SimpleNamespace()

        def with_connection(self, operation):
            return operation(self.conn)

        def run_service(self, _email, _audiences, operation, **_kwargs):
            return operation(Service())

    monkeypatch.setattr(capture.channels, "channel_scope_for_capture", lambda *_args: {"finance"})
    monkeypatch.setattr(capture, "reserve_reaction", lambda *_args, **_kwargs: "reservation-1")
    monkeypatch.setattr(capture, "bind_thread", lambda *_args, **_kwargs: pytest.fail("must not bind"))
    monkeypatch.setattr(
        capture,
        "_release_reservation",
        lambda _conn, reservation_id: released.append(reservation_id),
    )

    async def run():
        task = asyncio.create_task(
            capture.handle_reaction_added(
                Context(),
                reaction=capture.BRAIN_REACTION,
                team_id="T1",
                channel_id="C1",
                message_ts="1.0",
                slack_user_id="U1",
                identity_result=Resolved(
                    email="member@example.com",
                    audiences=frozenset({"finance"}),
                ),
            )
        )
        assert await asyncio.to_thread(acquisition_started.wait, 0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert released == ["reservation-1"]


def test_unexpected_acquisition_exception_releases_the_unbound_reservation(monkeypatch):
    released = []

    class Connection:
        def transaction(self):
            return contextlib.nullcontext()

    class Gateway:
        async def conversations_info(self, _channel_id):
            return {"channel": {}}

        async def conversations_replies(self, _channel_id, _message_ts):
            raise RuntimeError("unexpected acquisition failure")

    class Service:
        conn = Connection()

        def check_submit_audience(self, _audience):
            return ["finance"]

    class Context:
        conn = Connection()
        gateway = Gateway()
        settings = SimpleNamespace(channels_path="ignored")

        def with_connection(self, operation):
            return operation(self.conn)

        def run_service(self, _email, _audiences, operation, **_kwargs):
            return operation(Service())

    monkeypatch.setattr(capture.channels, "channel_scope_for_capture", lambda *_args: {"finance"})
    monkeypatch.setattr(capture, "reserve_reaction", lambda *_args, **_kwargs: "reservation-1")
    monkeypatch.setattr(capture, "bind_thread", lambda *_args, **_kwargs: pytest.fail("must not bind"))
    monkeypatch.setattr(
        capture,
        "_release_reservation",
        lambda _conn, reservation_id: released.append(reservation_id),
    )

    async def run():
        with pytest.raises(RuntimeError, match="unexpected acquisition failure"):
            await capture.handle_reaction_added(
                Context(),
                reaction=capture.BRAIN_REACTION,
                team_id="T1",
                channel_id="C1",
                message_ts="1.0",
                slack_user_id="U1",
                identity_result=Resolved(
                    email="member@example.com",
                    audiences=frozenset({"finance"}),
                ),
            )

    asyncio.run(run())
    assert released == ["reservation-1"]


def test_cancellation_immediately_after_reserve_commit_releases_the_unbound_reservation(monkeypatch):
    commit_started = threading.Event()
    finish_commit = threading.Event()
    released = []

    class CommitTransaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            commit_started.set()
            finish_commit.wait(timeout=1)
            return False

    class Connection:
        def transaction(self):
            return CommitTransaction()

    class Gateway:
        async def conversations_info(self, _channel_id):
            return {"channel": {}}

        async def conversations_replies(self, *_args):
            pytest.fail("cancellation must be observed before acquisition")

    class Service:
        conn = Connection()

        def check_submit_audience(self, _audience):
            return ["finance"]

    class Context:
        conn = Connection()
        gateway = Gateway()
        settings = SimpleNamespace(channels_path="ignored")

        def with_connection(self, operation):
            return operation(self.conn)

        def run_service(self, _email, _audiences, operation, **_kwargs):
            return operation(Service())

    monkeypatch.setattr(capture.channels, "channel_scope_for_capture", lambda *_args: {"finance"})
    monkeypatch.setattr(capture, "reserve_reaction", lambda *_args, **_kwargs: "reservation-1")
    monkeypatch.setattr(
        capture,
        "_release_reservation",
        lambda _conn, reservation_id: released.append(reservation_id),
    )

    async def run():
        task = asyncio.create_task(
            capture.handle_reaction_added(
                Context(),
                reaction=capture.BRAIN_REACTION,
                team_id="T1",
                channel_id="C1",
                message_ts="1.0",
                slack_user_id="U1",
                identity_result=Resolved(
                    email="member@example.com",
                    audiences=frozenset({"finance"}),
                ),
            )
        )
        assert await asyncio.to_thread(commit_started.wait, 0.5)
        task.cancel()
        finish_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert released == ["reservation-1"]


def test_cancellation_after_bound_submission_does_not_release_the_reservation(monkeypatch):
    submitted = threading.Event()
    finish_commit = threading.Event()
    released = []
    phases = []

    class CommittedSubmission:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            submitted.set()
            finish_commit.wait(timeout=1)
            return False

    class Connection:
        def __init__(self, *, submission=False):
            self.submission = submission

        def transaction(self):
            return CommittedSubmission() if self.submission else contextlib.nullcontext()

    class Gateway:
        async def conversations_info(self, _channel_id):
            return {"channel": {}}

        async def conversations_replies(self, _channel_id, _message_ts):
            return [{"ts": "1.0"}]

        async def get_permalink(self, _channel_id, _message_ts):
            return "https://example.test/thread"

        async def chat_post_ephemeral(self, *_args, **_kwargs):
            return {}

        async def chat_post_message(self, *_args, **_kwargs):
            return {}

    class Service:
        conn = Connection(submission=True)

        def check_submit_audience(self, _audience):
            return ["finance"]

        def submit_artifacts(self, **_kwargs):
            phases.append("submit")
            return {"id": "submission-1"}

    class Context:
        conn = Connection()
        gateway = Gateway()
        settings = SimpleNamespace(channels_path="ignored")
        cache = SimpleNamespace(get_display_name=lambda *_args: "Member")

        def with_connection(self, operation):
            return operation(self.conn)

        def run_service(self, _email, _audiences, operation, **_kwargs):
            return operation(Service())

        async def post_or_log(self, coro, **_kwargs):
            return await coro

    monkeypatch.setattr(capture.channels, "channel_scope_for_capture", lambda *_args: {"finance"})
    monkeypatch.setattr(capture, "reserve_reaction", lambda *_args, **_kwargs: "reservation-1")
    monkeypatch.setattr(capture, "bind_thread", lambda *_args, **_kwargs: phases.append("bind") or True)
    monkeypatch.setattr(capture, "attach_submission", lambda *_args, **_kwargs: phases.append("attach"))

    async def build_snapshot(*_args, **_kwargs):
        return b"snapshot", (), (), "1.0"

    monkeypatch.setattr(capture, "_build_snapshot", build_snapshot)
    monkeypatch.setattr(
        capture,
        "_release_reservation",
        lambda _conn, reservation_id: released.append(reservation_id),
    )

    async def run():
        task = asyncio.create_task(
            capture.handle_reaction_added(
                Context(),
                reaction=capture.BRAIN_REACTION,
                team_id="T1",
                channel_id="C1",
                message_ts="1.0",
                slack_user_id="U1",
                identity_result=Resolved(
                    email="member@example.com",
                    audiences=frozenset({"finance"}),
                ),
            )
        )
        assert await asyncio.to_thread(submitted.wait, 0.5), phases
        task.cancel()
        finish_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert released == []


def test_cancellation_during_submit_rollback_releases_the_unbound_reservation(monkeypatch):
    submit_started = threading.Event()
    finish_rollback = threading.Event()
    rolled_back = threading.Event()
    released = []

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, *_args):
            if exc_type is not None:
                rolled_back.set()
            return False

    class Connection:
        def transaction(self):
            return Transaction()

    class Gateway:
        async def conversations_info(self, _channel_id):
            return {"channel": {}}

        async def conversations_replies(self, _channel_id, _message_ts):
            return [{"ts": "1.0"}]

        async def get_permalink(self, _channel_id, _message_ts):
            return "https://example.test/thread"

    class Service:
        conn = Connection()

        def check_submit_audience(self, _audience):
            return ["finance"]

        def submit_artifacts(self, **_kwargs):
            submit_started.set()
            finish_rollback.wait(timeout=1)
            raise RuntimeError("submit rollback")

    class Context:
        conn = Connection()
        gateway = Gateway()
        settings = SimpleNamespace(channels_path="ignored")
        cache = SimpleNamespace()

        def with_connection(self, operation):
            return operation(self.conn)

        def run_service(self, _email, _audiences, operation, **_kwargs):
            return operation(Service())

    async def build_snapshot(*_args, **_kwargs):
        return b"snapshot", (), (), "1.0"

    async def post_or_log(_coro, **_kwargs):
        return None

    Context.post_or_log = post_or_log

    monkeypatch.setattr(capture.channels, "channel_scope_for_capture", lambda *_args: {"finance"})
    monkeypatch.setattr(capture, "reserve_reaction", lambda *_args, **_kwargs: "reservation-1")
    monkeypatch.setattr(capture, "bind_thread", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(capture, "attach_submission", lambda *_args, **_kwargs: pytest.fail("must not attach"))
    monkeypatch.setattr(capture, "_build_snapshot", build_snapshot)
    monkeypatch.setattr(
        capture,
        "_release_reservation",
        lambda _conn, reservation_id: released.append(reservation_id),
    )

    async def run():
        task = asyncio.create_task(
            capture.handle_reaction_added(
                Context(),
                reaction=capture.BRAIN_REACTION,
                team_id="T1",
                channel_id="C1",
                message_ts="1.0",
                slack_user_id="U1",
                identity_result=Resolved(
                    email="member@example.com",
                    audiences=frozenset({"finance"}),
                ),
            )
        )
        loop = asyncio.get_running_loop()
        cancellation_delivered = threading.Event()

        def cancel_during_submit():
            assert submit_started.wait(timeout=0.5)
            loop.call_soon_threadsafe(
                lambda: (task.cancel(), cancellation_delivered.set())
            )
            assert cancellation_delivered.wait(timeout=0.5)
            finish_rollback.set()

        helper = threading.Thread(target=cancel_during_submit)
        helper.start()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            helper.join()

    asyncio.run(run())
    assert rolled_back.is_set()
    assert released == ["reservation-1"]
