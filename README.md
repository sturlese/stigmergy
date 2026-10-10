<p align="center">
  <img src="docs/assets/stigmergy.png" alt="Stigmergy" width="100%">
</p>

<p align="center">
  <a href="https://github.com/sturlese/stigmergy/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/sturlese/stigmergy/actions/workflows/ci.yml/badge.svg"></a>
  <a href="./LICENSE"><img alt="License: AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0-3b82f6.svg"></a>
  <a href="./pyproject.toml"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB.svg"></a>
  <a href="#from-claude-code-or-codex"><img alt="MCP server and local bridge" src="https://img.shields.io/badge/MCP-server%20%2B%20local%20bridge-a78bfa.svg"></a>
  <a href="./specs/karpathy-team-wiki.md"><img alt="Specification" src="https://img.shields.io/badge/spec-karpathy--team--wiki-f6b73c.svg"></a>
</p>

**Stigmergy** is the team version of the wiki in
[Karpathy's gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). Immutable
source material enters one queue, one librarian agent keeps a small Git-and-Markdown wiki current,
and every search and answer is scoped to what the caller may see.

Ants coordinate by leaving traces in the environment, not by talking to each other. Here every
capture is a trace — a person reacting in Slack, an agent finishing a task in Claude Code — the
librarian follows the traces, and the wiki emerges. Nobody approves a queue.

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

A one-person wiki works because the loop is tiny. Once several people and agents write to the
same wiki, you need what a shared deployment forces on you: identity, visibility, concurrency,
binary evidence, Slack, audit. Stigmergy adds exactly that and nothing that duplicates the loop.

| | |
|---|---|
| **Capture where the work happens** | Agents through the MCP bridge, people through a `:brain:` reaction in Slack or the backoffice. |
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
   librarian for a `FilingPlan`, and advances the branch only when every gate passes. A source may
   produce zero or more knowledge mutations: the graph unit is a reusable idea, not the document
   that happened to contain it.
4. **Remember.** The knowledge repository is plain Git and Markdown. Postgres is operational state
   and a rebuildable index, never a second wiki.
5. **Read.** Five MCP tools for agents, `@brain` for people, one visibility policy. A webhook
   indexes incrementally; a nightly full rebuild guarantees convergence.

## The write path

<p align="center">
  <img src="docs/assets/capture-lifecycle.svg" alt="The write path" width="100%">
</p>

States are `queued → processing → landed | failed`. Nothing waits for a human: ambiguity becomes
an explicit contradiction. Processing has one capture-wide budget anchored at its first claim; only
an explicit transient failure receives one automatic retry after ten seconds, within the persisted
deadline's remaining budget, except that a filing request that timed out or reasoned until its
output limit retries on a new budget. A terminal failure keeps its original evidence and typed
error for an administrator's retry. A crash after the commit receives a bounded recovery-only claim
and is reconciled by commit SHA, never by a second commit or another model run.

The librarian may create or rewrite a note, concept, entity, or project page, consolidate and
delete a redundant page, add or resolve a contradiction, or file nothing — the source still lands.
It never rewrites `sources/` and never broadens an ACL. Deletion is a separate explicit operation
(`brain_delete`) through the same writer and gates.

Each accepted knowledge mutation must be readable without reopening the source: it states the
supported conclusion, gives local source attribution, and explains every material page or entity
relationship in prose. A plan rejected by deterministic writer gates may receive one bounded
replacement-plan request using the same source, safe context, and exact violations; the replacement
supersedes the complete plan or nothing. If it also fails, the writer commits only the immutable
source and reports the plan as rejected; it never leaves a partial derived graph.

## The knowledge model

<p align="center">
  <img src="docs/assets/knowledge-model.svg" alt="The knowledge model" width="100%">
</p>

| Role | Location | Mutable by filing? | Meaning |
|---|---|:---:|---|
| Note | `wiki/notes/` | yes | contextual conclusion, decision, or event |
| Concept | `wiki/concepts/` | yes | durable explanatory knowledge |
| Entity | `wiki/entities/<Title>.md` | yes | a person, organization, product, tool, repository, or place, with aliases |
| Project | `wiki/projects/<Title>.md` | yes | an ongoing initiative: goal, dated current state, decisions, open questions, and its notes |
| Source | `sources/YYYY/MM/<capture-id>.md` | no | immutable evidence for one capture |

That layout is the `public` layer. Every other layer keeps the same layout under `layers/<layer>/`,
and a page's `acl` names its layer. Every knowledge page carries a maturity (`seed`, `developing`,
`mature`, `evergreen`) and its sources; an entity page adds its type and aliases. Pages relate only
through wikilinks:

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
sources:
- sources/2026/08/20000000-0000-4000-8000-000000000002.md
---

