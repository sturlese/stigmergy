# Stigmergy

![Stigmergy — robot ants leaving glowing traces between documents that link into a network, feeding a cloud and an insights panel: AI agents that follow traces, knowledge that emerges](./docs/assets/stigmergy.png)

**A team's knowledge, captured where the work happens, filed by an agent, and answered with
citations you can check.**

Notes, meeting transcripts and documents arrive from Slack or a CLI. An agent turns each one into a
page in a plain git repository — but *code*, not the model, decides what is allowed to land. Reads
go through a single MCP server that answers questions with sources, and refuses when it cannot
support an answer. Everything it stores is a markdown file in a repo you own.

## One person's wiki, and a team's

The starting shape is Andrej Karpathy's **LLM wiki**: instead of re-deriving an answer from raw
sources on every question, let a model keep a markdown wiki current — reading each new source,
folding it into the pages it touches, and maintaining the cross-references. It works for a reason
worth stating plainly: a model does not get bored doing the bookkeeping, and that bookkeeping is
precisely the chore that makes people abandon wikis. Knowledge then compounds in one place rather
than being recomputed per query. *(Original write-up:
[karpathy/442a6bf5…](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — the idea
is restated here in full, because nothing this repository explains should depend on a link.)*

That model assumes one person, curating their own vault. Point it at a **team** and three things
stop being optional:

**A mistake is now somebody else's problem.** In a personal vault a bad edit costs you one undo. In
a shared one it quietly becomes what the company believes, and the person it misleads is not the
person who made it. That asymmetry is why nothing here is left to the model's judgement — the next
section is the whole answer.

**The vocabulary has to be agreed, not coined.** A solo wiki can let the model invent a page for
every name it meets. With several writers that yields three pages for one customer under three
spellings, and every link pointing at the wrong one. Here an entity is **born through a human**: the
agent proposes, a steward approves in Slack, and only then does a governed writer mint the page and
the registry entry. A capture naming something unknown parks and asks — once — rather than guessing.

**Not everyone may read everything.** A team wiki holds salaries, board material and a customer's
confidential figures. Visibility is enforced at one point (`acl.visible()`), on every read surface,
by an architecture test that refuses to let a new reader skip it — and an unknown page and a
forbidden page return the same string, because *which* one it was is itself a leak.

## Why it is built this way

Three commitments, and most of the design falls out of them:

**Knowledge lives in git, as markdown, in a repository you control.** This platform stores no pages.
It reads and writes a *separate* repository — the knowledge repo — so the substrate outlives the
software. Delete this platform and you still have your knowledge, in files, with history.

**A model writes; code decides.** An agent drafts the page, but eight deterministic gates run over
the resulting diff — zone, binary-page, body-rewrite, secrets, PII, frontmatter, contract,
anchoring — and the diff those gates approved is provably the diff that lands. A model can be
argued with. A gate cannot.

**An honest refusal beats a confident guess.** Answers are verified against what the tools actually
returned *this run*: any figure that cannot be traced back is withheld and the answer becomes a
refusal that says so. A system that never refuses is the failure, not the success.

There is a fourth, quieter one: **untrusted content is never read as instructions.** Page bodies
reach a model inside one hardened fence, built in one place, with in-band neutralization — so
captured material carrying the closing delimiter cannot end the fence early and have the rest read
as commands. Fields that travel as structure rather than as content are neutralized at the service
boundary instead; `SECURITY.md` states exactly which, and where that is not yet true.

## Architecture

Three pictures, under one convention that is the argument of the system rather than decoration:
**colour is who decides.** Purple is a model — it drafts, gathers and proposes, and is never the last
word on anything. Grey is code, which decides. Amber is a human, for the cases code should not decide
alone. Green is git, the one thing here that is not rebuildable. Each diagram carries the key for
the colours it uses — the read path has no green, because it touches no git.

### The shape

<p align="center">
  <img src="docs/assets/architecture.svg" alt="the shape: Slack, operator CLIs and MCP clients all submit into one durable capture queue (raw bytes to an evidence store); the librarian is the ONE writer and commits to the knowledge repo (git, markdown, yours — the only thing not rebuildable); the repo rebuilds pages_index in Postgres+pgvector, which the MCP server — the only API, filtering through acl.visible() — serves back to the same people — alongside it, on the same process group but behind its own token and its own ASGI branch, the /admin operations console, which never reads pages" width="100%">
</p>

**Read that diagram by asking what survives deleting this software.** The knowledge repo does: it
is markdown in git, with history, and it is yours. Postgres is a cache — `stigmergy-index --rebuild`
reconstructs every row of it from the repo, which is why the local one can be wiped between test
runs without anybody flinching. The object store keeps the raw bytes a page was derived from, so a
claim can always be walked back to what actually arrived.

Two narrow seams do all the work: **one writer** into git, and **one API** out of it.

The dashed box on the right is the part people are usually surprised by: **there is a web console**, at `/admin`, for the operations you would otherwise do from a terminal — draining a parked capture, running or disabling the four crons, reading the gardener's findings and approving the repairs proposed for them, previewing the digest, watching the worker. It rides the same process group as MCP but is an ASGI branch in *front* of the bearer middleware, so it never borrows MCP's auth: it has its own token, it is a **404 until that token's hash is configured**, and an architecture test keeps it from ever becoming a reader of pages. Full tour: [`docs/reference/admin-console.md`](./docs/reference/admin-console.md).

### The write path

<p align="center">
  <img src="docs/assets/write-path.svg" alt="the write path: material enters the capture queue, the server attributes it, the agent drafts a page in a throwaway worktree; a name the registry does not know makes it ask ONCE and park until a steward answers, which puts the capture back in the queue; the draft is a diff, and 8 deterministic gates — zone, binary-page, body-rewrite, secrets, pii, frontmatter, contract, anchoring — either bounce it back with the reason or commit exactly the diff they approved" width="100%">
</p>

The purple box is the only place a model decides anything, and everything downstream of it is a
gate it cannot argue with. **The loop back through orange is the point of the whole design**: when
the agent meets a name the registry does not know, it does not invent a page — it asks once, parks,
and waits for a person. A queue whose parked count is permanently zero would mean nobody is
capturing anything.

`brain_submit` queues a capture, archives its raw material content-addressed (MinIO locally, any
S3-compatible store in production) and attributes it to the identity the **server** resolved — never
to anything the client sent. The librarian then claims one item at a time and runs the agent inside
a throwaway `git worktree`, with its operating procedure versioned as a skill in the knowledge repo.

Then code decides what leaves: **eight gates** over the resulting diff — zone · binary-page ·
body-rewrite · secrets · pii · frontmatter · contract · anchoring — and the diff the gates approved
is provably the diff that lands (`gitcmd.commit(gated_entries=…)`).

Nothing dead-ends. A librarian that cannot resolve an entity **asks** once (`brain_reply`), then
parks the item; a steward drains it with `stigmergy-queue requeue/resolve/reject` or from the review
inbox; and `stigmergy.entities` is the only writer of the entity registry, however the mint is driven. A meeting re-filed after a
park **reuses the parked distillation** instead of re-reading the transcript — a park must not cost
knowledge.

Two flows sit on top: the **meeting distiller** (a dropped transcript becomes a source page, a
meeting page and one decision page per decision, each anchored) and **views** (per-entity rollups
whose ACL is the intersection of their members'). A view is derived, so it can go stale whatever
wrote the page — the worker fixes that by CONVERGENCE rather than by a hook per door: whenever its
queue is idle and its interval has elapsed, it asks the corpus which views diverge and regenerates
those, bounded by a per-pass ceiling that says what it deferred.

### The read path

<p align="center">
  <img src="docs/assets/read-path.svg" alt="the read path: a question passes acl.visible() BEFORE anything is fetched (a forbidden page and a non-existent one answer identically), then hybrid full-text and vector retrieval, then the answering agent — under the caller's own identity in a DM, and under the CHANNEL's scope for a public mention, which is a grant and not a narrowing; a verifier then asks whether every figure and quote traces back to what the tools returned this run, yielding either a cited answer or an honest refusal" width="100%">
</p>

Same shape, mirrored: a model gathers, and code decides what ships. Access is checked *before*
retrieval rather than filtered out of the results afterwards, and the verifier runs *after* the
model has written — so a figure the tools never returned cannot reach you, however confidently it
was phrased.

`stigmergy-index --rebuild --repo <checkout>` builds the index; `stigmergy-search "<question>"` queries
it (ES/EN, every hit showing its ranking factors). `stigmergy-server --identity <name>` serves MCP
over stdio; `--transport http --port <p>` serves streamable HTTP with per-request bearer-token auth.

**`acl.visible()` is the one enforcement point.** Every read surface filters through it, and an
architecture test holds the line: any module reading `pages_index` either names an ACL predicate or
sits on a named exception list. Existence leaks count as leaks — an unknown page and a forbidden
page return the same string, deliberately.

`ask(question)` is the answer path: an evidence-gathering agent calls three read tools (`search`,
`read_page`, `describe_entity`) under the caller's identity and writes a cited answer; then a
deterministic verifier traces every figure and citation back to what the tools returned *this run*,
and a strict gate decides what ships. Any untraced figure is withheld and the answer becomes an
honest refusal.

The read path also walks the house: `pages_index` carries a resolved, GIN-indexed `links` column;
`read_page` returns `type`/`status`/`supersedes`/`superseded_by` plus `links`/`backlinks`
(ACL-scoped, capped with the truncation stated); `list_entities`/`describe_entity` serve the entity
vocabulary and a layered "everything anchored to X" view. Entity-first resolution lives at the
service layer, so every client gets it, not only `ask`. Full narrative:
[`docs/reference/navigation.md`](./docs/reference/navigation.md).

**Ten MCP tools**, and the list is pinned by a test: read — `search_brain`, `read_page`,
`list_entities`, `describe_entity`, `ask`; write — `brain_submit`, `brain_submissions`,
`brain_reply`; review — `review_queue`, `review_decide`.

### The layering, and why it is a test

`tests/test_architecture.py` parses every module's imports and fails with the offending file and
line number if a seam is crossed. A layering that lives only in a README is decoration; this one is
a test.

The load-bearing rule is the simplest one: **`stigmergy.kernel` imports nothing from this project**,
exactly like `stigmergy.text`. That is what makes them safe for every package to depend on — nobody
reaching for the ACL resolver inherits the librarian's git stack. The rest of the file is
per-package boundaries of the same shape: what each subsystem may import, every exception declared
by name with its reason, and pruning tests that fail when a declared exception stops being used.

## Requirements

- **Python 3.12+**
- **Docker** (the local stack: Postgres + pgvector, MinIO, a bare git remote)
- **`gitleaks` on `PATH`** (`brew install gitleaks`) — the secrets gate shells out to the real
  scanner, so the librarian worker *refuses to start* without it rather than filing unscanned.
- A **git repository for your knowledge** — see
  [the knowledge-repo contract](./docs/reference/knowledge-repo.md) for its layout and for the
  short list of files it has to carry before anything will index or file.
- API keys only when you want real models, and they are not interchangeable: `OPENAI_API_KEY` is
  the embedder and the `ask` model; the librarian files through
  `STIGMERGY_LIBRARIAN_BACKEND=pydantic` on any provider-prefixed pydantic-ai model
  (`anthropic:claude-sonnet-5` by default), authenticated with that provider's own key. The test
  suite is keyless by construction, and so is the walkthrough below.

## Quick start

```bash
git clone <this repo> && cd stigmergy
make venv        # bootstrap the virtualenv
make db-up       # postgres+pgvector + minio + a bare git remote, all on loopback
make test        # the whole suite (coverage gate 75%)
make lint        # ruff over src/ tests/ evals/ scripts/
```

### See it work before you own a knowledge repo

Three narrated walks drive the real stack — real Postgres, real git, real gates — with the offline
double standing in for the agent. **No API key, no knowledge repo, nothing to configure**: they
build their own throwaway one and tell you what each step proved.

```bash
make db-up
.venv/bin/python scripts/walk_meeting_distiller.py   # a transcript becomes a page SET, and a second one PARKS
.venv/bin/python scripts/walk_views.py               # the filing regenerates the entity's view, then the honest no-op
.venv/bin/python scripts/walk_navigation.py          # links/backlinks, and the existence rule shown live on two identities
```

Run the meeting-distiller one first: its second transcript names an entity the registry does not
know, so it asks once and parks — the loop through orange in the write-path diagram above,
happening for real, with the `brain_reply` a steward would answer printed at the end.

### Point it at your own knowledge repo

```bash
cp .env.example .env && set -a && . ./.env && set +a   # the CLIs read the ENVIRONMENT, never the file
.venv/bin/stigmergy-index --rebuild --repo "$STIGMERGY_REPO"           # add --embedder fake to stay keyless
.venv/bin/stigmergy-search "what did we decide about pricing?"
.venv/bin/stigmergy-server --identity you@example.com --repo "$STIGMERGY_REPO"   # MCP over stdio
```

Nothing in `src/` loads a dotenv file — `make` does, for its own targets, and that is the whole of
it. Copying `.env.example` without sourcing it leaves `$STIGMERGY_REPO` empty and the first command
refuses. To point a Claude Code or Desktop session at the server, the `.mcp.json` block is in
[`docs/reference/server.md`](./docs/reference/server.md#connect-claude-code--desktop-stdio).

`make help` lists every target. Four end-to-end proofs run the real thing in Docker — each **wipes
the local queue** and says so:

```bash
make e2e                       # index idempotency: build -> golden -> wipe -> rebuild -> identical hits
make e2e-write                 # submit -> archive -> claim -> kill a worker -> reclaim -> purge
make e2e-librarian             # N captures -> gates -> commits on a real bare git remote
make e2e-librarian-container   # the DEPLOYED image, same proof, SIGTERM/SIGKILL + redelivery
```

All of these run **offline** against deterministic fake model backends — the container proof
included, because the librarian's agent step runs against its offline double there. A target that
silently needed an API key would be a target CI cannot trust, so none of them do. Targets that use
real models or real cloud resources are opt-in, need the env file, and are documented in the
[operator runbook](./docs/reference/operator-runbook.md).

## What is here

One package, its tests under `tests/`. Every **package** row below has a code map (`index.md`)
beside it in the source; the two bare modules at the top are small enough to be their own map.

#### `src/stigmergy/`

| Module | What it is |
|---|---|
| `text.py` | the bottom of the stack: the hardened UNTRUSTED-DATA fence, sanitize, clamp, and the one parser for a capture's `<path>@<sha>` result ref |
| `review_kinds.py` | the review inbox's THREE kind constants (entity-proposal, parked-capture, repair-proposal) — dependency-free, so a Block Kit renderer can name them without importing the server |
| `kernel/` | a LIBRARY that imports nothing from this project: the model dispatch, the page contract's cap + scalar emitter, frontmatter parsing, the ACL resolver, the entity registry, and the document converters the Drive door runs — text extraction plus the vision OCR fallback |
| `index/` | the hybrid derived index: postgres+pgvector, reciprocal rank fusion, contract ranking |
| `server/` | the single MCP server — the ONLY API over the brain; HTTP transport with per-request bearer auth, audit log, rate limits, the capture surface, the incremental-index webhook, entity navigation and the review lane |
| `answer/` | the answering agent + deterministic verifier: powers the `ask` tool |
| `capture/` | the durable capture queue: submit, claim, the evidence plane, retention; the human loop's write surfaces; and the TWO operator drop CLIs — the meeting one, and the Drive door (fetch with the operator's own Google auth, original bytes to evidence, one `kind="drive"` row, no model) |
| `librarian/` | the filing engine: the worker, the agent, the eight gates, the commit; ask-back, the deployed worker, the meeting flow |
| `entities/` | governed entity birth: proposal → approve → registry regenerate — the ONE path-scoped writer of the knowledge repo's `ops/entity-registry.json` and `wiki/entities/` |
| `slack/` | the Slack transport: 🧠 capture, Q&A, the steward doorbell |
| `views/` | per-entity rollups: a deterministic skeleton + a bounded synthesis |
| `gardener/` | corpus health on demand: ten deterministic checks + three bounded model passes (an editorial sweep over changed-plus-sampled pages, every entity page judged for a body that says nothing, and every registered entity judged against the others for a second identity of the same thing), findings persisted and reported — fixes nothing, writes nothing, vetoes nothing |
| `repair/` | the governed repair loop: an agent PROPOSES a repair for a finding — an additive edit, a drafted body for an entity page whose own body says nothing about it, or a merge of two registry entries that are one entity (the agent picks which name survives; code computes every page that moves) — and a person proposes the fourth kind, removing a page and sweeping every reference to it out of the corpus. Code validates each at propose time and again at apply time through the same eight gates, a steward approves one at a time, and code applies exactly that as one App-authored commit |
| `digest/` | the week's activity in one Slack post |
| `admin/` | the ops console: `/admin` on the same app process group — queue drain, cron remote-control, gardener/digest/index panels, activity. INERT until its token hash is configured, and never a read surface over pages — though its Activity tab does show the QUESTIONS people asked, which is user content behind one shared credential |

#### Around it

| Path | What it is |
|---|---|
| `tests/` | the behavioural invariant + the architecture tests that make the seams rules |
| `evals/` | three real instruments, one per model surface — golden retrieval, golden QA and golden filing (the only one that WRITES) — each over a frozen fixture, plus the git-resident score series |
| `docs/` | `decisions/` (why) · `reference/` (what) |
| `scripts/` | the end-to-end harnesses, plus the three keyless `walk_*.py` narrations the quick start runs |
| `deploy/` | **tracked, not gitignored**: the empty `ops/` defaults a deploy bakes your real ones over, and in `workflows/` the four cron templates you copy into your *knowledge* repo — never into this one, because Actions logs on a public repo are world-readable and these jobs narrate the corpus out loud |
| `docker-compose.yml` · `Dockerfile` · `fly.toml` | the local test stack (postgres+pgvector, minio, a bare git remote), the one image all three process groups run, and the staging deployment that splits them |
| `.github/` | this repository's OWN CI only — one workflow, plus the issue and PR templates |
| `.claude/` | this repository's own agent skills: how to land a change here, and how to validate a deployment. Not to be confused with the knowledge repo's `.claude/`, which is where the librarian's operating procedure lives |

## What is deliberately not here

Scope discipline, not a roadmap. These are ruled out rather than pending:

- **A separate read site.** Navigation is served through `read_page`; there is no second surface to
  keep in sync and no second place for an ACL to be wrong.
- **A second knowledge repository.** One repo, permanently. Enclaves multiply the places a
  permission can be misconfigured.
- **PR ceremony over knowledge.** `status` is a maturity axis, not a court. If an organization ever
  needs signed company truth, that is a field over the substrate, not a lane through it.
- **Ingest-time figure verification.** Figures are verified at *answer* time, against what the tools
  returned, which is the only moment the claim is actually being made.
- **SQL over certified datasets.** This is a knowledge system, not a data warehouse.

## Documentation

| Question | Document |
|---|---|
| How do I contribute? | [`CONTRIBUTING.md`](./CONTRIBUTING.md) |
| What does each subsystem do? | [`docs/reference/`](./docs/reference) — one per package, plus `src/stigmergy/*/index.md` code maps |
| Why is it built this way? | [`docs/decisions/`](./docs/decisions) — the architecture decision records |
| How do I operate it? | [`docs/reference/operator-runbook.md`](./docs/reference/operator-runbook.md) |
| How do I operate it from a browser? | [`docs/reference/admin-console.md`](./docs/reference/admin-console.md) |
| What does my knowledge repo need to look like? | [`docs/reference/knowledge-repo.md`](./docs/reference/knowledge-repo.md) |
| What does a page look like? | [`docs/reference/page-contract.md`](./docs/reference/page-contract.md) |
| What changed, and what may still move? | [`CHANGELOG.md`](./CHANGELOG.md) — below `1.0.0` the contracts in `docs/reference/` may move between minor releases; *behaviour* does not move without a decision record |
| How do I report a vulnerability? | [`SECURITY.md`](./SECURITY.md) — which also states the threat model this is actually built against |
| What is expected of people here? | [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md) |

## License

[Apache-2.0](./LICENSE). Dependency licenses, and the one obligation that is not automatic, are in
[`THIRD-PARTY-LICENSES.md`](./THIRD-PARTY-LICENSES.md).
