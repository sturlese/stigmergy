# Changelog

All notable changes to this project are documented here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style, following
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version stays below `1.0.0` the contracts described in
[`docs/reference/`](./docs/reference) may still move between minor releases. What will not move
without a decision record in [`docs/decisions/`](./docs/decisions) is *behaviour*: this project
treats its test suite as the contract.

## [0.2.0] - 2026-08-12

The filing engine moves onto this project's own agentic harness (pydantic-ai), and the
Claude-Code-harness path retires. Two changes an operator upgrading from `0.1.0` must act on:
`STIGMERGY_LIBRARIAN_BACKEND=sdk` is refused at startup, by name (see **Removed**), and
`STIGMERGY_LIBRARIAN_MODEL` now takes a provider-prefixed id — `anthropic:claude-sonnet-5`, not
`claude-sonnet-5` (see **Changed**).

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
  runs as one structured, tool-less call on any provider-prefixed pydantic-ai model string.
- **The structured ordinary flow** (ADR 033) — a deterministic gatherer (`librarian/gather.py`)
  reads the checkout at the base commit and hands the model candidates, an entity view and the
  link neighbourhood instead of live `Read`/`Glob`/`Grep` exploration; the agent returns the page's
  own text in its account and code writes it, confined by construction rather than by a permission
  hook. The pydantic-ai backend now serves both flows, so the meeting-only restriction above (and
  its eval-rig escape) is gone.
- **The agentic pydantic harness — the ordinary filing agent explores again** (ADR 034), on this
  project's own harness rather than a vendor's. `PydanticFilingAgent.run()` is an ITERATING
  pydantic-ai run with five tools over the item's checkout — `search_pages`, `read_page`,
  `list_page_names`, `resolve_entities`, `write_page` — whose bodies are the gatherer's own pure
  functions, with confinement asked INSIDE each tool instead of in a permission hook. Reads reach
  the content zones and the per-type page templates (`ops/templates/<type>.md`, the structural
  source of truth for the container a self-writing run must produce) and nothing else; writes reach
  one new page in the fast-lane folders and the outcome file, through the unchanged
  `agent.confined_write`. ADR 033's
  gathered block survives as the SEED those tools go further from, so the port grows a second
  declaration (`wants_gathered`) beside `structured_ordinary`; the agent writes its own page and
  returns its account as `.librarian-outcome.json` again. The rule behind it, which the next
  milestone should not have to re-derive: **deterministic code may seed context and implement
  tools, and must not replace the model's ability to decide the context is not enough.** The
  meeting flow is untouched — one structured call, no tools.

### Changed

- **`STIGMERGY_LIBRARIAN_MODEL` now takes a PROVIDER-PREFIXED id, and the default moved with it**
  (`claude-sonnet-5` -> `anthropic:claude-sonnet-5`). Same model, same provider: the surviving
  backend resolves ids through pydantic-ai, where a bare name means an OpenAI model, so a bare
  default would have been the one value a worker could not boot on. A worker configured with a bare
  id is refused at startup, and the refusal names the prefixed spelling of the id it was given.
- **`STIGMERGY_LIBRARIAN_MAX_TURNS` is LIVE again** (ADR 034), at the same default and with the
  same meaning it always had: how many model requests one ordinary capture may spend going round
  with its tools. It reaches pydantic-ai as `UsageLimits(request_limit=…)`, and exceeding it is a
  refusal that names the variable rather than a silent stop. It had been deprecated for one
  milestone, while the ordinary flow made a single call; no operator has to re-derive a number,
  because the number did not move.
- **`STIGMERGY_LIBRARIAN_MAX_TOOL_CALLS` stays DEPRECATED** and read by no shipped backend: it was
  a tool-call ceiling the worker counted itself for a harness that had none, and pydantic-ai both
  accumulates tool calls and bounds the loop that makes them by REQUESTS — so a second
  hand-maintained ceiling would need a defect behind it rather than a symmetry. Still PARSED, so a
  value an operator set is not silently dropped — but a malformed one fails the boot with a bare
  `ValueError` rather than a named refusal, which is pre-existing behaviour. Removing it is the
  recorded follow-up. `..._TIMEOUT_S` is unaffected and IS refused by name: the wall clock is still
  enforced around the WHOLE run, and the visibility lease is still derived from it.
