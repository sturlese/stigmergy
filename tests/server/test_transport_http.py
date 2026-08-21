"""The HTTP transport (ADR 013): a REAL uvicorn server, a REAL MCP `streamablehttp_client`,
driving the exact production wiring (`transport_http.build_http_app`). Local-only, offline: no
Fly, no Supabase, no R2, no OpenAI key (`ask` runs `ANSWER_LLM=fake`). Skips without postgres,
same posture as the rest of `tests/server/`.

The property this surface owes: a real identity resolves to audiences through the mapping file;
two identities asking the same question get different results; audit attributes each call.
"""
import asyncio
import json
import time

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from stigmergy.server.audit import ensure_audit_table
from tests.server.conftest import (
    build_test_http_app,
    call_json,
    evidence_store_of,
    issue_test_token,
    rate_limiter_of,
    run_http_server,
)


def _run(coro):
    return asyncio.run(coro)


async def _call_over_http(url: str, token: str, name: str, **args) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with (
        streamablehttp_client(url, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await call_json(session, name, **args)


async def _call_over_http_with_raw_header(url: str, authorization_value: str, name: str,
                                          **args) -> dict:
    """Like `_call_over_http`, but takes the raw `Authorization` header VALUE verbatim (case and
    all) — `_call_over_http` always spells the scheme `Bearer`, which cannot exercise the scheme's
    case-insensitivity."""
    async with (
        streamablehttp_client(url, headers={"Authorization": authorization_value}) as (
            read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await call_json(session, name, **args)


async def _rpc_call_tool(client: httpx.AsyncClient, url: str, token: str, name: str,
                         arguments: dict, *, msg_id: int) -> dict:
    """One raw JSON-RPC `tools/call` request over an EXISTING `httpx.AsyncClient` (so the caller
    controls connection reuse) — stateless HTTP needs no prior `initialize` handshake: every
    request is independently dispatched. Returns the decoded JSON-RPC envelope (`result`/`error`).
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
              "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
           "params": {"name": name, "arguments": arguments}}
    r = await client.post(url, json=body, headers=headers, timeout=10)
    r.raise_for_status()
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    return json.loads(r.text)


# ── token_store_from_env: the startup half — the token store is a deploy secret ────────────────
def test_token_store_from_env_prefers_inline_json(monkeypatch):
    from stigmergy.server.transport_http import token_store_from_env
    monkeypatch.setenv("STIGMERGY_TOKEN_STORE", '{"abc123": "ana@example.com"}')
    monkeypatch.delenv("STIGMERGY_TOKEN_STORE_FILE", raising=False)
    assert token_store_from_env() == {"abc123": "ana@example.com"}


def test_token_store_from_env_reads_the_file_path(monkeypatch, tmp_path):
    from stigmergy.server.transport_http import token_store_from_env
    path = tmp_path / "tokens.json"
    path.write_text('{"abc123": "ana@example.com"}', encoding="utf-8")
    monkeypatch.delenv("STIGMERGY_TOKEN_STORE", raising=False)
    monkeypatch.setenv("STIGMERGY_TOKEN_STORE_FILE", str(path))
    assert token_store_from_env() == {"abc123": "ana@example.com"}


def test_token_store_from_env_neither_set_fails_closed(monkeypatch):
    from stigmergy.server.errors import IdentityError
    from stigmergy.server.transport_http import token_store_from_env
    monkeypatch.delenv("STIGMERGY_TOKEN_STORE", raising=False)
    monkeypatch.delenv("STIGMERGY_TOKEN_STORE_FILE", raising=False)
    with pytest.raises(IdentityError, match="no token store configured"):
        token_store_from_env()


def test_identities_fixture_is_keyed_by_email(fixture):
    """`ops/identities.json` is keyed by email, not by bare name — the seam HTTP auth resolves
    through, and the invariant every peer test in this file depends on."""
    import json
    with open(fixture.identities_path, encoding="utf-8") as f:
        keys = set(json.load(f))
    assert keys == {fixture.STEWARD, fixture.ANA, fixture.ENG}
    assert all("@" in k for k in keys)




# ── the real-wire contract: the mounted tool set, and two tokens getting two realities ─────────
# Why these two are worth their runtime: `test_mcp_adapter.py` asserts the same closed tool set,
# but IN-PROCESS against a mocked BrainService with no identity resolution at all, and
# `test_service_acl.py` proves ACL scoping by calling BrainService directly. Neither can catch a
# regression in the real server's token -> identity -> per-tool wiring. These two can, and that is
# this file's whole reason for existing (see its own docstring).
def test_the_mounted_tool_set_over_real_http_is_exactly_the_ten_supported_tools(indexed):
    """The tool list, asserted at the transport boundary rather than in-process: a tool that
    `build_mcp()` mounts but the real HTTP app does not serve — or the reverse — turns this red."""
    _, fx = indexed
    token, digest = issue_test_token(fx.STEWARD)
    app = build_test_http_app(fx, {digest: fx.STEWARD})

    async def go():
        with run_http_server(app) as url:
            async with (
                streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (
                    read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert names == {
                    "search_brain", "read_page", "list_entities", "describe_entity", "ask",
                    "brain_submit", "brain_submissions", "brain_delete",
                    "review_queue", "review_decide",
                }

                search = await call_json(session, "search_brain", query="quarterly revenue")
                assert search["hits"] and search["built_at"]
                page = await call_json(session, "read_page", path=fx.OPEN_PAGE)
                assert "UNTRUSTED-DATA" in page["body"]
    _run(go())


# ── the canonical case: same question, two tokens, different realities ─────────────────────────
def test_canonical_acl_over_http_two_tokens_different_realities(indexed):
    """Unrestricted (steward) vs eng-scoped (eng): the SAME question, over the SAME running server,
    through two DIFFERENT bearer tokens — different result sets on both read tools AND `ask`'s
    citations. The whole point of per-identity scoping, proven mechanically."""
    _, fx = indexed
    steward_token, steward_hash = issue_test_token(fx.STEWARD)
    eng_token, eng_hash = issue_test_token(fx.ENG)   # eng: NOT in the finance audience
    app = build_test_http_app(fx, {steward_hash: fx.STEWARD, eng_hash: fx.ENG})
    q = "acme payroll total compensation"
    question = "what is the total compensation for acme?"

    async def go():
        with run_http_server(app) as url:
            steward_search = await _call_over_http(url, steward_token, "search_brain", query=q)
            steward_read = await _call_over_http(url, steward_token, "read_page", path=fx.ACME_PAGE)
            steward_ask = await _call_over_http(url, steward_token, "ask", question=question)

            eng_search = await _call_over_http(url, eng_token, "search_brain", query=q)
            eng_read = await _call_over_http(url, eng_token, "read_page", path=fx.ACME_PAGE)
            eng_ask = await _call_over_http(url, eng_token, "ask", question=question)
        return steward_search, steward_read, steward_ask, eng_search, eng_read, eng_ask

    steward_search, steward_read, steward_ask, eng_search, eng_read, eng_ask = _run(go())

    # unrestricted: sees it everywhere
    assert any(h["path"] == fx.ACME_PAGE for h in steward_search["hits"])
    assert "body" in steward_read
    assert steward_ask["refused"] is False and steward_ask["citations"][0]["path"] == fx.ACME_PAGE

    # scoped (eng): a different reality — never an out-of-scope page in ANY surface, and the
    # refusal never names the page it is refusing about (existence itself is scoped).
    assert not any(h["path"] == fx.ACME_PAGE for h in eng_search["hits"])
    assert eng_read == {"error": f"unknown page: {fx.ACME_PAGE}"}
    assert eng_ask["refused"] is True and fx.ACME_PAGE not in eng_ask["reason"]


# ── capture over real HTTP: token-email attribution, scoped submissions, and the rate limiter
# covering writes as well as reads ─────────────────────────────────────────────────────────────
def test_brain_submit_attributes_to_the_tokens_email_over_http(indexed):
    """The HTTP half of attribution (stdio's half is `test_mcp_harness.py`'s stdio test)."""
    _, fx = indexed
    token, digest = issue_test_token(fx.ANA)
    app = build_test_http_app(fx, {digest: fx.ANA})
    material = f"http harness capture {time.monotonic_ns()}"

    with run_http_server(app) as url:
        ack = _run(_call_over_http(url, token, "brain_submit", kind="raw", material=material))

    assert ack["status"] == "queued"
    assert ack["submitted_by"] == fx.ANA   # the TOKEN's email, never a client-supplied name


def test_brain_submit_forged_submitted_by_is_refused_over_http_with_no_row_or_blob(indexed):
    conn, fx = indexed
    token, digest = issue_test_token(fx.STEWARD)
    app = build_test_http_app(fx, {digest: fx.STEWARD})
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue WHERE submitted_by = %s",
                    ("forged-ceo@example.com",))
        before = cur.fetchone()[0]

    with run_http_server(app) as url:
        out = _run(_call_over_http(url, token, "brain_submit", kind="raw",
                                   material="forged capture over http",
                                   submitted_by="forged-ceo@example.com"))

    assert "error" in out and "submitted_by" in out["error"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue WHERE submitted_by = %s",
                    ("forged-ceo@example.com",))
        after = cur.fetchone()[0]
    assert after == before


def test_brain_submissions_two_tokens_different_scopes_over_http(indexed):
    """Over real HTTP: a scoped identity (eng) never sees another identity's (steward's)
    submissions; an unrestricted identity (steward) sees the whole queue with `mine` marking only
    its own."""
    _, fx = indexed
    steward_token, steward_hash = issue_test_token(fx.STEWARD)
    eng_token, eng_hash = issue_test_token(fx.ENG)
    app = build_test_http_app(fx, {steward_hash: fx.STEWARD, eng_hash: fx.ENG})
    steward_marker = f"steward http capture {time.monotonic_ns()}"
    eng_marker = f"eng http capture {time.monotonic_ns()}"

    with run_http_server(app) as url:
        steward_ack = _run(_call_over_http(url, steward_token, "brain_submit", kind="raw",
                                        material=steward_marker))
        eng_ack = _run(_call_over_http(url, eng_token, "brain_submit", kind="raw",
                                       material=eng_marker))
        eng_view = _run(_call_over_http(url, eng_token, "brain_submissions", limit=200))
        steward_view = _run(_call_over_http(url, steward_token, "brain_submissions", limit=200))

    assert eng_view["scope"] == "own"
    eng_ids = {row["id"] for row in eng_view["submissions"]}
    assert eng_ack["id"] in eng_ids
    assert steward_ack["id"] not in eng_ids            # eng never sees steward's submission

    assert steward_view["scope"] == "all"
    steward_by_id = {row["id"]: row for row in steward_view["submissions"]}
    assert steward_by_id[steward_ack["id"]]["mine"] is True
    assert steward_by_id[eng_ack["id"]]["mine"] is False   # visible (unrestricted), correctly unmarked


def test_brain_submit_refused_by_the_rate_limiter_over_http_creates_no_row(indexed):
    """A submit refused by the per-identity limiter creates no row and no blob, and returns the
    SAME generic refusal shape as a refused read tool.

    **Flake diagnosis** (this test once went green on one CI event and red on another, from the
    same commit). The bucket refills CONTINUOUSLY (`ratelimit.py`'s own docstring: "the clock is
    injectable... so tests can drive the bucket deterministically" — this test could not reach
    that constructor, since `build_http_app` builds its own `RateLimiter()` internally with no
    override seam). The unpinned condition was refill DURING THE SPEND LOOP, not capacity and not
    cross-test bucket sharing (a fresh `RateLimiter()` is built per `build_test_http_app` call, so
    no neighbouring test's identity shares state): at the default 30/min, the bucket refills at
    30/60 = 0.5 tokens/sec, so only 2 CUMULATIVE seconds of elapsed wall-clock time across the
    spend loop hands back a full token. The old version spent the budget with 30 SEPARATE real
    HTTP round trips, each opening a brand-new `streamablehttp_client` + `ClientSession.initialize()`
    handshake — on a loaded/virtualized CI runner, 30 such round trips summing past 2s is entirely
    plausible (and is exactly what the red run showed: the 31st call's ack was returned instead of
    a refusal).

    **The fix**: `rate_limiter_of(app)` reaches the EXACT `RateLimiter` object `build_http_app`
    wired into `_BearerAuthMiddleware` (Starlette's own `user_middleware`/`Middleware.kwargs`
    hold it by reference — not a copy, not a mock: the real, unmodified `.check()` method on the
    real object every request will consult). Spending the budget this way costs ~16 MICROSECONDS
    for all 30 calls (measured — five orders of magnitude under the 2s refill threshold), so the
    remaining wall-clock exposure shrinks from "30 real round trips must cumulatively finish
    under 2s" (fragile under CI load — this is what actually broke) to "the ONE real round trip
    this test still makes, for the call that must be refused, must finish under 2s" — verified
    directly: an artificial 1.9s delay inserted right before that one call DOES reproduce the
    original failure (confirming the ~2s boundary the math predicts), while the real, undelayed
    call consistently completes in well under a second across 20 repeated local runs. The
    property under test still goes over REAL HTTP end to end: only the SETUP (spending this
    identity's own budget) moved off the network; the refused call and the DB/evidence assertions
    after it are unchanged real HTTP + real Postgres + real MinIO."""
    conn, fx = indexed
    token, digest = issue_test_token(fx.ENG)
    app = build_test_http_app(fx, {digest: fx.ENG})
    limiter = rate_limiter_of(app)
    evidence = evidence_store_of(app)

    async def go():
        with run_http_server(app) as url:
            for _ in range(limiter.overall_per_min):     # exhaust for real, zero network I/O
                limiter.check(fx.ENG, "search_brain")
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM capture_queue WHERE submitted_by = %s", (fx.ENG,))
                rows_before = cur.fetchone()[0]
            objects_before = len(evidence.client().list_objects_v2(
                Bucket=evidence.bucket).get("Contents", []))
            refused = await _call_over_http(url, token, "brain_submit", kind="raw",
                                            material="refused by rate limit")
            return refused, rows_before, objects_before
    refused, rows_before, objects_before = _run(go())

    assert "error" in refused and "rate limited" in refused["error"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue WHERE submitted_by = %s", (fx.ENG,))
        rows_after = cur.fetchone()[0]
    objects_after = len(evidence.client().list_objects_v2(
        Bucket=evidence.bucket).get("Contents", []))
    assert rows_after == rows_before      # no row
    assert objects_after == objects_before   # no blob — the docstring's other half, now asserted


# ── the request-body cap, enforced BEFORE the body is buffered ─────────────────────────────────
# `_declared_body_length`/`_capped_receive` in transport_http.py. Real uvicorn, real oversized
# bytes on the wire — no way to fake "the server actually refused this before buffering it".
def test_oversized_declared_content_length_is_refused_with_413_before_any_row_is_created(indexed):
    from stigmergy.server.transport_http import MAX_REQUEST_BODY_BYTES
    conn, fx = indexed
    token, digest = issue_test_token(fx.ANA)
    app = build_test_http_app(fx, {digest: fx.ANA})
    oversized = b"x" * (MAX_REQUEST_BODY_BYTES + 1)

    with run_http_server(app) as url:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM capture_queue WHERE submitted_by = %s", (fx.ANA,))
            before = cur.fetchone()[0]
        r = httpx.post(url, content=oversized,
                       headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json",
                               "Accept": "application/json, text/event-stream"}, timeout=10)

    assert r.status_code == 413
    assert r.json() == {"error": "request too large"}
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue WHERE submitted_by = %s", (fx.ANA,))
        after = cur.fetchone()[0]
    assert after == before   # the body was never read, so no tool could possibly have run


def test_a_bad_token_with_an_oversized_body_still_gets_401_auth_wins_before_the_size_check(indexed):
    """The property that stops the size cap from becoming a token oracle (module docstring: "auth
    wins, body never read"): auth is resolved from HEADERS ALONE, so an invalid token is refused
    identically whether the declared body is 1 byte or far over the cap — a caller can never use
    the 413-vs-401 distinction to learn anything about whether the size gate even ran."""
    from stigmergy.server.transport_http import MAX_REQUEST_BODY_BYTES
    _, fx = indexed
    token, digest = issue_test_token(fx.STEWARD)
    app = build_test_http_app(fx, {digest: fx.STEWARD})
    oversized = b"x" * (MAX_REQUEST_BODY_BYTES + 1)

    with run_http_server(app) as url:
        r = httpx.post(url, content=oversized,
                       headers={"Authorization": "Bearer not-a-real-token",
                               "Content-Type": "application/json",
                               "Accept": "application/json, text/event-stream"}, timeout=10)

    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}   # never 413 — auth is checked first, on headers only


def test_a_chunked_body_with_no_declared_length_is_capped_mid_stream_with_no_row_created(indexed):
    """The backstop for a body that never declares `content-length` at all (module docstring: "a
    client that streams a body without saying how big it is has already opted out of being told
    before it sends"). Honestly characterized: `_capped_receive` reports `http.disconnect`, which
    surfaces as a `ClientDisconnect` INSIDE the MCP SDK's own request handling — deliberately less
    polite than the clean 413 the declared-length path returns above; this test asserts what is
    ACTUALLY true (the read is aborted and nothing downstream ever runs), not a tidier shape the
    code never promised for this path."""
    from stigmergy.server.transport_http import MAX_REQUEST_BODY_BYTES
    conn, fx = indexed
    token, digest = issue_test_token(fx.ENG)
    app = build_test_http_app(fx, {digest: fx.ENG})

    def oversized_chunks():
        chunk = b"y" * (64 * 1024)
        for _ in range(2 * ((MAX_REQUEST_BODY_BYTES // len(chunk)) + 1)):   # well past the cap
            yield chunk

    with run_http_server(app) as url:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM capture_queue WHERE submitted_by = %s", (fx.ENG,))
            before = cur.fetchone()[0]
        r = httpx.post(url, content=oversized_chunks(),
                       headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json",
                               "Accept": "application/json, text/event-stream"}, timeout=10)

    # no declared content-length reached the wire (httpx streams a generator as chunked transfer)
    assert "content-length" not in {k.lower() for k in
                                    httpx.Request("POST", url, content=oversized_chunks()).headers}
    assert r.status_code == 500          # the read was aborted mid-stream — not the clean 413
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue WHERE submitted_by = %s", (fx.ENG,))
        after = cur.fetchone()[0]
    assert after == before               # the body was never fully read; no tool could have run


def test_an_ordinary_small_submit_is_unaffected_by_the_body_cap(indexed):
    """The benign twin for the cap: it must not touch a legitimately small request — every HTTP
    submit test above proves this implicitly, this makes it explicit and named."""
    _, fx = indexed
    token, digest = issue_test_token(fx.STEWARD)
    app = build_test_http_app(fx, {digest: fx.STEWARD})

    with run_http_server(app) as url:
        ack = _run(_call_over_http(url, token, "brain_submit", kind="raw",
                                   material="a perfectly normal, small capture"))
    assert ack["status"] == "queued"


# ── fail-closed auth, from every adversarial angle ─────────────────────────────────────────────
class TestAuthAdversarial:
    def test_no_authorization_header_is_401_generic(self, indexed):
        _, fx = indexed
        token, digest = issue_test_token(fx.STEWARD)
        app = build_test_http_app(fx, {digest: fx.STEWARD})
        with run_http_server(app) as url:
            r = httpx.post(url, json={}, timeout=5)
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}

    def test_forged_token_is_401_generic(self, indexed):
        _, fx = indexed
        token, digest = issue_test_token(fx.STEWARD)
        app = build_test_http_app(fx, {digest: fx.STEWARD})
        with run_http_server(app) as url:
            r = httpx.post(url, json={}, headers={"Authorization": "Bearer not-a-real-token"},
                           timeout=5)
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}

    def test_truncated_authorization_header_is_401_generic(self, indexed):
        """'Bearer' with no token at all, a non-Bearer scheme, and the bare token with the
        'Bearer ' prefix dropped — all must collapse to the same fail-closed 401, never a crash or
        a default scope."""
        _, fx = indexed
        token, digest = issue_test_token(fx.STEWARD)
        app = build_test_http_app(fx, {digest: fx.STEWARD})
        with run_http_server(app) as url:
            for bad_header in ("Bearer", "Basic dXNlcjpwYXNz", token):
                r = httpx.post(url, json={}, headers={"Authorization": bad_header}, timeout=5)
                assert r.status_code == 401, f"header {bad_header!r} did not 401"
                assert r.json() == {"error": "unauthorized"}

    def test_token_whose_email_is_absent_from_identities_json_is_401_never_a_default_scope(
            self, indexed):
        """A token store entry can point at an email that legitimately hashes/authenticates but
        has no row in `ops/identities.json` (e.g. revoked from identities, token not yet
        revoked) — this must fail closed, never silently fall back to an unrestricted or empty
        default scope."""
        _, fx = indexed
        token, digest = issue_test_token("ghost@example.com")   # not a key in fx.identities_path
        app = build_test_http_app(fx, {digest: "ghost@example.com"})
        with run_http_server(app) as url:
            r = httpx.post(url, json={}, headers={"Authorization": f"Bearer {token}"}, timeout=5)
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}

    def test_malformed_identities_file_at_request_time_is_401_generic(self, indexed, tmp_path):
        """The identities file is read PER REQUEST — HTTP resolves identity per request, never
        at startup — so a file that turns malformed after the server started (or was always broken)
        must refuse the request, not crash the process or open a default scope. A dedicated
        broken file, never the shared session-scoped fixture."""
        _, fx = indexed
        bad_identities = tmp_path / "identities.json"
        bad_identities.write_text("{not valid json", encoding="utf-8")
        token, digest = issue_test_token(fx.STEWARD)
        app = build_test_http_app(fx, {digest: fx.STEWARD}, identities_path=str(bad_identities))
        with run_http_server(app) as url:
            r = httpx.post(url, json={}, headers={"Authorization": f"Bearer {token}"}, timeout=5)
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}

    def test_a_second_valid_token_still_works_after_a_refused_request(self, indexed):
        """A refusal must not corrupt shared process-wide state (the contextvar, the connection):
        the very next request, this time correctly authenticated, must serve normally."""
        _, fx = indexed
        token, digest = issue_test_token(fx.STEWARD)
        app = build_test_http_app(fx, {digest: fx.STEWARD})
        with run_http_server(app) as url:
            bad = httpx.post(url, json={}, headers={"Authorization": "Bearer nope"}, timeout=5)
            assert bad.status_code == 401
            out = _run(_call_over_http(url, token, "search_brain", query="quarterly revenue"))
        assert out["hits"]

    # ── the Bearer scheme is case-insensitive (RFC 9110 §11.1), and a second Authorization
    # header is refused outright ───────────────────────────────────────────────────────────────
    def test_lowercase_bearer_scheme_is_accepted(self, indexed):
        _, fx = indexed
        token, digest = issue_test_token(fx.STEWARD)
        app = build_test_http_app(fx, {digest: fx.STEWARD})
        with run_http_server(app) as url:
            out = _run(_call_over_http_with_raw_header(url, f"bearer {token}", "search_brain",
                                                        query="quarterly revenue"))
        assert out["hits"]

    def test_mixed_case_bearer_scheme_is_accepted(self, indexed):
        _, fx = indexed
        token, digest = issue_test_token(fx.STEWARD)
        app = build_test_http_app(fx, {digest: fx.STEWARD})
        with run_http_server(app) as url:
            out = _run(_call_over_http_with_raw_header(url, f"BeArEr {token}", "search_brain",
                                                        query="quarterly revenue"))
        assert out["hits"]

    def test_duplicate_authorization_headers_is_401_generic(self, indexed):
        """A request smuggling a SECOND Authorization header is itself adversarial-shaped
        (which one would a naive `dict()` collapse to?) and must be refused outright, never
        silently resolved against whichever value happened to win."""
        _, fx = indexed
        token, digest = issue_test_token(fx.STEWARD)
        app = build_test_http_app(fx, {digest: fx.STEWARD})
        with run_http_server(app) as url:
            headers = httpx.Headers([("Authorization", f"Bearer {token}"),
                                     ("Authorization", f"Bearer {token}")])   # same token, TWICE
            r = httpx.post(url, json={}, headers=headers, timeout=5)
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}

    def test_two_different_valid_authorization_headers_is_still_401(self, indexed):
        """Not just identical duplicates — two DIFFERENT valid tokens in one request must also be
        refused (there is no principled way to pick a winner, and picking one silently would be
        exactly the ambiguity this fix closes)."""
        _, fx = indexed
        steward_token, steward_hash = issue_test_token(fx.STEWARD)
        ana_token, ana_hash = issue_test_token(fx.ANA)
        app = build_test_http_app(fx, {steward_hash: fx.STEWARD, ana_hash: fx.ANA})
        with run_http_server(app) as url:
            headers = httpx.Headers([("Authorization", f"Bearer {steward_token}"),
                                     ("Authorization", f"Bearer {ana_token}")])
            r = httpx.post(url, json={}, headers=headers, timeout=5)
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}


# ── stateless HTTP — no session identity for a token to "borrow" ───────────────────────────────
def test_initialize_response_carries_no_mcp_session_id_header(indexed):
    """The direct, observable signature of `stateless_http=True`: FastMCP's stateful (default)
    mode hands out an `mcp-session-id` on the request that creates a session; stateless mode never
    does, because every request gets its own fresh, request-scoped dispatch task."""
    _, fx = indexed
    token, digest = issue_test_token(fx.STEWARD)
    app = build_test_http_app(fx, {digest: fx.STEWARD})
    with run_http_server(app) as url:
        r = httpx.post(url, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": "test", "version": "0"}},
        }, headers={"Authorization": f"Bearer {token}",
                   "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}, timeout=5)
    assert r.status_code == 200
    assert "mcp-session-id" not in {k.lower() for k in r.headers}


def test_two_identities_on_the_same_client_connection_each_get_their_own_scope(indexed):
    """The statelessness property itself, not just its header side effect: a session-hijack-shaped
    hole in FastMCP's STATEFUL default means a later request on a reused connection can execute
    inside an earlier request's frozen identity context. Here ONE `httpx.AsyncClient` (one connection
    pool, real TCP-connection reuse to the same host) makes two independent `tools/call` requests
    with two DIFFERENT bearer tokens, back to back — each must see exactly its own ACL scope, with
    no leftover identity from the other."""
    _, fx = indexed
    steward_token, steward_hash = issue_test_token(fx.STEWARD)
    eng_token, eng_hash = issue_test_token(fx.ENG)   # eng: not in the finance audience
    app = build_test_http_app(fx, {steward_hash: fx.STEWARD, eng_hash: fx.ENG})
    q = "acme payroll total compensation"

    async def go():
        with run_http_server(app) as url:
            async with httpx.AsyncClient() as client:
                steward_out = await _rpc_call_tool(client, url, steward_token, "search_brain",
                                                {"query": q}, msg_id=1)
                eng_out = await _rpc_call_tool(client, url, eng_token, "search_brain",
                                               {"query": q}, msg_id=2)
        return steward_out, eng_out

    steward_out, eng_out = _run(go())
    steward_hits = json.loads(steward_out["result"]["content"][0]["text"])
    eng_hits = json.loads(eng_out["result"]["content"][0]["text"])
    assert any(h["path"] == fx.ACME_PAGE for h in steward_hits["hits"])       # steward: unrestricted
    assert not any(h["path"] == fx.ACME_PAGE for h in eng_hits["hits"])   # eng: never this identity's


# ── the unauthorized/unknown-identity body leaks nothing ───────────────────────────────────────
def test_unauthorized_and_unknown_identity_bodies_leak_no_identity_list_path_or_dsn(indexed):
    _, fx = indexed
    steward_token, steward_hash = issue_test_token(fx.STEWARD)
    ghost_token, ghost_hash = issue_test_token("ghost@example.com")
    app = build_test_http_app(fx, {steward_hash: fx.STEWARD, ghost_hash: "ghost@example.com"})

    with run_http_server(app) as url:
        no_token = httpx.post(url, json={}, timeout=5)
        forged = httpx.post(url, json={}, headers={"Authorization": "Bearer garbage"}, timeout=5)
        unknown_identity = httpx.post(url, json={},
                                      headers={"Authorization": f"Bearer {ghost_token}"}, timeout=5)

    forbidden_substrings = [
        fx.STEWARD, fx.ANA, fx.ENG,                 # no identity enumeration
        fx.repo, fx.identities_path,              # no filesystem path
        "postgresql://", "stigmergy:stigmergy",       # no DSN fragment/credentials
        "ghost@example.com",                      # not even the REQUESTING identity's own email
    ]
    for response in (no_token, forged, unknown_identity):
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}
        body = response.text
        for needle in forbidden_substrings:
            assert needle not in body, f"{needle!r} leaked in {body!r}"


# ── audit attributes each HTTP call to its OWN resolved identity ───────────────────────────────
def test_audit_attributes_each_http_call_to_its_own_identity(indexed):
    conn, fx = indexed
    ensure_audit_table(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM audit_log")

    steward_token, steward_hash = issue_test_token(fx.STEWARD)
    ana_token, ana_hash = issue_test_token(fx.ANA)
    app = build_test_http_app(fx, {steward_hash: fx.STEWARD, ana_hash: fx.ANA})

    async def go():
        with run_http_server(app) as url:
            await _call_over_http(url, steward_token, "search_brain", query="quarterly revenue")
            await _call_over_http(url, ana_token, "search_brain", query="acme payroll")
    _run(go())

    with conn.cursor() as cur:
        cur.execute("SELECT identity, tool FROM audit_log WHERE tool = 'search_brain'"
                    " ORDER BY id DESC LIMIT 2")
        rows = cur.fetchall()
    identities = {row[0] for row in rows}
    assert identities == {fx.STEWARD, fx.ANA}   # each call attributed to ITS OWN token's identity
