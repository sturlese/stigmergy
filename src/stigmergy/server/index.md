# server — the one API: identity, ACL, the audited seam, both transports, and the webhook

Narrative reference: [`docs/reference/server.md`](../../../docs/reference/server.md) (the tool
table, the HTTP transport, the error sweep) and
[`docs/reference/navigation.md`](../../../docs/reference/navigation.md) (the entity-navigation
surface). Design records: [`docs/decisions/`](../../../docs/decisions).

The one place a caller's identity resolves to an audience scope, ACL is enforced, a rate-limit
check runs and an audit row is written — over two transports (stdio for one local operator,
streamable HTTP for remote callers sharing one process) and two directions (read:
search/read/navigate/ask; write: submit/list/reply/review). `BrainService` is the
transport-agnostic core both transports — and `stigmergy.slack`, a third caller — build and call
into. This package files nothing: a capture is queued and attributed here, and turning it into a
committed page is the librarian's, reached through the durable queue and never through an import.
`webhook.py` serves the public HTTP surface without being a transport: the one bearer-auth-exempt
path, authenticated by HMAC instead.

## Modules

| Module | What it is — what to reuse, what to avoid |
|---|---|
| `service.py` | `BrainService` and `_call`/`call_async`, the ONE rate-limit + audit seam every tool rides — a tool off this seam is invisible to the limiter and the audit trail. `build_service` (stdio) and `open_scoped_resources` (the shared conn+embedder build every transport wires through). Also: the audit shapers, `check_arg_length`/`MAX_ARG_CHARS`, `fetch_page_raw` (the one fetch+ACL+sanitize base), `scoped_entities` (the one entity-existence rule), the `neutralize_fence`/`fence` and `SLACK_DOOR` re-exports, `UnavailableEmbedder` |
| `mcp_server.py` | The FastMCP tool closures BOTH transports share — `search_brain` · `read_page` · `list_entities` · `describe_entity` · `brain_submit` · `brain_submissions` · `review_queue` · `review_decide` · `brain_reply` (mounted under `capture_schema.REPLY_TOOL`, never the function's own name) · `ask` — plus the `stigmergy-server` entry point and `_dsn_location`. Tool docstrings are the client-visible contract: leave them byte-identical unless the contract changes |
| `review.py` | The review lane: `review_queue`/`review_decide`/`review_decide_safe`, the shared base `_collect_open_items`, the authorization predicates (`_is_steward`, `_guard_*`), the append-only `review_decisions` ledger, the governed mint seam (`_mint_entity_proposal` → `entities.remote.mint_via_clone`, reached as a module attribute), and the doorbell's read side (`items_for_doorbell`, `load_stewards`, `resolve_stewards_for_scope`, `record_undeliverable`) |
| `transport_http.py` | The streamable-HTTP transport: `_BearerAuthMiddleware` (raw ASGI), `_ScopedServiceProxy` + the `_current_service` contextvar, the request-body cap, the DNS-rebinding allowlist (`$STIGMERGY_PUBLIC_HOST`), `build_http_app`/`serve_http`, `token_store_from_env`, the webhook mount, and the admin console's ASGI branch |
| `webhook.py` | `POST /webhook/github`: HMAC over the RAW body, then a two-phase incremental `pages_index` upsert (`process_push`), split-chain supersession propagation included. Reuses `corpus.page_row` and `store.upsert_pages`/`delete_pages` — never write a second `pages_index` path |
| `identity.py` | `resolve_audiences` (the file-backed resolver both transports use) and the per-request half: `hash_token` · `load_token_store` · `resolve_email_for_token`. Fail-closed on every step |
| `acl.py` | `visible(acl, audiences)` — the ONE visibility rule, with its fail-closed truth table — and `all_visible` (all-or-nothing for text composed from several pages). Never re-implement label matching |
| `entity_aliases.py` | The plain-file reader over `ops/entity-registry.json`: one loader behind `load_aliases`/`load_registry`, `resolve_entity` (longest whole-word alias inside a question), `resolve_exact` (this string names one entity). Never read the registry a second way |
| `audit.py` | `audit_log`'s DDL and `AuditWriter` — one row per tool call, both transports; a write failure never fails the serving call |
| `ratelimit.py` | `RateLimiter` — per-identity token buckets: overall 30/min plus a stricter 10/min for `ask`; injectable clock. A new expensive tool is one entry in `_extra`, never a new branch in `check` |
| `settings.py` | `Settings.from_args` — the ONE place flags and env fallbacks are read; no module here reads the environment at import time |
| `errors.py` | `StigmergyServerError` and its subclasses (`IdentityError`, `StartupError`, `CapabilityUnavailableError`, `RegistryError`, `RateLimitError`); library code never raises `SystemExit` |
| `pilot_report.py` | `stigmergy-pilot-report` — the measurement table from columns other code already writes. Reads only, no DDL: it works under a read-only database role |
| `issue_token.py` | `stigmergy-issue-token <email>` — prints the plaintext bearer token once and the sha256 store line |

## Consumers (one-way, always — nothing here imports any of them)

| Consumer | Reaches |
|---|---|
| `answer` | `service.BrainService` (`search`, `read_page`, `describe_entity`, `fetch_page_raw`, `scoped_entities`), `service.fence`/`neutralize_fence` |
| `slack` | `service.open_scoped_resources`/`BrainService`/`SLACK_DOOR`, `identity.resolve_audiences`, `audit`, `ratelimit`, `settings.Settings`, `errors`, `review` (the doorbell reads, `review_decide_safe`) |
| `admin` | `review` (`record_decision` and the review reads), `pilot_report`, `identity.hash_token`, `webhook.JOB_NAME`, `errors` |
| `gardener` | `errors`, `acl.visible`/`all_visible`, `review` |
| `digest` | `errors`, `acl.visible`, `review` (it reads the decisions ledger directly) |

## Invariants

- Every read goes through `BrainService`; never query Postgres from an MCP tool closure — the
  service is the only place ACL filtering and the page contract are enforced.
- `visible()` runs BEFORE every cap and truncation (the search candidate pool, `_capped`'s
  `NAV_CAP`): an out-of-scope row never steals a slot and never leaks via a count.
- Unauthorized reads as nonexistent, byte-identically: `read_page`'s unknown-page shape,
  `describe_entity`'s absence shape, `NO_REPLY_WAITING`, `NOT_YOURS_TO_DECIDE`. Specific refusals
  surface only to an already-authorized caller.
- Never echo an unanticipated exception's message from a tool closure — class name only; the one
  exemption is `check_arg_length`'s rejection, marked `is_arg_length_error` (a bare
  `except ValueError` would also echo `pydantic_core.ValidationError` content).
