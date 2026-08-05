"""`stigmergy.slack` — the Slack transport: a third way of resolving *who is asking* and calling the
same `BrainService` / `AnswerService` every other transport calls.

**This package enforces nothing.** `stigmergy.server.acl.visible()` stays the ONE enforcement
point; nothing here inspects a page's `acl`, and nothing here decides whether content is visible.
What this package DOES decide — the same class of decision `transport_http.py` makes for a bearer
token — is WHICH `BrainService`/`AnswerService` a given Slack event is served by:
`stigmergy.slack.identity` resolves a Slack user to an email exactly the way
`transport_http._BearerAuthMiddleware` resolves a bearer token to one (`users.info` -> email ->
`stigmergy.server.identity.resolve_audiences`, the SAME function and the SAME `ops/identities.json`
every other transport reads), and `stigmergy.slack.channels` resolves a channel to its audience
scope (`ops/slack-channels.json`) — a second, smaller version of the same "read a versioned file,
fail closed" shape `identity.py` establishes.

**Layering (pinned by `tests/test_architecture.py`, per module): `slack` imports `server`,
`answer` and `review_kinds`, plus ONE narrow, named exception — `stigmergy.slack.store` may reach
`stigmergy.capture.schema` — and nothing imports `slack`.**

**`slack_submissions` lives in this package** (`stigmergy.slack.store`), not a layer below it:
`team_id`/`channel_id`/`slack_user_id` are SLACK's own vocabulary, and a repo where that
vocabulary sits BELOW the package it names is the drift
`test_no_slack_identifiers_below_the_slack_package` exists to catch.

What that placement has to keep narrow is the reach into `stigmergy.capture` it implies —
`capture.schema.startup_ddl_lock` (`CREATE INDEX IF NOT EXISTS` is not atomic against a
concurrent creator) is behind exactly ONE door, because `stigmergy.capture` may otherwise only be
imported by `stigmergy.server` (`tests/test_architecture.py::test_server_imports_capture`). That
door is this package's own: `stigmergy.slack.store` imports `stigmergy.capture.schema` directly, and
only for the schema helpers (`startup_ddl_lock`, `ensure_capture_schema`) plus the constants it
re-exports (`FILED`, `NEEDS_INPUT`, `MAX_HINT_CHARS`, `withheld_reason`) — the pinned edge
`test_slack_store_imports_only_capture_schema` guards, so the reach never widens into the rest of
`stigmergy.capture`.

`stigmergy.capture.queue` is never imported at all. The poller's reads of `capture_queue` are
read-only — never a claim, a lease or a mutation — and `stigmergy.slack.store` embeds RAW SQL that
joins directly to `capture_queue` and names its columns by hand (`q.status`, `q.reply`,
`q.report`, `q.result_ref` — see `store.py`'s own module docstring). That is a real coupling the
import-level pinned-edge tests cannot see, because it is not an import:
`tests/test_architecture.py::test_slack_store_sql_column_names_exist_on_capture_queue` pins the
SQL-referenced column names against `capture.schema`'s own DDL instead, so a `capture_queue`
rename breaks a test with a message rather than the poller at runtime.

`slack_submissions` is deliberately Slack's own table rather than a shared submission-origin
abstraction: the steward doorbell's PR/parked-row notifications are a different key and a
different lifecycle from a Slack dedup reservation.

Every handler in this package is built to be driven with NO network: `stigmergy.slack.gateway`
defines the one seam every Slack API call crosses (`SlackGateway`), and
`stigmergy.slack.gateway.FakeSlackGateway` is the offline double `tests/slack/` drives them with —
the same posture the fake embedder and the fake answer LLM already take in this repo.
"""
