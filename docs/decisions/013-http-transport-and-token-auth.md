# ADR 013 — HTTP transport, per-user bearer-token auth, and cloud staging

**Status:** accepted · 2026-07-20

## Context

There was one MCP server enforcing the page contract and ACL server-side ([ADR 010](./010-acl.md)),
but it only spoke stdio: one process, one `--identity` flag, one machine. `--identity` is an
honor-system flag, which is acceptable for a single local operator and nothing else; the pilot
needs a handful of technical testers reaching the same server from their own machines with their
own identity, each attributed. Three gaps close together, because each is unsafe without the
others: no network transport, no real per-request identity, no audit trail — and every call has to
be attributable to a person.

## Decision

1. **Transport: MCP streamable HTTP**, added beside stdio (`stigmergy-server --transport http
   --host --port`), not instead of it. `mcp_server.build_mcp(service)` is shared, unmodified code
   for both transports — the SAME tool closures run either way, which is how HTTP inherits the
   service-layer rate limiting/audit "for free" and how stdio stays byte-for-byte unaffected.

2. **Auth: per-user bearer tokens, not OAuth, not Cloudflare Access.**
   `Authorization: Bearer <token>` → SHA-256 hex → a server-side token store (a deploy secret,
   `{"<sha256hex>": "<email>"}`) → email → `ops/identities.json`, keyed by email → audiences, via
   the SAME `identity.resolve_audiences` stdio already used. The identity seam pays off here
   exactly as designed: `identity.py` gained a per-request half (`hash_token`, `load_token_store`,
   `resolve_email_for_token`) beside the startup-flag half, and `acl.py`/`visible()` did not change
   at all.

   Rejected: Google OAuth — there is no real Workspace tenant to authenticate against yet, and it
   would need either a dynamic-client-registration proxy or a FastMCP 2.x migration: new security
   surface disproportionate to what the pilot needs. It stays the explicit later target.
   Cloudflare Access degenerates to per-tester service tokens anyway — this option, with more
   moving parts.

