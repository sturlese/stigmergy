# server — the one API: identity, ACL, the audited seam, both transports, and the webhook

Narrative reference: [`docs/reference/server.md`](../../../docs/reference/server.md) (the tool
table, the HTTP transport, the error sweep) and
[`docs/reference/navigation.md`](../../../docs/reference/navigation.md) (the entity-navigation
surface). Design records: [`docs/decisions/`](../../../docs/decisions).

The one place a caller's identity resolves to an audience scope, ACL is enforced, a rate-limit
check runs and an audit row is written — over two transports (stdio for one local operator,
streamable HTTP for remote callers sharing one process) and two directions (read:
search/read/navigate/ask; write: submit/list/delete). `BrainService` is the
transport-agnostic core both transports — and `stigmergy.slack`, a third caller — build and call
into. This package files nothing: a capture is queued and attributed here, and turning it into a
committed page is the librarian's, reached through the durable queue and never through an import.
`webhook.py` serves the public HTTP surface without being a transport: the one bearer-auth-exempt
path, authenticated by HMAC instead.

## Modules

| Module | What it is — what to reuse, what to avoid |
|---|---|
| `service.py` | `BrainService` and `_call`/`call_async`, the ONE rate-limit + audit seam every tool rides — a tool off this seam is invisible to the limiter and the audit trail. `build_service` (stdio) and `open_scoped_resources` (the shared conn+embedder build every transport wires through). Also: the audit shapers, `check_arg_length`/`MAX_ARG_CHARS`, `fetch_page_raw` (the one fetch+ACL+sanitize base), `scoped_entities` (the one entity-existence rule), `_registry_source` (the ONE place the registry's bytes are chosen — the index's snapshot, else the `--entity-registry` file — memoized until the next `_call`/`call_async` drops it, and read through `_registry_aliases`/`_registry_records` by all four registry readers so no path-bearing `ValueError` escapes), `unrestricted` (the ONE spelling of `audiences is None`, which widens a QUEUE read to every identity's rows and is never an ACL decision — page visibility is `acl.visible()`'s alone), the `neutralize_fence`/`fence` and `SLACK_DOOR` re-exports, `UnavailableEmbedder` |
| `mcp_server.py` | The FastMCP tool closures BOTH transports share — EIGHT: `search_brain` · `read_page` · `list_entities` · `describe_entity` · `brain_submit` · `brain_submissions` · `brain_delete` · `ask` — plus the `stigmergy-server` entry point and `_dsn_location`. Tool docstrings are the client-visible contract: leave them byte-identical unless the contract changes |
| `review.py` | The one thing a person still writes to the knowledge repo from this process, and nothing else since [ADR 044](../../../docs/decisions/044-the-capture-is-the-approval.md) retired the review lane and moved the repair loop into the worker: the DELETION act (`delete_pages` → `delete_and_record` → `repair.apply.apply_and_record`, the SAME door the worker's repairs go through — what differs is one field, `actor`, which puts a person's name in the commit's `Approved-by:` trailer where a derived repair carries a `Repair:` line — a person's own deletion, decided and applied in one call: `repair.deletion` plans, `repair.sweep` WRITES the pages that referred to the removed ones, the row lands `applied` with its diff and the per-page diff goes back to the caller, [ADR 043](../../../docs/decisions/043-a-sweep-is-written.md)), and `commission_registration`, the console's Register door — which touches no git at all: it queues a `raw` capture carrying `capture.schema.registration_hints`, and the librarian writes the page and births the identity confirmed by whoever asked (ADR 042). Neither sequence takes an authorization argument: `brain_delete` requires an unrestricted identity at the tool, the console sits behind its operator token, and the caller sets are pinned in `tests/test_architecture.py` |
| `transport_http.py` | The streamable-HTTP transport: `_BearerAuthMiddleware` (raw ASGI), `_ScopedServiceProxy` + the `_current_service` contextvar, the request-body cap, the DNS-rebinding allowlist (`$STIGMERGY_PUBLIC_HOST`), `build_http_app`/`serve_http`, `token_store_from_env`, the webhook mount, and the admin console's ASGI branch |
| `webhook.py` | `POST /webhook/github`: HMAC over the RAW body, a delivery-id dedupe (an already-applied `X-GitHub-Delivery` is acknowledged, never re-applied — the id is recorded inside phase 2, so a FAILED delivery stays redeliverable), then a two-phase incremental `pages_index` upsert (`process_push`), split-chain supersession propagation included. Reuses `corpus.page_row` and `store.upsert_pages`/`delete_pages` — never write a second `pages_index` path. It also refreshes the cached ops files the push carries (`ops_files_pushed`, asked of the RAW changed paths — `ops/` is in no zone), each fetched at the BRANCH ref (the replay defense: a replayed delivery installs only what the branch says now), written in the same transaction as the pages and reported in `ops_files_refreshed` — above `store.MAX_OPS_FILE_BYTES` a file is refused instead (logged, recorded in `ops_files_refused`, previous snapshot standing) and the push's pages still land |
| `identity.py` | `audiences_from_text` (the one parse of `ops/identities.json`, under both the snapshot road and `resolve_audiences`' file road) and the per-request half: `hash_token` · `load_token_store` · `resolve_email_for_token`. Fail-closed on every step — an empty text is malformed and resolves nobody |
| `ops_files.py` | which copy of an `ops/` control file a running process answers from — the ONE preference order (the index's snapshot, else the process's own file), stated once and shared by the HTTP transport's per-request audience resolution, Slack's per-event one and `slack.channels`' scope lookup. Chooses BYTES only; each file's own reader owns the parse. Also the road `stigmergy.slack` legally reaches the snapshots by (it may import `server`, not `index`) |
| `acl.py` | `visible(acl, audiences)` — the ONE visibility rule, with its fail-closed truth table — and `all_visible` (all-or-nothing for text composed from several pages). Never re-implement label matching |
| `entity_aliases.py` | The one reader of `ops/entity-registry.json`, split so that TEXT is the unit: `aliases_from_text`/`registry_from_text` are the ONE parser, `read_file` the one file read, and `load_aliases`/`load_registry` are those two composed for a caller holding only a path (`evals/run_qa.py`). Plus `resolve_entity` (longest whole-word alias inside a question), `resolve_exact` (this string names one entity) and `ENTITY_REGISTRY_RELPATH`, the POSIX spelling `webhook.py` matches a pushed path against. The service reads the registry from TWO sources and both go through this parser — never read the registry a second way |
| `audit.py` | `audit_log`'s DDL and `AuditWriter` — one row per tool call, both transports; a write failure never fails the serving call |
| `ratelimit.py` | `RateLimiter` — per-identity token buckets: overall 30/min plus a stricter 10/min for `ask`; injectable clock. A new expensive tool is one entry in `_extra`, never a new branch in `check` |
| `settings.py` | `Settings.from_args` — the ONE place flags and env fallbacks are read; no module here reads the environment at import time |
| `errors.py` | `StigmergyServerError` and its subclasses (`IdentityError`, `StartupError`, `CapabilityUnavailableError`, `RegistryError`, `RateLimitError`); library code never raises `SystemExit` |
| `pilot_report.py` | `stigmergy-pilot-report` — the measurement table from columns other code already writes. Reads only, no DDL: it works under a read-only database role |
| `issue_token.py` | `stigmergy-issue-token <email>` — prints the plaintext bearer token once and the sha256 store line |

## Consumers (one-way, except the admin console's ASGI branch, composed only in `transport_http.build_http_app`)

| Consumer | Reaches |
|---|---|
| `answer` | `service.BrainService` (`search`, `read_page`, `describe_entity`, `fetch_page_raw`, `scoped_entities`), `service.fence`/`neutralize_fence` |
| `slack` | `service.open_scoped_resources`/`BrainService`/`SLACK_DOOR`, `identity.resolve_audiences`, `audit`, `ratelimit`, `settings.Settings`, `errors` |
| `admin` | `review` (`delete_and_record`, `commission_registration`, `ensure_repair_schema`), `pilot_report`, `identity.hash_token`, `webhook.JOB_NAME`, `errors` |
| `gardener` | `errors`, `acl.visible`/`all_visible` |
| `digest` | `errors`, `acl.visible` |

Neither `gardener` nor `digest` reaches the write lane at all: the gardener reports findings and
the digest broadcasts them, and neither has anything to decide.

## Invariants

- Every read goes through `BrainService`; never query Postgres from an MCP tool closure — the
  service is the only place ACL filtering and the page contract are enforced.
- `visible()` runs BEFORE every cap and truncation (the search candidate pool, `_capped`'s
  `NAV_CAP`): an out-of-scope row never steals a slot and never leaks via a count.
- Unauthorized reads as nonexistent, byte-identically: `read_page`'s unknown-page shape,
  `describe_entity`'s absence shape, `NOT_YOURS_TO_DECIDE`. Specific refusals
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
  be refused (FastMCP drops undeclared fields silently); `brain_submit` accepts every one of
  `capture_schema.KINDS` — one vocabulary for every door, with each kind's required hints enforced
  at the enqueue seam — while the trusted provenance hints stay door-gated (`SLACK_DOOR`).
- `brain_delete` is the one TOOL that touches git, and it decides and writes in the same call —
  because the person calling it is the decision (ADR 043 D2, ADR 044 D3). It authorizes on an
  UNRESTRICTED identity: a removal touches every page that refers to the ones named, a set nothing
  knows before the clone, so "may this caller see the whole corpus" is the one question the door
  can answer before spending a network leg. The console reaches the same sequence with its token.
- **There is no repair verdict here any more** (ADR 044 D2). The worker derives and applies
  repairs on its own idle branch, and this process reaches `repair.apply` for the ONE repair a
  person performs. `repair` exceptions leave this module UNTRANSLATED — the door that calls it
  maps them.
- `audit_log.result` is a per-tool outcome SUMMARY, never a transcript; `args` carries caller
  content verbatim for exactly two fields (`ask.question`, `search_brain.query`) — the named
  exemption stated in `audit.py`'s docstring.
- Entity-first resolution changes result ORDER, never membership, and fires only when the caller
  passed no explicit `entity` filter (key presence, not truthiness); `entity_hint` is told to the
  ranker, never re-inferred inside it.
- The registry the server SERVES is the index's snapshot wherever the database has one; the
  `--entity-registry` file answers only where it has none. That order is chosen ONCE per service
  instance, in `service._registry_source`, and every registry reader goes through it: a reader
  going to the file directly would serve the copy baked at deploy time again, which is issue #74 —
  an entity born after a rollout served with no name, no type and no aliases, and its aliases
  resolving nowhere, until the next deploy.
- Both body caps are enforced before buffering: `transport_http.MAX_REQUEST_BODY_BYTES` on the
  bearer path, `webhook.MAX_BODY_BYTES` on the webhook path (refused before the HMAC ever runs).
- Every startup ensures the audit, capture and review schemas behind
  `capture.schema.startup_ddl_lock`, plus `index.store.ensure_ops_file_table` and
  `ensure_webhook_dedupe_table` (create-only, safe concurrently):
  `IF NOT EXISTS` is a check, not a lock — and losing that race inside the webhook's phase-2
  transaction would roll the pushed PAGES back with it, on a delivery GitHub does not repeat.
- `review.py` imports `service` lazily inside functions (`service.py` imports it at module
  scope); `mcp_server.py`'s `ask` closure imports `stigmergy.answer` the same way — the reverse
  edges must never be taken at import time.

Tests live in `tests/server/`; this package's import edges — the librarian/entities allowlists,
the ACL-reader rule, the slack one-way edge — are pinned in `tests/test_architecture.py`.
