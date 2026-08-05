# ADR 017 — the Slack transport

Status: accepted. Narrative: [`docs/reference/slack.md`](../reference/slack.md).

## Context

Every read/write tool the brain exposes required a terminal (stdio) or a bearer token pasted into
a config file (HTTP) — a fine surface for the technical half of a company and an impossible one
for everybody else. Two things had to exist for Slack to become a real
transport rather than a second system with its own rules: a way to resolve who is asking that
reuses the SAME identity file every other transport reads, and a policy for a broadcast surface —
a channel is many readers, and no transport before this one had to answer for that.

## Decisions

**D1 — in-process, not over HTTP.** The bot calls `BrainService`/`AnswerService` directly, in the
same process, rather than over the HTTP transport with a service token + an acting-user argument.
The alternative is exactly the `submitted_by` forgery the capture layer already traps and refuses,
generalized to every tool: a service token plus a client-supplied identity is a credential vault in
a chat bot.
In-process, the bot is a transport that resolves identity and calls the same seam stdio and HTTP
call — the single enforcement point (`acl.visible()`) stays single.

**D2 — the channel scopes the thread; the asker gets the rest by DM.** A public-channel
answer is computed at the CHANNEL's audience scope (`ops/slack-channels.json`), never the asker's
own. The obvious proposal ("asker-scope + DM fallback") answers the wrong question in a
room: the ACL model scopes a READER, and a channel post has many of them at once. The DM-fallback
half of that proposal survives — it is what keeps the asker whole — but the primary scope inverts.
A channel not listed in that file gets the EMPTY set, never "everything": fail-closed by default,
widening requires an edit, staying safe requires none.

**D3 — compare retrieval sets, not two full `ask()` runs.** Determining whether the asker's wider
scope actually surfaced something the channel's could not is done with two ACL-filtered
`search()` calls (each its own `BrainService`, each its own embedding — kept as two separate
scoped services rather than one shared raw-hit comparison, so `acl.visible()` stays the one
enforcement point and this package never reimplements it), not two complete answering runs. Two
full `ask()` runs would double the system's single most expensive operation per public question
to detect a difference that is usually absent; comparing surfaced page sets is conservative in the
correct direction (if the
wider scope surfaces nothing new, no answer computed from it could differ) and costs a fraction as
much.

**D4 — the 🧠 gesture captures the thread, verbatim, and only the reacting human is attributed.**
No summarizing, no redacting, no tidying — a client that improves the material leaves every
downstream check comparing a filed page against a rewrite instead of against what was actually
said, and the evidence plane's whole promise is that material is
archived as received. `BrainService.submit()`'s four trap parameters are refused exactly as they
are for every other client; the bot has no more authority to set `submitted_by` than an MCP caller
does.

**D5 — `capture.schema.HINT_KEYS` gets a compatible extension, `SOURCE_HINT_KEYS`, rather than a
second hint channel.** `hints` has to carry Slack-specific provenance
(permalink, channel id/name, thread ts, participants, message timestamps, the fact the client is
Slack) — a different KIND of hint from the existing four (page-PLACEMENT suggestions), which the
existing allowlist has no room for. Rather than open a second, ad hoc metadata channel, the
allowlist gained seven new key NAMES; every value is still a plain string (a list is comma-joined
by the caller, never a structure `normalize_hints` has to parse), so `HINT_KEYS` itself and its
value validation are both UNCHANGED — every existing caller, and the test pinning its exact four
names, are unaffected. This is a compatible extension per the breaking-change discipline: additive,
no removal, no migration, no behavior change for any existing consumer.

**D6 — the `slack_submissions` mapping table lives in `stigmergy.server`, not `stigmergy.slack`.**
`stigmergy.slack`'s own import list is pinned to exactly `{server, answer}`. The
table's DDL has to ride `capture.schema.startup_ddl_lock` (`CREATE INDEX IF NOT EXISTS` is not
atomic against a concurrent creator) and the
poller has to read `capture_queue` read-only. `stigmergy.capture` is a dependency only `stigmergy.server`
is allowed among the transports (`test_server_imports_capture` already pins that edge for the
write path). Rather than open a second door into `capture` beside the one the architecture tests
already guard, the table — and the one function the Slack layer actually calls,
`ensure_write_path_schema`/`reserve`/`find_thread_submissions`/`due_for_report` — lives in
`stigmergy.server.slack_store`, following the precedent `transport_http.py` already set (transport-
specific code living inside `stigmergy.server` beside the transport-agnostic core). `stigmergy.server.
service.open_scoped_resources` is the same move applied to `(conn, embedder)` construction, shared
by a third caller instead of copied a third time.

**Amended: D6 is reversed.** `slack_submissions` moved INTO `stigmergy.slack`
(`stigmergy.slack.store`), and `stigmergy.slack` gained one narrow, pinned exception to its own
`{server, answer}` import list — `store.py` imports `stigmergy.capture.schema` directly, never a
wider slice of `stigmergy.capture`. The reasoning above was protecting the wrong layer: Slack's own
table/column vocabulary (`team_id`, `channel_id`, `slack_user_id`) belongs to `stigmergy.slack`, and
a repo where it lived a layer beneath the package it names was exactly the drift
`tests/test_architecture.py::test_no_slack_identifiers_below_the_slack_package` now exists to
catch — verified red against the pre-move tree. Recorded here rather than by editing D6 in place,
so this document stays an honest account of what was decided first and why it was judged wrong on
inspection. See `stigmergy/slack/__init__.py`'s own docstring for the argument in full.

**D7 — five identity outcomes, never fewer.** `Ignored` / `ForeignTeam` / `TransientFailure` /
`NoAccess` / `Resolved`, each its own type. Two distinctions the types make impossible to blur: a
transient Slack API failure while resolving identity is
NOT an unmapped user (it would misdirect an already-mapped colleague to "ask the steward to add
you" over a one-off API
hiccup); a foreign-workspace mention is silent, not the no-access reply (which names the steward
and so presumes a relationship a stranger from an unrelated company does not have).

**D8 — `render.render_answer` is a pure function, and never reads `answer['confidence']`.**
Purity is what makes the honesty properties (a `partial` verdict can never render as `verified`;
the verdict is a literal lookup that raises on an unrecognized value rather than flattening)
testable with no Slack at all. `confidence` is the model's own self-report and can disagree with
the code-computed verdict; only the verdict — the checked signal — ever ships.

## Consequences

- A fourth "hint kind" (a different provenance shape from a different future transport) should
  extend `capture.schema` the same way — a new small allowlist, string-valued, not a widened
  meaning for an existing one.
- A browsable read surface would replace `settings.no_link_resolver`'s VALUE and nothing else;
  `render.py`'s contract does not change when one appears. There is no such surface today —
  `app.build_context` wires `no_link_resolver` permanently ([ADR 022](./022-entity-navigation.md)
  D9) — and the seam is what makes that a one-line reversal rather than a rewrite.
- The Block Kit review inbox and the gardener digest are new renders and new handlers on the SAME
  `SlackContext`/`SlackGateway` seam — neither has to re-solve identity or rendering.