- **`AgentRun.turns` and `AgentRun.tool_calls` carry real numbers again** for the ordinary flow,
  read from the framework's own accumulator rather than counted a second time by hand. Zero stays a
  legitimate answer and now means one specific thing — this shape has no loop (every meeting run,
  any structured backend) — never "nobody counted".

- The HTTP tier's tests skip without Docker instead of failing, and the librarian code map names
  both halves of the refusal-routing suite.

### Removed

- **The `sdk` filing backend — the Claude Code harness path — is retired** (ADR 033 D6's gate,
  spent: the full M0 golden all-bars-PASS on the structured flow, a 20-capture staging shakedown
  with zero flow failures, the container e2e green on CI per push, and then an explicit decision).
  Gone with it: `SdkAgent` and its two run methods, the options builders, the three tool-permission
  hooks, the tool allow/deny lists, the subprocess environment allow-list and the Claude-credential
  startup pre-flight; the `claude-agent-sdk` dependency; and, from the image, the Node runtime and
  the ~500MB agent CLI with their entries in `scripts/docker/tool-checksums.txt`. **The image is
  roughly 55% smaller.** `fly.toml` moves to `backend=pydantic` with the prefixed model id.

  **A deployment still configured for `sdk` is refused at startup, by name.** The value lives in a
  `fly.toml` or a gitignored `.env` that a `git pull` does not touch, so this is configuration
  outliving its code rather than a typo: the message says the backend was retired, names the two
  edits the replacement takes, and gives the image rollback (`fly releases` -> `fly deploy
  --image`) for getting a worker running meanwhile. The queue is durable; nothing claimed is lost
  while it is down.

  What is genuinely lost, rather than replaced: the harness lockdown that hardened a subprocess
  which no longer exists. The write-confinement RULE (`agent.confined_write`) is untouched, and it
  is now asked by BOTH shipped backends — the offline double on every keyless filing, and the real
  backend's `write_page` tool on every live one (ADR 034). The hand-counted tool-call ceiling stays
  gone: the framework counts tool calls itself.

- **`worker._check_brief_matches_backend`** (ADR 034) — the startup refusal for a structured worker
  whose knowledge-repo brief still described a tool-holding run. Keyed on `structured_ordinary`, it
  went inert the moment the shipped ordinary backend declared `False`, and an inert check that
  still reads as coverage is worse than no check. The landing-order rule it enforced survives in the
  mechanism that made it enforceable: the brief is environment-neutral, and each backend states its
  own mechanics in the preamble it composes. `pydantic_backend.ORDINARY_ADR` retired with its only
  reader.

### Fixed

- **Filing reliability: a symmetric brief, a corrective facts line, and faults that name
  themselves** (ADR 035) — measured on the agentic harness, 8 of 13 first-pass drafts omitted the
  page's frontmatter block entirely under the old brief emphasis; after the knowledge-repo brief
  rewrite, 0 of 12. Two shape-neutral defenses land with it: the contract gate's `frontmatter`
  finding now appends a facts line stating the field split (what the worker stamps after the draft
  versus what the draft must already carry) to every corrective retry, and a pydantic-ai
  `UnexpectedModelBehavior` fault now persists its real message — bounded, fence-neutralized where
  it reaches a prompt — instead of surviving only as a class name. The two hand-rolled one-line
  composers collapse into one seam, `stigmergy.text.one_line`. The filing eval fixture is re-frozen
  a third time (the librarian brief alone moved; the linter and the meeting brief are
  byte-identical).

Sixteen further bug-sweep fixes and one documentation correction, none of them behaviour changes in
the ADR sense — each closes a gap between what the code promised and what it did. Grouped by what
they protect:

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

[0.2.0]: https://github.com/sturlese/stigmergy/releases/tag/v0.2.0
[0.1.0]: https://github.com/sturlese/stigmergy/releases/tag/v0.1.0
