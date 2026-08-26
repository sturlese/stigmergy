"""Characterize that Slack database work never blocks Socket Mode tasks."""

import asyncio
import threading
from types import SimpleNamespace

from stigmergy.kernel.blocking import run_blocking
from stigmergy.slack import capture
from stigmergy.slack.context import SlackContext
from stigmergy.slack.identity import Resolved


def test_capture_database_policy_work_leaves_socket_mode_responsive(monkeypatch):
    """An independent Socket Mode task must release blocked policy work without a watchdog."""
    release = threading.Event()
    watchdog_released = threading.Event()
    unrelated_ran = asyncio.Event()

    def blocking_channel_scope(*_args, **_kwargs):
        if not release.wait(timeout=0.2):
            watchdog_released.set()
        return frozenset({"finance"})

    monkeypatch.setattr(capture.channels, "channel_scope_for_capture", blocking_channel_scope)

    class Gateway:
        async def conversations_info(self, _channel_id):
            return {"channel": {}}

        async def chat_post_ephemeral(self, *_args, **_kwargs):
            return {}

    class Service:
        def check_submit_audience(self, _audience):
            raise capture.SubmitRefused("refused")

    ctx = SimpleNamespace(
        gateway=Gateway(),
        conn=object(),
        settings=SimpleNamespace(channels_path="ignored"),
        build_service=lambda *_args: Service(),
        post_or_log=lambda coro, **_kwargs: coro,
    )

    async def capture_event():
        return await capture.handle_reaction_added(
            ctx,
            reaction=capture.BRAIN_REACTION,
            team_id="T1",
            channel_id="C1",
            message_ts="1.0",
            slack_user_id="U1",
            identity_result=Resolved(email="member@example.com", audiences=frozenset({"finance"})),
        )

    async def unrelated_socket_task():
        await asyncio.sleep(0)
        unrelated_ran.set()
        release.set()

    async def run():
        captured, _ = await asyncio.gather(capture_event(), unrelated_socket_task())
        assert captured is False

    asyncio.run(run())

    assert unrelated_ran.is_set()
    assert not watchdog_released.is_set(), "Slack database policy work blocked Socket Mode"


def test_slack_listener_connection_is_owned_by_one_blocking_worker_thread():
    thread_ids = []

    class Connection:
        def close(self):
            thread_ids.append(("close", threading.get_ident()))

    def connection_factory():
        thread_ids.append(("open", threading.get_ident()))
        return Connection()

    ctx = SlackContext(
        settings=SimpleNamespace(),
        gateway=object(),
        conn=object(),
        embedder=object(),
        connection_factory=connection_factory,
    )

    def use(conn):
        assert isinstance(conn, Connection)
        thread_ids.append(("use", threading.get_ident()))

    asyncio.run(run_blocking(ctx.with_connection, use))

    assert [phase for phase, _thread_id in thread_ids] == ["open", "use", "close"]
    assert len({thread_id for _phase, thread_id in thread_ids}) == 1
    assert thread_ids[0][1] != threading.get_ident()
