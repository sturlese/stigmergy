# server — the one API: identity, ACL, the audited seam, both transports, and the webhook

Narrative reference: [`docs/reference/server.md`](../../../docs/reference/server.md) (the tool
table, the HTTP transport, the error sweep) and
[`docs/reference/navigation.md`](../../../docs/reference/navigation.md) (the entity-navigation
surface). Design records: [`docs/decisions/`](../../../docs/decisions).

The one place a caller's identity resolves to an audience scope, ACL is enforced, a rate-limit
check runs and an audit row is written — over two transports (stdio for one local operator,
streamable HTTP for remote callers sharing one process) and two directions (read:
search/read/navigate/ask; write: submit/list/review). `BrainService` is the
transport-agnostic core both transports — and `stigmergy.slack`, a third caller — build and call
into. This package files nothing: a capture is queued and attributed here, and turning it into a
committed page is the librarian's, reached through the durable queue and never through an import.
`webhook.py` serves the public HTTP surface without being a transport: the one bearer-auth-exempt
path, authenticated by HMAC instead.

## Modules

| Module | What it is — what to reuse, what to avoid |
|---|---|
| `service.py` | `BrainService` and `_call`/`call_async`, the ONE rate-limit + audit seam every tool rides — a tool off this seam is invisible to the limiter and the audit trail. `build_service` (stdio) and `open_scoped_resources` (the shared conn+embedder build every transport wires through). Also: the audit shapers, `check_arg_length`/`MAX_ARG_CHARS`, `fetch_page_raw` (the one fetch+ACL+sanitize base), `scoped_entities` (the one entity-existence rule), `_registry_source` (the ONE place the registry's bytes are chosen — the index's snapshot, else the `--entity-registry` file — memoized until the next `_call`/`call_async` drops it, and read through `_registry_aliases`/`_registry_records` by all four registry readers so no path-bearing `ValueError` escapes), `unrestricted` (the ONE spelling of `audiences is None`, which widens a QUEUE read to every identity's rows and is never an ACL decision — page visibility is `acl.visible()`'s alone), the `neutralize_fence`/`fence` and `SLACK_DOOR` re-exports, `UnavailableEmbedder` |
| `mcp_server.py` | The FastMCP tool closures BOTH transports share — NINE: `search_brain` · `read_page` · `list_entities` · `describe_entity` · `brain_submit` · `brain_submissions` · `review_queue` · `review_decide` · `ask` — plus the `stigmergy-server` entry point and `_dsn_location`. Tool docstrings are the client-visible contract: leave them byte-identical unless the contract changes |
| `review.py` | The review lane over its THREE item kinds (`identity-proposal`, `alias-proposal`, `repair-proposal`): `review_queue`/`review_decide`/`review_decide_safe`, the shared base `_collect_open_items` (which lists a repair proposal in the MANAGEMENT read only — it has no submitter, and it names page paths), the authorization predicates (`is_steward` — public, also the read-side gate — and `_guard_*`), the post-authorization staleness enrichment (`_already_decided_suffix`), the `review_decisions` ledger — OWNED by `capture.decisions` since issue #51 and re-exported here under the names callers already used (`ensure_review_schema`, `record_decision`, `latest_decisions`, and the `SOURCE_*`/`DECISION_SOURCES` door vocabulary), so the `stigmergy-entities` CLI can write the same row without importing this package and `stigmergy.slack` can read it without reaching into `stigmergy.capture` — `VERDICTS_BY_KIND` (the ONE translation from a surface's button label to the stored verdict, which is why a Slack button and a ledger row can never disagree), the governed DECISION sequence (`decide_and_record` → `entities.remote.decide_via_clone`, reached as a module attribute — the ONE ordering both SERVER-SIDE doors run) and its `commission_registration` sibling for an entity nobody proposed — which touches neither git nor the ledger: it queues a `raw` capture carrying `capture.schema.registration_hints`, and the librarian writes the page, births the identity confirmed by that steward and records the approval after the push (ADR 042). The other half of that door is `service._submit`'s `capture_schema.reject_registration_hints`, so no client can assert a `register_*` hint, the governed REPAIR sequence (`apply_repair_and_record`/`reject_repair_and_record` → `repair.remote.apply_approved`, also a module attribute — the ONE ordering both approving doors run, [ADR 039](../../../docs/decisions/039-governed-repair-loop.md)), and the doorbell's read side (`items_for_doorbell`, `load_stewards`, `resolve_stewards_for_scope`, `record_undeliverable`). **The inbox is DERIVED and owns no table**: the two proposal kinds are read off the entity registry this server already serves (`proposed`/`approved_by`/`proposed_aliases` per entity), `pages_index` says what anchors to each, and the ledger says what has been decided |
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
| `slack` | `service.open_scoped_resources`/`BrainService`/`SLACK_DOOR`, `identity.resolve_audiences`, `audit`, `ratelimit`, `settings.Settings`, `errors`, `review` (the doorbell reads, `review_decide_safe`) |
| `admin` | `review` (`decide_and_record`, `commission_registration`, `apply_repair_and_record`/`reject_repair_and_record`, `VERDICTS_BY_KIND`, `SOURCE_ADMIN`, `ensure_review_schema` and the review reads), `pilot_report`, `identity.hash_token`, `webhook.JOB_NAME`, `errors` |
| `gardener` | `errors`, `acl.visible`/`all_visible` |
| `digest` | `errors`, `acl.visible` |