# Aurora renewal

[[Aurora Systems]] agreed to an annual renewal with a budget of EUR 120,000. The renewed term
starts on 15 September 2026.

## Entities
- [[Aurora Systems]] - the renewing customer
```

`wiki/entities/Aurora Systems.md` is an ordinary page with `entity_type: organization` and
`aliases`, written and linked by the librarian like any other. Titles and aliases are unique, so
the librarian disambiguates ambiguous names in the title. `describe_entity` returns the entity page
with the reader-visible pages that link it; the index derives backlinks. A link to a page that does
not exist becomes plain text and is reported, and a second page for a claimed name is never
created. Merging needs an exact assertion in a source — resemblance does nothing.

When credible sources disagree, the librarian keeps both claims in a strict marker on the narrowest
page whose readers may see both:

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
<!-- stigmergy-contradiction-data:<the same record as base64url JSON> -->
<!-- stigmergy-contradiction-end:con_3f1c2b9a-6d4e-4a2b-9c1d-2f7e8a9b0c1d -->
```

An administrator may later submit a resolution from the Contradictions screen; it is an ordinary
capture, and the marker goes away only when the new evidence actually resolves it. Direct and local
MCP submissions use the optional `resolution_of="con_<uuid>"` argument. The writer accepts it only
from a `brain-admins` member, only for that exact visible marker, and only at a safe audience; an
unauthorized attempt still archives its source but applies none of the librarian plan.

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
    "stigmergy-bridge": {
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
[mcp_servers.stigmergy-bridge]
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

The same tools and the same token rules apply whether a person is driving the session or an
agent runs on its own after a task. Private Drive needs
`STIGMERGY_GOOGLE_CLIENT_SECRETS=/absolute/path/google-oauth-client.json`.

The bridge is transport and local acquisition, not a second knowledge compiler. It forwards the
same `describe_entity` projection and filing contract as the cloud MCP server.

### From Slack

- **Ask:** `@brain what is the status of the Borealis rollout?` in a mapped channel. If you can
  see more than the channel, the extra follows up privately.
- **Capture:** react with `:brain:` on a thread. Speakers, timestamps, permalinks, and attachments
  become one capture in the channel's layer. Unmapped channels and reactors who are not members of
  that layer capture nothing.

Channels map to one layer each in the backoffice's Slack channels screen; the app manifest is
[`deploy/slack-app-manifest.json`](./deploy/slack-app-manifest.json).

### From the backoffice

`/admin` on the `app` process. People and agents sign in with their personal token and see the wiki
of their layers; administrators also see operations and access.

| View | |
|---|---|
| Wiki | every page you may read, rendered, with its layers, backlinks, and a local graph |
| Graph | every page you may read and every link between them, searchable, filterable by layer |
| Account | your layers, your default layer, and where you may publish |
| Overview | pipeline, writer, and index at a glance |
| People | create people and agents, set layers, default layer, and admin role, issue and revoke tokens |
| Layers · Slack channels | the layers, their members, and each channel's layer |
| Captures | paste, upload, public URL; provenance, extraction, retries, commit, change |
| Changes | plain-language summary, per-path diff, exact Git patch on demand |
| Contradictions | live list from current Markdown, resolution form |
| Entities | one entry per name with its layers; rename everywhere, merge and delete within a layer |
| Gardener | run history and a manual trigger |
| Index | repository HEAD vs indexed commit, dirty flag, last full rebuild, and graph measures per layer with their history and a constellation of the hubs |
| Activity | every administrative action |

### MCP tools

The cloud server and the local bridge expose the same surface:

| Tool | |
|---|---|
| `search_brain(query, filters?, max_results?)` | hybrid lexical + vector search: the most similar pages |
| `read_page(path)` | one visible page with links and citations; `path#n` reads the part a search hit names |
| `ask(question)` | a cited, verified answer — or an honest refusal |
| `list_pages(type?, entity_type?, layer?, since?, until?, links_to?, sort?, cursor?)` | every visible page of a kind, a period, or a connection, 50 per call |
| `describe_entity(entity)` | the entity page plus the visible pages that link it |
| `brain_submit(text \| path \| url, title?, occurred_at?, audience?, resolution_of?)` | capture one input; the optional contradiction ID is for `brain-admins` members only |
| `brain_submissions(limit?, status?)` | capture progress, with each capture's layer and how it was chosen |
| `brain_delete(paths, why)` | explicit deletion with reference sweep |

There is no `kind`. `path` and private Drive exist only in the bridge. `audience` names exactly one
layer (`["public"]` included). Omit it and Stigmergy chooses among your own layers from their
descriptions, or uses your default layer when no single layer clearly fits; it never chooses
`public`. The receipt says which layer, how it was chosen (`explicit`, `default`, `classified`),
and the classifier's probabilities when it ran.

## Visibility and security

- Every identity — whoever or whatever holds the token — has layers and a default layer in the
  Postgres roster. A capture files in one layer and everything derived from it stays there; a
  capture that names no layer is routed among the sender's own layers by a typed classifier that
  sees only that capture and never picks `public`;
  members publish in their layers, everyone reads and may publish to `public`. `brain-admins` reads
  every layer and may delete. Administering the backoffice is a separate role that only people hold.
- The librarian never sees a layer: it files into a view shaped like a single-layer wiki, and the
  writer stores the result in the capture's layer — for a public page, only what the layer adds.
  Readers see the union of their layers, with pages that share a name fused into one.
- Unknown, hidden, and unauthorized pages, entities, and captures look identical from outside.
- Personal tokens are issued in the backoffice and shown once; the server keeps only SHA-256
  hashes. The cloud never sees Google credentials; clients never see the object store.
- Captured content is data, never instructions. The answer verifier is pure code; adversarial
  tests keep it that way.
- Public fetching blocks private and metadata destinations and revalidates every redirect.
  Parsers detect types from bytes and enforce size, page, and decompression limits.
- Secrets, tokens, presigned URLs, bytes, and restricted titles never enter logs. CI runs `gitleaks`.

Report vulnerabilities privately: [`SECURITY.md`](./SECURITY.md).

## Models

One `OPENROUTER_API_KEY`, a closed allowlist in `kernel.llm`, no model fallback or direct
alternate-provider credentials, and zero-data retention. Direct Anthropic, OpenAI, or Gemini
credentials are rejected. The librarian uses strict provider-native JSON Schema on
zero-data-retention OpenRouter hosts in a fixed order: filing starts on the cheapest measured host,
a capture's second attempt and cited answers start on the faster one, and each falls back to the
same model on another host; a host that declares structured output but returns invalid JSON is
excluded by name. One coherent agent reads the source and safe graph context and
decides page boundaries, prose, links, entities, and contradictions together. A normal filing uses one
request; one bounded correction may repair a failed mechanical writer contract, within a ceiling of
three requests that also covers one schema retry. Both requests use the
same `deepseek/deepseek-v4.1-flash` route. Provider requests require supported parameters, deny data collection,
and require zero-data-retention processing. Librarian reasoning is `medium` and excluded from output.
Its output ceiling, reasoning included, is `65536` tokens and is sent to OpenRouter as
`max_tokens`. If the bounded correction
cannot satisfy writer gates, the draft is never applied.
Retryable failures use the existing bounded queue-attempt policy; answer and OCR requests retain
same-model provider failover.

`POST /admin/api/knowledge/recompile` is a separate administrator-only control: it rebuilds derived notes,
concepts, entities, and links from immutable sources in one isolated worktree. It
reports source count, page mutations, link health, request count, and final
commit; any failed source or gate abandons
the complete rebuild. A recompile is a `GardenRequest` in `recompile` mode and records its commit
under the existing `garden` ledger trigger, so the ledger contract remains one coherent mutation
history rather than adding a second change type.

Before release, the recorded real-model parity artifact must pass
`python evals/filing/parity.py --artifact <result.json>`; `make deploy-staging` refuses to deploy
without it. The artifact (schema v9) binds the candidate's commit and librarian-skill hash, replays
three complete runs of the candidate and as many of the deployed production configuration over the
same corpus and initial graph, and carries a blind review of both against a Hippocampus baseline.
The candidate passes when it has no more material regressions than production, so model variance is
measured rather than failing a zero bar.

| Purpose | Model |
|---|---|
| filing and bounded contract correction | `deepseek/deepseek-v4.1-flash` |
| cited answers | `deepseek/deepseek-v4.1-flash` |
| embeddings | `openai/text-embedding-3-large`, 2560 dimensions |
| OCR | `qwen/qwen3-vl-8b-instruct` |
| capture layer and filing-time alias decisions | `typesafe/jev-1.13` |

Query embeddings use one keep-alive client with a three-second connect and read timeout. A provider timeout
degrades that request to ACL-scoped lexical ranking and reports `vector_search: "timed_out"`;
indexing remains strict and never accepts an incomplete vector build. Serving connections — pooled
HTTP request connections and the Slack adapter — bound every Postgres statement at 30 seconds; the
writer bounds its own at 10 seconds, and its index rebuilds and erasures at 15 minutes.

## Quality and tests

The keyless suite — about 2,000 tests over real Postgres and Git, fake models, 75% coverage
gate — is the contract. Optional real-model evaluations run over a frozen corpus and append to
[`evals/history.ndjson`](./evals/history.ndjson). Latest run:

| Measure | Result | Bar |
|---|:---:|:---:|
| Retrieval Recall@5 (15 questions, 9 ACL-filtered) | **1.00** | ≥ 0.80 |
| Answer honesty | **1.00** | ≥ 0.90 |
| Answer groundedness | **1.00** | ≥ 0.84 |
| False-premise refutation | **1.00** | — |

```bash
make retrieval-golden EMBEDDER=openrouter
make qa-golden EMBEDDER=openrouter LLM=openrouter QA_ARGS="--live --max-usd 1"
```

## Quick start

Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Docker, and Node 24 (`make test` builds the
console).

```bash
git clone https://github.com/sturlese/stigmergy.git && cd stigmergy
make venv
make db-up     # Postgres + pgvector, MinIO
make test
make lint
make web       # build the console; make web-dev serves it with hot reload
```

Index a knowledge repository and serve it over stdio:

```bash
export STIGMERGY_INDEX_DSN=postgresql://stigmergy_app:stigmergy_app@localhost:54321/stigmergy
stigmergy-admin tenant create --slug demo --name Demo --repo you/your-brain --status active
export STIGMERGY_TENANT=demo
stigmergy-index --rebuild --repo ../your-brain --embedder fake
stigmergy-admin bootstrap you@example.com --name "Your Name"   # prints your first token, once
stigmergy-server --transport stdio --identity you@example.com --embedder fake
```

## Deployment

To run your own from scratch, follow [`docs/SELF-HOSTING.md`](./docs/SELF-HOSTING.md). One
deployment serves every workspace (tenant). Each workspace has its own people, layers,
tokens, index, queue, and private knowledge repository; row-level security keeps every query
inside one workspace. One image, three Fly process groups:

| Process | Command | Role |
|---|---|---|
| `app` | `stigmergy-server --transport http` | MCP over HTTP, uploads, index webhook, backoffice |
| `worker` | `stigmergy-librarian-boot` | the only writer for every workspace, gardener, and index rebuilds |
| `slack` | `stigmergy-slack` | Socket Mode adapter, one active instance; off unless `STIGMERGY_SLACK_MACHINES=1` |

```bash
make deploy-staging PARITY_ARTIFACT=<result.json>
```

The deploy refuses a dirty checkout or a candidate whose parity artifact does not pass; an operator
deploying a released version turns the gate off explicitly with
`STIGMERGY_PARITY_GATE=off make deploy-staging`. `STIGMERGY_LEGAL_SOURCE=<dir>` copies the
operator's legal pages (one Markdown file each) onto every machine and points `STIGMERGY_LEGAL_DIR`
at them.

On a fresh database, create the first platform operator once with
`fly ssh console -C "stigmergy-admin operator bootstrap you@example.com --name 'Your Name'"`, then
sign in at `/admin`. The master console creates workspaces: it registers the workspace and prints
the first administrator's token, and the writer creates its private repository in the GitHub
organization. The console also requests a workspace's index rebuild; the writer runs one daily.

| Area | Variables |
|---|---|
| Models | `OPENROUTER_API_KEY`, `STIGMERGY_LIBRARIAN_MODEL`, `ANSWER_MODEL`, `STIGMERGY_OCR_MODEL` |
| Database | `STIGMERGY_INDEX_DSN` |
| Evidence store | `STIGMERGY_EVIDENCE_ENDPOINT`, `_BUCKET`, `_ACCESS_KEY_ID`, `_SECRET_ACCESS_KEY` |
| Server | `STIGMERGY_PUBLIC_HOST`, `STIGMERGY_LANDING` (`on` serves the operator's own landing page, built into the image, at the bare host) |
| Workspaces | `STIGMERGY_GITHUB_ORG` (where workspace repositories live) |
| Writer on demand | `STIGMERGY_LIBRARIAN_IDLE_EXIT_S` (seconds idle before the writer stops), `STIGMERGY_FLY_API_TOKEN` (lets the app wake it) |
| Writer | `STIGMERGY_REPO` (checkout root), `STIGMERGY_LIBRARIAN_APP_ID`, `_INSTALLATION_ID`, `_PRIVATE_KEY`, `STIGMERGY_LIBRARIAN_GARDEN_AT`, `STIGMERGY_INDEX_REBUILD_AT` |
| Index webhook | `STIGMERGY_GITHUB_WEBHOOK_SECRET` |
| Slack | `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_TEAM_ID` (the workspace that owns the Slack app) |

Each workspace's knowledge is a separate private repository:

```text
your-brain/
├── sources/YYYY/MM/<capture-id>.md   the public layer
├── wiki/
│   ├── notes/
│   ├── concepts/
│   ├── entities/<Title>.md
│   └── projects/<Title>.md
└── layers/<layer>/                the same layout for each restricted layer
```

Only the writer's GitHub App identity commits to `wiki/`, `sources/`, and `layers/`. Runbook:
[`docs/OPERATIONS.md`](./docs/OPERATIONS.md); reset: [`docs/RESET.md`](./docs/RESET.md).

## Repository layout

| Package | |
|---|---|
| `kernel` | tenancy, ACL flow, layer paths and page fusion, deadlines, the model boundary, normalization |
| `capture` | envelopes, evidence, uploads, extraction and OCR, queue, sources |
| `bridge` | the local stdio MCP client |
| `knowledge` | page contracts, `FilingPlan`, writer, linter, repair, contradictions, write guard |
| `entities` | entity page merge, rename, and the name index |
| `changes` | exact patches and the change ledger |
| `index` | corpus selection, ranking, incremental updates, full rebuild, health |
| `server` · `answer` | MCP tools, HTTP transport, webhook, verified answers, workspaces, usage |
| `slack` | Socket Mode adapter |
| `admin` | the backoffice: sign-in, wiki, operations, people and tokens, the master console |
| `librarian` | the writer process, workspace checkouts and repositories, Git and GitHub App transport, schedule |
| `ops` | the guarded non-production reset, layer erasure, workspace deletion and refiling |

## Design principles

1. Git and Markdown are current knowledge; Postgres is a rebuildable index.
2. Every adapter produces the same kind-free `CaptureEnvelope`.
3. Original bytes and source pages are immutable except through explicit deletion.
4. One serialized writer: one commit and one change record per operation.
5. Visibility is a write constraint.
6. The librarian owns notes, concepts, entity, and project pages; each source can produce zero or
   more reusable, connected ideas, an entity page holds an identity and its dated facts, and a
   project page holds an ongoing initiative's goal, current state, and decisions.
7. No write waits for a human. Uncertainty is represented honestly.
8. A health finding is preventable or autonomously repairable, or it is not a finding.
9. Every capability is reachable through Slack, MCP, or the backoffice.

Rationale and acceptance criteria: [`specs/karpathy-team-wiki.md`](./specs/karpathy-team-wiki.md).

## Documentation

- [Specification](./specs/karpathy-team-wiki.md) · [Retrieval and curation](./specs/retrieval-and-curation.md)
- [Architecture](./docs/ARCHITECTURE.md)
- [Self-hosting](./docs/SELF-HOSTING.md) · [Operations](./docs/OPERATIONS.md)
- [Clean reset](./docs/RESET.md)
- [Data protection](./docs/DATA-PROTECTION.md) · [Erasure](./docs/ERASE.md)
- [Quality evaluations](./evals/README.md)
- [Changelog](./CHANGELOG.md) · [Contributing](./CONTRIBUTING.md) · [Security](./SECURITY.md)

## License

GNU Affero General Public License 3.0 ([`LICENSE`](./LICENSE)). If you run a modified Stigmergy for
other people over a network, the AGPL asks you to offer them its source. Contributions are made
under the contributor license agreement in [`CLA.md`](./CLA.md). Third-party licenses:
[`THIRD-PARTY-LICENSES.md`](./THIRD-PARTY-LICENSES.md).
