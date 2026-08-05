"""`stigmergy.digest` — the week's learning in one Slack post.

Sibling of `gardener`, `views`, `capture`, `index`, `server`, `slack`. Owns no table of
its own: it READS `gardener_findings`/`job_runs` (corpus
health — the gardener's own findings store), `capture_queue`/`pages_index` (corpus deltas) and
`review_decisions` (entities born), and WRITES exactly one thing — its own `job_runs` row, the
watermark the next default run's window starts from. `stigmergy-digest` is the CLI: assemble two
deterministic sections, post to the configured Slack channel, or `--dry-run` to preview.

**Findings-only reading, write-only-to-`job_runs`, by construction — no different from `gardener`
in kind, narrower in scope.** This package never writes a page, opens a PR, edits the registry, or
mutates any row it reads; the ONE write is `capture.ops.record_job_run` against `job_runs`, the
same shared bookkeeping table every operator CLI in this codebase already writes through. Ruled out
structurally, not by discipline alone: this package imports no git plumbing beyond one pure-policy
symbol (below) and holds no path under `wiki/` (`tests/test_architecture.py`'s mirrors of the
same proofs `gardener` already carries).

**The constraint `gardener` never had to answer: this package broadcasts.** It renders corpus page
titles into a Slack channel — a channel is a broadcast surface, and `acl.visible()` is the
ONE place read access is decided in this codebase (CLAUDE.md's invariant table).
So, unlike `gardener` (terminal output only, no caller identity to scope to — an
`ACL_REACHABILITY_EXCEPTIONS` entry), `digest` names a REAL predicate: every page it names
(`corpus deltas`'s "pages filed") is read
through `stigmergy.server.acl.visible(acl, audiences)` at `audiences = stigmergy.slack.channels.
channel_audiences(...)`, resolved for the destination channel — that CHANNEL's own scope, never the
operator's, never unscoped. `ops/slack-channels.json` does not exist in the real deployment
(`slack/channels.py`'s own docstring), so this filter is currently indistinguishable from no
scoping at all — it becomes load-bearing the moment the first labelled page exists. It is built
anyway, deliberately, because retrofitting a filter onto a shipped broadcast surface is how leaks
happen.

**Layering** (enforced by `tests/test_architecture.py`, mirroring `gardener`'s own edge
assertions): `digest` may import `stigmergy.capture.ops` (the shared `job_runs` writer) and
`stigmergy.capture.schema` (`ensure_capture_schema` + the `FILED` status literal — this package owns
no DDL of its own, so it never touches `startup_ddl_lock` directly); `stigmergy.gardener.store`
(`findings_for_run`/`latest_completed_run` — the gardener's findings store, never a second,
independently-written `job_runs`/`gardener_findings` query) and `stigmergy.gardener.schema` (the
severity vocabulary) and `stigmergy.gardener.settings` (`DIGEST_CHANNEL_ID_ENV`/`SLACK_BOT_TOKEN_ENV`
— imported, never re-declared: one channel setting, one place to look); `stigmergy.server.acl`
(`visible` — the broadcast predicate above) and `stigmergy.server.review`
(`KIND_ENTITY_PROPOSAL`/`APPROVE`/`ensure_review_schema` — a one-way read edge, `digest → server`,
that creates no cycle); `stigmergy.slack.channels` (`channel_audiences`), `stigmergy.slack.gateway`
(the `SlackGateway` protocol + `FakeSlackGateway`, the posting seam) and `stigmergy.slack.mrkdwn`
(`escape_mrkdwn` — every corpus-derived string this package interpolates into a Slack message, a
page title included, is client-generated text by that module's own definition and must be escaped
before it is composed into mrkdwn, never after).

Only `cli.py` additionally imports `stigmergy.index.store` (the connection seam, mirroring every
other operator CLI in this codebase), `stigmergy.slack.bolt_gateway` (the real gateway, from just a
bot token — nothing else in this package ever reaches the Slack SDK) and `stigmergy.librarian.config`
(the `--repo` default, mirroring `gardener/cli.py`'s identical use of the same two constants —
`REPO_ENV`/`REPO_DEFAULT` are pure policy, not git plumbing).

It must NEVER import `stigmergy.answer`, `stigmergy.entities`, `stigmergy.views`, or `stigmergy.
librarian` beyond the one declared `config` symbol above — this package has no caller identity and
no write path into the knowledge repo, so it has no business reaching into any package that serves
or governs one.
"""
