# Changelog

All notable changes to this project are documented here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style, following
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version stays below `1.0.0` the contracts described in
[`docs/reference/`](./docs/reference) may still move between minor releases. What will not move
without a decision record in [`docs/decisions/`](./docs/decisions) is *behaviour*: this project
treats its test suite as the contract.

## [Unreleased]

### Added

- **The filing golden** — a third eval instrument, on the one surface that writes: `make
  filing-golden` drives ten frozen captures through the real filing path (agent, eight gates, real
  git, real Postgres) against a frozen mini knowledge repo and scores functional facets
  deterministically, each with its own denominator and its own bar, fixed from the first Sonnet-5
  baseline pair. The fixture pins the brief version every score was measured under.
- **The `FilingAgent` port and the pricing seam** (ADR 032) — the filing backend contract is an
  explicit, typed Protocol with three conforming implementations, and token usage becomes
  `report.cost_usd` through one pricing module (declared-inclusive token convention, a rate table
  with an env override that refuses non-finite, negative and zero-output rates — an unpriced model
  is a loud startup refusal, never a silent `$0.00`).
- **A pydantic-ai meeting backend** (`STIGMERGY_LIBRARIAN_BACKEND=pydantic`) — the meeting flow
  runs as one structured, tool-less call on any provider-prefixed pydantic-ai model string. Meeting
  flow only in this milestone: a worker configured with it refuses at startup by name, and only the
  eval rig's meeting-only runs may use it. The ordinary flow stays on the Claude Agent SDK.

Sixteen bug-sweep fixes and one documentation correction since `0.1.0`, none of them behaviour
changes in the ADR sense — each closes a gap between what the code promised and what it did. Grouped
by what they protect:

### Fixed

- **Pages that indexed open.** A page whose frontmatter could not be parsed, and two further routes
  to the same end, no longer land in the index with no `acl` label — the failure mode where an
  unreadable page becomes a readable one.
- **Access and identity.** A scoped queue read that failed open; an entity proposal accepted as a
  parked capture on a caller's say-so; an entity registry that could leak its own path or mint a
  phantom alias; a raw byte in a signature header answered `500` instead of `401`; a signed
  non-object webhook body ignored rather than acted on.
- **Refusals that blamed the wrong party.** Two librarian refusals named the wrong cause, a secret
  split across a line break reported a line number that was not one, and `Ctrl-C` stopped claiming
  work on its way out.
- **The surfaces people actually see.** Four Slack defects; a view that never went stale and one
  that cited itself; a hand-written page under `views/` that killed the gardener run; a recency word
  that was matched as a substring; a snippet that was not reproducible.
- **Prose that the code did not keep.** Six documented promises reconciled with the code, and four
  faults that nothing told anybody about.

### Changed

- The HTTP tier's tests skip without Docker instead of failing, and the librarian code map names
  both halves of the refusal-routing suite.

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