Neither `gardener` nor `digest` reaches the review lane: the `review_decisions` ledger they read
lives in `capture.decisions`, below both packages, and this package only re-exports it.

## Invariants

- Every read goes through `BrainService`; never query Postgres from an MCP tool closure — the
  service is the only place ACL filtering and the page contract are enforced.
- `visible()` runs BEFORE every cap and truncation (the search candidate pool, `_capped`'s
  `NAV_CAP`): an out-of-scope row never steals a slot and never leaks via a count.
- Unauthorized reads as nonexistent, byte-identically: `read_page`'s unknown-page shape,
  `describe_entity`'s absence shape, `NOT_YOURS_TO_DECIDE`. Specific refusals
  surface only to an already-authorized caller.
- `_already_decided_suffix` is composed inside `_translate`, where an `entities` exception is met —
  strictly after the caller has cleared its own `_guard_*`. It names who decided the item, through
  which door and when — facts read out of `review_decisions`, which no refused caller may learn.
  Appending it to `NOT_YOURS_TO_DECIDE` would turn one anonymous sentence into an
  existence-and-attribution oracle, which is why the enrichment lives inside that path rather than
  in a wrapper around it.
- Every ledger row names its DOOR. `record_decision` takes a REQUIRED `source` from the closed
  `DECISION_SOURCES` set and raises `ValueError` on anything else — the table is append-only, so a
  door's own misspelling is permanent. `review_decide`/`review_decide_safe`/`decide_and_record`
  thread it undefaulted, so a new door that forgets fails loudly rather
  than being attributed to an existing one: MCP is `mcp_server.py`'s closure, Slack is
  `slack.review._decide_and_confirm`, the console is `admin.service`, the CLI is `entities.cli`.
  `commission_registration` takes the same required `source` and spends it one layer further on —
  it rides to the worker as the `register_source` hint, which is what the ledger row eventually
  names its door with.
- **The ledger is not only a record; it is the librarian's memory.** `librarian.processing` reads
  the latest `identity-proposal` verdict per entity id and refuses to propose a declined identity
  again. That is why every decline — from any door — must be recorded under exactly that kind and
  that item id, and why the row is written AFTER the push: a row claiming a decline whose commit
  never landed would make the librarian refuse an identity whose proposed page still stands.
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
- `review_decide` touches git for EVERY verdict on a proposal kind and for a repair `approve`;
  a repair `reject` is the one Postgres-only verdict left.
