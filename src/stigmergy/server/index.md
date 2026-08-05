# server — the one API: identity, ACL, the audited seam, both transports, and the webhook

Narrative doc: [`docs/reference/server.md`](../../../docs/reference/server.md) (the how and why for
an operator and a tool caller — the tool table, the HTTP transport, the full error sweep);
[`docs/reference/navigation.md`](../../../docs/reference/navigation.md) for the entity-navigation
surface. Design records: [ADR 007](../../../docs/decisions/007-answer-layer.md) (the answer layer
sits ABOVE this service), [ADR 010](../../../docs/decisions/010-acl.md) (the visibility rule),
[ADR 013](../../../docs/decisions/013-http-transport-and-token-auth.md) (HTTP transport + hashed
token auth), [ADR 014](../../../docs/decisions/014-capture-queue-and-attribution.md) (the write
path this service mounts), [ADR 018](../../../docs/decisions/018-pilot-readiness.md) (the webhook,
entity-first resolution, the pilot instrument), [ADR 022](../../../docs/decisions/022-entity-navigation.md)
(`read_page`'s graph, `list_entities`/`describe_entity`, entity-first resolution moved down into
the service — D5 is the amendment `describe_entity`'s second resolution path implements),
[ADR 026](../../../docs/decisions/026-the-purge.md) (**read this first**: D1 cut the canon lane
out of `canon.py` and left `review.py`; D2 deleted the facts store and the `verification` verdict)
and [ADR 027](../../../docs/decisions/027-the-contraction.md) (the learning loop deleted whole —
every trace of it left this package).

**Three removals ran through this package back to back**, and the names they took with them are
worth knowing because the history around them is dense: the purge took the canon lane and the facts
store; the door work renamed zones and added the capture DOOR; the contraction deleted
`stigmergy.loop`. Nothing named `stigmergy.loop`, `stigmergy.pipeline`, `stigmergy.supervisor`, `canon.py`,
`facts.db`, `query_metrics`, `brain_propose`/`brain_promote`, `canon_proposals`,
`stage_mcp_ask`/`stage_slack_turn` or the `candidate` review kind exists in this package. If a
comment names one, it is describing a removal, not a live organ.

This file is the code map — for whoever is about to edit this package, not run it.

## Purpose

The one place a caller's identity resolves to an audience scope, ACL is enforced, a rate-limit
check runs and an audit row is written — over **two transports** (stdio for one local operator,
streamable HTTP for remote callers sharing one process) and **two directions** (read:
search/read/navigate/ask; write: submit/list/reply/review). `BrainService` is the
transport-agnostic core both transports — and `stigmergy.slack`, a third caller — build and call
into. Nothing downstream of it re-derives visibility, re-opens a connection, or re-implements
attribution.

**This package also carries code that serves the public HTTP surface without being a transport**:
`webhook.py`, the GitHub receiver mounted through the app `transport_http.py` builds. It is the one
path exempt from bearer auth, because it authenticates a completely different way.

**It files nothing.** A capture is queued and attributed here; turning it into a committed page is
`librarian`'s, reached through the durable queue row and never through an import.

## Key entry points

| Module | Owns |
|---|---|
| `service.py` | `BrainService` — the transport-agnostic core, and `_call`/`call_async`, the ONE rate-limit + audit seam every tool (read and write) rides. `build_service` (stdio) / `open_scoped_resources` (the shared `(conn, embedder)` three-step every transport wires through). The audit shapers (`_truncate_for_audit`, `_audit_args`, `_result_for`, `_submit_audit_args`, `_audit_hint_keys`), `check_arg_length`/`MAX_ARG_CHARS`, the `neutralize_fence`/`fence` re-export, `UnavailableEmbedder`/`missing_embedder_reason`/`_resolve_embedder`, and the `SLACK_DOOR` re-export `stigmergy.slack` reaches `capture.schema`'s constant through |
| `mcp_server.py` | The FastMCP tool closures BOTH transports share, and the `stigmergy-server` console entry point (`--transport stdio\|http`). **Ten tools**, no more: `search_brain` · `read_page` · `list_entities` · `describe_entity` · `brain_submit` · `brain_submissions` · `review_queue` · `review_decide` · `brain_reply` (mounted under `capture_schema.REPLY_TOOL`, never this function's own name) · `ask` (async, the only one whose implementation lives one layer up). Also `_dsn_location`, the credential-free startup-error line |
| `review.py` | (renamed from `canon.py` when the canon lane was cut out of it) the review lane end to end: `review_queue`/`review_decide`/`review_decide_safe`, the shared base `_collect_open_items`, the authorization predicates (`_is_steward`, `_guard_governance_decision`, `_guard_parked_capture_decision`), the append-only `review_decisions` ledger and its DDL, the governed mint seam an entity-proposal approve drives (`_mint_entity_proposal` -> `entities.remote.mint_via_clone`, [ADR 030](../../../docs/decisions/030-server-side-entity-minting.md)), and the doorbell's read side (`items_for_doorbell`, `load_stewards`, `resolve_stewards_for_scope`, `record_undeliverable`) |
| `transport_http.py` | The streamable-HTTP transport: `_BearerAuthMiddleware` (raw ASGI, not `BaseHTTPMiddleware`), `_ScopedServiceProxy` + the `_current_service` contextvar, the two body caps, the DNS-rebinding allowlist (`$STIGMERGY_PUBLIC_HOST`), `build_http_app`/`serve_http`, `token_store_from_env`, and the webhook route's mount |
| `webhook.py` | `POST /webhook/github` — HMAC over the RAW body, then a two-phase incremental `pages_index` upsert. `WEBHOOK_PATH`, `WebhookSettings`/`webhook_settings_from_env`, `verify_signature`, `changed_paths_from_push`, `in_zone_changes`, `fetch_file_content`, `_resolve_outbound_links`, `_propagate_split_chain_supersession`, `process_push`, `_read_body_capped`, `webhook_endpoint` |
| `identity.py` | `resolve_audiences` (the file-backed resolver both transports use) and the per-request half beside it: `hash_token` · `load_token_store` · `resolve_email_for_token`. Fail-closed on every step |
| `acl.py` | `visible(acl, audiences)` — the ONE visibility rule; `all_visible(paths, visible_paths)` — the all-or-nothing predicate for text composed from SEVERAL pages |
| `entity_aliases.py` | The plain-file reader over `ops/entity-registry.json`: `_load_entities` (the one loader), `load_aliases`/`load_registry` (the two readers), `resolve_entity` (longest whole-word alias inside a question) and `resolve_exact` (this string NAMES one entity), plus `_norm` and `default_path` |
| `audit.py` | `audit_log`'s DDL (`ensure_audit_table`) and `AuditWriter` — one row per tool call, both transports, and a write failure never fails the serving call |
| `ratelimit.py` | `RateLimiter` — a per-identity continuous token bucket, injectable clock. **Two live buckets**: overall 30/min, plus a stricter 10/min for `ask` |
| `settings.py` | `Settings.from_args` — the ONE place flags and env fallbacks are read; no module here reads the environment at import time |
| `errors.py` | `StigmergyServerError` and its four subclasses (`IdentityError`, `StartupError`, `CapabilityUnavailableError`, `RateLimitError`) — library code never raises `SystemExit` |
| `pilot_report.py` | `stigmergy-pilot-report` — the measurement table, built from columns other milestones already wrote. **Reads only, and runs no DDL at all** |
| `issue_token.py` | `stigmergy-issue-token <email>` — prints the plaintext bearer token once and the sha256 store line |

**Who depends on this package** (one-way, always):

| Consumer | Reaches |
|---|---|
| `answer` | `service.BrainService` (`search`, `read_page`, `describe_entity` for the agent's three tools; `fetch_page_raw`, `scoped_entities` for the verifier and the offline fake), `service.fence`/`neutralize_fence`. It sits ABOVE the service and BELOW `mcp_server.py`, so it never rides `call_async` itself — `mcp_server.py`'s closure and `slack.mention._run_ask` wrap it in one |
| `slack` | `service.open_scoped_resources` / `BrainService` / `SLACK_DOOR` / `EmptyIndexError` / `evidence_plane`, `identity.resolve_audiences`, `audit`, `ratelimit`, `settings.Settings`, `errors`, and `review` (`items_for_doorbell`, `load_stewards`, `resolve_stewards_for_scope`, `record_undeliverable`, `review_decide_safe`, the kind constants) |
| `gardener` | `errors.StartupError`/`IdentityError`, `acl.visible`/`all_visible`, `review.ensure_review_schema` |
| `digest` | `errors.StartupError`, `acl.visible`, `review.ensure_review_schema` + `review.KIND_ENTITY_PROPOSAL`/`APPROVE` (it reads the ledger directly for "entities born") |

Nothing in this package imports any of them. `test_server_never_imports_slack` pins the Slack half.

## Use these

- **`service.BrainService._call` / `.call_async`** — the ONE seam every tool rides for rate
  limiting and the audit row. A new tool that talks to Postgres or the answer layer directly
  instead of through this seam is invisible to both the limiter and the audit trail. `call_async`
  is public precisely because `ask` cannot be a method here (`service.py` may never import
  `stigmergy.answer`), so `mcp_server.py`'s closure and `slack.mention._run_ask` both call it — one
  definition of what an `ask` call's audit row looks like, whichever transport made it.
- **`service.open_scoped_resources`** — the three-step sequence (open the connection, read
  `index_meta`, resolve the query embedder against it), written once. A new transport builds its
  per-identity `BrainService`s through it and never re-opens `stigmergy.index` itself; that is what
  keeps `stigmergy.slack`'s pinned import list literally true.
- **`acl.visible(acl, audiences)`** — the one ACL rule. Search, `read_page`, the nav sections, the
  timeline and `scoped_entities` all filter through it. Never re-implement label matching.
- **`acl.all_visible`** — for text composed from MORE than one page's identity. All-or-nothing by
  design: a partial scrub is the kind of defense that looks complete and is not.
- **`identity.resolve_audiences`** — the one identity resolver, fail-closed, raising
  `IdentityError`. stdio calls it at startup with a name; HTTP calls it per request with a
  bearer-resolved email. A caller must not proceed without a resolved scope.
- **`service.neutralize_fence` / `service.fence`** — re-exports of `stigmergy.text`'s hardened
  implementations. The answer layer imports them FROM here, which is what still proves this is the
  function the wire actually uses; do not re-derive a fence anywhere (`tests/test_architecture.py`
  pins `stigmergy.text` as its only home).
- **`service.BrainService.fetch_page_raw`** — the single fetch + ACL + sanitize + excerpt base.
  `read_page` fences its body for an agent; the answer layer's verifier needs it unfenced. One ACL
  read path, two renderings.
- **`service.BrainService._capped` / `_cap_note`** — the shared existence-scope + truncation-note
  base behind `_nav_section` (links/backlinks) and `_timeline_section`. `visible()` runs BEFORE the
  cap, so an out-of-scope row never steals a shown slot nor hints at itself through a count. Never
  a third cap+note implementation.
- **`service._neutralize_entity_record` / `_display_title`** — the one application of
  neutralization to a registry record, and the one "never fall back to the raw path" title rule.
  Both `list_entities` and `describe_entity` ride them.
- **`entity_aliases.resolve_entity`** (substring over a question) vs **`resolve_exact`** (this
  string IS an entity) — two questions, one loader, one normalization. A new caller asking "does
  this name a registered entity" uses one of these, never a fresh read of the registry file and
  never an import of `stigmergy.kernel.registry`.
- **`service.BrainService.scoped_entities`** — the ONE existence rule for entities.
  `list_entities`, `describe_entity`'s absence gate and its fallback resolution all consult the
  same set; the answer layer's offline fake reuses it rather than re-querying `pages_index`.
- **`review.review_queue` / `review.review_decide`** — the only entry points that read the unified
  inbox or record a verdict. There is no canon lane, no case file and no promotion to ask about any
  more: a page's `status` is a maturity axis a person edits (ADR 026 D1).
- **`review.review_decide_safe`** — the clean-refusal-as-a-value twin `stigmergy.slack` calls,
  because that package is architecturally barred from importing `stigmergy.capture.errors` /
  `stigmergy.server.errors` to catch the raising version. A new non-MCP caller uses this rather than
  inventing a third way to turn a `CaptureError` into a return value.
- **`review._collect_open_items`** — the shared base under BOTH `review_queue` (operational,
  ownership-scoped) and `items_for_doorbell` (management, unscoped). Two consumers, one answer to
  "which rows are open" and "which kind is this row".
- **`review._is_steward` / `load_stewards` / `resolve_stewards_for_scope`** — the ONE steward
  resolution, read fresh at `origin/main`'s tip on every call WHERE A CHECKOUT EXISTS — the worker and a local stdio server. The deployed `app`/`slack` groups hold none and read the snapshot the deploy baked (issue #34), so there a steward's authority changes at the next redeploy, not the next push; the fast lever for cutting someone off entirely is their token in `STIGMERGY_TOKEN_STORE`.
  Never cached on that path; `slack.doorbell`'s own 300 s cache is a deliberately looser bound for
  a notifier only.
- **`review._mint_entity_proposal`** — the ONE call into the governed door for a server-driven
  mint ([ADR 030](../../../docs/decisions/030-server-side-entity-minting.md)):
  `entities.remote.mint_via_clone`, reached as a module attribute so it stays patchable, with
  `entities.errors.{EntityError,CapabilityUnavailableError}` mapped into this package's own
  `ReviewError`/`CapabilityUnavailableError` — the one place both vocabularies are in scope. A new
  surface that mints an entity from a decision calls `review.review_decide` itself (which threads
  `name`/`entity_type`/... through to this), never this helper directly and never a second mint
  seam. `generator.canonical_id_for` is still reused, now for `entity_id`'s own default (the
  proposal's slug) rather than for a printed command — `mint_command` and the
  `suggestable_entity_name` shell-safety predicate it needed are gone (ADR 030 D5).
- **`webhook.WEBHOOK_PATH`** — the ONE constant naming the exempt path, imported by
  `transport_http.py` for the exemption check and by `webhook.py` for the route mount. Never
  retyped as a literal in a second place.

## Avoid / anti-patterns

- **Never query `stigmergy.index` (Postgres) directly from an MCP tool closure.** Every read goes
  through `BrainService`, the only place ACL filtering and the page contract are enforced.
- **Never treat the webhook's exemption as anything looser than an exact string match.**
  `_BearerAuthMiddleware.__call__` compares `scope.get("path") == webhook.WEBHOOK_PATH` — never a
  prefix, never a regex, never a second allowlisted route. A prefix match would exempt
  `/webhook/github/../anything` from every identity check on this server;
  `test_a_prefix_of_the_webhook_path_is_not_exempt` is the standing proof.
- **Never let the webhook route touch `_current_service` or resolve an identity.** It authenticates
  by HMAC over the raw body, inside `webhook_endpoint` itself, and writes `pages_index` through
  `index.store` without a `BrainService` at all. An unauthenticated GitHub delivery cannot present
  a bearer token; wiring it through that path would solve a problem it does not have.
- **Never verify the HMAC over anything but the RAW, unparsed body, and never skip the size cap
  first.** `_read_body_capped` runs BEFORE `verify_signature`, so an oversized body gets the same
  generic 401 without the HMAC ever running over it.
- **Never write a second `pages_index` writer.** `process_push` reuses `corpus.page_row` (the same
  parser the full directory walk calls per file), `corpus.resolve_links`/`by_stem_index`,
  `store.upsert_pages`/`delete_pages`/`set_superseded_by`. A second incremental path that re-parses
  a page or re-derives its row shape reopens the two-writers-drift risk.
- **Never let a webhook failure suggest the write path failed.** A filed page is already committed
  to git before this endpoint sees it. Every exception in `process_push` becomes a 500 for OPERATOR
  visibility only — and GitHub does NOT auto-redeliver on 5xx, so the nightly rebuild is the only
  automatic reconciler.
- **Never let `audit_log.result` carry a transcript.** `answer.service.audit_summary` /
  `_verdict_shape` ship COUNTS (`unverified_figures`, `citation_problems`) and citation PATHS —
  never the verdict's own problem strings (which embed up to 80 characters of a drafted quote) and
  never a question or an answer. A `summarize` callback that passes drafted-answer-derived strings
  into that column reopens a regression that has already been closed once here.
- **Never import `stigmergy.librarian` beyond the two declared, symbol-scoped exceptions.**
  `webhook.py` reaches exactly `githubapp` + `errors.LibrarianConfigError`; `review.py` reaches
  exactly `gitcmd`, `gates`, `base_inputs` DIRECTLY (plus `librarian.githubapp` TRANSITIVELY,
  since ADR 030, through `entities.remote` — that reach belongs to `entities`, which owns the App
  credential machinery, and is what keeps `review.py`'s OWN declared set from having to grow a
  third symbol for it). Both direct allowlists are asserted as SUPERSETS of what is actually
  imported (`test_review_actually_uses_its_declared_librarian_exception` — a declared door nothing
  walks through is a defect), and a separate test forbids `worker`/`processing`/`agent`
  independently of what either allowlist happens to say.
- **Never import `stigmergy.entities` outside `review.py`'s six declared symbols.** `situations`
  (the whole module — the inbox and `stigmergy-entities` must classify a triage row byte for byte
  identically, which means calling the same functions), `generator.canonical_id_for` /
  `generator.ENTITY_TYPES` (an entity-proposal approve's `entity_id` default and `entity_type`
  validation), and — since [ADR 030](../../../docs/decisions/030-server-side-entity-minting.md) —
  `remote` (the whole module, for the same byte-for-byte reason `situations` is: a test needs to
  monkeypatch the ATTRIBUTE `entities.remote.mint_via_clone`, not a name `review.py` bound at
  import time) plus `errors.EntityError` / `errors.CapabilityUnavailableError`, the two names a
  mint refusal is mapped through into this package's own vocabulary. None of them opens a
  connection, and the one (`remote`) that writes `ops/`/git is reached only from the one governed
  verdict allowed to. `cli.suggestable_entity_name` is GONE from this list: `_entity_mint_command`,
  its one caller, was deleted the same change `mint_command` left the response shape (D5) — nothing
  on this lane prints a shell command any more.
- **Never let entity-first resolution return FEWER hits than the same query without it.** `_search`
  fires only when the caller passed NO explicit `entity` filter (key PRESENCE, not truthiness), and
  it now feeds the rank-time boost rather than scoping the search — resolving an entity may change
  the ORDER of the results, never their membership. It used to search the entity's own material
  first and fall back only on zero hits, which meant any hits at all eclipsed the blended ranking:
  a company-wide page was unreachable through every query naming a registered company (issue #33,
  ADR 022 D4 amended). Retrieval here has a measured floor (the golden set), and this property is
  what keeps a resolution mistake off it.
- **Never re-infer the entity boost inside the ranker.** `entity_hint` is TOLD: `_search` resolves
  it, `_run_search` threads it into `search.search_arms`, and `rank.contract_factors` matches it by
  membership. A second inference site inside `rank` would let the service and the ranker disagree
  about which entity a query named.
- **Never let `review_decide` push, commit or merge anything OUTSIDE the one governed mint
  seam.** `reject` and every `parked-capture` verdict stay Postgres-only, categorically (ADR 026
  D1's rule, narrowed rather than repealed by [ADR 030](../../../docs/decisions/030-server-side-entity-minting.md)):
  no git-writing entry point exists on those paths at all, and
  `test_review_decide_never_writes_to_git_the_full_matrix` walks every REMAINING kind × verdict
  pair (`approve` excluded by name, with the reason recorded beside it) against a real ref so
  removing the property breaks a test, not silently a principle. Approving an `entity-proposal` is
  the one path that DOES write git, and only through `_mint_entity_proposal` ->
  `entities.remote.mint_via_clone` — never a second, ad hoc git call anywhere else in this module.
- **Never let a governance refusal be specific before authorization has cleared.**
  `NOT_YOURS_TO_DECIDE` is one byte-identical sentence for "does not exist", "not yours" and "not
  a steward" — three separately-worded refusals are how an existence oracle gets built. The
  specific refusals `dispositions`/`situations` raise are fine, and only AFTER the predicate
  passed.
- **Never ask `review_queue` for more rows than one page and assume you got them.**
  `capture_queue.query_submissions` silently clamps at `MAX_LIST_LIMIT` (200);
  `_query_all_open_submissions` pages around that. A doorbell that asked for 500 in one call got
  the NEWEST 200 — precisely backwards for a surface whose reason to exist is the OLDEST parked
  items.
- **Never take `submitted_by`, `verification`, `acl` or `content_hash` from client input.** They
  are declared on `brain_submit`'s signature ONLY so they can be refused: FastMCP builds its
  argument model with pydantic's `extra="ignore"`, so an undeclared field is dropped silently and
  can be ignored but never refused.
- **Never widen `brain_submit` to `capture_schema.KINDS`.** It is pinned to `MCP_SUBMIT_KINDS`
  explicitly, because `kind` is a MODEL-CHOSEN argument and growing `KINDS` for the drop CLI once
  silently made `meeting` enqueueable beside that CLI's declared "only door".
- **Never trust `hints["source_client"]`/`["source_permalink"]` from a client.**
  `_submit` calls `capture_schema.reject_source_provenance_hints(hints, door=self.door)`; only
  `slack.context.SlackContext.build_service` — server code — passes `door=SLACK_DOOR`.
- **Never add a second existence check inside `describe_entity`.** One check
  (`entity_id not in self.scoped_entities()`) is what makes a nonexistent name and a
  registered-but-wholly-out-of-scope entity return the byte-identical absence shape.
- **Never move `scoped_entities()` back inside the resolution branch.** It is computed
  UNCONDITIONALLY, before resolution, and reused by both the fallback and the absence gate — the
  pre-amendment code paid for the DB read only in one branch, so response LATENCY told a caller
  which case applied.
- **Never construct a `BrainService` per identity and expect its resources to be per-identity.**
  HTTP shares one connection, one embedder, one `RateLimiter` and one `AuditWriter` across every
  request, deliberately: that is what makes the 30/10 budgets honestly per-identity across the
  whole process rather than per connection.
- **Never let `stateless_http` default to False on the HTTP path.** It is mandatory, not a style
  choice — FastMCP's stateful mode runs later requests inside the session CREATOR's frozen
  context, so a caller presenting someone else's `mcp-session-id` would execute and be audited
  under that identity's scope.
- **Never echo an unanticipated exception's message from a tool closure.** The rule is class name
  only; the one narrow exemption is `check_arg_length`'s own rejection, marked
  `is_arg_length_error`, because a bare `except ValueError` would also catch a
  `pydantic_core.ValidationError` carrying untrusted LLM output or internal field paths.

## Data & contracts

- **`audit_log`** (Postgres, `ensure_audit_table`) — `id`, `ts`, `identity`, `tool`, `args`
  (JSONB), `duration_ms`, `outcome`, `error_class`, plus the additive nullable `result` (JSONB).
  Index on `(identity, ts DESC)`. `result` is a per-tool outcome SUMMARY and NULL is honest: it
  means "this call predates the column" or "this tool has no summarizer", never "the summary was
  empty". A row is written even when the call raised — the shaping runs in `_call`'s `finally`.
- **The audit row's three independent bounds.** `MAX_ARG_CHARS` (8192) bounds each string;
  `MAX_AUDIT_HINT_KEYS` (32) bounds how many hint key NAMES are recorded;
  `MAX_AUDIT_DEPTH` (20) bounds recursion — a few KB of `[[[[…` would otherwise raise
  `RecursionError` inside that same `finally`, replacing the caller's real result or real
  exception with an audit-shaping crash. Dict KEYS are truncated as well as values, because
  `filters`' keys are exactly as client-controlled as its values.
- **`_audit_args` and `_result_for` are both failure-safe.** Both run in the `finally`, outside
  `AuditWriter.write`'s own try; a raise there would clobber what the caller came for. A shaping
  failure still lands the row, carrying `{"args_unavailable": …}` — a row saying "this call
  happened and its arguments could not be shaped" is worth strictly more than no row.
- **`review_decisions`** (Postgres, `ensure_review_schema`) — append-only: `item_kind`, `item_id`,
  `verdict`, `actor`, `notes`, `extra` (JSONB), `created_at`; index on `(item_kind, item_id)`. No
  path here ever `UPDATE`s or `DELETE`s, so "a second decision does not overwrite the first" holds
  by construction. `_latest_decisions` is a `DISTINCT ON` rendering convenience over it, not a
  state machine.
- **`review.ITEM_KINDS`** = `entity-proposal` · `parked-capture`. **Two kinds, not three.** Defined
  ONCE in `stigmergy.review_kinds` (the dependency-free root module, renamed from `canon_kinds`);
  `review.py` imports them and so do `slack.render`/`slack.doorbell`, which is why a pure
  Block Kit function does not drag `stigmergy.librarian`/`stigmergy.entities` into its import graph.
  Kinds are disjoint BY CONSTRUCTION: `situations.classify` runs first, and only a row that is not
  an entity situation falls through to `parked-capture`.
- **The verdict vocabularies deliberately differ per kind.** `entity-proposal` takes `approve` or
  `reject` and nothing else — there is nothing to "request changes" to. `parked-capture` takes
  `capture.dispositions`' own three verbs (`requeue`/`resolve`/`reject`) verbatim, because there
  is no honest `approve` equivalent of a `resolve` that carries a REQUIRED note, and a button
  label that disagreed with the recorded verdict is the property that must never happen.
  `GENERIC_VERDICTS` still names `request_changes`; nothing accepts it.
- **`ops/stewards.json`** — `{"<zone-path-prefix-or-*>": ["email", …]}`. Longest matching prefix
  wins; `"*"` is the universal fallback and is never itself compared as a prefix. `scope_path=""`
  (a parked capture or an entity proposal has no page path) can therefore only ever match `"*"`.
  Read fresh at `origin/main`'s tip on every call, including authorization, because a revoked
  steward's approval must never succeed off a stale read.
- **`SELF_APPROVAL_REFUSED`** — an approver may not be the submitter, even when they are also the
  resolved steward for the scope. The message says what to do ("ask another steward") rather than
  merely refusing, because the walk it correctly blocks is the solo operator approving his own
  proposal, and the fix is a second human, never weakening the rule.
- **`ops/identities.json`** — name-or-email → `"*"` (unrestricted, resolves to `None`) or a list of
  audience labels. Malformed anything raises `IdentityError`; the server never starts open. It is
  CONFIGURATION, not authentication — the bearer token is what an impersonator cannot fabricate.
- **The token store** — `{"<sha256hex>": "<email>"}`, from `$STIGMERGY_TOKEN_STORE` (inline JSON,
  the usual Fly-secret shape) or `$STIGMERGY_TOKEN_STORE_FILE`. Plaintext tokens are never stored.
  No constant-time compare is needed on lookup: it is a dict hit on a SHA-256 hash, not a
  byte-by-byte comparison of the token.
- **The ACL truth table** (`acl.visible`, fail-closed): `None` → open to everyone; `[]`/`""` → open
  to an UNRESTRICTED client, hidden from every scoped one; `[labels…]` → visible iff a label is
  shared; anything else (a dict, a bool, a list with a non-string element) → visible to NOBODY,
  not even unrestricted, and logged loudly. That last row is the deliberate divergence from the
  ingest-side predicate: a value we cannot trust must never resolve to "open" at the one place
  access is decided.
- **Two independent body-size caps, for two threat models.**
  `transport_http.MAX_REQUEST_BODY_BYTES` (1 MiB) bounds every BEARER-authenticated request — the
  MCP SDK does `await request.body()` with no limit and the 256 KB material cap fires only after
  the whole body is buffered. `webhook.MAX_BODY_BYTES` (26 MB — GitHub's 25 MB ceiling plus
  framing slack) bounds the one path an UNAUTHENTICATED caller can throw concurrent bodies at.
  Neither substitutes for the other. The declared-length path returns a clean 413; a chunked body
  is capped mid-stream by `_capped_receive`, which reports `http.disconnect` — deliberately less
  polite, because a client that will not say how big its body is has opted out of being told.
- **`webhook.WebhookSettings`** (frozen) — `secret=""` or `repo=""` both fail every request closed:
  this endpoint is INERT, not merely unauthenticated, until an operator sets
  `STIGMERGY_GITHUB_WEBHOOK_SECRET` and `STIGMERGY_GITHUB_REPO`. `_parse_file_cap` rejects `"0"`
  explicitly — `"0".isdigit()` is `True`, and a cap of 0 made `len(changes) > file_cap` true for
  every non-empty push, deferring all of them forever with no signal.
- **`process_push`'s two phases.** Phase 1 is every network call (GitHub's Contents API at the
  pushed sha, the embedder) plus every read-only lookup, with NO database write in it. Phase 2 is
  exactly one `with conn.transaction():` around delete + fresh-embedding store + upsert +
  supersession propagation. A phase-1 failure never touches `pages_index`; a phase-2 failure rolls
  the delete back WITH the upsert, so a rename that fails mid-run loses neither page.
- **`_propagate_split_chain_supersession`** — a push that upserts a split chain's PRIMARY
  stamps or clears `superseded_by` on its already-indexed part siblings in the same transaction.
  BOTH conventions are prefetched — historical `#p<n>` AND the live `-p<n>` the meeting splitter
  actually writes — because prefetching only `#p` left this propagation inert over every real
  split while the regex that had learned `-p` never saw a candidate. The regex stays the one
  decider, and a directory gate (`posixpath.dirname` equality) mirrors the build-time rule so an
  id-less `-p2`-stemmed twin elsewhere never inherits. Known residual, stated rather than
  discovered: a push editing ONLY a part reverts that part until the nightly rebuild — the same
  direction `_resolve_outbound_links` is honest about for backlinks.
- **`webhook.in_zone_changes`** is bound to `index.corpus.ZONES`, never a hand-kept list. Above
  `settings.file_cap` (default 50) in-zone changed files the push is DEFERRED wholesale, never
  partially applied.
- **`service.NAV_CAP`** (20) caps `read_page`'s `links`/`backlinks` AND `describe_entity`'s
  timeline — one shared constant (the spec names the same value `TIMELINE_CAP` in one place),
  never two that could drift. Every cap ships an explicit `*_note` sentence stating the true total
  and how many are shown; a silent truncation is never acceptable.
- **`describe_entity` resolves through TWO paths, and that is the current contract**
  ([ADR 022](../../../docs/decisions/022-entity-navigation.md) D5, as amended):
  `entity_aliases.resolve_exact` first, then EXACT raw-string
  membership of the caller's own `scoped_entities()`. The second is never normalized — a scoped id
  is an index fact, not free text a person typed, so fuzzing it could only produce a false match.
  This is what lets an anchored-but-unregistered id (which `list_entities` already serves honestly
  as a bare `{"id": …}`) resolve here too, closing the navigation loop.
- **`entity_aliases` fails OPEN on absence and CLOSED on corruption.** A missing/absent registry
  path yields `{}` — entity-first resolution and registry enrichment simply find nothing. Malformed
  JSON, or a top level that is not `{"entities": {…}}`, RAISES: silently degrading retrieval has no
  signal anywhere an operator or a golden run would see it. One loader (`_load_entities`) behind
  both readers, so the two postures cannot diverge per caller.
- **`_norm` is deliberately NOT `kernel.normalize.normalize`.** That one is the registry's own
  stricter folding for resolve-before-mint collision detection, where a false negative lets a
  duplicate entity through a gate. This one only has to recognize a registered name inside a
  question; a false negative here costs a fallback to ordinary search.
- **`_expansion_terms`** — the registry's OTHER names for a resolved entity (canonical name +
  aliases) handed to the LEXICAL arm as extra OR-lexemes, so a query naming an alias matches pages
  naming the canonical form and vice versa. The vector arm embeds the raw query untouched.
  Registry-missing serves `()`; registry-malformed raises, the standing posture.
- **`UnavailableEmbedder`** — what a keyless process gets instead of an embedder. It raises rather
  than returning a vector, because a degraded embedder returning SOMETHING is the `--embedder fake`
  hazard wearing a different hat: a query embedded into a space the index was not built in returns
  unrelated results and reports success. `require_embedder` reads it by `isinstance`, never by
  attribute probe — `test_arg_length.py` uses a `Poison` double that raises on ANY attribute
  access to prove the length check runs before the embedder is touched at all.
- **`NO_REPLY_WAITING`** — the ONE sentence every `brain_reply` IDENTITY failure answers with
  (nonexistent id, someone else's row, someone else's row that is not even parked). Written once
  as a constant because the security property IS that the three are byte-identical. A STATE
  failure for an ALREADY-authorized caller may name the real status: that caller can read the row
  through `brain_submissions` anyway, and the generic sentence would actively mislead someone
  looking at their own capture.
- **Attribution and scope are decided here and nowhere else.** `submitted_by` is `self.identity`,
  the value the transport resolved; a submitter sees their own rows, an unrestricted identity sees
  the whole queue — from `self.audiences`, never from a client argument. There is no
  `submitted_by` filter on `brain_submissions` at all. A steward may reply on a submitter's behalf,
  and the reply is attributed to who actually made it (`on_behalf_of` names the other party).
- **`brain_reply`'s audit row records the answer's SIZE and HASH, never its text** — the hash
  joins the row to the `reply` column for anyone who needs to prove what was said, which is what an
  audit trail owes and no more. Same posture `_submit_audit_args` already takes for material.
- **`Settings`** — `identity`, `identities_path`, `entity_registry_path`, `knowledge_repo`, `stewards_path`, `dsn`,
  `embedder`, `llm`, `model` (`gpt-5.6-terra`), `reasoning_effort`. An EXPLICIT `--identities` /
  `--entity-registry` wins over the `--repo` convention, and that precedence is load-bearing: the
  deployed server passes no `--repo` at all, so derivation alone would leave both permanently
  empty in production. `stewards_path` (`--stewards`, `$STIGMERGY_STEWARDS_PATH`) is the baked snapshot a process with no checkout resolves stewards from; the repo read wins wherever one exists. `knowledge_repo` has no CLI flag of its own — `$STIGMERGY_KNOWLEDGE_REPO`, or
  `--repo` — and only `review`'s steward resolution still needs it; every read tool and both
  fast-lane write tools work with it empty, exactly like a keyless embedder.
- **`_result_for` returns `None` unless a call site supplied `summarize` AND the call succeeded.**
  An error outcome has no return value to summarize. `search_brain` ships `{"hits": count}`;
  `ask` ships `answer.service.audit_summary`'s shape; every other tool carries a NULL column.

## Tests

`tests/server/` — 31 test modules plus `conftest.py`, ~7,300 lines. Postgres-backed suites skip
cleanly without `make db-up` and FAIL (never skip) when `$STIGMERGY_TEST_DSN` is set (CI mode);
`tests/testdb.py` refuses any database but `stigmergy_test`, and every DSN handed to real runtime —
the `stigmergy-server` subprocess, the HTTP app's own connection — goes through
`require_test_database` first, because both of those WRITE.

| Suite | Covers |
|---|---|
| `conftest.py` | `Fixture` (a hand-built knowledge repo shaped like the write path's output contract), `indexed` (real Postgres + fake embedder), `mcp_session` (a real stdio subprocess + a real MCP client), `make_service`, `build_test_http_app`/`run_http_server`, the review lane's own `env`/`conn`/`make_review_service`, `require_gitleaks`, and the autouse `no_real_github_app` guard |
| `test_identity.py` | both halves: the file-backed resolver's fail-closed paths, and `hash_token`/`load_token_store`/`resolve_email_for_token` |
| `test_acl_visibility.py` | the exhaustive truth table (page acl × client audiences), unrestricted and malformed included |
| `test_acl_empty_and_malformed_e2e.py` | the same two cases end to end through the real build + real service: `acl: []` and a YAML shape the index cannot recognize must reach the SAME observable state, never silently open |
| `test_service_acl.py` | `BrainService` end to end on all three read surfaces: enforcement, the existence-leak guarantee, the output shape |
| `test_fence.py` | the UNTRUSTED-DATA fence is inescapable in-band — imported FROM `service` on purpose, so a `service.py` that stopped re-exporting the hardened version would be caught |
| `test_startup.py` | fail-closed startup through the real `main`: no/unknown identity, malformed identities file, unreachable Postgres, a forged `index_meta`, and the clean missing-key exit |
| `test_mcp_adapter.py` | the tool closures in-process against a `create_autospec(BrainService)` double — the coverage the subprocess harness genuinely exercises but `coverage.py` cannot see |
| `test_mcp_harness.py` | the real MCP protocol over a spawned stdio subprocess — the transport is part of the contract |
| `test_ask_mcp.py` | `ask` over the real stdio protocol: answer, citations, verdict, `built_at`, with `ANSWER_LLM=fake` |
| `test_service_layer_wrapping.py` | `_call`/`call_async`: rate limit first, then the call, then an audit write in a `finally` — pure, against fake doubles |
| `test_arg_length.py` | `MAX_ARG_CHARS` before any DB/embedder/LLM work, proven with poisoned doubles that raise on any touch; also that the audited `error_class` is exactly `"ValueError"` |
| `test_ratelimit.py` | the token-bucket boundary (30th ok, 31st refused) against an injectable fake clock; no database |
| `test_audit.py` | the DDL, the writer's swallow-on-failure guarantee, and a real row end to end — including the three shaping bounds |
| `test_transport_http.py` | the HTTP transport end to end: a real uvicorn server, a real `streamablehttp_client`, the production wiring, two identities and their audit attribution |
| `test_host_header.py` | the DNS-rebinding allowlist fix (`$STIGMERGY_PUBLIC_HOST`) — the pilot bug's permanent regression guard |
| `test_token_hygiene.py` | no real token or hash may land in a tracked file in this repo — checked mechanically rather than by review |
| `test_issue_token_cli.py` | the issuance CLI's argparse/print plumbing and its email check |
| `test_service_capture.py` | the write half: attribution, the forgery refusal, evidence archiving, submitter scoping, and the rate-limit/audit wiring — two tiers (guards that raise before touching the DB, then real Postgres) |
| `test_keyless_capability.py` | a missing embedder degrades `search_brain`/`ask` only — asserted at BOTH levels, the tool closure (where the message a person reads is shaped) and `require_embedder` (so a tool cannot merely *happen* to fail downstream) |
| `test_rebuild_while_serving.py` | a live rebuild refreshes a serving process with no restart, and searches during one never error |
| `test_webhook.py` | the largest suite (43 tests): HMAC valid/invalid/absent/oversized, the byte-identical 401, the exact-path exemption AND the prefix that must not be exempt, event/branch/repo filtering, the two-phase transaction, the file cap through the real endpoint, the `job_runs` row, immediate searchability, and the two split-chain propagation tests (live `-p<n>` parts, and never across directories) |
| `test_entity_aliases.py` | `load_aliases`/`resolve_entity` (longest match, whole-word boundaries, fail-open on absence, raise on malformed) and `load_registry`/`resolve_exact` |
| `test_read_page_graph.py` | `type`/`status`/`supersedes`/`superseded_by`, the `{path, title}` link shape both directions, the existence-leak check, `NAV_CAP` truncation stated, and a hostile title neutralized |
| `test_entity_tools_pg.py` | `list_entities`/`describe_entity`: scoped vocabulary, registry absent/missing/malformed, id/name/alias resolution, byte-identical absence for unknown vs out-of-scope |
| `test_entity_tools_neutralization_pg.py` | the same surfaces against HOSTILE values — a shape test alone would stay green if neutralization were never applied |
| `test_entity_first_search_pg.py` | entity-first resolution witnessed twice: directly against `BrainService.search`, and through the real `search_brain` MCP surface; the explicit-filter rule and the blend's own property (resolution changes order, never membership) |
| `test_granularity_tripwire_pg.py` | the standing granularity instrument: the same content filed one-page-per-subject vs bundled with `entity: []`, proving the bundled form is structurally invisible to all three entity mechanisms |
| `test_settings_entity_registry.py` | `--entity-registry` beats the `--repo` convention |
| `test_pilot_report.py` | `build_report`'s sections against real rows, and that it runs no DDL |
| `test_review.py` | the review lane: disjoint kinds, boundary neutralization, the append-only ledger (a second decision does not overwrite the first), the git-untouched property across every REMAINING kind × verdict pair against a REAL ref (`approve` excluded by name, ADR 030 D5), the secrets scan over a note, and an entity-proposal approve's own section — a real mint against a real bare remote (commit, trailer, page, registry, the ledger's `extra`), the old-shape/self-approval/non-steward/drift/credential-missing refusals, and a forbidden-character name refused via `birth.prepare`'s own validation |
| `test_admin_branch.py` | the admin console's ASGI branch over the REAL `build_http_app` wiring: inert without its env, and — with it — that the MCP token store does not open the console and the admin token opens nothing on `/mcp`. See [`admin/index.md`](../admin/index.md) |

`tests/test_architecture.py` pins this package's edges — more of them than for any other package:

| Pin | Holds |
|---|---|
| `test_server_never_imports_the_pipeline` | direct imports only (the pipeline package is gone; the rule outlived it) |
| `test_review_transitive_kernel_reach_is_a_named_declared_exception` | a SUBPROCESS import-graph check: `review.py` may transitively load exactly five `stigmergy.kernel` modules, no more AND no fewer. An AST check cannot see a transitive load at all, and this one names both new reach and stale declarations |
| `test_server_imports_the_index_as_a_library` | the positive edge — the server is built on the index seams |
| `test_server_service_layer_never_imports_answer` | everything except `mcp_server.py`; the layer must not invert |
| `test_server_imports_capture` / `test_capture_never_imports_server_answer_or_pipeline` | the one-way capture edge |
| `test_server_never_imports_the_librarian` | the two symbol-scoped allowlists |
| `test_review_actually_uses_its_declared_librarian_exception` | `declared ⊆ used` — a SUPERSET assertion, because the old any-intersection version passed with two of eight declared symbols unused |
| `test_webhook_actually_uses_its_one_declared_librarian_exception` | the same, for `githubapp` |
| `test_server_review_never_imports_the_async_librarian_loop` | `worker`/`processing`/`agent`, asserted independently of both allowlists |
| `test_server_never_imports_entities_beyond_the_one_declared_review_lane_exception` + its positive twin | the six entity symbols (`situations`, `generator.canonical_id_for`/`ENTITY_TYPES`, `remote`, `errors.EntityError`/`CapabilityUnavailableError`) |
| `test_server_never_imports_slack` | nothing may import the Slack transport |
| `test_slack_imports_only_server_and_answer` | the other side: `stigmergy.slack` reaches only server/answer/slack/`review_kinds` (plus `store.py`'s one `capture.schema` edge) |
| `test_stigmergy_review_kinds_is_the_bottom_of_the_stack` | the kind constants' module imports nothing from this project |
| `test_every_reader_of_an_acl_bearing_store_enforces_or_is_a_named_exception` | AST-based, so a docstring MENTIONING `visible()` cannot satisfy it. **No module in this package is a named exception** — `service.py` is expected to enforce, and does |
| `test_the_untrusted_data_fence_is_built_only_in_stigmergy_text` | `server/service.py` was removed from the exception list when it started re-exporting; adding a fence literal back here fails |

## Common tasks

| Task | Touch |
|---|---|
| Add a new MCP tool | a closure in `mcp_server.build_mcp` calling a new `BrainService` method that rides `_call`/`call_async` — never a closure that talks to Postgres directly. Give it a narrow known-exception tuple and a class-name-only fallback |
| Add a new HTTP route with its own auth scheme | a new, reviewed exemption in `_BearerAuthMiddleware.__call__`, matched by EXACT path against a new named constant imported by both the mount and the middleware — never a prefix or a regex |
| Change what `audit_log.result` records for a tool | its `summarize` callback — counts, paths and booleans only, never a raw verdict dict or drafted text |
| Add a new expensive tool that needs its own budget | one entry in `RateLimiter._extra` (`{tool: (capacity, {})}`) — never a third `if tool == …` branch, and never a bare constructor parameter that nothing reads (see Notes) |
| Change the webhook's cap, branch or repo | `WebhookSettings`/`webhook_settings_from_env` — env-only, resolved once; `_parse_file_cap`'s docstring carries the "0 is invalid, not unlimited" history |
| Change entity-first resolution's matching rule | `entity_aliases.resolve_entity`/`_norm`; if the change touches what the LEXICAL arm sees, it is `service._expansion_terms`. The golden set is what arbitrates either |
| Change the rank-time entity boost | `index.rank.contract_factors` — but the id must keep arriving as `entity_hint` from `service._search`, never be re-derived inside the ranker |
| Change what links/backlinks or the timeline show | `_capped`/`_cap_note` (the shared base) and `_nav_section`/`_timeline_section` (the per-surface item shape) — never a third cap+note |
| Change the navigation cap size | `service.NAV_CAP` — one named constant, never a literal `20` at a call site |
| Add a review-item kind | the string in `stigmergy.review_kinds.ITEM_KINDS` (the one definition), a branch in `review._collect_open_items` that classifies BEFORE any generic fall-through, a `_decide_*` function with its own guard, and a verdict vocabulary decided on its own terms |
| Add a fact to the pilot report | `pilot_report.build_report`, reading a column another milestone already wrote — never a new write, and never DDL |
| Change the HTTP body ceiling | `transport_http.MAX_REQUEST_BODY_BYTES` (bearer path) or `webhook.MAX_BODY_BYTES` (webhook path); decide which threat model you mean |
| Change what a governance decision may do | `review.review_decide`/`_decide_*` — Postgres-only for `reject` and every `parked-capture` verdict, always; adding a git write to either reopens ADR 026 D1's ruling and breaks `test_review_decide_never_writes_to_git_the_full_matrix`. The one exception is `_decide_entity_proposal`'s `approve` branch, which mints through `_mint_entity_proposal` (ADR 030) — changing what THAT may do is a change to `entities.mint.mint` (shared with the CLI) or to the credential/URL resolution in `entities.remote.mint_via_clone`, never a second ad hoc git call here |

## Notes

**Design facts worth not rediscovering.**

- **`RateLimiter` accepts a `propose_per_min` and nothing spends it.** `_extra` registers `"ask"`
  alone, so there are TWO live buckets, and `DEFAULT_PROPOSE_PER_MIN` / `self.propose_per_min`
  survive as the SHAPE for the next expensive write tool — a deliberate dead knob, named as one in
  `ratelimit.py`'s own docstring. Adding that tool is one line in `_extra`, never a third `if tool
  == …` branch in `check` and never a second bare constructor parameter.
- **`entity_aliases.py`, `acl.py` and `service.py` carry parenthetical asides about a facts store
  that no longer exists.** They are correctly written in the past tense and are load-bearing
  history: `acl._labels`'s bare-string branch, for instance, exists only because of that dead
  stored shape and is kept deliberately so a future stored shape fails closed rather than crashing.
  Do not "clean up" the branch along with the prose.
- **Entity-first resolution lives entirely in this package.** It once split across
  `entity_aliases.py` here and a private helper in `stigmergy.answer`, so only
  `ask` benefited. `BrainService._search` now calls `entity_aliases` directly and the answer-layer
  wrapper is deleted, not merely unused — every client (stdio, HTTP, Slack, `ask`) gets it, which
  is "how to query well lives BEHIND the API" made true in code rather than stated.
- **The resolved entity has two more jobs**, both TOLD rather than inferred: it becomes
  `entity_hint` (the rank-time boost, matched by membership in `rank.contract_factors`) and it
  becomes `fts_expansion` (the registry's other names, OR-ed into the lexical arm's query).
  `_run_search` is the one implementation both the entity-scoped attempt and the plain call ride,
  so there are never two places that fetch, filter and shape a hit list.
- **`slack_submissions` does not live here.** It moved to `stigmergy.slack.store`: the
  table's vocabulary (`team_id`/`channel_id`/`slack_user_id`) is Slack's, and a repo where that
  vocabulary lives a layer below the package it names is a layering violation
  (`test_no_slack_identifiers_below_the_slack_package`), whatever door it reached `capture.schema`
  through.
- **`SLACK_DOOR` crosses through `service.py` on purpose.** `stigmergy.slack` may import only
  server/answer/`review_kinds` (its `store.py` alone holds the pinned `capture.schema` edge), so
  the door constant it hands back to `BrainService(door=…)` re-exports through this layer rather
  than becoming a second sideways edge. `capture.schema` stays the owner.
- **`review.py` imports `service` lazily, inside functions, in three places** (`_neutralize`,
  `_check_len`, `_neutralize_leaves`) — `service.py` imports THIS module at module scope to mount
  the two methods, so the reverse edge must never be taken at import time. By the time any of
  those functions runs, both modules are fully loaded, so the deferred import costs nothing. Same
  pattern `mcp_server.py`'s `ask` closure uses for `stigmergy.answer`.
- **The webhook is the only place in this package that writes `pages_index`**, and it does so
  without a `BrainService`, an identity or an ACL scope — correctly, because the rows it writes are
  the corpus itself, not a view of it for one caller.
- **Two audited fields carry caller CONTENT verbatim, and that is a named, accepted exemption
  rather than an oversight.** `audit_log.args` is content-free for every tool EXCEPT `ask`'s
  `question` and `search_brain`'s `query`, where the caller's free text is written as-is (bounded
  by `MAX_ARG_CHARS`) for operator diagnosability — without it, "why did this come back empty" is
  unanswerable from the database at all. Hashing the two fields instead is the other arm, deferred.
  `audit.py`'s docstring carries the reasoning in full.
- **`build_service` wires no `RateLimiter`, deliberately.** The 30/10 budgets exist to protect
  spend behind a PUBLIC url; one local operator over stdio already has unmediated Postgres/OpenAI
  access through other CLIs, so throttling stdio would add friction without closing any exposure —
  and `test_rebuild_while_serving.py`'s rapid-fire stdio hammer assumes exactly this.
- **Every startup ensures three schemas in the same order** — `ensure_audit_table`,
  `ensure_capture_schema`, `review.ensure_review_schema` — on both transports, all three behind
  `capture.schema.startup_ddl_lock`. `IF NOT EXISTS` is a check, not a lock: two servers starting
  against a fresh database can both see "does not exist" and the loser dies on `pg_class`.
