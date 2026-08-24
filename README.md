<p align="center">
  <img src="docs/assets/stigmergy.png" alt="Stigmergy" width="100%">
</p>

<p align="center">
  <a href="https://github.com/sturlese/stigmergy/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/sturlese/stigmergy/actions/workflows/ci.yml/badge.svg"></a>
  <a href="./LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-3b82f6.svg"></a>
  <a href="./pyproject.toml"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB.svg"></a>
  <a href="#from-claude-code-or-codex"><img alt="MCP server and local bridge" src="https://img.shields.io/badge/MCP-server%20%2B%20local%20bridge-a78bfa.svg"></a>
  <a href="./specs/karpathy-team-wiki.md"><img alt="Specification" src="https://img.shields.io/badge/spec-karpathy--team--wiki-f6b73c.svg"></a>
</p>

**Stigmergy** is the team version of the wiki in
[Karpathy's gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). Immutable
source material enters one queue, one librarian agent keeps a small Git-and-Markdown wiki current,
and every search and answer is scoped to what the caller may see.

Ants coordinate by leaving traces in the environment. Here every capture is a trace, the librarian
follows the traces, and the wiki emerges — nobody approves a queue.

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

## Why

A one-person wiki works because the loop is tiny. A team needs the same loop plus what a shared
deployment forces on it: identity, visibility, concurrency, binary evidence, Slack, audit.
Stigmergy adds exactly that and nothing that duplicates the loop.

| | |
|---|---|
| **Capture where the work happens** | Claude Code or Codex, a `:brain:` reaction in Slack, or the backoffice. |
| **Evidence you can trust** | Exact bytes in a private store; one immutable source page per capture. |
| **A librarian that files** | Creates, rewrites, consolidates, and deletes pages without approval. |
| **Answers with receipts** | Hybrid search and `ask` with citations verified by code. |
| **Visibility on writes too** | Restricted evidence never shapes a page a broader audience can read. |
| **Honest contradictions** | Conflicting claims stay explicit, dated, and cited. |
| **Self-healing corpus** | A scheduled gardener repairs through the same gates. No human to-do list. |
| **Full audit** | One operation, one commit, one change record with the exact patch. |

## How it works

<p align="center">
  <img src="docs/assets/architecture.svg" alt="Architecture" width="100%">
</p>

1. **Capture.** Thin adapters authenticate and acquire bytes. Local files and private Google Drive
   documents stay on your machine until uploaded through a presigned URL.
2. **Queue.** Every adapter produces the same kind-free `CaptureEnvelope`. The Postgres queue is
   durable, leased, and idempotent per actor and client key.
3. **Write.** One serialized writer extracts text, renders the immutable source page, asks the
   librarian for a `FilingPlan`, and advances the branch only when every gate passes.
4. **Remember.** The knowledge repository is plain Git and Markdown. Postgres is operational state
   and a rebuildable index, never a second wiki.
5. **Read.** Five tools, one visibility policy. A webhook indexes incrementally; a nightly full
   rebuild guarantees convergence.

## The write path

<p align="center">
  <img src="docs/assets/capture-lifecycle.svg" alt="The write path" width="100%">
</p>

States are `queued → processing → landed | failed`. Nothing waits for a human: ambiguity becomes
an explicit contradiction, technical failures retry within a bounded lease, and a terminal failure
carries a typed error the master can retry. A crash after the commit is reconciled by commit SHA,
never by a second commit.

The librarian may create or rewrite a note or concept, consolidate and delete a redundant page,
propose an entity claim, add or resolve a contradiction, or file nothing — the source still lands.
It never rewrites `sources/` and never broadens an ACL. Deletion is a separate explicit operation
(`brain_delete`) through the same writer and gates.

## The knowledge model

<p align="center">
  <img src="docs/assets/knowledge-model.svg" alt="The knowledge model" width="100%">
</p>

| Role | Location | Mutable by filing? | Meaning |
|---|---|:---:|---|
| Note | `wiki/notes/` | yes | contextual conclusion, decision, or event |
| Concept | `wiki/concepts/` | yes | durable explanatory knowledge |
| Entity identity | `wiki/entities/ent_<uuid>.md` | entity primitives only | opaque ID and scoped name claims |
| Source | `sources/YYYY/MM/<capture-id>.md` | no | immutable evidence for one capture |

A note or concept carries a maturity (`seed`, `developing`, `mature`, `evergreen`), an optional
ACL, entity anchors, and its sources:

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
on 15 September 2026.
```

Entities are opaque IDs with scoped, sourced name claims; facts live in notes and concepts and
`describe_entity` composes them at read time. Merging needs a shared external ID or an exact
assertion in a source — resemblance does nothing.

When credible sources disagree, the librarian keeps both claims in a strict marker on the narrowest
page whose readers may see both:

```markdown
> [!WARNING] Unresolved contradiction `con_3f1c2b9a-6d4e-4a2b-9c1d-2f7e8a9b0c1d`
> The two renewal sources disagree on the annual budget.
> - **Claim:** The annual renewal budget is EUR 120,000
>   **Date:** `2026-08-10`
>   **Source:** `sources/2026/08/20000000-0000-4000-8000-000000000002.md`
> - **Claim:** The annual renewal budget is EUR 95,000
>   **Date:** `2026-08-18`
>   **Source:** `sources/2026/08/40000000-0000-4000-8000-000000000004.md`
```

A master may later submit a resolution; it is an ordinary capture, and the marker goes away only
when the new evidence actually resolves it.

## Using it

### From Claude Code or Codex

Install the bridge once per machine and point it at your deployment. It proxies the read tools to
the cloud and acquires local files, public URLs, and private Google Drive documents locally.

```bash
uv tool install git+https://github.com/sturlese/stigmergy.git
export STIGMERGY_TOKEN="<identity-token>"
```

Claude Code, `.mcp.json`:

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

Codex, `.codex/config.toml`:

```toml
[mcp_servers.stigmergy]
command = "stigmergy-bridge"
args = ["--url", "https://stigmergy.example.com"]
env_vars = ["STIGMERGY_TOKEN", "STIGMERGY_GOOGLE_CLIENT_SECRETS"]
required = true
```

| You say | What happens |
|---|---|
| *"Save the conclusions to the brain."* | `brain_submit(text=…)` with a self-contained synthesis. |
| *"File ~/Downloads/board-deck.pdf."* | The bridge uploads the bytes; the worker extracts, OCRs scanned pages, files. |
| *"Capture https://docs.google.com/document/d/…"* | Local Google OAuth, token in your keychain, DOCX export uploaded. |
| *"What did we decide about the Aurora renewal?"* | `ask` retrieves within your visibility and answers with verified citations. |

Private Drive needs `STIGMERGY_GOOGLE_CLIENT_SECRETS=/absolute/path/google-oauth-client.json`.

### From Slack

- **Ask:** `@brain what is the status of the Borealis rollout?` in a mapped channel. If you can
  see more than the channel, the extra follows up privately.
- **Capture:** react with `:brain:` on a thread. Speakers, timestamps, permalinks, and attachments
  become one capture under the channel's audience. Unmapped channels and unauthorized reactors
  capture nothing.

Channels map to audiences in `ops/slack-channels.json`; the app manifest is
[`deploy/slack-app-manifest.json`](./deploy/slack-app-manifest.json).

### From the backoffice

`/admin` on the `app` process, enabled by `STIGMERGY_ADMIN_TOKEN_HASH`, one master identity.

| View | |
|---|---|
| Captures | paste, upload, public URL; provenance, extraction, retries, commit, change |
| Changes | plain-language summary, per-path diff, exact Git patch on demand |
| Contradictions | live list from current Markdown, resolution form |
| Entities | scoped claims and provenance, evidence-backed merge and delete |
| Gardener | run history and a manual trigger |
| Index health | repository HEAD vs indexed commit, dirty flag, last full rebuild |

### MCP tools

The cloud server and the local bridge expose the same surface:

| Tool | |
|---|---|
| `search_brain(query, filters?, max_results?)` | hybrid lexical + vector search |
| `read_page(path)` | one visible page with links and citations |
| `ask(question)` | a cited, verified answer — or an honest refusal |
| `list_entities()` | identities with a name you may see |
| `describe_entity(entity)` | knowledge composed from visible pages |
| `brain_submit(text \| path \| url, title?, occurred_at?, audience?)` | capture one input |
| `brain_submissions(limit?, status?)` | capture progress |
| `brain_delete(paths, why)` | explicit deletion with reference sweep |

There is no `kind`. `path` and private Drive exist only in the bridge. An omitted `audience` uses
your configured default, never organization-wide by accident.

## Visibility and security

- Identities and groups live in the knowledge repository's `ops/identities.json`; a page's `acl`
  is `null` or a list of groups. `brain-admins` is unrestricted.
- One policy for reads and writes: `server.acl.visible`, `kernel.acl.flows_into`, and the write
  guard. A restricted capture gets a restricted companion page; open pages are never rewritten
  from narrower evidence.
- Unknown, hidden, and unauthorized pages, entities, and captures look identical from outside.
- Per-user bearer tokens from `stigmergy-issue-token`; the server keeps only SHA-256 hashes. The
  cloud never sees Google credentials; clients never see the object store.
- Captured content is data, never instructions. The answer verifier is pure code; adversarial
  tests keep it that way.
- Public fetching blocks private and metadata destinations and revalidates every redirect.
  Parsers detect types from bytes and enforce size, page, and decompression limits.
- Secrets, tokens, presigned URLs, bytes, and restricted titles never enter logs. CI runs `gitleaks`.

Report vulnerabilities privately: [`SECURITY.md`](./SECURITY.md).

## Models

One `OPENROUTER_API_KEY`, a closed allowlist in `kernel.llm`, no provider fallback, zero-data
retention. Direct Anthropic, OpenAI, or Gemini credentials are rejected.

| Purpose | Model |
|---|---|
| filing and semantic repair | `deepseek/deepseek-v4-flash` |
| cited answers | `z-ai/glm-5.2` |
| embeddings | `qwen/qwen3-embedding-8b`, 2560 dimensions |
| OCR | `qwen/qwen3-vl-8b-instruct` |

## Quality and tests

The keyless suite — 1,100+ tests over real Postgres and Git, fake models, 75% coverage gate — is
the contract. Optional real-model evaluations run over a frozen corpus and append to
[`evals/history.ndjson`](./evals/history.ndjson). Latest run (2026-08-24):

| Measure | Result | Bar |
|---|:---:|:---:|
| Retrieval Recall@5 (15 questions, 9 ACL-filtered) | **1.00** | ≥ 0.80 |
| Answer honesty | **1.00** | ≥ 0.90 |
| Answer groundedness | **1.00** | ≥ 0.84 |
| False-premise refutation | **1.00** | — |

```bash
make retrieval-golden EMBEDDER=openrouter
make qa-golden EMBEDDER=openrouter LLM=openrouter
make gates
```

## Quick start

Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Docker.

```bash
git clone https://github.com/sturlese/stigmergy.git && cd stigmergy
make venv
make db-up     # Postgres + pgvector, MinIO
make test
make lint
```

Index a knowledge repository and serve it over stdio:

```bash
export STIGMERGY_INDEX_DSN=postgresql://stigmergy:stigmergy@localhost:54321/stigmergy
stigmergy-index --rebuild --repo ../stigmergy-brain --embedder fake
stigmergy-server --transport stdio --repo ../stigmergy-brain \
  --identity you@example.com --embedder fake
```

## Deployment

One image, three Fly process groups:

| Process | Command | Role |
|---|---|---|
| `app` | `stigmergy-server --transport http` | MCP over HTTP, uploads, index webhook, backoffice |
| `worker` | `stigmergy-librarian-boot` | the only writer and the scheduled gardener |
| `slack` | `stigmergy-slack` | Socket Mode adapter, one active instance |

```bash
make deploy-staging
make rebuild-staging
```

| Area | Variables |
|---|---|
| Models | `OPENROUTER_API_KEY`, `STIGMERGY_LIBRARIAN_MODEL`, `ANSWER_MODEL`, `STIGMERGY_OCR_MODEL` |
| Database | `STIGMERGY_INDEX_DSN` |
| Evidence store | `STIGMERGY_EVIDENCE_ENDPOINT`, `_BUCKET`, `_ACCESS_KEY_ID`, `_SECRET_ACCESS_KEY` |
| Server | `STIGMERGY_PUBLIC_HOST`, `STIGMERGY_TOKEN_STORE` or `STIGMERGY_TOKEN_STORE_FILE` |
| Backoffice | `STIGMERGY_ADMIN_TOKEN_HASH`, `STIGMERGY_ADMIN_ACTOR` |
| Writer | `STIGMERGY_REPO`, `STIGMERGY_LIBRARIAN_REPO_URL`, `STIGMERGY_LIBRARIAN_APP_ID`, `_INSTALLATION_ID`, `_PRIVATE_KEY`, `STIGMERGY_LIBRARIAN_GARDEN_AT` |
| Index webhook | `STIGMERGY_GITHUB_WEBHOOK_SECRET`, `STIGMERGY_GITHUB_REPO`, `STIGMERGY_GITHUB_BRANCH` |
| Slack | `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_TEAM_ID` |

Your team's knowledge is a separate private repository:

```text
your-brain/
├── sources/YYYY/MM/<capture-id>.md
├── wiki/
│   ├── notes/
│   ├── concepts/
│   └── entities/ent_<uuid>.md
├── ops/
│   ├── identities.json            people, groups, default audience
│   ├── slack-channels.json        channel id → audience
│   └── entity-registry.json       derived, written by the platform
├── .claude/skills/librarian/SKILL.md
└── .github/workflows/             nightly index rebuild
```

Only the writer's GitHub App identity commits to `wiki/`, `sources/`, and the registry. Runbook:
[`docs/OPERATIONS.md`](./docs/OPERATIONS.md); reset: [`docs/RESET.md`](./docs/RESET.md).

## Repository layout

| Package | |
|---|---|
| `kernel` | ACL flow, deadlines, the model boundary, normalization |
| `capture` | envelopes, evidence, uploads, extraction and OCR, queue, sources |
| `bridge` | the local stdio MCP client |
| `knowledge` | page contracts, `FilingPlan`, writer, linter, repair, contradictions, write guard |
| `entities` | opaque identities, claims, registry, merge, rename, delete |
| `changes` | exact patches and the change ledger |
| `index` | corpus selection, ranking, incremental updates, full rebuild, health |
| `server` · `answer` | MCP tools, HTTP transport, webhook, verified answers |
| `slack` | Socket Mode adapter |
| `admin` | the master backoffice |
| `librarian` | the writer process, bootstrap, Git and GitHub App transport, schedule |
| `ops` | the guarded non-production reset |

## Design principles

1. Git and Markdown are current knowledge; Postgres is a rebuildable index.
2. Every adapter produces the same kind-free `CaptureEnvelope`.
3. Original bytes and source pages are immutable except through explicit deletion.
4. One serialized writer: one commit and one change record per operation.
5. Visibility is a write constraint.
6. The librarian owns notes and concepts; entity pages hold identity only.
7. No write waits for a human. Uncertainty is represented honestly.
8. A health finding is preventable or autonomously repairable, or it is not a finding.
9. Every capability is reachable through Slack, MCP, or the backoffice.

Rationale and acceptance criteria: [`specs/karpathy-team-wiki.md`](./specs/karpathy-team-wiki.md).

## Documentation

- [Specification](./specs/karpathy-team-wiki.md)
- [Architecture](./docs/ARCHITECTURE.md)
- [Operations](./docs/OPERATIONS.md)
- [Clean reset](./docs/RESET.md)
- [Quality evaluations](./evals/README.md)
- [Changelog](./CHANGELOG.md) · [Contributing](./CONTRIBUTING.md) · [Security](./SECURITY.md)

Apache License 2.0.
