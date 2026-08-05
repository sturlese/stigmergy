# slack — the third transport

Narrative doc: [`docs/reference/slack.md`](../../../docs/reference/slack.md) (the how and why for
an operator and a submitter) for the transport itself; the steward doorbell and review
surface (`doorbell.py`/`review.py`) are narrated in the operator runbook's own doorbell section
instead. Design records: [ADR 017](../../../docs/decisions/017-slack-transport.md) (the transport),
and [ADR 026](../../../docs/decisions/026-the-purge.md) (**read this one first**: D1 is why
this package's `review.py` handler offers no Promote button, and D3 is why the doorbell
has one fewer background concern to reason about). Operating it day to day: the runbook's
[three process groups](../../../docs/reference/operator-runbook.md#the-three-process-groups)
(this package is the `slack` one) and
[draining parked rows](../../../docs/reference/operator-runbook.md#draining-parked-rows) (what the
doorbell rings about). This file is the code map — for whoever is
about to edit this package, not run it.

**`tests/slack/` proves this package's own logic, never a real workspace's behavior.** The whole
suite is offline (`FakeSlackGateway`, real Postgres, the `fake` embedder/answer synthesizer), so
rate limits, real event-payload quirks and Socket Mode reconnect behavior are outside what a green
run says anything about. One measured walk against a real workspace exists and is written up at
[`docs/archive/socket-mode-spike.md`](../../../docs/archive/socket-mode-spike.md): steady-state
answering, the singleton refusing a second process, `kill -9` and reconnect, and events delivered
while the connection was down (redelivered, not lost). Read it before making any claim about live
behavior — including the per-database scope limit in Data & contracts below, which that walk is
where the hazard was found.

## Purpose

A third transport, a sibling of stdio (`server/mcp_server.py`) and HTTP (`server/transport_http.py`),
so a non-technical pilot user can ask and capture from Slack instead of a terminal or a bearer
token pasted into a config file. It resolves *who is asking* from a Slack event and calls the SAME
`BrainService`/`AnswerService` every other transport calls.

**This package is a transport. It enforces nothing.** `stigmergy.server.acl.visible()` remains the
one place visibility is decided, exactly as it is for stdio and HTTP. Any function added here that
asks "is this page/answer visible to this person" — rather than building a correctly-scoped
`BrainService` and letting the service layer answer that question — is a design error, not a
style choice: it would be a second enforcement point the day it disagreed with the first one.
`channels.channel_audiences` and `mention._scope_could_be_wider` look like exceptions and are not:
the first resolves a channel's *label set* from a config file (a fact about the channel, not a
verdict about a page), and the second compares label sets arithmetically to decide whether a
comparison is even worth running — neither ever inspects a page or calls `acl.visible()` itself.
**`doorbell.py`/`review.py` hold to the identical rule for the review lane**: neither decides who
may approve, resolve, or see an item — `review.review_decide`/`review._is_steward`
(`stigmergy.server`) remain the one place a governance decision is made. This package resolves who is
asking, reads `ops/stewards.json` to know who to ring, and calls the same seam every MCP caller
calls. **The canon lane's own half of this surface is gone**
([ADR 026](../../../docs/decisions/026-the-purge.md) D1) — the Promote button, the company-wide
confirmation modal, and the canon-pr/contradiction buttons and doorbell cards; **the contraction
then removed the dormant loop's `candidate` kind**
([ADR 027](../../../docs/decisions/027-the-contraction.md)). What is left is exactly the TWO kinds
a human decides on directly — an entity proposal and a parked capture — which is what
`stigmergy.review_kinds.ITEM_KINDS` contains.

**Every learning-loop staging hook is gone from this package too.** `mention._stage_exchange`,
`replies`' follow-up hook and the `stage_slack_turn` / `stage_slack_followup` calls they wrapped
no longer exist. What survives them is a rule, stated under Avoid: any bookkeeping write placed on
a handler's hot path is wrapped in its own `try`/`except`, because those calls sat ahead of
`_edit_or_fallback` and would otherwise have taken the "please wait" placeholder down with them.

## Key entry points