3. **Fail-closed on every step, generic on the wire**: a missing token, an unrecognized hash, an
   email absent from `identities.json`, or a malformed identities/token-store file all collapse to
   ONE HTTP response — `401 {"error": "unauthorized"}` — logged with the real reason server-side
   only. The identity-enumeration affordance `resolve_audiences` gives a local operator
   (`unknown identity 'x' (known: ...)`, useful on a CLI's own stderr) never crosses the HTTP
   boundary.

4. **One shared FastMCP app, not one per identity — and it MUST be built `stateless_http=True`.**
   `build_mcp(service)` is called exactly once (stdio's own test contract, `test_mcp_adapter.py`,
   requires this). Rather than building a separate Starlette app (and separately driving its
   `StreamableHTTPSessionManager` lifespan) per identity, `transport_http._ScopedServiceProxy`
   stands in for the concrete `BrainService`: each attribute access forwards to whichever
   `BrainService` the auth middleware resolved, via the `_current_service` contextvar.

   That design is only TRUE under `stateless_http=True` — caught in review before ship, because the
   original draft of this ADR claimed correctness under FastMCP's DEFAULT stateful mode, which was
   wrong. Stateful mode spawns the session's message-dispatch task ONCE, on the request that
   creates the session (the one returning an `mcp-session-id` header); every later request against
   that session ID is proxied into that ALREADY-RUNNING task, which keeps executing inside whatever
   `contextvars.Context` was captured at its creation — a LATER request's `_current_service.set(...)`
   (the auth middleware still runs it, correctly, every time) lands in a context the dispatch task
   never reads again. Two concrete holes that opened: (a) a caller with their OWN valid token, but
   presenting someone else's `mcp-session-id`, would execute — and be audited — under the session
   creator's identity/ACL scope, a session-hijack shape; (b) stateful sessions have no idle timeout
   by default and `initialize` isn't rate-limited pre-auth, so nothing bounds how many persistent
   dispatch tasks accumulate (an unbounded-task DoS). `build_http_app` now passes
   `stateless_http=True` to `build_mcp` (an explicit opt-in kwarg, default `False`, so stdio —
   which never touches `streamable_http_app()` at all — is provably unaffected): every HTTP request
   gets a fresh, request-scoped dispatch task instead, no `mcp-session-id` is ever handed out, and
   `_current_service`'s value — set immediately before `await self.app(...)`, same coroutine, no
   task hand-off — is exactly what the new per-request task inherits (`asyncio`/`anyio` both copy
   the CURRENT context at spawn time). See `transport_http.py`'s module docstring for the full
   mechanism.

   Sharing one Postgres connection and one query embedder across every identity is safe because
   FastMCP invokes a sync tool body directly on the event loop, never via a thread pool (verified
   against the installed `mcp` package) — more precisely, the actual invariant is that no DB
   helper here holds a cursor open across an `await` (`ask` is async and awaits the LLM BETWEEN
   its own read calls, never mid-cursor). Any later change that adds a connection pool or a
   concurrent write path must preserve that invariant explicitly rather than relying on "no thread
   pool" alone.

5. **Rate limiting is an HTTP-side budget, not a stdio one.** A process-wide `RateLimiter`
   (token bucket, 30 req/min overall + 10 req/min for `ask`) is wired into every per-request
   `BrainService` on the HTTP path. `build_service` (stdio) deliberately does NOT wire one: the
   30/10 budgets exist to protect the model spend behind a PUBLIC url — a local operator over stdio
   already has unmediated Postgres/OpenAI access via other CLIs (`stigmergy-search`,
   `stigmergy-index`), so limiting stdio adds friction without closing any new exposure, and the
   pre-existing `test_rebuild_while_serving.py` hammer test assumes unlimited local throughput.

6. **Audit is a service-layer wrapper, both transports.** `BrainService._call` / `.call_async`
   wrap every entry point (`search`, `read_page`, `list_entities`, `describe_entity`, and — via
   `call_async`, called from `mcp_server.py`'s `ask` closure — `ask` itself) with a rate-limit
   check (if a limiter is wired) and an `audit_log` write, attributed to `self.identity`. Both are
   duck-typed and default to `None` (no enforcement), so every caller that builds a `BrainService`
   directly (`tests/server/conftest.py::make_service`) keeps working unchanged.
   `audit_log` lives in the same Postgres as the index (`CREATE TABLE IF NOT EXISTS` at
   startup, both transports) — no separate migration tool. An audit-write failure is logged
   loudly and swallowed: the read or answer the caller asked for still ships.

7. **Deploy target: Fly.io**, not Cloud Run or a VM behind a mesh VPN: one small container +
   HTTPS + secrets, no cloud bootstrap, no machine to administer. Single machine
   (`min_machines_running = 1`) — no distributed rate limiting or sticky-session concerns are owed
   until the pilot outgrows one instance. `ops/identities.json` is baked into the image at deploy
   time (`scripts/deploy_staging.sh` copies it from a sibling knowledge-repo checkout into the
   TRACKED `deploy/` directory just before `fly deploy`, restoring that directory's committed empty
   defaults on every path out through an `EXIT` trap) — the versioned file in the knowledge repo
   remains the single source of truth; this repo never stores its content.

8. **The R2 bucket is provisioned ahead of its first consumer.** The evidence plane
   ([ADR 014](./014-capture-queue-and-attribution.md)) is what uses it, and standing the bucket
   and its scoped credentials up early — by hand, deliberately — means the write path is never
   blocked on a cloud resource. Nothing in this repo creates one: what ships is a runbook section
   and `scripts/r2_smoke.py`, a put+get+delete check proving that scoped credentials for an
   already-provisioned bucket work end to end.

9. **The nightly index rebuild lives in this repo**, not in the knowledge repo: the code, the
   dependencies and the `stigmergy-index` CLI already live here.
   `.github/workflows/index-rebuild.yml` checks out the knowledge repo read-only via a
   fine-grained PAT secret and runs the CLI unchanged against a DSN secret — the keyless test CI
   (`ci.yml`) is untouched, and no index-builder code is touched either.

   **Reversed 2026-08-07, when this repository became public.** D9's premise was that the code
   lives here and the data lives there, so the job should run where the code is. Publishing added a
   term the original decision could not weigh: **Actions logs on a public repository are readable
   by anyone**, and these jobs narrate the corpus — the gardener prints entity ids and page paths,
   and repository *variables* are not masked at all. The three operational crons therefore run from
   the knowledge repo, which is private; this repo keeps the workflow files as adopter templates,
   disabled. The premise survives the reversal intact, because `pip` closes the gap D9 was avoiding:
   the CLI is installed from this repo's published tree rather than copied, so no code moved. Two
   things improve — the cross-repo read-only PAT loses its last reader (the workflow's own
   `GITHUB_TOKEN` covers a checkout of its own repository), and the platform version the crons run
   is pinnable. See the runbook's Actions section.

## Consequences

- The identity seam ([ADR 010](./010-acl.md)) is proven exactly as designed: per-request token
  resolution slots in beside the startup flag with zero changes to `acl.py`/`visible()` or to the
  enforcement inside `search`/`read_page`.
- Keying `ops/identities.json` by email rather than by name is transparent to
  `resolve_audiences` — a name and an email are both just string keys — so the ONLY place that
  changes is the file's content (an ops step in the knowledge repo) and the fixture data the test
  suite builds by hand.
- `stigmergy.server` gains two new leaf modules (`ratelimit.py`, `audit.py`) and one new
  transport module (`transport_http.py`); none of the three import `stigmergy.answer`
  (`tests/test_architecture.py` gates this for every file under `stigmergy/server/` except
  `mcp_server.py`, which already carries the `ask` import).
- Every stdio call now writes an audit row too — "every call attributable" is not scoped to HTTP —
  so stdio gets accountability without gaining a rate limit.

## Alternatives rejected

- **A FastMCP/Starlette sub-app per identity** — correctness-equivalent, but means manually
  driving N `StreamableHTTPSessionManager` lifespans instead of the one uvicorn already runs
  for the top-level app; real extra complexity for a single-machine staging deployment with a
  handful of testers. Revisit only if the shared-connection assumption (§4 above) stops holding —
  e.g. if a future change offloads sync tool bodies to a thread pool.
- **A Postgres connection pool** — not needed at this concurrency; the single shared,
  autocommit connection is safe under FastMCP's current invocation model (see §4) and adding a
  pool now would be complexity the traffic doesn't justify.
- **Rate-limiting stdio too** — rejected in §5 above; the existing rapid-fire stdio test and the
  differing threat model (public URL vs. local operator) both argue against it.
- **A `[[http_service.checks]]` HTTP health check in `fly.toml`** — every HTTP path on this app
  goes through the fail-closed bearer-auth middleware by design, so an unauthenticated health
  probe would always 401 and Fly would flap the machine; the default passive TCP check is
  enough for a single-machine staging app.

## Amendment (2026-07-25) — Host-header allowlisting (pilot bug)

Every real client at the real deployed URL got `421 Misdirected Request` before auth or any tool
ever ran. Root cause, confirmed against the `mcp` SDK (1.28.1 at the time; the dependency is
major-pinned `mcp>=1.28,<2`):
`FastMCP.__init__` auto-builds a `TransportSecuritySettings` (DNS-rebinding protection) whenever
its OWN `host` constructor parameter — unrelated to uvicorn's real bind host, defaulted to
`"127.0.0.1"` and never overridden by `build_mcp` — is a localhost spelling, allowlisting exactly
`127.0.0.1`/`localhost`/`::1`. The real deployed hostname matches none of them, so the SDK's own
`TransportSecurityMiddleware` rejected every real request inside the transport's request
handler — after `_BearerAuthMiddleware` (the outermost layer) had already accepted the token,
but before any tool ran, which is why the pilot saw authenticated clients fail with 421 and no
audit row (`tests/server/test_host_header.py` reproduces this against the real production
wiring).

Fix: `build_mcp` gained an optional `transport_security` passthrough (`None` for stdio — inert,
byte-identical to before); `transport_http.build_http_app` now constructs an explicit
`TransportSecuritySettings` mirroring the SDK's own localhost defaults, PLUS the real host(s)
read from `$STIGMERGY_PUBLIC_HOST` (comma-separated, a plain non-secret `fly.toml` `[env]` value).
DNS-rebinding protection stays ON either way — this was a configuration gap (nobody told the SDK
what the real host is), never a reason to weaken or disable the check itself. Unset
`STIGMERGY_PUBLIC_HOST` reproduces the exact localhost-only behavior, so local dev and every
pre-existing test are unaffected.

A second, independent precondition surfaced while verifying the fix against the reproduction
test: without `json_response=True` (→ the SDK's `is_json_response_enabled`), every streamable-
HTTP response — success or failure, regardless of what the client's `Accept` header offers — is
SSE-framed, never plain JSON; there is no per-request content negotiation, only this server-wide
flag. The real MCP client SDK (`streamablehttp_client`) parses either transparently, so this was
invisible to every passing test that used it, but a raw `httpx.post(...).json()` caller (the
reproduction test itself, and any future health probe or `curl`) needs it. Added the same way,
opt-in via `build_mcp`, `True` only for the HTTP transport.

Sibling gaps this amendment does NOT close (flagged, not fixed — out of this fix's scope):
`allowed_origins` only ever gets an `https://` entry per configured host, so a browser-based MCP
client served from a DIFFERENT origin (e.g. a web UI, deliberately out of scope here but not
forever) would need its own origin added; and `serve_http`'s `uvicorn.run(...)` does not pass
`proxy_headers`/`forwarded_allow_ips`, so `request.client`/scheme as this app sees them are
whatever Fly's proxy sends, unvalidated against a trusted-proxy list — worth a look if any
future logic ever branches on the caller's IP or `X-Forwarded-*` (nothing does today).
