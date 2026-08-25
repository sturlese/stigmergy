"""The MCP protocol harness: spawn the real `stigmergy-server` console entry point as a subprocess
and drive it over stdio with a real MCP client — the transport layer is part of the contract, so
nothing here is an in-process shortcut. Skips without postgres."""
import asyncio
import json
import time

import pytest

from tests.server.conftest import call_json, mcp_session


def _run(coro):
    return asyncio.run(coro)


# ── the two stdio-level witnesses below — the closed MCP tool surface, and per-identity ACL
# scoping — are properties of `search_brain`/`read_page`/`ask`, and this file's own docstring is
# explicit that "the transport layer is part of the contract". An in-process mock
# (`tests/server/test_mcp_adapter.py`) cannot stand in for a real `stigmergy-server` subprocess
# speaking real stdio, because it cannot prove the ACTUAL entry point mounts the same tools, or
# that a real per-connection identity resolution reaches every read tool rather than only the one
# an in-process test happened to call. `ask` keeps its own dedicated real-protocol coverage in
# `tests/server/test_ask_mcp.py`.
def test_server_exposes_the_read_tools_and_ask_over_stdio(indexed):
    _, fx = indexed

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            tools = (await session.list_tools()).tools
            names = {tool.name for tool in tools}
            assert names == {"search_brain", "read_page", "list_entities", "describe_entity",
                             "ask", "brain_submit", "brain_submissions", "brain_delete"}
            submit = next(tool for tool in tools if tool.name == "brain_submit")
            assert set(submit.inputSchema["properties"]) == {
                "text", "path", "url", "title", "occurred_at", "audience", "resolution_of"
            }
            assert "resolution_of" not in submit.inputSchema.get("required", ())
            # exactly eight tools and no more. The in-process mirror of this same closed set is
            # `test_mcp_adapter.py::test_the_mounted_tool_list_is_exactly_the_eight_supported_tools`
            # — that one proves `build_mcp()`'s own output; this one proves the REAL entry point
            # mounts the same set over the wire.
    _run(go())


def test_brain_delete_answers_over_the_protocol_and_never_with_a_class_name(indexed):
    """**Found on the deployment, not in the suite** (#132), and kept because the class of defect
    outlived its cause.

    OLD BEHAVIOUR: `brain_delete` cloned, ran a model over every referring page, scanned, linted
    and pushed INSIDE the call. FastMCP drives a sync tool on the event-loop thread, so the sweep
    writer's `asyncio.run` raised `RuntimeError: cannot be called from a running event loop`, and
    the first real call over HTTP answered `brain_delete failed (RuntimeError)`. Every unit and
    Postgres test passed, because every one of them called the service from an ordinary thread.
    The fix ran the whole sequence in a worker thread.

    That sequence is no longer here at all: the tool authorizes and QUEUES, and the librarian
    worker does the writing, so nothing on this path can await, clone or push and the
    worker-thread hop went with the work. What survives is the property the fix was actually
    defending, which no unit test can reach: driving the REAL server over the REAL transport, a
    caller gets a caller-facing sentence rather than a class name. A tool that reaches an
    unhandled exception is a tool whose behaviour on the deployment nobody has seen."""
    _, fx = indexed

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            out = await call_json(session, "brain_delete",
                                  paths=["wiki/notes/Whatever.md"], why="a reason")
            blob = json.dumps(out)
            assert "Error" not in blob and "Exception" not in blob, (
                f"brain_delete answered with a class name over the protocol: {blob}")
            # An unrestricted identity, so it is queued rather than refused — and the queue
            # acknowledgement is what a caller reads back through `brain_submissions`.
            assert out.get("id"), blob
            assert out.get("status") == "queued", blob

    _run(go())


def test_a_scoped_identity_is_refused_by_name_over_the_protocol(indexed):
    """The benign twin's opposite half: the refusal must also arrive as a sentence. Removal is the
    unrestricted identity's act, and a scoped caller has to be told so in words they can act on —
    over the transport, where the whole stack is in play."""
    _, fx = indexed

    async def go():
        async with mcp_session(fx, fx.ANA) as session:
            out = await call_json(session, "brain_delete",
                                  paths=["wiki/notes/Whatever.md"], why="a reason")
            assert out.get("error"), json.dumps(out)
            assert "Error" not in out["error"] and "Exception" not in out["error"], out["error"]

    _run(go())


def test_the_read_tools_route_through_the_protocol(indexed):
    _, fx = indexed

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            search = await call_json(session, "search_brain", query="quarterly revenue")
            assert search["hits"] and search["built_at"]
            assert search["embedding_model"] == "fake-hashed-bow-256"
            assert all({"factors", "score", "arms"} <= set(h) for h in search["hits"])

            page = await call_json(session, "read_page", path=fx.OPEN_PAGE)
            assert "UNTRUSTED-DATA" in page["body"] and page["title"]
    _run(go())