| Module | Owns |
|---|---|
| `identity.py` | Slack profile email -> `ops/identities.json` via `resolve_audiences` (the SAME function and file every transport uses); `is_ignorable_event`; `is_configured_workspace` (the cheap synchronous half of the workspace check, extracted so a caller can ask it without any identity work); the `UsersInfoCache` (email, the doorbell's reverse lookup, and display names — three maps, one shape); the five-way `IdentityResult` |
| `channels.py` | `ops/slack-channels.json` — a channel's audience scope, empty-set default for anything not listed |
| `context.py` | `SlackContext` — the process-wide resources (conn, embedder, rate limiter, audit, evidence, cache, link resolver, the "Show it here" token store) and `build_service`, the per-identity `BrainService` constructor every handler calls; the one decline seam (`decline`) and the one post-or-log seam (`post_or_log`) |
| `mention.py` | `@brain <question>` / a DM: the placeholder, the channel/DM scope split, the cheap retrieval-set comparison and the DM fuller answer, the edit-retry-then-fallback policy |
| `capture.py` | the 🧠 gesture: public-channel-only, the verbatim thread material (participant display names fanned out through `asyncio.gather` over the identity cache's misses), the provenance hints, the reserve-then-fill dedup, and the instant progress-reaction lifecycle (`mark_in_progress`/`finish_progress`, called from `app.py` around it, never from inside it) |
| `replies.py` | the submitter's ask-back reply (and nobody else's), and the "Show it here" click |
| `poller.py` | the push channel: `store.REPORTABLE_STATUSES` (`filed`/`needs_input`/`triage`/`rejected`/`resolved`/`failed`), read-only against `capture_queue` |
| `doorbell.py` | the steward doorbell: a second background task in this SAME process — never a fourth always-on one — read-only against `stigmergy.server.review.items_for_doorbell`; one notification per (item, steward) on a real state change, undeliverable outcomes recorded never swallowed, no material excerpt for a capture the librarian has not looked at. TWO item kinds: entity-proposal, parked-capture |
| `review.py` | the Block Kit review surface: buttons that call `review.review_decide_safe` — one call, one confirmation, never two (the canon lane's Promote button and its "click twice" rule went whole with the lane, [ADR 026](../../../docs/decisions/026-the-purge.md) D1) — plus two modal shapes: the note modal for the verdicts that require one piece of free text, and the entity-mint modal (ADR 030 D5) an entity-proposal Approve opens instead of firing directly, collecting the metadata it mints from |
| `render.py` | the PURE `(answer_dict, link_resolver) -> blocks` renderer, plus every other message's blocks; also the doorbell cards and the review surface's two modals (a free-text note/reason, and the entity-mint metadata form), over `stigmergy.review_kinds`' constants (renamed from `canon_kinds`) rather than `stigmergy.server.review`'s (see Notes) |
| `mrkdwn.py` | CommonMark -> Slack `mrkdwn` (bold, links, lists, inline/fenced code), code spans protected |
| `copy.py` | every user-facing string, in one place so a wording change is one diff. `STEWARD_NAME` (`$STIGMERGY_STEWARD_NAME`, default `your steward`) is the one interpolation |
| `gateway.py` | `SlackGateway` (the Protocol every Slack Web API call crosses) and `FakeSlackGateway`, the offline double every test drives |
| `bolt_gateway.py` | the real gateway, wrapping `slack_sdk`'s `AsyncWebClient` — imported only by `app.py` |
| `settings.py` | `SlackSettings.from_args` — the three Slack secrets from the environment, plus the same `server.Settings` every transport builds from `--repo`/`--identities`/`--entity-registry`/`--dsn`/`--embedder`/`--answer-llm` |
| `app.py` | `stigmergy-slack`'s entry point: Bolt's async app, Socket Mode, event registration, `acquire_singleton_lock`, and the poller AND doorbell as two background tasks in this one process |

`app.py` is where one event's path starts; read it first when tracing an event end to end — every
listener acks immediately, then delegates to `mention`/`capture`/`replies`, none of which import
`slack_bolt`/`slack_sdk` themselves.

## Use these

- `identity.resolve_slack_identity` — the ONE function every handler calls before constructing a
  `BrainService`. It has **five outcomes** (`Ignored`, `ForeignTeam`, `TransientFailure`,
  `NoAccess`, `Resolved`), deliberately five separate types rather than a shared sentinel, so a
  `match`/`isinstance` is exhaustive-checkable and a caller cannot quietly collapse two of them.
  **Every outcome except `Resolved` is fail-closed, and no `BrainService` is constructed on any of
  those paths**:
  - `Ignored` — a bot/app/workflow event, or the bot's own message. Checked FIRST
    (`is_ignorable_event`), before identity is even attempted. Zero Slack traffic.
  - `ForeignTeam` — the event's own workspace does not match the configured one. Silent
    a stranger from an unrelated workspace gets no "ask your steward" instruction
    that presumes a relationship they do not have.
  - `TransientFailure` — `users.info` itself failed (a timeout, a 5xx). NOT an unmapped user
    the caller renders the server-error copy, never "ask your steward to add you".
  - `NoAccess` — no email, an empty email, or an email `resolve_audiences` does not recognize.
    ONE outcome for all three reasons — a caller must not let a determined prober learn the
    identity file's shape by comparing responses.
  - `Resolved` — an email and its audience scope, the only outcome a `BrainService` is ever built
    from.

  A new handler that builds a `BrainService` before checking which of these five it got is the
  exact defect class this type was built to make impossible to write by accident.

- `context.SlackContext.decline` — the ONE way this package tells someone a request was declined
  for an identity reason. Before this seam existed the package declined **three different ways**:
  silently (a bystander no-op in `replies.py`), ephemerally (`capture.py`), and — wrongly —
  publicly (`mention.py`, disclosing an identity failure to the whole channel). A new identity
  refusal goes through `decline`, not a fourth way.
- `context.SlackContext.post_or_log` — the ONE seam every non-critical Slack send goes through:
  post-or-log, never raise. Before it existed, a Slack outage while posting could crash a handler
  straight out into Bolt. A caller that needs the response itself (the placeholder's own `ts`,
  so the edit-retry-then-fallback policy has something to retry against) does NOT use this seam —
  see `mention._edit_or_fallback`, which keeps its own more specific policy.
- `context.SlackContext.build_service` — the per-identity `BrainService` constructor, sharing
  every process-wide resource. `rate_limited=False` is for SYSTEM-initiated work the asker did not
  request (the wider-scope comparison and its possible DM `ask`) — it must never draw on the asker's own
  rate-limit budget, or an asker for whom content was withheld becomes measurably likelier to hit
  the public rate-limit message on their next real question.
- `service.call_async` (via `mention._run_ask`) — **every `ask` goes through it.** It is the seam
  that writes `audit_log` and spends the `ask` rate-limit bucket. Before this fix, `_run_ask`
  called `AnswerService(service).ask(question)` directly, and no Slack `ask` — ever — wrote an
  audit row or spent a rate-limit bucket, no matter which `service` it was handed. A new call site
  that answers a question without going through `call_async` reopens exactly that gap: Slack
  questions become invisible to the audit trail again.
- `render.render_answer` — the ONE renderer for an `answer` dict, PURE (`(answer_dict,
  link_resolver) -> blocks`, no gateway, no event). A new message type that shows model- or
  page-derived text renders it through `_render_markdown`/`escape_mrkdwn` and puts it in a
  `section` block; it never composes a `context` block (verdict, sources) around untrusted text
  and never reads `answer['confidence']` (see Avoid, below).
- `store` (`reserve`/`attach_submission`/`release_reservation`/`find_thread_submissions`/
  `due_for_report`/`mark_reported` for `slack_submissions`; `last_notified_state`/`mark_notified`
  for `steward_notifications`) — the ONLY way this package reads or writes either table, and this
  package's OWN, ONLY door into `stigmergy.capture`
  (see Notes). A new Slack-originated write reuses `reserve`'s dedup pattern rather than inventing
  a second one.
- `review.review_decide_safe` (`stigmergy.server.review`) — the ONLY call `review.py` makes to
  actually change anything. There is no second, git-writing call beside it (`canon.
  promote_proposal_safe` went whole with the canon lane) — every confirmation this
  surface posts is the same plain message, because there is only one kind of outcome left to
  confirm.
- `doorbell._load_stewards_cached` — the ONE place `ops/stewards.json` is cached in this package,
  TTL-bounded (300s) and scoped to the doorbell's own notification decisions only. It must never be
  wired into `review._is_steward` (the AUTHORIZATION check `review_decide` runs) —
  that read stays uncached and fresh at every call, because a revoked steward's approval must never
  succeed off a stale doorbell-side cache. A new caller that needs "who is the steward for this
  scope" for a DECISION calls `review.load_stewards` directly, never this cached wrapper.
- `doorbell._record_delivered` — the ONE place a successfully delivered doorbell DM is recorded
  (one `audit_log` row per delivery, attributed to the notified steward), the positive counterpart
  to `review.record_undeliverable`'s `job_runs` row. Together they are what makes the spec's success
  signal ("the steward never discovered a pending item by remembering to look") measurable from a
  pilot report instead of asserted on faith; a new doorbell event reuses both, never silently skips
  the delivered half because only the failure half existed before this pass.
- `gateway.SlackGateway` / `FakeSlackGateway` — every handler takes a gateway as a plain argument.
  A new handler does the same; it does not import `slack_sdk`/`slack_bolt` itself (only `app.py`
  and `bolt_gateway.py` may).

## Avoid / anti-patterns

- **Never decide visibility here.** No function in this package should end up answering "can this
  person see this page/answer" by itself — that is `acl.visible()`'s job, reached only through a
  correctly-scoped `BrainService`. `channels.channel_audiences` resolves a channel's *label set*,
  never a verdict; `mention._scope_could_be_wider` compares label sets *arithmetically*, never a
  page. If a new function's return value is used as "may this person see X", something upstream
  of it already called `acl.visible()` — this package only ever supplies the scope that call uses.
- **Never take the workspace check from `context["team_id"]`.** Bolt's `context["team_id"]` is the
  *installation's* team (which workspace this app is installed in), not the event's sender.
  `app._event_team_id` reads `event.get("user_team") or event.get("team") or body["team_id"]`
  instead — the sender's own workspace, then the envelope's. For an external user in a Slack
  Connect shared channel (the exact threat the spec
  names), the installation's team_id equals the configured one *by construction*, so comparing
  against `context["team_id"]` is a tautology that can never fail: every foreign-workspace mention
  would silently pass the check meant to catch it. Every call to `resolve_slack_identity` must
  receive the EVENT's own team id — `tests/test_architecture.py`'s
  `test_no_call_to_resolve_slack_identity_passes_the_configured_team_id_as_the_events_own` pins
  this by AST inspection, because the bug is invisible from a diff that merely reads correctly.
- **Never decline, post or fail silently outside `SlackContext.decline`/`post_or_log`.** A new
  failure path that reaches for `chat_post_message`/`chat_post_ephemeral` directly and lets a
  `SlackApiError` propagate re-opens the crash-on-outage defect both seams were built to close.
- **Never let a side-effect that is not the answer propagate an exception into the surface it rides
  alongside.** The rule outlived its original subject: the learning loop's staging calls sat ahead
  of `_edit_or_fallback` and would have taken the "please wait" placeholder down with them, so each
  was wrapped in its own `try`/`except`, logged loudly with a correlation ref, and continued —
  deliberately NOT routed through `post_or_log` (which is for a Slack SEND) and deliberately not
  silent. Those calls are gone with the loop, but any future bookkeeping write placed on a
  handler's hot path inherits the same requirement.
- **Never let `answer['confidence']` reach a render.** The model's own self-reported confidence and
  the code-computed verdict are two different signals that can disagree; only the verdict — the
  checked signal — ever ships. `render.render_answer` does not read that key under any
  circumstance, and a new render must not either.
- **Never render a citation, a verdict or "Sources" as a `section` block.** They are `context`
  blocks behind a `divider`, deliberately: Slack's smaller, grey chrome for the bot's OWN trust
  signals, in a shape a prompt-injected answer body — always a `section` — cannot imitate no
  matter what it contains. Moving the verdict or Sources into a `section` (for "consistency", or
  because a `context` block truncates something) reopens the exact impersonation this structural
  choice exists to close; a text scrubber is not a substitute (see `render.py`'s module docstring).
- **Never treat Socket Mode as though it had leader election.** A second `stigmergy-slack` process
  against the same database is not a redundancy story here — Socket Mode dispatches every event to
  every connected process, so two live processes double-handle every event Slack delivers. See
  `app.acquire_singleton_lock` in Data & contracts, and never remove or bypass that call from
  `_async_main`.
- **Never call `AnswerService(...).ask(...)` directly.** Every `ask` goes through
  `service.call_async` (`mention._run_ask`), the seam that writes `audit_log` and spends the rate
  limit. A new ask-shaped code path that skips it produces an answer nobody can audit.
- **Never widen `HINT_KEYS` for a Slack-shaped fact.** `capture.schema.SOURCE_HINT_KEYS` is the
  compatible extension the 🧠 gesture's provenance uses; a new provenance field is a new key name
  added there, not a repurposed existing one and not a second hint channel of this package's own.
- **Never let the identity cache, or the "Show it here" token store, grow without bound.**
  `identity.UsersInfoCache` and `context.SlackContext`'s `_show_it_here_tokens` both cap their size
  with oldest-first eviction on insert, alongside a TTL — a process that runs for weeks needs both
  bounds, not just the TTL.
- **Never fold a second governance action into a confirmation, or offer an action the surface was
  not asked for.** There is no Promote button to fold into anything —
  `review._post_decision_confirmation` posts the SAME plain message for every kind/verdict, because
  `review.py`'s canon half (Approve/Promote as two separate clicks, D5) went whole with the lane. A
  future action that "helpfully" offers a further step by default reopens the exact
  one-click-does-two-things shape that lane's own removal makes moot; do not re-invent it here.
- **Never record WHO is deciding in a modal's `private_metadata`.** It carries only WHAT a decision
  is about (item kind, id, verdict, field, where to post the confirmation) — the identity making the
  decision is re-resolved from the SUBMISSION event's own authoritative body
  (`handle_note_modal_submission`'s `slack_user_id`/`event_team_id` arguments), never round-tripped
  through a value this code itself stamped when the modal was opened. This inverted the package's
  own load-bearing rule once, on the one surface that writes a
  governance verdict; a new modal follows the fixed pattern, not the one that regressed.
- **Never let the doorbell — or any render function — fall back to `stigmergy.server.review`'s
  `KIND_*`/`ENTITY_TYPES` for a pure renderer.** `render.py`'s whole reason for being "fully
  testable without those dependencies mattering" depended on it importing `stigmergy.review_kinds`
  (dependency-free, renamed from `canon_kinds`) instead of `stigmergy.server.review` (which drags in
  `librarian`/`entities`/`subprocess`/PyYAML for a few string constants) — see `review_kinds.py`'s
  own module docstring. `render_entity_mint_modal`'s `static_select` options are the newest
  instance: `ENTITY_TYPES` is read from `review_kinds`, never from `entities.generator` directly,
  for the identical reason. A new render function that needs an item-kind string or the entity-type
  list imports the root module, never the server one.
- **Never cache `ops/stewards.json` on the authorization path.** `doorbell._load_stewards_cached`'s
  300s TTL is for the NOTIFIER only; `review._is_steward` (what `review_decide` actually checks)
  reads fresh on every call by design, and nothing in this package should offer it a cached copy
  instead.

## Data & contracts

- **`identity.IdentityResult`** = `Ignored | ForeignTeam | TransientFailure | NoAccess | Resolved`
  — see Use these for what each means and why the caller must not conflate them. `Resolved` carries
  `email` and `audiences` (`frozenset[str] | None`, `None` = unrestricted).
- **`app._event_team_id`** — the sender's own workspace, in three ordered fallbacks:
  `event.get("user_team") or event.get("team") or body["team_id"] or ""`. Fails closed on an absent
  value (an empty/missing team id is never treated as "the configured workspace" — see Avoid).
  **The envelope fallback is not optional.** A `reaction_added` payload is
  `{type, user, reaction, item, item_user, event_ts}` and carries NEITHER `user_team` NOR `team` —
  those exist on message events only — so without `body["team_id"]` this returns `""`, the
  fail-closed rule in `identity.resolve_slack_identity` classifies every reaction as `ForeignTeam`,
  and the 🧠 gesture dies silently: no Slack traffic, not one log line. No test catches it, because
  every reaction test constructs its own payload and includes a team field the real event does not
  have.
- **`app._SINGLETON_LOCK_KEY`** / **`app.acquire_singleton_lock`** — `pg_try_advisory_lock` (never
  the blocking `pg_advisory_lock`, because a second machine must fail its startup immediately, not
  hang) on a key distinct from `capture.schema`'s own startup-DDL lock key. Session-scoped on the
  SAME connection this process holds for everything else, so the lock is released automatically the
  instant the process dies — a crash, a deploy, `fly machine stop`. This is a **mechanism**, not a
  comment in `fly.toml`: Socket Mode has no leader election of its own, so nothing else in this
  system stops a second `stigmergy-slack` process from double-handling every event.

  **Scope limit, and it is an operational hazard rather than a footnote: the lock is held per
  DATABASE.** Two deployments pointed at the same Slack app but different databases — a local bot
  on the docker Postgres and a staging bot on its own — take *different* locks, both start
  cleanly, and **double-handle every event Slack delivers**. The mechanism protects one deployment
  from itself; it cannot protect one Slack app from two deployments, and nothing else does either.
  The operational rule that follows: stop the local bot before a second deployment against the same
  app goes live. Measured, not theorised — see
  [`docs/archive/socket-mode-spike.md`](../../../docs/archive/socket-mode-spike.md).
- **`render.render_answer`** contract — `(answer: dict, link_resolver: Callable[[str], str | None],
  *, asker_slack_user_id="", mint_token=...) -> list[dict]`. `link_resolver` is injected
  configuration — `settings.no_link_resolver` (every path resolves to `None`) is `SlackContext`'s
  bare dataclass default, every test double's own default, AND what production wires
  (`app.build_context`): every citation renders with the "Show it here" affordance and no link. The
  one read site that once replaced this VALUE (never `render.py`'s own contract) was deleted with
  zero readers and was never deployed; the seam is unchanged, so a future resolver wires back in
  exactly the same place. `mint_token` is
  `SlackContext.mint_show_it_here_token` in production; the module-local default is
  deterministically unusable so a test that does not care about the button still works.
- **`context.SlackContext`** — the process-wide resources: `settings`, `gateway`, `conn`,
  `embedder`, `rate_limiter`, `audit`, `evidence`, `cache` (`UsersInfoCache`), `link_resolver`,
  `bot_user_id`, and the "Show it here" token store. Built once by `app.build_context`.
- **`gateway.SlackGateway`** (a `Protocol`) / **`gateway.SlackApiError`** — every Slack Web API
  failure this package sees is this ONE exception, collapsed from `slack_sdk`'s own error shape at
  `bolt_gateway._call`'s boundary, so no caller anywhere in `stigmergy.slack` needs to know
  `slack_sdk`'s exception types.
- **`capture.schema.SOURCE_HINT_KEYS`** (owned by `stigmergy.capture`, extended for this milestone) —
  `source_client`, `source_permalink`, `source_channel_id`, `source_channel_name`,
  `source_thread_ts`, `source_participants`, `source_message_timestamps`. Every value is a plain
  string (a list — participants, timestamps — is comma-joined and `MAX_HINT_CHARS`-truncated by
  `capture._material_and_hints` before it reaches `capture.schema`, never a structure that module
  parses; truncating a `source_*` hint loses none of "the thread, verbatim", and NOT truncating it
  made a long enough thread fail deterministically on every retry). **Server-composed is not
  trusted**: only `source_client`/`source_permalink` are unforgeable-by-a-client (door-gated at
  `reject_source_provenance_hints`), and `source_participants` is a display name any workspace
  member sets on themselves, crossing this door with no token at all — the librarian fences the
  whole hints dict as UNTRUSTED-DATA in the filing agent's prompt for exactly that reason. A new
  key here inherits both facts. See [`capture/index.md`](../capture/index.md) for the full
  allowlist story.
- **`store.py` owns TWO tables**, both living INSIDE this package (see [Notes](#notes)) — the DDL
  and every query for each. **`slack_submissions`**: the mapping the 🧠 gesture's dedup reservation
  and the poller's read-back both go through, UNIQUE on
  `(team_id, channel_id, thread_ts, slack_user_id)`. **`steward_notifications`**: one row per
  (item_kind, item_id, steward_email) carrying the `state` that pair was last notified at — the
  doorbell's "one notification per (item, steward), re-sent only on a state change" is that column
  compared against `_state_signature`, and an `undeliverable:<class>` state is recorded in the same
  column so a failure dedups without a third table.
- **`stigmergy.review_kinds.ITEM_KINDS`** (renamed from `canon_kinds`) — the TWO `KIND_*`
  strings this package's renderer and doorbell key on: `entity-proposal` and `parked-capture`.
  `canon-pr` and `contradiction` went with the canon lane
  ([ADR 026](../../../docs/decisions/026-the-purge.md) D1); `candidate` went with the learning loop
  ([ADR 027](../../../docs/decisions/027-the-contraction.md)).
  Imported from the package ROOT, never from `stigmergy.server.review` — see Avoid, above, and
  `review_kinds.py`'s own docstring for why a module below both consumers is what lets `render.py`
  stay dependency-light.
- **`stigmergy.review_kinds.ENTITY_TYPES`** (ADR 030 D5) — the closed six
  `render_entity_mint_modal`'s `static_select` offers, read the same way `ITEM_KINDS` is. Unlike
  `KIND_*`, this is a RESTATEMENT of `entities.generator.ENTITY_TYPES` rather than a shared
  definition both sides import — `review_kinds.py` may depend on nothing, so it cannot import
  `entities.generator`, and `stigmergy.server.review` was not changed to import it from here in this
  pass (it still reads `entities.generator.ENTITY_TYPES` directly). A drift test
  (`tests/test_architecture.py::test_review_kinds_entity_types_matches_the_generators_closed_list`)
  is what keeps the restatement honest in the meantime; folding `stigmergy.server.review` onto this
  same definition — matching `KIND_*`'s own pattern exactly, and retiring the drift test — is the
  natural follow-up.
- **`doorbell._state_signature`** — the fingerprint compared per (item, steward) to decide whether
  to re-notify. Folds in the item's `attempts` (`capture_queue`'s own monotonic
  redelivery fence) wherever present, so a requeue-and-reprocess that parks a row back into the SAME
  status/situation is still a detectable state change — a status/situation string alone used to
  produce the identical signature, and the bell never rang a second time for that item.
- **`doorbell.LOOKUP_FOUND` / `LOOKUP_NOT_FOUND` / `LOOKUP_FAILED`** — the tri-state result of
  resolving a steward's email to a Slack user id. A transient API failure (`LOOKUP_FAILED`) is never
  recorded as the same fact an honest "no such person in this workspace" (`LOOKUP_NOT_FOUND`) is —
  collapsing the two onto one `None` used to let a rate limit read as a permanent identity fact.
- **`review._MODAL_FIELD`** — which `(item_kind, verdict_token)` pairs need the generic note modal
  before `review_decide` is even called, and what field name that note lands under.
  `(entity-proposal, approve)` needs a modal too but is checked BEFORE this table, not added to
  it — its metadata modal (name/type/aliases/role/requeue) has no `(field, label, placeholder)`
  shape to fit. Every OTHER button fires directly with no modal — friction only where a human
  sentence, or a mint's metadata, is actually required.

## Tests

`tests/slack/` (offline throughout: `FakeSlackGateway`, real Postgres, the `fake`
embedder/answer synthesizer — no Slack credentials, no network):

| Suite | Covers |
|---|---|
| `test_identity.py` | the five `IdentityResult` outcomes, the workspace check (and its cheap synchronous half, `is_configured_workspace`), all three `UsersInfoCache` maps' TTL and bound, `resolve_slack_identity` populating the display-name map as a side effect |
| `test_channels.py` | `channel_audiences`'s empty-set default and malformed-file refusal |
| `test_mention.py` | `@brain`/DM, the channel/DM scope split, the retrieval-set comparison, the edit-retry-then-fallback policy, every error state |
| `test_capture.py` | the 🧠 gesture, verbatim material, provenance hints, the reserve-then-fill dedup, the progress-reaction lifecycle's own units (`mark_in_progress`/`finish_progress`, best-effort under a reactions-API outage), the display-name cache sparing a second `users.info` |
| `test_replies.py` | the ask-back reply (submitter-only), the "Show it here" click and its server-side scoping |
| `test_bolt_gateway.py` | the real gateway's error collapsing onto the one `SlackApiError`, and `reactions_add`/`reactions_remove`'s own translation of `already_reacted`/`no_reaction` to success vs. a real failure (`missing_scope` included) still raising |
| `test_poller.py` | every reportable status, including `failed`, and "one message per state change" |
| `test_render.py` | the honesty properties: an unrecognized verdict raises, `confidence` is never read, citations/verdict render as `context` |
| `test_mrkdwn.py` | the CommonMark -> mrkdwn transform, code-span protection |
| `test_app_wiring.py` | every listener acks first; the singleton lock; the event-team-id (never `context["team_id"]`) wiring; the progress-reaction lifecycle end to end (done mark on success, cleared-only on a refusal, zero reaction traffic for a foreign workspace, a reactions-API outage never breaking the capture it wraps) |
| `test_doorbell.py` | the three doorbell properties end to end: one notification per (item, steward) on a real state change (including the requeue-and-reprocess-into-the-same-status case), undeliverable outcomes recorded and deduplicated by reason class, the tri-state Slack-lookup result, `_summary_for_doorbell`'s fail-closed withholding, `_record_delivered`'s audit row |
| `test_review.py` | button routing (`_parse_action_id`'s two prefixes), the modal open/submit round trip, `private_metadata` carrying WHAT never WHO, and that every decision posts the SAME plain confirmation — there is no Promote button and no company-wide confirmation left to test |

`test_store_pg.py` covers the mapping table's own primitives
(`reserve`/`attach_submission`/`find_thread_submissions`/`due_for_report`, plus the dedup-key
migration) against real Postgres. `tests/test_architecture.py`'s slack-boundary tests pin the
import list (`{server, answer, capture.schema, review_kinds}` — `capture.schema` scoped to
`store.py` alone, `review_kinds` allowed anywhere in the package, renamed from
`canon_kinds` — see Notes), the SQL-column pin
(`test_slack_store_sql_column_names_exist_on_capture_queue`, which catches a `capture_queue` rename
that this package's raw joins would otherwise hit only at runtime), that
nothing imports `stigmergy.slack` back, the knowledge-direction test (no Slack identifier below this
package), and the workspace-check AST rule described in Avoid, above.
`tests/test_deployment_config.py` pins the third `fly.toml` process group (`slack`, no HTTP
service, never scaled past one machine).

**No test here, and no test anywhere in this repo, exercises a real Slack workspace.** The one
walk that did was manual and is written up at
[`docs/archive/socket-mode-spike.md`](../../../docs/archive/socket-mode-spike.md); a claim about
live behavior cites that document or it cites nothing.

## Common tasks

| Task | Touch |
|---|---|
| Add a new Slack event/action listener | `app.build_bolt_app` — ack first, `is_ignorable_event`, `app._event_team_id` (never `context["team_id"]`), delegate to a handler module; wrap the body in the same `except Exception: _log_listener_failure(...)` every existing listener has |
| Add a new identity outcome, or change what one means | `identity.IdentityResult`'s five types and `resolve_slack_identity` — do not collapse two outcomes into one return value, and do not construct a `BrainService` on any non-`Resolved` path |
| Add a new way to decline / a new failure message | `SlackContext.decline` (identity refusals) or `post_or_log` (everything else non-critical) — not a new direct `chat_post_*` call |
| Add a new render (a new message shape) | `render.py` — the answer body is a `section`; anything that is the bot's own trust signal (a verdict, a source, a status) is a `context` block behind a `divider`; never read `answer['confidence']` |
| Add a new Slack-specific provenance field | `capture.schema.SOURCE_HINT_KEYS` — a new key name, string-valued, never a second hint channel |
| Change what the poller reports, or when | `store.REPORTABLE_STATUSES`/`due_for_report` and `poller._blocks_for`/`_needs_input_prose` — read-only against `capture_queue`, never a claim or a mutation |
| Change the singleton behavior | `app.acquire_singleton_lock`/`_SINGLETON_LOCK_KEY` — keep it a distinct key from `capture.schema`'s startup-DDL lock, and keep it `pg_try_advisory_lock`, never the blocking variant |
| Add a new Slack secret/setting | `settings.SlackSettings`/`_require_env` — read from the environment only, never the knowledge repo |
| Add a new doorbell event | `doorbell._EVENT_NAMES`/`_state_signature`/`_render_for_item` — decide the event's state-change fingerprint (fold in `attempts` if the item can revisit the same status), keep it read-only against `review.items_for_doorbell` |
| Add a new review-decision button | `review._parse_action_id`'s two prefixes (direct vs modal), `_VERDICT_TOKEN_TO_VERDICT` if the button's label diverges from the stored verdict word, `_MODAL_FIELD` if it needs one piece of free text first, or a distinct render + handler pair (like `render_entity_mint_modal` / `handle_entity_mint_modal_submission`) if it needs a whole form — never call a git-writing method (`promote_proposal_safe`) from the same handler that records a verdict |
| Change what the doorbell withholds | `doorbell._summary_for_doorbell`, asking `store.withheld_reason` (re-exported `capture.schema`) from the item's OWN status — fail closed for any status this rule has no opinion about, never assume an upstream query's filter already handles it |

## Notes

- **Layering** (`tests/test_architecture.py`): `stigmergy.slack` imports `stigmergy.server` and
  `stigmergy.answer`, plus ONE narrow, named exception: `store.py` imports
  `stigmergy.capture.schema` directly — never a wider slice of `stigmergy.capture` — for
  `startup_ddl_lock` (`CREATE INDEX IF NOT EXISTS` is a check, not a lock, and is not atomic
  against a concurrent creator) and the state constants it re-exports. `slack_submissions` moved
  INTO this package; it lived in `stigmergy.server.slack_store` first, reasoned about there on the
  same grounds `capture.schema` restricts imports to `stigmergy.server` among the transports — a
  reasoning later judged to be protecting the wrong layer (see this package's own
  `__init__.py` docstring for the argument in full and `test_no_slack_identifiers_below_the_
  slack_package` for the structural test that now pins it). `stigmergy.server.service.
  open_scoped_resources` is the other extracted seam: the `(conn, embedder)` construction
  `build_service`/`build_http_app` already did, shared by this package as a third caller instead of
  copied a third time.
- **The offline double is the whole test posture.** Every handler takes a `SlackGateway` as a
  plain argument; only `bolt_gateway.py` and `app.py` import `slack_sdk`/`slack_bolt` at all. That
  is what makes the whole surface testable with no network — and also
  why this package's own tests cannot, by construction, prove anything about a real workspace's
  behavior. Treat a green `tests/slack/` run as proof of this package's own logic, and see the
  measured walk linked at the top of this file for the rest.
- **The channel scopes the thread, the asker gets the rest by DM**
  ([ADR 017](../../../docs/decisions/017-slack-transport.md) D2/D3).
  A public-channel answer is computed at the CHANNEL's audience scope, never the asker's own wider
  one — a channel is many readers, and the ACL model scopes a reader. The comparison that decides
  whether to also DM a fuller answer is two cheap `search()` calls at each scope, never two full
  `ask()` runs and never a raw-hit comparison that reimplements `acl.visible()`.
- **Layering, the second edge**: `stigmergy.review_kinds` (was `canon_kinds`) is
  importable from ANYWHERE in this package
  (`tests/test_architecture.py::test_slack_imports_only_server_and_answer`'s allowlist reads
  `{server, answer, capture.schema, review_kinds}`) — it is a deliberately dependency-free module at
  the project root, existing solely so `render.py`/`doorbell.py` can depend on the two `KIND_*`
  strings (down from five across two removals — `canon-pr`/`contradiction` with the canon lane,
  `candidate` with the learning loop)
  without dragging in `stigmergy.server.review`'s own import graph (`librarian`/`entities`/
  `subprocess`/PyYAML). Unlike the old `canon_kinds.py`, `stigmergy.server.review` IMPORTS the
  `KIND_*` constants from `review_kinds` rather than restating them — see
  [`server/index.md`](../server/index.md) — so there is one definition, not two kept in sync
  by a test. `review_kinds.ENTITY_TYPES` (added for the entity-mint modal, ADR 030 D5) has NOT
  reached that same state yet: `stigmergy.server.review` still imports it from
  `entities.generator` directly, so `review_kinds`' own copy is a restatement held honest by
  `tests/test_architecture.py::test_review_kinds_entity_types_matches_the_generators_closed_list`
  rather than a shared import — see that test file and this package's own Data & contracts,
  above.
- **The doorbell and the review surface are additive, not a new transport.** `doorbell.run_doorbell`
  is a second `asyncio` background task inside the SAME `slack` process `poller.run_poller` already
  runs in — the ceiling is three process groups, and a fourth always-on one is not on the table;
  `review.py` adds action handlers to the
  SAME Bolt app `app.py` builds. Neither introduces a new process group, a new singleton lock, or a
  new identity-resolution path — both reuse `context.SlackContext`/`identity.resolve_slack_identity`
  exactly as `mention.py`/`capture.py` do.
- **This file's structure matches [`librarian/index.md`](../librarian/index.md)**, the first `src/`
  package to set the convention every code map in this repo follows.
