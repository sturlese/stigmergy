# slack — code map

The Slack transport: a sibling of stdio (`server/mcp_server.py`) and HTTP
(`server/transport_http.py`). It resolves *who is asking* from a Slack event and calls the same
`BrainService`/`AnswerService` every other transport calls. It enforces nothing:
`stigmergy.server.acl.visible()` stays the one place visibility is decided.
Narrative: [`docs/reference/slack.md`](../../../docs/reference/slack.md).

## Modules

| Module | What it is |
|---|---|
| `app.py` | `stigmergy-slack`'s entry point: Bolt async app, Socket Mode, event registration, `acquire_singleton_lock`, and the poller as its one background task. Read it first when tracing an event; only it and `bolt_gateway.py` may import `slack_bolt`/`slack_sdk` |
| `identity.py` | Slack profile email -> `ops/identities.json` via `resolve_audiences`; `is_ignorable_event`; `is_configured_workspace`; `UsersInfoCache`; the five-way `IdentityResult` |
| `channels.py` | `ops/slack-channels.json` — a channel's groups, empty-set default for anything unlisted (a channel not listed is PUBLIC: it reads only unlabeled pages, and a 🧠 capture taken there is filed open). The grammar and the parse are `server.identity`'s, so the roster and this map cannot disagree about what a group may be called; `channel_audiences_live` prefers the index's snapshot over the baked file (`server.ops_files`' order), so a scoping edit lands without a deploy |
| `context.py` | `SlackContext` (process-wide conn, embedder, rate limiter, audit, evidence, cache, link resolver, "Show it here" tokens), `resolve_slack_identity`, `build_service`, and the two seams `decline` and `post_or_log` |
| `mention.py` | `@brain <question>` and DMs: placeholder, channel/DM scope split, retrieval-set comparison, edit-retry-then-fallback |
| `capture.py` | the 🧠 gesture: public channels only, verbatim thread material, provenance hints, reserve-then-fill dedup, progress-reaction lifecycle (`mark_in_progress`/`finish_progress`, driven from `app.py`) |
| `show_it_here.py` | the "Show it here" click: a cited page re-read under the clicker's own identity, server-side scoped |
| `poller.py` | the push channel back into the thread, over `store.REPORTABLE_STATUSES` (the terminal statuses), read-only against `capture_queue`; a filed card names the entities that capture introduced |
| `poller.py`'s `notify_rewrites_once` | the second pass: a capture may bring an existing page up to date, and the person who FILED that page is DM'd what changed and why. It is here rather than in the worker because the credentials are split — the librarian holds the checkout and no Slack token, this process holds the token and no checkout. At-least-once: the record is written after the DM |
| `render.py` | the pure `(answer_dict, link_resolver) -> blocks` renderer plus every other message's blocks |
| `mrkdwn.py` | CommonMark -> Slack `mrkdwn`, code spans protected |
| `store.py` | this package's own table and its DDL: `slack_submissions`. Also the package's only door into `stigmergy.capture` (`.schema` alone) |
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
- `store` — the only reader/writer of this package's table; a new Slack-originated write reuses
  `reserve`'s dedup pattern.
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
- Deciding anything about an identity HERE. There is nothing to decide: a capture introduces the
  entity it is about, born confirmed by whoever captured.

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
## Behaviour worth knowing before editing

- **The channel scopes the thread; the asker gets the rest by DM.** A public-channel answer is
  computed at the CHANNEL's audience scope, never the asker's wider one — a channel is many
  readers, and the ACL model scopes a reader. Whether to also DM a fuller answer is decided by two
  cheap `search()` calls, never two `ask()` runs and never a raw-hit comparison.
- **Nothing is ever asked of a submitter, before or after.** A threaded message is ordinary
  conversation to this bot; the one thing a submitter hears back is the poller's report of what the
  librarian did — a filed card names the entities their capture introduced and says the identity is
  confirmed by them.
- `store.py` is the only module that may import `stigmergy.capture`, and only `.schema`.

## Tests

`tests/slack/` is offline throughout (`FakeSlackGateway`, real Postgres, the `fake`
embedder/synthesizer), one suite per module, plus `test_store_pg.py` (the mapping table's
primitives and the dedup-key migration against real Postgres) and `test_app_wiring.py` (ack-first,
the singleton lock, event-team-id wiring, the progress-reaction lifecycle). No test here exercises
a real workspace: rate limits, real payload quirks and Socket Mode reconnects are outside what a
green run says anything about. `tests/test_architecture.py` pins the import allowlist
(`{server, answer, capture.schema}`, `capture.schema` scoped to `store.py`), the raw
SQL column names against `capture_queue`, that nothing imports `stigmergy.slack` back, that no
Slack identifier appears below this package, and the event-team-id rule.
`tests/test_deployment_config.py` pins the `slack` process group.