def test_same_question_two_identities_get_different_realities(indexed):
    """Over the real protocol: identity A (finance) finds the ACL'd page and reads it fully;
    identity B (eng), the same two calls, gets a reality without it — search omits it, and
    read_page cannot distinguish it from a page that does not exist.

    This is exactly the property `tests/server/test_service_acl.py` already proves in-process
    against a real `BrainService` — proven here over the REAL wire instead (two real stdio
    sessions, two real identities), which is the whole reason this file exists rather than that
    one (see the module docstring)."""
    _, fx = indexed
    q = "acme payroll total compensation"

    async def go():
        async with mcp_session(fx, fx.ANA) as a:   # finance
            a_search = await call_json(a, "search_brain", query=q)
            a_read = await call_json(a, "read_page", path=fx.ACME_PAGE)
        async with mcp_session(fx, fx.ENG) as b:    # eng: not in the finance audience
            b_search = await call_json(b, "search_brain", query=q)
            b_read = await call_json(b, "read_page", path=fx.ACME_PAGE)
            b_ghost = await call_json(b, "read_page", path="wiki/finance/does-not-exist.md")
        return a_search, a_read, b_search, b_read, b_ghost

    a_search, a_read, b_search, b_read, b_ghost = _run(go())
    # A (finance) sees it fully
    assert any(h["path"] == fx.ACME_PAGE for h in a_search["hits"])
    assert "UNTRUSTED-DATA" in a_read["body"]
    # B (eng) lives in a reality without it — and cannot even tell it exists
    assert not any(h["path"] == fx.ACME_PAGE for h in b_search["hits"])
    assert set(b_read) == set(b_ghost) == {"error"}


def test_filters_roundtrip_over_mcp(indexed):
    """Structured params survive the MCP boundary."""
    _, fx = indexed

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            out = await call_json(session, "search_brain", query="revenue",
                                  filters={"entity": fx.INITECH_ID},
                                  max_results=3)
            assert out["hits"] and all(fx.INITECH_ID in h["entity"] for h in out["hits"])
            assert len(out["hits"]) <= 3
            # an unknown filter comes back as a clean error, not a crash
            bad = await call_json(session, "search_brain", query="x", filters={"body": "nope"})
            assert "error" in bad and "unknown filter" in bad["error"]
    _run(go())


# ── capture over the REAL stdio protocol: submit end to end, and stdio attribution ─────────────
def test_brain_submit_and_brain_submissions_round_trip_over_stdio(indexed):
    _conn, fx = indexed
    material = f"stdio harness capture {time.monotonic_ns()}"

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            ack = await call_json(
                session,
                "brain_submit",
                text=material,
                title="stdio harness",
            )
            listed = await call_json(session, "brain_submissions", limit=200)
            return ack, listed
    ack, listed = _run(go())

    assert ack["status"] == "queued"
    assert ack["submitted_by"] == fx.STEWARD             # the --identity name, resolved over stdio
    assert isinstance(ack["id"], str)

    row = next(r for r in listed["submissions"] if r["id"] == ack["id"])
    assert row["mine"] is True
    assert row["submitted_by"] == fx.STEWARD
    assert material not in json.dumps(row)


def test_postgres_down_fails_the_mcp_handshake_promptly_not_a_hang(fixture):
    """The same posture at the real protocol boundary (companion to
    test_startup.py::test_postgres_unreachable_exits_cleanly, which checks `main()`'s stderr
    in-process): a subprocess pointed at an unreachable database prints its actionable message and
    exits (rc=2) before completing the MCP handshake. The client-facing contract this test protects
    is narrower but load-bearing: the session must fail PROMPTLY — a client spawning `stigmergy-server`
    against a down database must never hang waiting for a handshake that will never arrive. Needs no
    database (the DSN is deliberately unreachable)."""
    # Unreachable by PORT (nothing listens on 1), not by database name — the harness refuses to
    # aim a real server subprocess at any database but the test one (tests/testdb.py), and what
    # this test asserts is the handshake failing promptly, which the port already guarantees.
    bad_dsn = "postgresql://stigmergy:stigmergy@127.0.0.1:1/stigmergy_test?connect_timeout=1"

    async def go():
        async with mcp_session(fixture, fixture.STEWARD, dsn=bad_dsn) as session:
            await session.list_tools()   # never reached — initialize() itself must fail first

    started = time.monotonic()
    with pytest.raises(Exception):    # noqa: B017 — the mcp/anyio internals raise their own type;
                                       # our contract is "fails", not which exception class it is
        _run(asyncio.wait_for(go(), timeout=15))
    elapsed = time.monotonic() - started
    # "promptly" as opposed to "only because our own 15s safety net gave up" — the actionable
    # message above is printed and the process exits well under a second in practice.
    assert elapsed < 10, f"the handshake took {elapsed:.1f}s to fail — looks like a hang, not a clean error"
