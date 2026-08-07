# Changelog

All notable changes to this project are documented here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style, following
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version stays below `1.0.0` the contracts described in
[`docs/reference/`](./docs/reference) may still move between minor releases. What will not move
without a decision record in [`docs/decisions/`](./docs/decisions) is *behaviour*: this project
treats its test suite as the contract.

## [0.1.0] - 2026-08-07

First public release. The system it describes has been running against a real corpus before being
published, so this entry describes what the release **contains** rather than what changed since a
previous one — there isn't one.

### Added

- **The write path.** Captures arrive on a durable Postgres queue and are drained one at a time by
  the librarian: an agent (Claude Agent SDK) drafts a page in an ephemeral git worktree, and then
  **code judges the resulting diff** before anything can commit. Eight deterministic gates —
  zone, binary-page, body-rewrite, secrets, PII, frontmatter, contract linter, anchoring — each of
  which can veto. The agent proposes; code decides.
- **The human loop.** When a capture cannot be placed, the librarian asks its submitter exactly one
  question rather than guessing, and the budget for that is a database column, so it survives a
  retry, a redelivery and a steward requeue. Creating a new entity is a governed act: a steward
  approves it, and `stigmergy-entities` is the only writer of the registry.
- **Read access decided in one place.** Ordered path rules stamp audience labels at write time;
  `server.acl.visible()` is the single function the read path goes through, and an architecture
  test fails the build if any reader of the index bypasses it.
- **One MCP server, ten tools**, over stdio and streamable HTTP, with per-user hashed-token auth,
  an audit log and rate limiting. `ask` answers with citations or refuses — figures and quotes are
  verified against the sources at answer time, by code.
- **Three doors onto the same queue**: Slack (a 🧠 reaction captures a thread verbatim, `@`-mention
  or DM to ask, Block Kit review surfaces for stewards), the meeting distiller (a transcript
  becomes an atomic page *set* — source, meeting, and one page per decision, each anchored
  separately), and a Drive door that fetches through the operator's own Google auth.
- **A hybrid derived index** — Postgres + pgvector, a lexical and a semantic arm fused with
  Reciprocal Rank Fusion, then explainable contract ranking (superseded pages demoted, entity and
  period matches boosted, staleness penalised against an injected `today`). Disposable by design:
  wipe it and rebuild from git.
- **Views**: a per-entity rollup with a deterministic skeleton plus an agent-written synthesis,
  whose audience is the intersection of its members'.
- **The gardener**, eight deterministic corpus-health checks plus a bounded model editorial sweep,
  and a two-section Slack digest scoped to its destination channel.
- **An admin console** at `/admin` on the existing app process group — steward drain, remote
  control of the crons, and an activity view. Inert until its token hash is configured.
- **Seventeen CLIs**, a `docker-compose` stack for the whole thing, and cron templates in
  [`deploy/workflows/`](./deploy/workflows) to copy into your own knowledge repo.

### Notes

- Python 3.12+, Apache-2.0. Knowledge lives in a **separate git repository you own** — this one
  stores no pages.
- The suite is keyless by construction: 3,598 tests at 92.77% coverage run against real Postgres,
  real MinIO and real git, with an offline double standing in for the model. If something needs an
  API key to pass, it is in the wrong place.
- Not yet load-tested beyond a single team's corpus, and the SLA notice path has no producer today
  (nothing emits an `sla` finding) — both are stated in the reference docs rather than left to be
  discovered.

[0.1.0]: https://github.com/sturlese/stigmergy/releases/tag/v0.1.0
