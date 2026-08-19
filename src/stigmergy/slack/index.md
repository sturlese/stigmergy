# slack — code map

The Slack transport: a sibling of stdio (`server/mcp_server.py`) and HTTP
(`server/transport_http.py`). It resolves *who is asking* from a Slack event and calls the same
`BrainService`/`AnswerService` every other transport calls. It enforces nothing:
`stigmergy.server.acl.visible()` stays the one place visibility is decided.
Narrative: [`docs/reference/slack.md`](../../../docs/reference/slack.md).

## Modules

| Module | What it is |
|---|---|
| `app.py` | `stigmergy-slack`'s entry point: Bolt async app, Socket Mode, event registration, `acquire_singleton_lock`, and the poller and doorbell as two background tasks. Read it first when tracing an event; only it and `bolt_gateway.py` may import `slack_bolt`/`slack_sdk` |
| `identity.py` | Slack profile email -> `ops/identities.json` via `resolve_audiences`; `is_ignorable_event`; `is_configured_workspace`; `UsersInfoCache`; the five-way `IdentityResult` |
| `channels.py` | `ops/slack-channels.json` — a channel's audience scope, empty-set default for anything unlisted; `channel_audiences_live` prefers the index's snapshot over the baked file (`server.ops_files`' order), so a scoping edit lands without a deploy |
| `context.py` | `SlackContext` (process-wide conn, embedder, rate limiter, audit, evidence, cache, link resolver, "Show it here" tokens), `resolve_slack_identity`, `build_service`, and the two seams `decline` and `post_or_log` |
| `mention.py` | `@brain <question>` and DMs: placeholder, channel/DM scope split, retrieval-set comparison, edit-retry-then-fallback |
| `capture.py` | the 🧠 gesture: public channels only, verbatim thread material, provenance hints, reserve-then-fill dedup, progress-reaction lifecycle (`mark_in_progress`/`finish_progress`, driven from `app.py`) |
| `replies.py` | the submitter's ask-back reply, and the "Show it here" click |
| `poller.py` | the push channel back into the thread, over `store.REPORTABLE_STATUSES`, read-only against `capture_queue` |
| `doorbell.py` | the steward doorbell: read-only over `review.items_for_doorbell`, one DM per (item, steward) per state change, the card a replacement supersedes edited shut first, undeliverable outcomes recorded, and `close_decided_cards` — the end of every pass, which edits a decided item's newest DM into a buttonless closed card off `review.latest_decisions`. `TERMINAL_EDIT_CODES` is what separates an edit worth retrying from a message that is gone |
| `review.py` | the Block Kit review surface: buttons calling `review.review_decide_safe`, the free-text note modal and the entity-mint modal (the one branch that also gates its READ on `server.review.is_steward`) |
| `render.py` | the pure `(answer_dict, link_resolver) -> blocks` renderer plus every other message's blocks, doorbell cards and the two modals |
| `mrkdwn.py` | CommonMark -> Slack `mrkdwn`, code spans protected |
| `store.py` | this package's own two tables and their DDL: `slack_submissions` and `steward_notifications`. Also the package's only door into `stigmergy.capture` (`.schema` alone) |
| `copy.py` | every user-facing string, so a wording change is one diff |
| `gateway.py` | `SlackGateway` (the Protocol every Web API call crosses), `SlackApiError`, and `FakeSlackGateway` |
| `bolt_gateway.py` | the real gateway over `slack_sdk`'s `AsyncWebClient` |
| `settings.py` | `SlackSettings.from_args` — the Slack secrets from the environment, plus the shared `server.Settings` |

## Reuse

- `context.SlackContext.resolve_slack_identity` — the one identity call every handler makes; it
  delegates to `identity.resolve_slack_identity` with the identities path and the CONFIGURED
  workspace read off `ctx.settings`, so a handler supplies only the EVENT's own team id. Five
  outcomes (`Ignored`, `ForeignTeam`, `TransientFailure`, `NoAccess`, `Resolved`). Every one but
  `Resolved` is fail-closed; no `BrainService` is built on those paths. `NoAccess` covers all three
  no-identity reasons at once so a prober cannot map `ops/identities.json` by comparing replies.
- `context.SlackContext.decline` — the one way an identity refusal is told to someone.
- `context.SlackContext.post_or_log` — the one seam for non-critical sends: post or log, never
  raise. `mention._edit_or_fallback` keeps its own policy because it needs the response.
- `context.SlackContext.build_service` — the per-identity `BrainService`. `rate_limited=False` is
  for system-initiated work, so it never spends the asker's own budget.
- `service.call_async` (via `mention._run_ask`) — the seam that writes `audit_log` and spends the
  `ask` bucket. Every `ask` goes through it; never `AnswerService(...).ask(...)` directly.
- `render.render_answer` — the one answer renderer, pure.
- `store` — the only reader/writer of both tables; a new Slack-originated write reuses `reserve`'s
  dedup pattern. `last_notification` is the one read of a (item, steward) row — state AND card
  pointer together; `last_notified_state` is its state-only wrapper, and `is_live_card` the
  row-level twin of `open_notifications`' own filter.
- `review.review_decide_safe` — the only call `review.py` makes to change anything.
- `server.review.is_steward(service, "")` — the read-side gate `review.handle_block_action` asks
  before it opens the entity-mint modal, at the SAME universal scope `_guard_governance_decision`
  uses for a proposal, refusing with the SAME `NOT_YOURS_TO_DECIDE` sentence. Never a second rule
  spelled here, and never wrapped in a try of its own: the predicate fails closed on its own
  faults. Called through `asyncio.to_thread` — on a checkout-backed deployment it runs a real
  `git fetch`, and Slack's `trigger_id` expires in ~3s.
- `doorbell._load_stewards_cached` — the notifier's 300s cache. A decision path calls
  `review.load_stewards` fresh instead, so a revoked steward cannot approve off a stale cache.
- `gateway.SlackGateway` / `FakeSlackGateway` — every handler takes a gateway as an argument.

## Avoid

- Deciding visibility here. `channels.channel_audiences` resolves a channel's label set and
  `mention._scope_could_be_wider` compares label sets arithmetically; neither inspects a page.
- Taking the workspace check from Bolt's `context["team_id"]` — that is the installation's team, so
  the comparison is a tautology that passes every foreign-workspace event. Use `app._event_team_id`
  (the event's own), pinned by AST inspection in `tests/test_architecture.py`.
- Declining, posting or failing outside `decline`/`post_or_log`.
- Letting a bookkeeping write on a handler's hot path propagate: wrap it in its own
  `try`/`except`, logged with a correlation ref, or it takes the placeholder down with it.
- Letting `answer['confidence']` reach a render — the model's self-report and the computed verdict
  can disagree; only the checked signal ships.
- Rendering a citation, a verdict or "Sources" as a `section`. They are `context` blocks behind a
  `divider`: page-derived text always renders as a `section`, so it can never imitate the bot's own
  trust chrome. A text scrubber is not a substitute.
- Treating Socket Mode as though it had leader election — see `app.acquire_singleton_lock`.
- Widening `HINT_KEYS` for a Slack fact: a new provenance field is a new key in
  `capture.schema.SOURCE_HINT_KEYS`.
- Letting `identity.UsersInfoCache` or the "Show it here" token store grow unbounded — both need a
  size cap with oldest-first eviction, not just a TTL.
- Recording WHO decides in a modal's `private_metadata`. It carries only WHAT the decision is
  about; the decider is re-resolved from the submission event's own body.
- Prefilling a mint field from a review item's `subject`, or deciding HERE when a default is safe.
  `subject` is the DISPLAY string, which joins several unresolved names with `", "`.
  `review._mint_modal_inputs` takes `subjects` (the per-name list) and `mint_name_prefill` off the
  same item in one read, and `render.render_entity_mint_modal` obeys that prefill rather than
  counting anything: an empty prefill with names still to place IS the several-names case, so the
  names are listed above a field left empty. The rule itself lives in
  `entities.situations.mint_name_prefill`, which this package may not import — which is why the
  decided value travels in the item dict. That shared decision fixes WHEN a default is offered and
  WHICH name it is, on this door and on the admin console's alike; it does not make the two forms
  byte-identical, because sanitizing is per transport (the console strips control characters, this
  one does not). What stops that from mattering is `entities.birth`, which refuses C0/C1 in a name
  for every door alike. Submitting that modal still mints, so an accepted default remains one click
  from a signed commit in the knowledge repo — it just cannot be a garbled one any more.
- Importing `stigmergy.server.review`'s `KIND_*`/`ENTITY_TYPES` from a renderer — use
  `stigmergy.review_kinds`, which keeps `render.py` free of `librarian`/`entities`/PyYAML.
- Caching `ops/stewards.json` on the authorization path.

## Data & contracts

- `identity.IdentityResult` = `Ignored | ForeignTeam | TransientFailure | NoAccess | Resolved`;
  `Resolved` carries `email` and `audiences` (`frozenset[str] | None`, `None` = unrestricted).
- `app._event_team_id` — `event["user_team"] or event["team"] or body["team_id"] or ""`, fail-closed
  on absent. The envelope fallback is load-bearing: a `reaction_added` payload carries neither of
  the first two, so without it every 🧠 gesture is classified `ForeignTeam` and dies silently.
- `app.acquire_singleton_lock` / `_SINGLETON_LOCK_KEY` — `pg_try_advisory_lock` (never the blocking
  variant: a second machine must fail startup, not hang) on a key distinct from the startup-DDL
  lock, session-scoped on the process's own connection. **The lock is held per DATABASE**: two
  deployments on the same Slack app but different databases both start and double-handle every
  event. Stop the local bot before a second deployment against the same app goes live.
- `render.render_answer(answer, link_resolver, *, asker_slack_user_id="", mint_token=...)`.
  `link_resolver` is injected configuration; production wires `settings.no_link_resolver`, so every
  citation renders with the "Show it here" affordance and no link.
- `capture.schema.SOURCE_HINT_KEYS` — `source_client`, `source_permalink`, `source_channel_id`,
  `source_channel_name`, `source_thread_ts`, `source_participants`, `source_message_timestamps`.
  Every value is a plain string (lists are comma-joined and `MAX_HINT_CHARS`-truncated by
  `capture._material_and_hints`; not truncating made a long thread fail on every retry). Only
  `source_client`/`source_permalink` are unforgeable by a client — `source_participants` is a
  self-set display name, which is why the librarian fences the whole hints dict as UNTRUSTED-DATA.
- `slack_submissions` — UNIQUE on `(team_id, channel_id, thread_ts, slack_user_id)`: the dedup grain
  is per (thread, reactor), so one person 🧠-ing two messages of a thread is one capture and two
  people are two.
- `steward_notifications` — one row per (item_kind, item_id, steward_email) with the `state` last
  notified at, plus `channel_id`/`message_ts`, the card's own Slack coordinates. ONE row means one
  pair of coordinates: a second card for the same item overwrites the first's, which is why
  `_notify_item` supersedes the old message before posting the new one. `state` carries three
  namespaces separated by prefixes owned in `store.py`: a real item state (unprefixed), an
  `undeliverable:<class>` outcome that never became a message, and a `closed:*` card the doorbell
  has already finished with — `closed:<verdict>` edited shut, or `CLOSED_UNREACHABLE` for one Slack
  will never let it edit again. `mark_notified` PRESERVES the coordinates on a state-only re-mark;
  `open_notifications` is the reader that skips both prefixed namespaces and every row missing
  either coordinate (a pre-change row — nothing can recover where its message went).
- `stigmergy.review_kinds.ITEM_KINDS` — `entity-proposal`, `parked-capture` and
  `repair-proposal`, the three kinds a human decides on directly; the doorbell deliberately
  rings for only the first two (ADR-039's no-ring decision for repairs). `ENTITY_TYPES` is the
  closed list the entity-mint modal offers, restated here and held honest by a drift test
  against `entities.generator`.
- `doorbell._state_signature` — the per-(item, steward) fingerprint; it folds in `attempts` so a
  requeue that parks a row back into the same status is still a detectable change.
- `doorbell.LOOKUP_FOUND` / `LOOKUP_NOT_FOUND` / `LOOKUP_FAILED` — a transient API failure must
  never be recorded as the permanent fact "no such person in this workspace".
- `review._MODAL_FIELD` — which `(item_kind, verdict_token)` pairs need the note modal and under
  which field. `(entity-proposal, approve)` is checked before this table: its mint modal has no
  `(field, label, placeholder)` shape. Every other button fires directly.

## Behaviour worth knowing before editing

- **The channel scopes the thread; the asker gets the rest by DM.** A public-channel answer is
  computed at the CHANNEL's audience scope, never the asker's wider one — a channel is many
  readers, and the ACL model scopes a reader. Whether to also DM a fuller answer is decided by two
  cheap `search()` calls, never two `ask()` runs and never a raw-hit comparison.
- **Only the original submitter's reply counts** as an answer to an ask-back; `replies` scans all
  of a thread's mapped rows for the resolved replier's own email and consults `q.reply` rather than
  inferring "answered" from status.
- `store.py` is the only module that may import `stigmergy.capture`, and only `.schema`. The
  doorbell's closing pass therefore reads the `review_decisions` ledger through
  `server.review.latest_decisions`, never `capture.decisions` directly.
- **A doorbell card is a control surface with a lifetime.** Once the item is decided — through any
  door that writes the ledger — the card is edited shut rather than left clickable, and a card a
  newer one replaces is edited shut before the replacement goes out. The trigger for closing is the
  LEDGER, not the queue state: a `requeue` verdict puts the row back in the queue, so the item
  leaves this inbox while its card stays in the DM. The corollary is that a parked capture drained
  through `stigmergy-queue` or the console's Queue tab writes no ledger row at all, so its card
  ages out rather than closing.
- Every decision this package records names its door: `_decide_and_confirm` passes
  `review.SOURCE_SLACK` once, and every button and both modals funnel through it.

## Tests

`tests/slack/` is offline throughout (`FakeSlackGateway`, real Postgres, the `fake`
embedder/synthesizer), one suite per module, plus `test_store_pg.py` (the mapping table's
primitives and the dedup-key migration against real Postgres) and `test_app_wiring.py` (ack-first,
the singleton lock, event-team-id wiring, the progress-reaction lifecycle). No test here exercises
a real workspace: rate limits, real payload quirks and Socket Mode reconnects are outside what a
green run says anything about. `tests/test_architecture.py` pins the import allowlist
(`{server, answer, capture.schema, review_kinds}`, `capture.schema` scoped to `store.py`), the raw
SQL column names against `capture_queue`, that nothing imports `stigmergy.slack` back, that no
Slack identifier appears below this package, and the event-team-id rule.
`tests/test_deployment_config.py` pins the `slack` process group.