- The webhook's bearer-auth exemption is an EXACT string match on `webhook.WEBHOOK_PATH` — never
  a prefix, never a regex, never a second route — and that route never touches
  `_current_service` or resolves an identity. The admin console is an ASGI branch in front, not a
  second exemption.
- `stateless_http=True` is mandatory on the HTTP path: FastMCP's stateful mode runs later
  requests inside the session creator's frozen context — a presented `mcp-session-id` would
  execute and be audited under that identity's scope.
- HTTP shares one connection, embedder, `RateLimiter` and `AuditWriter` across every identity —
  budgets are per-identity across the process. No DB helper holds a cursor across an `await`,
  and no sync tool body may perform blocking network or subprocess I/O.
- `submitted_by`, `verification`, `acl` and `content_hash` are declared on `brain_submit` ONLY to
  be refused (FastMCP drops undeclared fields silently); `brain_submit` is pinned to
  `MCP_SUBMIT_KINDS`, never `capture_schema.KINDS`; the trusted provenance hints are
  door-gated (`SLACK_DOOR`).
- `review_decide` is Postgres-only for `reject` and every `parked-capture` verdict; the ONE git
  path is an entity-proposal `approve`, through `_mint_entity_proposal` and nothing else.
- Steward resolution fails closed: `_is_steward` returns False without a checkout or a baked
  snapshot; `load_stewards` reads `origin/main`'s fresh tip wherever a checkout exists, and the
  same read decides both doorbell delivery and decision authority.
- `audit_log.result` is a per-tool outcome SUMMARY, never a transcript; `args` carries caller
  content verbatim for exactly two fields (`ask.question`, `search_brain.query`) — the named
  exemption stated in `audit.py`'s docstring.
- Entity-first resolution changes result ORDER, never membership, and fires only when the caller
  passed no explicit `entity` filter (key presence, not truthiness); `entity_hint` is told to the
  ranker, never re-inferred inside it.
- Both body caps are enforced before buffering: `transport_http.MAX_REQUEST_BODY_BYTES` on the
  bearer path, `webhook.MAX_BODY_BYTES` on the webhook path (refused before the HMAC ever runs).
- Every startup ensures the audit, capture and review schemas behind
  `capture.schema.startup_ddl_lock`: `IF NOT EXISTS` is a check, not a lock.
- `review.py` imports `service` lazily inside functions (`service.py` imports it at module
  scope); `mcp_server.py`'s `ask` closure imports `stigmergy.answer` the same way — the reverse
  edges must never be taken at import time.

Tests live in `tests/server/`; this package's import edges — the librarian/entities allowlists,
the ACL-reader rule, the slack one-way edge — are pinned in `tests/test_architecture.py`.
