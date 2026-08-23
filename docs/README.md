# docs — how the platform works and why

Three kinds of document, answering different questions:

| Where | Answers |
|---|---|
| [`DESIGN.md`](./DESIGN.md) | **what this system is** — the shape, the closed list of what multi-user forces, the page vocabulary and the rules. Read it before adding a subsystem, and before defending one from removal |
| [`reference/`](./reference) | **what** each stage does, in prose, for someone operating or reading it |
| `src/stigmergy/*/index.md` | **where** things live: module by module, one map per package |

**Read [`DESIGN.md`](./DESIGN.md) before changing what the system does.** It holds the choices
that cost something to reach — code vetoing an agent's diff instead of an LLM reviewing an LLM,
one enforcement point for access instead of per-surface checks, the capture as the approval.
Changing the code without reading it usually means rediscovering an option that was already
rejected. Why a particular guard is written the way it is lives in the comment or the test that
owns it, next to the code it constrains.

## Reference docs (what each stage does)

| Doc | Covers |
|---|---|
| [`reference/knowledge-repo.md`](./reference/knowledge-repo.md) | the repository this platform reads and writes: the three zones, the fast lane's three creatable types, the optional `ops/` files, and what the platform will never do to it. **Start here if you are setting one up** |
| [`reference/hybrid-index.md`](./reference/hybrid-index.md) | `stigmergy.index` — the hybrid derived index, the ranking batch, and the `stigmergy-index --check` substrate lint |
| [`reference/server.md`](./reference/server.md) | `stigmergy.server` — the single MCP server and its **eight** tools: stdio + streamable HTTP, auth, audit, rate limiting. It writes nothing to the knowledge repo: `brain_delete` authorizes and queues, and the librarian performs it |
| [`reference/answer.md`](./reference/answer.md) | `stigmergy.answer` — the answering agent + strict verifier; three agent tools |
| [`reference/capture.md`](./reference/capture.md) | `stigmergy.capture` — the durable capture queue, attribution, the evidence plane, retention, and the `register_*` hints that make a capture an entity's birth |
| [`reference/librarian.md`](./reference/librarian.md) | `stigmergy.librarian` — the filing engine: ONE pipe for every capture, the worker, the agent, the nine gates, the verbatim `sources/` archive, the commit, and the identities it writes into that same commit |
| [`reference/slack.md`](./reference/slack.md) | `stigmergy.slack` — the third transport: identity resolution, the `@brain`/DM ask, the 🧠 capture gesture and the push-channel poller. It asks nobody for a verdict: there is none to give |
| [`reference/navigation.md`](./reference/navigation.md) | `stigmergy.server` — the graph served (`read_page`'s `links`/`backlinks`), `list_entities`/`describe_entity`, entity-first resolution in the service layer so every client inherits it |
| [`reference/gardener.md`](./reference/gardener.md) | `stigmergy.gardener` — `stigmergy-gardener`'s **ten** deterministic corpus-health checks, findings persisted and reported over two severities (`info`/`warn`) and posted nowhere: the gardener holds no Slack credential, no model and no provider key, and notifies nobody. Its daily run is a pass on the worker's idle branch, and the document records why the model passes it used to run were retired |
| [`reference/repair.md`](./reference/repair.md) | `stigmergy.repair` — removing pages, the one write to the corpus a HUMAN decides: `brain_delete` (MCP for an unrestricted identity, and the console's Remove pages button) queues it, and the librarian worker performs it. Structure is code's — which paths this lane may delete, which pages refer to them, and their frontmatter with every entry that named one dropped — and prose is a model's: the bodies of the pages that stay, so a sentence that cited a removed page still reads, with every bound on what it wrote proved on the bytes. Nobody reads that prose first, so the diff is carried on the capture and kept in the `repairs` ledger after the capture is purged |
| [`reference/admin-console.md`](./reference/admin-console.md) | `stigmergy.admin` — the ops console at `/admin`, mounted on the SAME app process group rather than a fourth service: nine pages — a dashboard, the capture queue read-only, the registry browser and its Register form, the removal ledger and page removal, the night shift's last runs, gardener/index/worker panels and an activity view. It decides no identity: nothing is proposed to a person. One bearer credential, minted by `stigmergy-admin-token`, stored only as a hash; INERT until `STIGMERGY_ADMIN_TOKEN_HASH` is configured |
| [`reference/page-contract.md`](./reference/page-contract.md) | the FAST-LANE anchor rule: what `entity:` means, who writes it, how it is read |
| [`reference/brain-page-contract.md`](./reference/brain-page-contract.md) | the wider frontmatter dialect of pages already in the repo — a READ contract, since nothing writes it any more |
| [`reference/operator-runbook.md`](./reference/operator-runbook.md) | operator runbook: deploy, tokens, audit trail, object-store smoke check, the two databases, the librarian + its GitHub App, the Slack transport, removing pages and the night shift |

## Code maps (where things live)

Thirteen per-package maps live **beside the code**, one per package, in the standard
Purpose / Key entry points / Use these / Avoid / Data & contracts / Tests / Common tasks shape:

[`kernel`](../src/stigmergy/kernel/index.md) ·
[`index`](../src/stigmergy/index/index.md) ·
[`server`](../src/stigmergy/server/index.md) ·
[`answer`](../src/stigmergy/answer/index.md) ·
[`capture`](../src/stigmergy/capture/index.md) ·
[`librarian`](../src/stigmergy/librarian/index.md) ·
[`entities`](../src/stigmergy/entities/index.md) ·
[`slack`](../src/stigmergy/slack/index.md) ·
[`gardener`](../src/stigmergy/gardener/index.md) ·
[`repair`](../src/stigmergy/repair/index.md) ·
[`admin`](../src/stigmergy/admin/index.md) ·
plus [`evals/index.md`](../evals/index.md) for the measurement rig.

Two packages have a code map and **no** `reference/` page of their own, for the same reason: both
are libraries with no operator surface. `stigmergy.kernel` is what every package imports — the model
dispatch, the page contract's constants and emitter, the ACL resolver, and the entity registry.
`stigmergy.entities` is the rules of entity birth plus the registry generator, and it has no
command and no decision door: an entity is introduced by a capture, so its narrative is the
identity half of
[`reference/librarian.md`](./reference/librarian.md), the `register_*` hints in
[`reference/capture.md`](./reference/capture.md), and the registry shape in
[`reference/knowledge-repo.md`](./reference/knowledge-repo.md).
