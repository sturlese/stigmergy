# The Slack transport — `stigmergy.slack`

A third transport, a sibling of stdio (`server/mcp_server.py`) and HTTP
(`server/transport_http.py`). It resolves *who is asking* from a Slack event and calls the SAME
`BrainService`/`AnswerService` every other transport calls — it enforces nothing itself.
`stigmergy.server.acl.visible()` stays the one enforcement point. Design record:
[ADR 017](../decisions/017-slack-transport.md); operating it day to day — the `slack` process
group, the singleton rule, token rotation, the double-handling symptom — is
[operator-runbook.md](./operator-runbook.md).
Code map: [`src/stigmergy/slack/index.md`](../../src/stigmergy/slack/index.md).

**Two flows, one push channel and a review surface**: `@brain <question>` (ask, rendered for
humans, with the channel/DM ACL split), the 🧠 gesture (capture a thread, verbatim, public
channels only), the poller that turns a Slack-originated capture's later state changes into
replies in the originating thread, and the steward doorbell with its Block Kit review cards — all
on the same process, each documented below.

## Module map

| Module | Does |
|---|---|
| `identity.py` | Slack profile email -> `ops/identities.json` (the SAME `resolve_audiences` every transport uses), fail closed; `is_configured_workspace`, the cheap synchronous half of that check; the `UsersInfoCache` (positive results only — email, the doorbell's reverse email->id lookup, and display names, all three keyed the same way); the five-way `IdentityResult` (`Ignored`/`ForeignTeam`/`TransientFailure`/`NoAccess`/`Resolved`) |
| `channels.py` | `ops/slack-channels.json` — a channel's audience scope, defaulting to the empty set for anything not listed |
| `context.py` | `SlackContext` — the process-wide resources (conn, embedder, rate limiter, audit, evidence, cache, link resolver), and `build_service(email, audiences)`, the per-identity `BrainService` constructor every handler calls |
| `mention.py` | `@brain <question>` / a DM: the placeholder, the channel/DM split, the cheap retrieval-set comparison and the DM fuller answer, the edit-retry-then-fallback-message policy, every error state |
| `capture.py` | the 🧠 gesture: public-channel-only, the verbatim thread material (participant display names resolved through the identity cache, in parallel over the cache misses), the provenance hints, the dedup reservation, the ack, and the instant progress-reaction lifecycle (`mark_in_progress`/`finish_progress`) |
| `show_it_here.py` | the "Show it here" affordance: a cited page re-read under the clicker's own identity |
| `poller.py` | the push channel back into the thread, over `store.REPORTABLE_STATUSES` (every terminal status — `filed`/`rejected`/`failed`, plus the legacy `resolved` on old rows), read-only against `capture_queue`; a `filed` card names the entities the librarian proposed |
| `store.py` | this package's two tables: `slack_submissions` — the 🧠 dedup reservation and the `submission_id -> (channel, thread_ts, slack_user_id)` mapping the poller reads back — and `steward_notifications`, one row per (item, steward) carrying the state the doorbell last told them about plus the card's own `channel_id`/`message_ts`, so the DM can be edited once the item is decided. Owns the two `state` prefixes (`undeliverable:` / `closed:`) and `open_notifications`, the reader that skips both |
| `doorbell.py` | the steward doorbell — DMs a steward when the librarian proposes an identity or a spelling, over `stigmergy.server.review.items_for_doorbell`. One notification per (item, steward), re-sent only on a real state change (and the card it replaces superseded before the replacement is posted); an undeliverable notification recorded, never swallowed; and `close_decided_cards`, which edits a decided item's DM into a buttonless closed card at the end of every pass. What rings the bell is `identity-proposal` and `alias-proposal`; `repair-proposal` is deliberately silent |
| `review.py` | the Block Kit review surface — buttons on a doorbell DM, and the merge modal for the one verdict that needs a second fact, all calling `stigmergy.server.review.review_decide_safe` |
| `render.py` | the PURE `(answer_dict, link_resolver) -> blocks` renderer, plus every other message's blocks (including the two doorbell cards and the merge modal) |
| `mrkdwn.py` | CommonMark -> Slack `mrkdwn` (bold, links, lists, inline/fenced code) |
| `copy.py` | every user-facing string, in one place |
| `gateway.py` | `SlackGateway` (the one seam every Slack Web API call crosses) and `FakeSlackGateway`, the offline double |
| `bolt_gateway.py` | the real gateway, wrapping `slack_sdk`'s `AsyncWebClient` — imported only by `app.py` |
| `settings.py` | `SlackSettings.from_args` — the three Slack secrets from the environment, plus the same `server.Settings` every transport builds from `--repo`/`--identities`/`--dsn`/`--embedder`/`--answer-llm` |
| `app.py` | `stigmergy-slack`'s entry point: Bolt's async app, Socket Mode, event registration, `acquire_singleton_lock`, and the poller AND doorbell as two background tasks in this one process |

**Layering** (`tests/test_architecture.py`): `slack` imports `server`, `answer` and
`stigmergy.review_kinds` (the two `kind` string constants, at the bottom of the stack so the pure
Block Kit renderer can agree with the server without importing it), plus one narrow, declared
exception: `store.py` imports `stigmergy.capture.schema`
directly — `startup_ddl_lock` and the state constants it re-exports, nothing
wider. The `slack_submissions` mapping table lives INSIDE this package because Slack's own
vocabulary (`team_id`/`channel_id`/`slack_user_id`) belongs in `stigmergy.slack`, not a layer beneath
it (`test_no_slack_identifiers_below_the_slack_package`). `stigmergy.server.service.
open_scoped_resources` is the other shared seam: the `(conn, embedder)` construction
`build_service`/`build_http_app` already did, shared by a third caller instead of copied.

## What the Slack app has to be configured with

Three secrets, from the environment only — never a flag, never `.mcp.json`
(`settings.SlackSettings.from_args` requires all three and refuses by name if one is missing):

| Variable | What it is |
|---|---|
| `SLACK_APP_TOKEN` | `xapp-…`, the App-Level Token that opens the Socket Mode connection |
| `SLACK_BOT_TOKEN` | `xoxb-…`, the bot token every Web API call authenticates with |
| `SLACK_TEAM_ID` | `T…`, the ONE workspace this bot answers in — anything else is `ForeignTeam` |

**Socket Mode**, so the bot needs no public URL and no inbound port at all (it is the one process
group in the deployment with no HTTP service).

**An event that arrives while the bot is down is delayed, not lost.** Measured against a real
workspace: a question asked with the process killed was buffered by Slack and delivered on
reconnect about a minute later, and answered. That is also the real reason the single-instance pin
survives scrutiny — not because one Socket Mode connection is inherently stable, but because
Slack's own redelivery covers the gap a single instance leaves. It is a behaviour this system
observes and does not control, so if it ever changes, the design's safety margin changes with it.
(The measurement that established this also produced the instructive mistake: a screenshot taken
seconds after a restart showed no reply and was read as "events during downtime are silently
lost". The bot had simply not finished connecting. **Absence of a reply is not evidence of loss
until a control question proves the listener was live.**)

**Four event subscriptions**, and the code registers exactly these
(`app.build_bolt_app`): `app_mention` · `message` (DMs) · `reaction_added` (the
🧠 gesture) · `reaction_removed` (registered so it can be acknowledged and deliberately ignored —
see the gesture section below).

**The Web API methods the bot actually calls**, all through the one gateway seam, so the scope
list follows from this and nothing else: `users.info` · `users.lookupByEmail` ·
`conversations.info` · `conversations.replies` · `chat.getPermalink` · `chat.postMessage` ·
`chat.update` · `chat.postEphemeral` · `views.open` · `reactions.add` · `reactions.remove` —
the last two need `reactions:write`, and an app deployed without it does not lose a single
capture: every reaction call is best-effort (`capture._react_or_log`), so a `missing_scope`
failure is logged and swallowed exactly like any other non-critical Slack send.

**The App Home "Messages Tab" toggle** ("Allow users to send Slash commands and messages from the
messages tab") must be ON, or every DM the bot sends is refused with "Sending messages to this app
has been turned off". It is presentation, not permission: no scope hints at it and no reinstall
fixes it — see the runbook's Troubleshooting.

## Identity: the same seam, a Slack-shaped front door

```
Slack user id -> users.info -> profile email -> resolve_audiences(email) -> audiences
```

Five outcomes (`identity.IdentityResult`), never collapsed into fewer:

- **`Ignored`** — a bot/app/workflow event, or the bot's own message. Checked FIRST
  (`is_ignorable_event`), before identity is even attempted. Zero Slack traffic.
- **`ForeignTeam`** — the event's `team_id` doesn't match the configured workspace. Silent —
  a stranger from an unrelated workspace gets no "ask the operator" instruction that presumes a
  relationship they don't have.
- **`TransientFailure`** — `users.info` itself failed (a timeout, a 5xx). NOT an unmapped user:
  the server-error copy, never "ask to be added" — and no `BrainService` is
  constructed on this path either.
- **`NoAccess`** — no email, an empty email, or an email `resolve_audiences` doesn't recognize.
  One outcome, one reply, for all three: a determined prober must not be able to
  distinguish them by comparing responses, the same discipline `read_page`'s "unknown page"
  shape already applies.
- **`Resolved`** — an email and its audience scope, ready to build a `BrainService` from.

## The channel scopes the thread

A public-channel answer is computed at the **channel's** audience scope
(`ops/slack-channels.json`), never the asker's own — a channel is ten readers, and the ACL model
scopes a reader, not a room. A channel not in that file gets the **empty set**, not "everything":
under `acl.visible()`'s truth table that sees only pages carrying no `acl` label, which is the
fail-closed default (widening requires an edit; staying safe requires none).

When the asker's own scope could be wider than the channel's, `mention._scope_could_be_wider`
decides CHEAPLY whether it is even worth checking — pure arithmetic over audience label SETS
(never a page, never `acl.visible()`): a scoped audience that is a subset of the channel's can
never see more (a scoped visible-page set is monotonic in its label set), so the comparison is
skipped outright. Otherwise, two ACL-filtered `search()` calls (one at the channel's scope, one
at the asker's own — each its own embedding, so this is "cheap" relative to a full `ask()` run
rather than a single shared embedding call; keeping the two calls fully separate `BrainService`
instances is what keeps `acl.visible()` the one enforcement point, never reimplemented here as a
raw-hit comparison) compare the surfaced PAGE SETS. Only when the asker's
scope surfaces a path the channel's could not does the (expensive) second `ask()` run, and its
answer goes to the asker by DM — the ONE place a fuller answer is ever acknowledged. The channel
message itself never states, implies, or differs observably by the fact that something was
withheld — no hedge, no footnote, no conditionally-worded refusal.

## The 🧠 gesture: verbatim, public channels only

`reaction_added` with 🧠, on a message in a public channel the bot is in. The thread's messages
(`conversations.replies` — Slack's own API returns just the one message when it isn't part of a
thread, so "a thread" and "a single message" are handled identically) are joined with `\n`, byte
for byte — no summarizing, no tidying, no redacting: the evidence plane promises the
material is archived as received. Provenance travels as `hints`, under a small set of keys
`capture.schema.SOURCE_HINT_KEYS` adds ALONGSIDE the pre-existing placement-suggestion keys
(`type`/`path`/`entity`/`title`) — a compatible extension, not a widened meaning for either: every
value is still a plain string (a list — participant display names, message timestamps — is
comma-joined by the caller before it reaches `capture.schema`, never a structure that module has
to parse).

**Two of those keys decide something, and those two are unforgeable by a client.**
`source_client`/`source_permalink` decide whether the librarian attaches a verbatim `sources/`
page beside the synthesis (`processing._source_attachment` — see
[`librarian.md`](./librarian.md#the-source-attachment-a-parameter-never-a-third-flow)), so
a client that could assert them could choose a flow. It cannot:
`capture.schema.reject_source_provenance_hints` REFUSES those keys for every door but Slack's own,
and which door a service is is a constructor fact, not an argument — `context.build_service` passes
`door=SLACK_DOOR`, and every client-facing `BrainService` (stdio, HTTP) is built with `door=""`. The
hint stopped being client-writable at the exact moment it started deciding something.

**Unforgeable is not the same as trusted, and the rest of the hints are neither.**
`source_participants` is a Slack DISPLAY NAME, a string any workspace member sets on themselves;
this door composes it server-side, so no client argument forges it, but nobody vouches for its
CONTENT either — and it crosses into the system **with no token at all**: an attacker posts in a
public channel the bot is in, any legitimate member reacts 🧠, and the attacker's own display name
is in the capture's hints. The blast radius is the filing agent's prompt, and the agent's worktree
is a full checkout of the knowledge repo rather than the submitter's ACL scope. So the librarian
fences the whole hints dict inside UNTRUSTED-DATA (`agent.build_prompt`) rather than relying on the
label beside it: *a label saying "NOT instructions" is a request; a fence is a boundary.* A new
`source_*` key inherits that treatment and must be able to live with it — this transport composes
provenance, it does not certify it.

Attribution is the reacting human, computed server-side — `BrainService.submit()`'s four trap
parameters (`submitted_by`/`verification`/`acl`/`content_hash`) are never passed. Idempotency is a
reserve-then-fill pattern against Postgres's own UNIQUE index on
`(team_id, channel_id, thread_ts, slack_user_id)`, keyed on the THREAD rather than the message:
what gets submitted is the thread, so two 🧠 reactions by the same person on two different messages
of one thread are one capture, while two different people reacting to the same thread still produce
two (`slack.store.reserve`) — the ONLY write that can race, so a redelivered event or a fast
remove-then-re-add can never produce a second `capture_queue` row. `message_ts`
stays a stored column (the gesture's own provenance) without being part of the key.
`reaction_removed` is ignored outright: the material may already be queued or
filed, and an undo the system cannot honour is worse than no undo.

**The instant progress reaction.** Long before the "queued" thread ack can arrive (it sits
1.5-4s behind 6+N sequential Web API calls), `app.on_reaction_added` adds an hourglass (`capture.mark_in_progress`) as close to the raw event as
it can: after `is_ignorable_event` and `is_configured_workspace` (a foreign workspace or an ignored
event gets no reaction, exactly like it gets no other Slack traffic), but before any identity
resolution, so it needs neither `users.info` nor the cache. `capture.finish_progress` runs in a
`finally` around the whole capture, so EVERY exit — a refusal, a failure, a duplicate, or the
genuine success — clears the hourglass; only a genuine success upgrades it to a checkmark, driven
by `handle_reaction_added`'s own boolean return.

**What the two markers do and do not claim**, because the reaction is the most visible thing this
gesture does and its meaning is narrower than it looks:

- The hourglass fires after `is_ignorable_event` and `is_configured_workspace` but BEFORE the
  channel and identity checks — that is what makes it instant, and it is the whole trade. So an
  ignored event and a foreign workspace get no reaction at all (zero Slack traffic, as before),
  while a **private channel**, an unrecognized reactor (`NoAccess`), a transient identity failure
  and a duplicate DO get an hourglass that is then removed. The gesture is still public-channel-only
  — none of those queue anything, and the refusal is still the ephemeral only the reactor sees —
  but the bot now leaves a brief, visible mark where it previously left none. Closing that would
  cost a `conversations.info` round-trip before the reaction, which is exactly the wait the marker
  exists to remove.
- The checkmark means **queued**, and it is never revoked. It does not claim the thread ack
  posted (that send is best-effort), and it does not claim the librarian later accepted the
  capture — a rejected or failed filing keeps its checkmark, and the poller's thread report is what
  carries the terminal outcome. Same posture as ignoring `reaction_removed`: a marker the system
  cannot honour later is worse than one with a narrow, stated meaning.

Every reaction call is
best-effort (`capture._react_or_log`): a real `SlackApiError` is logged and swallowed, never
allowed to break the capture it wraps, and `already_reacted`/`no_reaction` — both reachable on
event redelivery — are not even errors by the time they get there, collapsed to a plain success at
`bolt_gateway`'s boundary the same way it collapses `users_not_found` for `users_lookup_by_email`.

## The push channel

`slack.store.due_for_report` is a READ-ONLY join of `slack_submissions` to
`capture_queue`, never a claim, a lease or a mutation — the poller runs in the bot's own process
(no fourth machine), polling on a plain interval. `store.REPORTABLE_STATUSES` is every TERMINAL
status the queue has (`capture_schema.TERMINAL_STATUSES`, derived rather than retyped, so a status
that joins the vocabulary is reported without an edit here); `queued` and `claimed` are deliberately
absent, since an ordinary in-flight row produces no Slack traffic. `failed` is handled like the
rest — say so plainly, with the reason the server gives and nothing added to it. `filed` gets a
bespoke Slack render built from the report's STRUCTURED fields, and it NAMES the entities the
librarian proposed while filing: the page is in the brain and a steward confirms the identity, so
the card says that rather than implying the capture is waiting on something. The other statuses
reuse `report['summary']` verbatim, converted to `mrkdwn`, with the enum-first prefix bolded —
never rewritten into "friendlier" prose.

**Nothing is ever asked of a submitter.** A threaded message is ordinary conversation to this bot;
the only thing a submitter hears back is this report of what the librarian did.

## The steward doorbell and the review surface

A second background task, `doorbell.run_doorbell`, runs alongside `poller.run_poller` in the SAME
`slack` process (the standing ceiling: no fourth always-on process) and DMs a steward when the
librarian has PROPOSED something — an identity it created unconfirmed, or a spelling it appended to
a registered entity. Nothing is stuck while the card sits there: the capture that prompted it is
already filed. It never claims, leases or mutates a queue row, and reads no queue row at all: every
read goes through `stigmergy.server.review.items_for_doorbell`, the management-shaped, unscoped
sibling of `review_queue` documented in [server.md](./server.md#the-review-tools), which derives
both proposal kinds from the entity registry the index snapshot carries.
What rings the bell is two of the three kinds `stigmergy.review_kinds.ITEM_KINDS` carries —
`identity-proposal` and `alias-proposal`; `repair-proposal` is deliberately silent (ADR-039's
no-ring decision: repairs are bounded, non-urgent, and reviewed from the queue).

Five properties, each enforced structurally rather than left to discipline:

- **One notification per (item, steward), re-sent only on a real state change** — a small,
  stable fingerprint per item (`_state_signature`). A proposal has exactly ONE open state, so its
  card is sent once and closed by a decision; there is no re-ring.
- **A decided item's most recent card closes itself** — `close_decided_cards` runs at the end of
  every pass and `chat.update`s that DM into a buttonless card naming the verdict, the actor and
  the door (`✅ reject — by ana@example.com via admin`). A card is a live control surface: left
  alone, its buttons keep offering actions that can now only answer with a staleness refusal. What
  triggers it is the LEDGER, not the registry — every door that decides a proposal (the console,
  MCP, `stigmergy-entities`) writes a ledger row, and the registry snapshot this surface reads
  catches up later, so the card closes on the decision rather than on the index. Same
  send-then-mark order as a delivery, so a Slack outage retries next pass; the card is then
  recorded at `closed:<verdict>`, which is what stops the pass rewriting the same DM every
  interval. A refusal Slack can never take back (`message_not_found`, `cant_update_message`,
  `channel_not_found`) is recorded as `closed:unreachable` instead, so a message that no longer
  exists is not re-edited once per interval forever. The coordinates it edits by
  (`channel_id`/`message_ts`) are read from the `chat.postMessage` response when the card is
  created, so rows written before this existed carry none and simply age out.
- **A card that a newer one replaces is superseded first** — `steward_notifications` holds one row
  per (item, steward) and therefore one pair of coordinates, so the post that records a second card
  overwrites the first card's. Before that happens, `_notify_item` edits the old message into the
  same buttonless frame (`render.render_doorbell_superseded`, no verdict — nothing was decided).
  Otherwise the older message is orphaned with its buttons live and the closing pass above can only
  ever reach the newest card. A failed supersede is logged and the new card is posted anyway.
- **An undeliverable notification is recorded, never swallowed** — no steward resolves for the
  scope, or the resolved steward has no Slack identity in this workspace, writes a `job_runs` row
  (`review.record_undeliverable`) naming the event and the reason, deduped by (item, steward,
  reason class).
- **A card shows what the librarian already filed, and no captured material** — the doorbell is
  terse by DESIGN: a proposal's own name, type, aliases and the page's What / Who paragraph, which
  the librarian wrote and the commit already carries. It reads no queue row and echoes no capture
  excerpt at all.

**The review cards.** Two doorbell renderers, one per item kind, and two more that either of them is
edited INTO, sharing one buttonless frame: `render.render_doorbell_closed`
(decided — the verdict, the actor and the door) and `render.render_doorbell_superseded` (replaced
by a newer card — no verdict, because nothing was decided):

| Item kind | Buttons | Notes |
|---|---|---|
| `identity-proposal` | Approve (direct) · Merge into… (modal) · Decline (direct) | the three verbs an identity takes. Approve and Decline fire immediately with no note: the card already carries everything the decision needs, and friction belongs only where a second FACT is required. Merge is that one case — the survivor's id — so it opens a modal offering `merge_candidates` as a `static_select` plus a typed field for a registry id not among them. The modal gates nothing on its own: the candidates are registry names `list_entities` serves to every identity, and `review_decide`'s steward guard runs on submit |
| `alias-proposal` | Approve (direct) · Decline (direct) | a spelling is one of that entity's names or it is not; there is no third answer and nothing to type |

`review._MODAL_VERDICTS` is the closed set of `(item_kind, verdict)` pairs that open a modal first,
and it holds exactly one pair — `(identity-proposal, merge)`. A button from an older deploy carrying
a pair this build no longer maps is answered with a worded staleness decline, never an opaque
listener failure.

A click (`review.handle_block_action`) or the merge modal's submission
(`review.handle_merge_modal_submission`) re-resolves the acting identity from Slack's own
authoritative event body every time — never from a value round-tripped through `private_metadata`,
which carries only WHAT the decision is about (item kind, id, where to post the confirmation), never
WHO is making it. Stamping the submitter in when the modal was OPENED would make them a value this
code wrote rather than a fact Slack is asserting about who just clicked Submit. A button's `value`
is likewise always the bare item id one of this surface's own renderers put there: nothing untrusted
ever becomes a button value here.

Every decision calls the SAME `review_decide_safe` an MCP caller calls, with `source="slack"`
stamped in the one place every path funnels through (`_decide_and_confirm`), so every ledger row
this surface writes names the door it came from. **This package decides nothing about a proposal.**
The merge modal collects one fact and hands it to `review_decide` as `into`; whether that entity
exists, is confirmed, or would collide is `server.review` and `entities.decide`'s to refuse, on
every door alike — and `server.acl.visible()` remains the ONE place read access to a page is
decided. There is no steward-only READ here to gate: the merge candidates are registry names every
identity can already list, so the authorization that matters is the one on submit.

Every confirmation this surface posts is the same plain message, and it names what the decision
DID — the entity and the commit it produced — since the commit is the thing a steward would
otherwise have to go and look for.

## The offline double

Every handler takes a `SlackGateway` (`gateway.py`) as a plain argument — no module besides
`bolt_gateway.py` and `app.py` imports `slack_sdk`/`slack_bolt` at all. `FakeSlackGateway` is the
test double every suite under `tests/slack/` drives: it RECORDS every call (so a test can assert
exactly what would have been posted) and every failure mode is SCRIPTED explicitly (a set of
ids that always raise, or a countdown of failures before success) — the same posture `fake_llm`
and `fake_embedder` already take elsewhere in this repo. Everything except the manual
real-workspace walk runs and is tested with no network.

## Rendering, and the property it exists to guarantee

`render.render_answer` is a PURE function: `(answer_dict, link_resolver) -> blocks`. Two
properties this buys, both security-class — rendering is where honesty is lost silently:

1. **A `partial` verdict can never render as `verified`.** `copy.verdict_line` is a literal
   dict keyed on the verdict string — an unrecognized value RAISES rather than silently
   flattening into nothing.
2. **`answer['confidence']` is never read at all.** The model's own self-reported confidence and
   the code-computed verdict are two different signals that can disagree; only the verdict ships.

`link_resolver` is `Callable[[str], str | None]` — `settings.no_link_resolver` (every citation
resolves to "no link", so every citation gets the "Show it here" affordance) is wired everywhere
(`app.build_context`). A read site that resolves a citation to a real URL replaces the VALUE, never
`render.py`'s own contract; there is no such site today.

**"Show it here", and why its button value is an opaque token.** The button is visible to everyone
who can see the message — Slack has no per-viewer button visibility — so anyone may click it, and
all of the access control is server-side, at click time (`replies.handle_show_it_here`). The
button's `value` is a random token, **never the page path and never the asker's email**: a button
value is readable by any workspace member through `conversations.history` and by any other app
holding history scope, so putting an identity or a path there would publish exactly what the
affordance exists to scope. `(path, owner_slack_user_id)` lives only in the bot's own process
(`context.mint_show_it_here_token`, a bounded, oldest-first-evicting store with a one-hour TTL;
not single-use, because a legitimate asker may click twice). A click by anyone other than the
token's owner — and an unknown or expired token — is declined **silently**: no ephemeral, no
error, nothing observable, so the button cannot be used to probe. The owner check is a plain
Slack-user-id equality, not a resolved-email comparison: it is Slack's own authenticated fact
about who clicked, exactly as strong and one `users.info` call cheaper on a mismatch. Only after
it passes is an identity resolved and a per-identity `BrainService` built, so the page read itself
still goes through `acl.visible()` like every other read.

**Its `block_id` is opaque for an unrelated reason: Slack rejects a whole message when two blocks
share one explicit `block_id`.** A path-derived id collides the moment one answer cites the same
page twice — ordinary on a small corpus — and Slack's rejection strands the entire render,
"thinking…" placeholder included. Each button's `block_id` therefore carries a random suffix, so
it can never collide however many buttons one render builds; a page cited twice still gets exactly one button,
deduped by PATH before rendering — a presentation choice, unrelated to why the id itself is random.
The fix has a floor beneath it for the wider `invalid_blocks` class (an unsupported block, a
nesting/length limit — not only a `block_id` collision): `mention._edit_or_fallback` retries the
edit, then a fresh post with `blocks`, then — only if every blocks-carrying attempt is refused — one
last text-only post that still carries the real answer and a compact, deduped Sources line
(`_answer_fallback_text`), so Block Kit itself failing never drops an answer the system already
paid for.

## Testing

`tests/slack/` (offline, `FakeSlackGateway`, real Postgres with the `fake` embedder/answer
synthesizer) plus `tests/slack/test_store_pg.py` (the mapping table's own primitives, including the
thread-keyed dedup migration and the card-pointer columns), `test_doorbell.py` (the doorbell
properties above, including the closing pass — closed exactly once, an undecided card untouched, a
pointerless row skipped, a transient edit failure retried, a card Slack says is gone recorded
`closed:unreachable` and never retried — and the supersede leg: a replaced card edited shut before
the replacement is posted, and posted anyway when that edit fails) and `test_review.py` (the Block
Kit button/merge-modal flow against `review_decide_safe`, identity re-resolved at click and at
submission).
`tests/test_architecture.py`'s slack-boundary tests pin the import list; `tests/test_deployment_config.py`
pins the third process group. The real-workspace walk is manual and this repository keeps no
record of any particular run, so live behaviour is measured, never assumed from a green suite.
