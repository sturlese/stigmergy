"""`stigmergy.digest` — the week's learning in one Slack post.

Owns no table: it READS `gardener_findings`/`job_runs`, `capture_queue`/`pages_index` and
`review_decisions`, and WRITES exactly one thing — its own `job_runs` row, the watermark the next
default run's window starts from. `stigmergy-digest` assembles two deterministic sections and
posts them, or previews with `--dry-run`.

This package BROADCASTS: every page title it names is read through
`server.acl.visible(acl, audiences)` at the DESTINATION CHANNEL's resolved audiences
(`slack.channels.channel_audiences`), never the operator's unscoped view — built before the first
labelled page exists, because retrofitting a filter onto a shipped broadcast surface is how leaks
happen. Every corpus-derived string is escaped via `slack.mrkdwn.escape_mrkdwn` before
composition.

Import edges are pinned per module by `tests/test_architecture.py`; only `cli.py` opens the
database connection or reaches the Slack SDK factory.
"""
