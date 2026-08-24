<p align="center">
  <img src="docs/assets/hero.svg" alt="Stigmergy: a team's knowledge, captured where the work happens, filed by an agent, and answered with citations you can check" width="100%">
</p>

<p align="center">
  <a href="https://github.com/sturlese/stigmergy/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/sturlese/stigmergy/actions/workflows/ci.yml/badge.svg"></a>
  <a href="./LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-3b82f6.svg"></a>
  <a href="./pyproject.toml"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB.svg"></a>
  <a href="#from-claude-code-or-codex"><img alt="MCP server and local bridge" src="https://img.shields.io/badge/MCP-server%20%2B%20local%20bridge-a78bfa.svg"></a>
  <a href="./specs/karpathy-team-wiki.md"><img alt="Specification" src="https://img.shields.io/badge/spec-karpathy--team--wiki-f6b73c.svg"></a>
</p>

**Stigmergy** is the team version of the wiki described in
[Karpathy's gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f): immutable
source material enters one queue, one librarian agent keeps a small Git-and-Markdown wiki current,
and every search and answer is scoped to what the caller is allowed to see.

Ants coordinate by leaving traces in the environment rather than by talking to each other. That is
*stigmergy*. Here every capture is a trace — a Slack thread, a PDF, a pasted synthesis — the
librarian follows the traces, and the wiki emerges without anyone approving a queue.

---

- [Why](#why)
- [How it works](#how-it-works)
- [The write path](#the-write-path)
- [The knowledge model](#the-knowledge-model)
- [Using it](#using-it) · [Claude Code / Codex](#from-claude-code-or-codex) · [Slack](#from-slack) · [Backoffice](#from-the-backoffice) · [MCP tools](#mcp-tools)
- [Visibility and security](#visibility-and-security)
- [Models](#models)
- [Quality and tests](#quality-and-tests)
- [Quick start](#quick-start)
- [Deployment](#deployment)
- [Repository layout](#repository-layout)
- [Design principles](#design-principles)
- [Documentation](#documentation)
- [Contributing](#contributing)

## Why

The single-person wiki works because the loop is tiny: material in, pages out, one editor. A team
needs the same loop plus everything a shared deployment forces on it — identity and visibility,
concurrent submissions, binary evidence, Slack, cloud operation, and audit. Stigmergy adds exactly
that infrastructure and refuses everything that would duplicate the loop: no capture "kinds", no
approval inboxes, no generated dossiers, no CLI-only features.

| You get | How |
|---|---|
| **Capture where the work happens** | Claude Code or Codex through a local MCP bridge, a `:brain:` reaction in Slack, or the master backoffice. Text, files, public URLs, and private Google Drive documents. |
| **Evidence you can trust** | Exact original bytes are content-addressed in a private object store. Each capture renders one immutable, readable source page in Git. |
| **A librarian that files, not a queue that waits** | The agent creates, rewrites, consolidates, and deletes wiki pages without approval. Deterministic gates protect the corpus. |
| **Answers with receipts** | Hybrid lexical + vector search, and `ask` returns cited answers verified by pure code. An unsupported figure means a refusal, never an invention. |
| **Visibility as a write constraint** | Restricted evidence can never shape a page visible to a broader audience. Hidden things are not discoverable, even by guessing paths or IDs. |
| **Honest uncertainty** | Credible contradictions stay explicit, dated, and cited. A resolution is simply a later capture. |
| **A corpus that heals itself** | A scheduled gardener lints and repairs through the same writer and gates. It creates no to-do list for humans. |
| **Everything auditable** | One landed operation is one Git commit plus one change record with a friendly diff and the hash-verified exact patch. |

## How it works

<p align="center">
  <img src="docs/assets/architecture.svg" alt="Architecture: three adapters produce one CaptureEnvelope, a durable queue feeds one serialized writer, the writer lands one commit in the Git knowledge repository, and an ACL-scoped index serves every read" width="100%">
</p>

1. **Capture.** Three thin adapters authenticate and acquire bytes. The local bridge reads files on
   your machine and exports private Google Drive documents through a local OAuth flow whose token
   never leaves your OS keychain. Slack snapshots a thread with speakers, timestamps, permalinks,
   and attachments. The backoffice accepts pasted text, an upload, or a public URL fetched with SSRF
   defenses. Large files travel as bytes through a short-lived presigned upload, never as base64
   inside a tool call.
2. **Normalize and queue.** Every adapter ends at the same `CaptureService` with the same kind-free
   `CaptureEnvelope`: actor, audience, provenance, and content-addressed artifact references. The
   queue holds no second copy of the document. It is durable, leased, and idempotent per actor and
   client key, so a retry returns the original receipt instead of a second commit.
3. **Write.** One serialized writer process owns every Git mutation. It extracts text (OCR only for
   images and scanned pages), renders one immutable `sources/YYYY/MM/<capture-id>.md`, asks the
   librarian for a structured `FilingPlan`, and advances the branch only when every gate passes:
   page schema, source immutability, links, ACL flow, entity claims, contradiction markers, registry
   reproduction, and trusted-writer checks. One landed operation is one commit and one change record.
4. **Remember.** The knowledge repository is a plain Git repository of Markdown. Notes and concepts
   belong to the librarian, entity pages hold only identity, sources are append-only. Postgres is
   operational state and a rebuildable index — never a second wiki.
5. **Read.** `search_brain`, `read_page`, `ask`, `list_entities`, and `describe_entity` share one
   visibility policy. Incremental indexing rides a GitHub webhook; a pinned nightly full rebuild
   guarantees convergence, and the backoffice warns when the index drifts from repository HEAD.

## The write path

<p align="center">
  <img src="docs/assets/capture-lifecycle.svg" alt="The write path: acquire, upload, queue, process, landed; a failure becomes a typed safe error with bounded retries and no partial commit" width="100%">
</p>

The durable state machine is `queued → processing → landed | failed`. There is no `awaiting_review`
and no `needs_human`: ambiguity is preserved as an explicit contradiction, technical failures retry
within a bounded lease policy, and a terminal failure carries a typed, safe error the master can
retry once the infrastructure problem is fixed. A crash after the Git commit but before the
database acknowledgement is reconciled by the operation marker and commit SHA, never by a second
commit.

The librarian receives the readable source, its provenance and visibility, only the existing wiki
material the submitter may read, deterministic search candidates, and the exact operations it may
return. It may create or rewrite a note or concept, consolidate and delete a redundant page,
propose an entity identity claim, add or resolve a contradiction marker, or decide that the source
adds no durable conclusion — in which case the source still lands and the result is still audited.
It never rewrites `sources/`, never broadens an ACL, and never waits for approval.

Deletion is not a capture. `brain_delete(paths, why)` is an explicit write operation with its own
authorization and rationale, executed by the same writer: it sweeps references across the corpus,
passes the same gates, lands one commit, and removes the pages from current search while Git keeps
the history.

## The knowledge model

<p align="center">
  <img src="docs/assets/knowledge-model.svg" alt="The knowledge model: notes, concepts, entity identities, immutable sources, and the rule that visibility is a write constraint" width="100%">
</p>

| Role | Location | Mutable by normal filing? | Meaning |
|---|---|:---:|---|
| Note | `wiki/notes/` | yes | contextual conclusion, decision, or event worth retrieving |
| Concept | `wiki/concepts/` | yes | durable explanatory knowledge |
| Entity identity | `wiki/entities/ent_<uuid>.md` | entity primitives only | opaque ID and scoped name claims, never a dossier |
| Source | `sources/YYYY/MM/<capture-id>.md` | no | immutable readable evidence for exactly one capture |

Notes and concepts carry an editorial maturity (`seed`, `developing`, `mature`, `evergreen`), an
optional ACL, the entities they anchor to, and the sources that support them. A page looks like this:

```markdown
---
id: page_aurora_renewal
type: note
title: Aurora renewal
status: mature
created: 2026-08-10
updated: 2026-08-10
acl:
- sales
entity:
- ent_11111111-1111-4111-8111-111111111111
sources:
- sources/2026/08/20000000-0000-4000-8000-000000000002.md
---

# Aurora renewal

Aurora Systems agreed to an annual renewal with a budget of EUR 120,000. The renewed term starts
on 15 September 2026. The sales team must provide the final order form before the start date.
```

**Entities** are opaque, immutable IDs. Names are claims with scope, provenance, actor, and time, so
a rename never moves a path and a confidential name never leaks through a filename or an anchor.
Facts about an entity live in ordinary notes and concepts; `describe_entity` composes them at read
time from the pages the caller may see. Merges require a shared external identifier or an exact
same-entity assertion in an immutable source — a fuzzy resemblance does nothing and creates no task.

**Contradictions** are a healthy corpus state. When two credible sources disagree, the librarian keeps
both claims, dated and cited, in a strict marker on the narrowest page whose readers may see both:

```markdown
<!-- stigmergy-contradiction-start:con_3f1c2b9a-6d4e-4a2b-9c1d-2f7e8a9b0c1d -->
> [!WARNING] Unresolved contradiction `con_3f1c2b9a-6d4e-4a2b-9c1d-2f7e8a9b0c1d`
> The two renewal sources disagree on the annual budget.
> - **Claim:** The annual renewal budget is EUR 120,000
>   **Date:** `2026-08-10`
>   **Source:** `sources/2026/08/20000000-0000-4000-8000-000000000002.md`
> - **Claim:** The annual renewal budget is EUR 95,000
>   **Date:** `2026-08-18`
>   **Source:** `sources/2026/08/40000000-0000-4000-8000-000000000004.md`
<!-- stigmergy-contradiction-data:eyJjbGFpbXMiOlt… -->
<!-- stigmergy-contradiction-end:con_3f1c2b9a-6d4e-4a2b-9c1d-2f7e8a9b0c1d -->
```

The backoffice derives its Contradictions view from these markers. A master may submit a resolution
with a rationale and optional evidence; it becomes an ordinary capture with `resolution_of`, and the
marker is removed only when the new material actually resolves it.

## Using it

### From Claude Code or Codex

Install the package once on each machine and point the bridge at your deployment. The bridge is a
local stdio MCP server that proxies the read tools to the cloud and performs local acquisition —
files on disk, public URLs, and private Google Drive documents — before uploading verified bytes.

```bash
uv tool install git+https://github.com/sturlese/stigmergy.git
export STIGMERGY_TOKEN="<identity-token>"
```

Claude Code, project-scoped `.mcp.json`:

```json
{
  "mcpServers": {
    "stigmergy": {
      "command": "stigmergy-bridge",
      "args": ["--url", "https://stigmergy.example.com"],
      "env": {
        "STIGMERGY_TOKEN": "${STIGMERGY_TOKEN}",
        "STIGMERGY_GOOGLE_CLIENT_SECRETS": "${STIGMERGY_GOOGLE_CLIENT_SECRETS:-}"
      }
    }
  }
}
```

Codex, project-scoped `.codex/config.toml`:

```toml
[mcp_servers.stigmergy]
command = "stigmergy-bridge"
args = ["--url", "https://stigmergy.example.com"]
env_vars = ["STIGMERGY_TOKEN", "STIGMERGY_GOOGLE_CLIENT_SECRETS"]
required = true
```

Then talk to your agent:

| You say | What happens |
|---|---|
| *"Save the conclusions of this conversation to the brain."* | The agent writes a self-contained synthesis and calls `brain_submit(text=…)`. Exactly that text becomes the immutable source; unseen history is never assumed. |
| *"File ~/Downloads/board-deck.pdf."* | The bridge reads the file, uploads it through a presigned URL, and the worker extracts digital text, OCRs only the scanned pages, and files the conclusions. |
| *"Capture https://docs.google.com/document/d/…"* | On first use a local Google OAuth flow opens; the refresh token stays in your OS keychain. The Doc is exported as DOCX and uploaded — Google credentials never reach Stigmergy. |
| *"What did we decide about the Aurora renewal?"* | `ask` retrieves within your visibility, synthesizes, verifies every figure and citation, and answers or refuses honestly. |

Private Drive capture requires `STIGMERGY_GOOGLE_CLIENT_SECRETS=/absolute/path/google-oauth-client.json`.
Follow progress with `brain_submissions`; remove knowledge explicitly with `brain_delete`.

### From Slack

- **Ask:** mention the bot — `@brain what is the status of the Borealis rollout?` — in a mapped
  channel. The answer is cited and rendered for humans. When your own visibility is wider than the
  channel's audience, the wider part follows up privately instead of leaking into the channel.
- **Capture:** react with `:brain:` on any thread. The adapter snapshots the selected messages with
  speakers, timestamps, and permalinks, downloads supported attachments, and submits one capture
  under the channel's configured audience. A private channel can never be broadened by the reacting
  user; an unmapped channel, a foreign workspace, or an unauthorized reactor captures nothing and
  reveals nothing.

Channels map to audiences in the knowledge repository's `ops/slack-channels.json`. The versioned app
configuration, including its least-privilege scopes, is
[`deploy/slack-app-manifest.json`](./deploy/slack-app-manifest.json).

### From the backoffice

The master backoffice lives at `/admin` on the `app` process and is enabled only when
`STIGMERGY_ADMIN_TOKEN_HASH` is configured. It is a single master identity with global visibility:

| View | What you can do |
|---|---|
| Captures | paste text, upload a file, or submit a public URL; inspect provenance, artifacts, extraction outcome, retries, source path, commit, and change; retry a failed capture |
| Changes | one entry per landed operation: a plain-language summary, per-path cards with the librarian's reason, colored diffs with large source additions collapsed, and the exact Git patch with commit and parent SHAs on demand |
| Contradictions | the live list derived from current Markdown, plus the resolution form |
| Entities | every scoped claim with provenance and redirects; explicit merge and delete with verified evidence |
| Gardener | run history with base/head commits, detected and fixed counts, and a manual trigger |
| Index health | repository HEAD, indexed commit, dirty flag, last incremental event, last full rebuild, and stale warnings |
| Worker | heartbeat, lease state, and the last successful write |

### MCP tools

The cloud server (streamable HTTP with per-user bearer tokens) and the local bridge expose the same
compact surface:

| Tool | Purpose |
|---|---|
| `search_brain(query, filters?, max_results?)` | hybrid lexical + vector ranking over the notes, concepts, and sources you may see |
| `read_page(path)` | one visible wiki or source page with its links and citations |
| `ask(question)` | a cited answer verified by code; refuses rather than invents |
| `list_entities()` | identities with at least one name claim visible to you |
| `describe_entity(entity)` | knowledge composed at read time for an ID, name, or alias |
| `brain_submit(text \| path \| url, title?, occurred_at?, audience?)` | capture exactly one input through the one filing flow |
| `brain_submissions(limit?, status?)` | your captures as `queued`, `processing`, `landed`, or `failed` |
| `brain_delete(paths, why)` | explicit deletion with reference sweep, one commit, one change record |

There is no `kind` argument anywhere. `path` and private Drive URLs exist only in the local bridge;
the cloud receives verified blob references. An omitted `audience` uses your configured default and
never silently falls back to organization-wide.

## Visibility and security

- **Identities and groups** live in the knowledge repository's `ops/identities.json`. A page's `acl`
  is `null` (organization-wide) or a list of groups. Members of `brain-admins` are unrestricted.
- **One policy, everywhere.** `server.acl.visible` owns reader visibility, `kernel.acl.flows_into`
  owns safe information flow, and the write guard composes them for every mutation — librarian
  filing, gardening, entity operations, deletion, indexing, and read projection. There are no copied,
  subtly different ACL predicates per subsystem.
- **Writes cannot leak.** A page may only incorporate evidence every one of its readers may read. A
  restricted capture creates or updates a restricted companion page; it never rewrites an open page
  and the system never silently narrows an existing one.
- **No existence oracle.** Unknown, hidden, and unauthorized pages, entity IDs, aliases, captures,
  contradictions, and change records return indistinguishable responses.
- **Credentials.** Per-user MCP tokens are issued with `stigmergy-issue-token`; the server stores only
  their SHA-256 hashes (`STIGMERGY_TOKEN_STORE` or `STIGMERGY_TOKEN_STORE_FILE`). The backoffice is
  protected by a hash from `stigmergy-admin-token`. Object-store, Slack, GitHub App, and model
  credentials stay in the deployment secret store; the client never sees R2, and the cloud never
  sees Google.
- **Untrusted input stays data.** Captured text, files, URLs, Slack content, page bodies, titles,
  links, and entity names are never instructions. The librarian contract says so explicitly, the
  answer verifier is pure code, and an adversarial test suite keeps it that way.
- **Public URL fetching** accepts HTTP(S) only, revalidates DNS and every redirect, blocks loopback,
  private, link-local, and metadata destinations, sends no ambient credentials, and streams under
  size and time limits.
- **Parsers** detect types from magic bytes and parser validation, enforce page, pixel, and
  decompression limits, and reject encrypted, corrupt, or unsafe containers with typed errors.
- **Nothing sensitive in logs.** Secrets, OAuth tokens, presigned URLs, artifact bytes, restricted
  titles, and DSNs are excluded from logs and safe error messages, and a test asserts it. CI runs a
  pinned `gitleaks` scan.
- **One trusted writer.** Only the librarian's GitHub App identity may change `wiki/`, `sources/`,
  and `ops/entity-registry.json`; the commit contains exactly the paths and bytes the gates accepted.

Report vulnerabilities privately as described in [`SECURITY.md`](./SECURITY.md).

## Models

Every model-backed path goes through one `OPENROUTER_API_KEY` and a closed allowlist enforced in
`kernel.llm`. Routing disables provider fallback, requires the requested parameters, denies data
collection, and requires zero-data-retention processing. Direct Anthropic, OpenAI, or Gemini
credentials are neither accepted nor forwarded.

| Runtime purpose | Model |
|---|---|
| librarian filing and semantic repair | `deepseek/deepseek-v4-flash` |
| cited answers | `z-ai/glm-5.2` |
| vector embeddings | `qwen/qwen3-embedding-8b`, 2560 dimensions |
| scanned-page and image OCR | `qwen/qwen3-vl-8b-instruct` |

Deterministic linting makes no model call; semantic repair is the only model-backed part of a
garden run. The test suite never uses real credentials — it forces deterministic fake backends.

## Quality and tests

The keyless suite is the contract: 1,100+ tests across pure units, real Postgres and Git
integration, adversarial ACL and prompt-injection probes, the Slack adapter, the local bridge with a
fake cloud and fake Google OAuth, and the backoffice API, with a 75% coverage gate. CI also builds
the deployment image.

Optional real-model evaluations measure quality over a frozen canonical corpus and append to
[`evals/history.ndjson`](./evals/history.ndjson). The latest recorded run (2026-08-24):

| Measure | Result | Release bar |
|---|:---:|:---:|
| Retrieval Recall@5 — 15 questions, 9 of them ACL-filtered | **1.00** | ≥ 0.80 |
| Answer honesty — refuses when the corpus cannot support an answer | **1.00** | ≥ 0.90 |
| Answer groundedness — every figure and citation traced to evidence | **1.00** | ≥ 0.84 |
| False-premise refutation | **1.00** | — |

```bash
make retrieval-golden EMBEDDER=openrouter   # ranking arms over the frozen corpus
make qa-golden EMBEDDER=openrouter LLM=openrouter
make adversarial                             # the armed prompt-injection categories
make gates                                   # everything a release must pass
```

See [`evals/README.md`](./evals/README.md) for the corpus, goldens, and the single home of the bars.

## Quick start

Requirements: Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and Docker (for Postgres with
pgvector and MinIO as a local evidence store).

```bash
git clone https://github.com/sturlese/stigmergy.git && cd stigmergy
make venv      # uv sync against the frozen lockfile
make db-up     # Postgres 16 + pgvector on :54321, MinIO on :9000
make test      # complete keyless suite, fake models, 75% coverage gate
make lint      # Ruff over src, tests, evals, and scripts
```

`make test-system` runs the real Postgres and Git acceptance paths; `make help` lists every
developer and operator target.

To poke at a knowledge repository locally, index it and serve it over stdio as one identity:

```bash
export STIGMERGY_INDEX_DSN=postgresql://stigmergy:stigmergy@localhost:54321/stigmergy
stigmergy-index --rebuild --repo ../stigmergy-brain --embedder fake
stigmergy-server --transport stdio --repo ../stigmergy-brain \
  --identity you@example.com --embedder fake
```

## Deployment

One image serves three Fly process groups; the same layout works anywhere a container, Postgres, and
an S3-compatible bucket exist.

| Process | Command | Role |
|---|---|---|
| `app` | `stigmergy-server --transport http` | streamable HTTP MCP, bridge upload endpoints, GitHub index webhook, master backoffice |
| `worker` | `stigmergy-librarian-boot` | clones and verifies the knowledge repository, then runs the only writer and the scheduled gardener |
| `slack` | `stigmergy-slack` | Socket Mode adapter; a Postgres advisory lock keeps exactly one active instance |

```bash
make test && make lint
make deploy-staging     # bakes the knowledge repo's ops files, deploys every group, pins process counts
make rebuild-staging    # full index rebuild from repository HEAD
```

### Configuration

| Area | Variables |
|---|---|
| Models | `OPENROUTER_API_KEY`, `STIGMERGY_LIBRARIAN_MODEL`, `ANSWER_MODEL`, `STIGMERGY_OCR_MODEL`, `ANSWER_LLM=openrouter` |
| Database | `STIGMERGY_INDEX_DSN` |
| Evidence store | `STIGMERGY_EVIDENCE_ENDPOINT`, `STIGMERGY_EVIDENCE_BUCKET`, `STIGMERGY_EVIDENCE_ACCESS_KEY_ID`, `STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY` |
| Server | `STIGMERGY_PUBLIC_HOST`, `STIGMERGY_TOKEN_STORE` or `STIGMERGY_TOKEN_STORE_FILE` |
| Backoffice | `STIGMERGY_ADMIN_TOKEN_HASH`, `STIGMERGY_ADMIN_ACTOR` |
| Writer | `STIGMERGY_REPO`, `STIGMERGY_LIBRARIAN_REPO_URL`, `STIGMERGY_LIBRARIAN_BRANCH`, `STIGMERGY_LIBRARIAN_APP_ID`, `STIGMERGY_LIBRARIAN_INSTALLATION_ID`, `STIGMERGY_LIBRARIAN_PRIVATE_KEY` or `_FILE`, `STIGMERGY_LIBRARIAN_GARDEN_AT` (default `05:07` UTC), `STIGMERGY_LIBRARIAN_TIMEOUT_S` |
| Index webhook | `STIGMERGY_GITHUB_WEBHOOK_SECRET`, `STIGMERGY_GITHUB_REPO`, `STIGMERGY_GITHUB_BRANCH` |
| Slack | `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_TEAM_ID` |

Credentials belong in the deployment secret store; [`fly.toml`](./fly.toml) carries only non-secret
defaults.

### The knowledge repository

Stigmergy owns the platform. Your team's knowledge is a separate private Git repository that the
writer clones, commits to through a GitHub App, and that operators control through plain files:

```text
your-brain/
├── sources/YYYY/MM/<capture-id>.md      immutable readable evidence, one per capture
├── wiki/
│   ├── notes/                           the librarian's contextual conclusions
│   ├── concepts/                        the librarian's durable knowledge
│   └── entities/ent_<uuid>.md           opaque identities with scoped name claims
├── ops/
│   ├── identities.json                  who exists, their groups, their default audience
│   ├── slack-channels.json              Slack channel id → audience (null = organization-wide)
│   └── entity-registry.json             derived from entity pages, regenerated by the writer
├── .claude/skills/librarian/SKILL.md    the filing contract, verified against the platform
└── .github/workflows/                   pinned nightly full index rebuild, 17 4 * * *
```

```json
{
  "ana@example.com": {
    "display_name": "Ana",
    "groups": ["sales", "leadership"],
    "default_audience": ["sales"]
  }
}
```

Only the trusted writer identity may touch `wiki/`, `sources/`, and the derived registry; operators
change `ops/` controls and workflows with their normal identity. The nightly rebuild checks out the
pinned platform revision, indexes repository HEAD, and fails visibly rather than succeeding as a
no-op. [`docs/OPERATIONS.md`](./docs/OPERATIONS.md) has the full runbook and post-deployment checks;
[`docs/RESET.md`](./docs/RESET.md) documents the guarded non-production reset.

## Repository layout

| Package | Owns |
|---|---|
| `kernel` | ACL information flow, deadlines, the approved model boundary, normalization, result types |
| `capture` | envelopes, evidence store, uploads, acquisition, extraction and OCR, queue, source rendering |
| `bridge` | the local stdio MCP client: local files, public URLs, private Google Drive export |
| `knowledge` | page contracts, filing context, `FilingPlan`, writer, linter, repair primitives, contradictions, write guard, the librarian skill |
| `entities` | opaque identities, scoped claims, registry derivation, merge, rename, delete |
| `changes` | exact patches and the append-only change ledger |
| `index` | canonical corpus selection, lexical/vector ranking, incremental updates, full rebuild, convergence health |
| `server` · `answer` | ACL-scoped MCP tools, HTTP transport, webhook, retrieval, verified cited answers |
| `slack` | Socket Mode transport, identity and channel mapping, thread snapshots, rendering |
| `admin` | the master-only operational API and browser UI |
| `librarian` | the long-running writer, bootstrap, Git transport, GitHub App credentials, schedule |
| `ops` | the explicitly guarded non-production reset |

`specs/` holds the implemented contract, `docs/` the runtime map and runbooks, `evals/` the frozen
corpus and quality gates, `deploy/` the Slack manifest and the fail-closed control-file defaults the
image bakes in, and `tests/` the keyless suite.

## Design principles

1. Git and Markdown are current knowledge. Postgres is operational state and a rebuildable index.
2. Every adapter produces the same kind-free `CaptureEnvelope` before queueing.
3. Original bytes and readable source pages are immutable except through explicit deletion.
4. One serialized writer owns every Git mutation: one commit and one change record per operation.
5. Visibility is a write constraint. Narrower evidence never reaches broader readers.
6. The librarian owns notes and concepts; entity pages are minimal identity data.
7. No active write state waits for a human. Supported uncertainty is represented honestly.
8. A corpus-health finding is preventable or autonomously repairable, or it is not a finding.
9. Every product capability is reachable through Slack, MCP, or the backoffice. Installed CLIs are
   service, bootstrap, security, index, or local-bridge entry points.

The full rationale, acceptance criteria, and the decisions behind each rule are in
[`specs/karpathy-team-wiki.md`](./specs/karpathy-team-wiki.md).

## Documentation

- [Specification](./specs/karpathy-team-wiki.md) — the implemented product contract
- [Architecture](./docs/ARCHITECTURE.md) — authorities, capture, gates, visibility, entities, read path
- [Operations](./docs/OPERATIONS.md) — deployment, Slack application, validation, nightly reconciliation
- [Clean reset](./docs/RESET.md) — the guarded non-production reset
- [Quality evaluations](./evals/README.md) — corpus, goldens, bars
- [Changelog](./CHANGELOG.md)

## Contributing

Read [`CONTRIBUTING.md`](./CONTRIBUTING.md) and [`CLAUDE.md`](./CLAUDE.md) before changing the
system: they carry the invariants, the testing doctrine, and the completion rules. Branch from
`main`, keep commits focused, and run `make lint` and `make test` before requesting review.

Stigmergy is released under the [Apache License 2.0](./LICENSE).
