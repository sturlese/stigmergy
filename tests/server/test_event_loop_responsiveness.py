"""Characterize that HTTP setup never blocks the ASGI event loop."""

import asyncio
import threading
from types import SimpleNamespace

from stigmergy.kernel.blocking import run_blocking


def test_request_connection_work_leaves_the_event_loop_responsive(monkeypatch):
    """An unrelated coroutine must release a blocked request without a watchdog rescue."""
    from stigmergy.server import ops_files
    from stigmergy.server.identity import Principal, hash_token
    from stigmergy.server.ratelimit import RateLimiter
    from stigmergy.server.transport_http import _BearerAuthMiddleware

    release = threading.Event()
    watchdog_released = threading.Event()
    unrelated_ran = asyncio.Event()

    class Connection:
        def close(self):
            return None

    def blocking_connection_factory():
        if not release.wait(timeout=0.2):
            watchdog_released.set()
        return Connection()

    monkeypatch.setattr(
        ops_files,
        "resolve_identity_principal",
        lambda _conn, _path, subject: Principal(
            subject=subject,
            display_name="Member",
            groups=(),
            default_audience=(),
        ),
    )

    async def app(_scope, _receive, _send):
        return None

    middleware = _BearerAuthMiddleware(
        app,
        settings=SimpleNamespace(identities_path="ignored"),
        token_store={hash_token("token"): "member@example.com"},
        connection_factory=blocking_connection_factory,
        embedder=object(),
        rate_limiter=RateLimiter(),
    )

    async def request():
        consumed = False

        async def receive():
            nonlocal consumed
            if consumed:
                return {"type": "http.disconnect"}
            consumed = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        await middleware(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/mcp",
                "raw_path": b"/mcp",
                "query_string": b"",
                "headers": [(b"authorization", b"Bearer token")],
                "client": ("127.0.0.1", 1),
                "server": ("localhost", 80),
            },
            receive,
            send,
        )

    async def unrelated_work():
        await asyncio.sleep(0)
        unrelated_ran.set()
        release.set()

    async def run():
        await asyncio.gather(request(), unrelated_work())

    asyncio.run(run())

    assert unrelated_ran.is_set()
    assert not watchdog_released.is_set(), "request connection work blocked the event loop"


def test_http_identity_connection_is_owned_by_one_blocking_worker_thread(monkeypatch):
    from stigmergy.server import ops_files
    from stigmergy.server.transport_http import _resolve_request_principal

    thread_ids = []

    class Connection:
        def close(self):
            thread_ids.append(("close", threading.get_ident()))

    def connection_factory():
        thread_ids.append(("open", threading.get_ident()))
        return Connection()

    def resolve(conn, _path, _email):
        assert isinstance(conn, Connection)
        thread_ids.append(("use", threading.get_ident()))
        return object()

    monkeypatch.setattr(ops_files, "resolve_identity_principal", resolve)

    asyncio.run(run_blocking(_resolve_request_principal, connection_factory, "ignored", "member@example.com"))

    assert [phase for phase, _thread_id in thread_ids] == ["open", "use", "close"]
    assert len({thread_id for _phase, thread_id in thread_ids}) == 1
    assert thread_ids[0][1] != threading.get_ident()
