"""The MCP protocol harness: spawn the real `stigmergy-server` console entry point as a subprocess
and drive it over stdio with a real MCP client — the transport layer is part of the contract, so
nothing here is an in-process shortcut. Skips without postgres."""
import asyncio
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
            names = {t.name for t in (await session.list_tools()).tools}
            assert names == {"search_brain", "read_page", "list_entities", "describe_entity",
                             "ask", "brain_submit", "brain_submissions", "brain_delete"}
            # exactly eight tools and no more. The in-process mirror of this same closed set is
            # `test_mcp_adapter.py::test_the_mounted_tool_list_is_exactly_the_eight_supported_tools`
            # — that one proves `build_mcp()`'s own output; this one proves the REAL entry point
            # mounts the same set over the wire.
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


def test_filters_and_include_superseded_roundtrip_over_mcp(indexed):
    """Structured params survive the MCP boundary."""
    _, fx = indexed

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            out = await call_json(session, "search_brain", query="revenue",
                                  filters={"entity": "initech"}, include_superseded=False,
                                  max_results=3)
            # `entity` is a LIST — membership, not equality.
            assert out["hits"] and all("initech" in h["entity"] for h in out["hits"])
            assert all(not h["superseded"] for h in out["hits"])   # current-only dropped the old page
            assert len(out["hits"]) <= 3
            # an unknown filter comes back as a clean error, not a crash
            bad = await call_json(session, "search_brain", query="x", filters={"body": "nope"})
            assert "error" in bad and "unknown filter" in bad["error"]
    _run(go())


def test_include_superseded_default_keeps_but_demotes_the_superseded_page_over_mcp(indexed):
    """At least one test per surface runs on the DEFAULTS: the `include_superseded=True` path is
    not passed here — proving the default itself, not just the explicit True — and it keeps a
    superseded page reachable in the results while ranking it below its current counterpart. The
    test above only proves `include_superseded=False` can drop it; this proves the default demotes
    rather than silently ignoring supersession."""
    _, fx = indexed

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            out = await call_json(session, "search_brain", query="quarterly revenue",
                                  filters={"entity": "initech"}, max_results=20)
            by_path = {h["path"]: h for h in out["hits"]}
            assert fx.OPEN_PAGE in by_path and fx.SUPERSEDED_PAGE in by_path
            assert by_path[fx.SUPERSEDED_PAGE]["superseded"] is True
            assert "superseded" in by_path[fx.SUPERSEDED_PAGE]["factors"]
            assert by_path[fx.SUPERSEDED_PAGE]["score"] < by_path[fx.OPEN_PAGE]["score"]
            positions = [h["path"] for h in out["hits"]]
            assert positions.index(fx.OPEN_PAGE) < positions.index(fx.SUPERSEDED_PAGE)
    _run(go())



# ── capture over the REAL stdio protocol: submit end to end, and stdio attribution ─────────────
def test_brain_submit_and_brain_submissions_round_trip_over_stdio(indexed):
    conn, fx = indexed
    material = f"stdio harness capture {time.monotonic_ns()}"

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            ack = await call_json(session, "brain_submit", kind="raw", material=material,
                                  hints={"title": "stdio harness"})
            listed = await call_json(session, "brain_submissions", limit=200)
            return ack, listed
    ack, listed = _run(go())

    assert ack["status"] == "queued"
    assert ack["submitted_by"] == fx.STEWARD             # the --identity name, resolved over stdio
    assert isinstance(ack["id"], int)

    row = next(r for r in listed["submissions"] if r["id"] == ack["id"])
    assert row["mine"] is True
    assert row["submitted_by"] == fx.STEWARD
    # a `queued` row's excerpt is withheld until the librarian has looked — move THIS row (by id,
    # never `queue.claim_next`: `conn` is shared with every other stdio test that submits and
    # leaves a row `queued`) past the gate before checking the UNTRUSTED-DATA fence.
    from stigmergy.capture import queue, schema
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capture_queue SET status = 'claimed', claimed_at = now(), "
            "attempts = attempts + 1 WHERE id = %s AND status = 'queued' RETURNING attempts",
            (ack["id"],))
        attempts = cur.fetchone()[0]
    queue.finish(conn, ack["id"], status=schema.FILED, expected_attempts=attempts,
                result_ref="wiki/x.md")

    async def go2():
        async with mcp_session(fx, fx.STEWARD) as session:
            return await call_json(session, "brain_submissions", limit=200)
    listed2 = _run(go2())
    row2 = next(r for r in listed2["submissions"] if r["id"] == ack["id"])
    assert row2["excerpt"].startswith("<<<UNTRUSTED-DATA\n")   # fenced, once the librarian looked


def test_brain_submit_a_forged_submitted_by_is_refused_over_stdio(indexed):
    """Over the real protocol: the tool DOES declare `submitted_by` on its signature
    (so it can be refused explicitly — see `tests/server/test_mcp_adapter.py`'s adversarial pair
    for what happens to an UNDECLARED extra argument by contrast), so a real client sending it
    reaches the service and is refused with no row created for the forged identity."""
    conn, fx = indexed

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            return await call_json(session, "brain_submit", kind="raw", material="forged capture",
                                   submitted_by="ceo@example.com")
    out = _run(go())

    assert "error" in out and "submitted_by" in out["error"]
    assert "ceo@example.com" not in out["error"]   # the message never echoes the forged value
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue WHERE submitted_by = %s",
                    ("ceo@example.com",))
        assert cur.fetchone()[0] == 0


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