- The steward guard asks a PER-PATH question wherever there is a path to ask about. A repair names
  the pages it would touch — including, for a deletion, every page rewritten to stop pointing at
  the pages that go — and `all(...)`, never `any(...)`. A proposal resolves at its OWN entity
  page's path (`_scope_path_of`), so a delegated zone authorizes its own steward; the empty
  universal scope is the fallback for a proposal the index has not seen yet.
- `decide_and_record` is the ONE decision sequence for both SERVER-SIDE doors — the review lane
  (MCP, Slack) through `_decide_identity`/`_decide_alias`, the admin console through
  `admin.service.entity_decide`: land the commit through the governed door, THEN the ledger row.
  A FOURTH door decides outside it: `stigmergy-entities` runs its own copy of the same order from
  the steward's clone, because `stigmergy.entities` cannot import this package. It DOES write the
  ledger row — the writer lives in `capture.decisions`, below both packages, so `review_decisions`
  answers "who decided this identity" for every door (issue #51). `decide_and_record` raises
  `entities` exceptions UNTRANSLATED because each door maps them differently (here into this
  package's vocabulary through `_translate`, there into `admin_actions`' recorded class name), and
  it takes NO authorization argument, so its caller set is closed and pinned.
- `apply_repair_and_record` is the same bargain for the repair loop's own irreversible verdict, and
  its caller set is closed for the same reason (it takes NO authorization argument): the review lane
  through `_decide_repair`, the console through `admin.service.repair_approve`. Its order is
  `mark_decided` (a CONDITIONAL update — that `WHERE status = 'pending'` is the whole concurrency
  story, and why repairs need no lease), then the apply, then the ledger row after the push. A
  failed apply stays `failed` with its reason and is never restored to pending: a silent revert
  would hide that a gate refused. `repair` exceptions leave it UNTRANSLATED, exactly as `entities`
  ones do, because the two doors map them differently.
- An `entities` exception type never leaves this package through `review_decide` or
  `review_decide_safe`: `_translate` maps it into `ReviewError` (or
  `CapabilityUnavailableError`) where it is met. The ONE exception is
  `decide_and_record`, left untranslated on purpose for the single caller
  (`admin.service`) whose `admin_actions` bookkeeping records the library's own class name.
  `stigmergy.slack` may not import `stigmergy.entities`, which is why Slack must reach a decision
  only through `review_decide`/`review_decide_safe`: anything untranslated would reach it as an
  unanticipated fault whose text it must not show.
- An `identity-proposal` item is built from three reads and nothing else: the registry record
  (`name`, `entity_type`, `aliases`, `proposed_aliases`), the entity page's own row in
  `pages_index` (its `path`, its `acl`, and the What / Who paragraph `_summary_of` lifts out of the
  body), and `_anchored_pages` — every page anchored to it that this caller may see, `visible()`
  asked per row, capped with `anchored_total` beside it. `merge_candidates` is computed here
  (`_merge_candidates`: registered, non-proposed entities sharing a whole word of ≥3 characters,
  bounded by `MAX_MERGE_CANDIDATES`) and is a HINT for a picker, never a shortlist anything
  enforces — the survivor a steward names is re-validated against the knowledge repo inside the
  clone.
- **A proposal's visibility is the entity PAGE's.** `_identity_item`/`_alias_item` ask
  `visible(page.acl, audiences)`, and a scoped caller is shown nothing for a proposal the index has
  not seen yet — nothing says who may see it, so nobody scoped does.
- Steward resolution fails closed: `is_steward` returns False without a checkout or a baked
  snapshot, AND when the map it reads cannot be loaded at all (a malformed `ops/stewards.json`, a
  broken checkout) — it catches that inside and logs it, because a caller on the READ leg has no
  other net and a raise there is a click that vanishes with no feedback;
  `load_stewards` reads `origin/main`'s fresh tip wherever a checkout exists, and the
  same read decides both doorbell delivery and decision authority. `is_steward` stays PUBLIC
  because it is the READ-side gate too: a surface that shows review material before any decision
  exists has to ask the same question at the same scope, or the decide leg's guard arrives after
  the material has been served.
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
