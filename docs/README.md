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
| [`reference/librarian.md`](./reference/librarian.md) | `stigmergy.librarian` — the filing engine: the worker, the agent, the nine gates, the commit, and the identities it writes into that same commit |
| [`reference/meeting-distiller.md`](./reference/meeting-distiller.md) | the librarian's meeting flow: a transcript submitted at `brain_submit(kind="meeting", …)` becomes a page SET (source + meeting + N decisions), filed atomically with per-page anchoring |
| [`reference/views.md`](./reference/views.md) | `stigmergy.views` — a derived, per-entity rollup: a deterministic skeleton (timeline, backlinks) plus an agent-written synthesis, carrying NO `acl:` at all — a view is the open rollup, and both feeds are filtered to open rather than the page being labelled to match them. No CLI and nothing scheduled outside the deployment: the librarian worker's convergence sweep is the guarantee (its interval, and the first idle tick after it drained something) and the post-meeting hook is the latency optimisation |
| [`reference/slack.md`](./reference/slack.md) | `stigmergy.slack` — the third transport: identity resolution, the `@brain`/DM ask, the 🧠 capture gesture and the push-channel poller. It asks nobody for a verdict: there is none to give |
| [`reference/navigation.md`](./reference/navigation.md) | `stigmergy.server` — the graph served (`read_page`'s `links`/`backlinks`), `list_entities`/`describe_entity`, entity-first resolution in the service layer so every client inherits it |
| [`reference/gardener-digest.md`](./reference/gardener-digest.md) | `stigmergy.gardener` + `stigmergy.digest` — `stigmergy-gardener`'s **eleven** deterministic corpus-health checks plus a bounded model editorial sweep, findings persisted and reported over two severities (`info`/`warn`) and posted nowhere — the gardener holds no Slack credential; `stigmergy-digest`'s two-section Slack post, ACL-scoped at the destination channel, `--dry-run` byte-identical. The digest is a command, and stays one; the gardener's daily run is a pass on the worker's idle branch |
| [`reference/repair.md`](./reference/repair.md) | `stigmergy.repair` — the repair loop, unattended: the librarian worker turns the six answerable gardener findings into additive edits, drafted entity bodies and entity merges, validates each against a real checkout, and applies it through the librarian's own nine gates as one App-authored commit — nobody approves anything. What stands where the approval stood is the ledger's permanent `content_key` memory, two ceilings per pass and the gates; what replaces the reading is the stored diff, because nobody saw it first. `brain_delete` (MCP for an unrestricted identity, and the console's Remove pages button) is the one repair a PERSON decides — queued there and performed by the same worker, with the pages that referred to the removed ones rewritten by a model and the diff carried on the capture rather than in the response |
| [`reference/admin-console.md`](./reference/admin-console.md) | `stigmergy.admin` — the ops console at `/admin`, mounted on the SAME app process group rather than a fourth service: ten pages — a dashboard, the capture queue read-only, the registry browser and its Register form, the repair ledger and page removal, the night shift's last runs, gardener/index/worker/digest panels and an activity view. It decides no identity: nothing is proposed to a person. One bearer credential, minted by `stigmergy-admin-token`, stored only as a hash; INERT until `STIGMERGY_ADMIN_TOKEN_HASH` is configured |
| [`reference/page-contract.md`](./reference/page-contract.md) | the FAST-LANE anchor rule: what `entity:` means, who writes it, how it is read |
| [`reference/brain-page-contract.md`](./reference/brain-page-contract.md) | the wider frontmatter dialect of pages already in the repo — a READ contract, since nothing writes it any more |
| [`reference/operator-runbook.md`](./reference/operator-runbook.md) | operator runbook: deploy, tokens, audit trail, object-store smoke check, the two databases, the librarian + its GitHub App, the Slack transport, removing pages, the night shift and the digest command |

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
[`views`](../src/stigmergy/views/index.md) ·
[`slack`](../src/stigmergy/slack/index.md) ·
[`gardener`](../src/stigmergy/gardener/index.md) ·
[`repair`](../src/stigmergy/repair/index.md) ·
[`digest`](../src/stigmergy/digest/index.md) ·
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
