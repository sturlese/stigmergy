"""`stigmergy.slack` — the Slack transport: resolve *who is asking* from a Slack event, then call
the same `BrainService`/`AnswerService` every other transport calls.

**This package enforces nothing.** `stigmergy.server.acl.visible()` stays the ONE enforcement
point. What this package decides is which scoped service serves an event: `identity` resolves a
Slack user to an email (the SAME `resolve_audiences` and the SAME `ops/identities.json` every
transport reads) and `channels` resolves a channel to its audience scope
(`ops/slack-channels.json`).

**Layering (pinned per module by `tests/test_architecture.py`): `slack` imports `server`, `answer`
and `review_kinds`, plus ONE narrow, named exception — `stigmergy.slack.store` may import
`stigmergy.capture.schema` (schema helpers and the re-exported constants only) — and nothing
imports `slack`.** The Slack-shaped tables live in this package because
`team_id`/`channel_id`/`slack_user_id` are Slack's own vocabulary
(`test_no_slack_identifiers_below_the_slack_package`). `store.py` also embeds RAW SQL that joins
`capture_queue` and names its columns by hand — a coupling no import-level test can see —
pinned instead by `test_slack_store_sql_column_names_exist_on_capture_queue`.

Every handler runs with NO network: `stigmergy.slack.gateway.SlackGateway` is the one seam every
Slack API call crosses, and `FakeSlackGateway` is the offline double `tests/slack/` drives.
"""
