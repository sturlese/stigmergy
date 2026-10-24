---
title: "Stigmergy: a Karpathy-style team wiki"
status: implemented
---

# Stigmergy: a Karpathy-style team wiki

The product contract of the current system. Companion specifications hold the detail of one area
each: `specs/audience-layers.md` (layers), `specs/multi-tenancy.md` (workspaces),
`specs/retrieval-and-curation.md` (search, listing, aliases), `specs/backoffice-users-and-wiki.md`
(backoffice), and `specs/data-erasure.md` (erasure). Code and tests are authoritative.

## 1. Purpose

Stigmergy is the team version of the wiki in
[Karpathy's gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f): immutable
sources enter; one librarian files them into a Git-and-Markdown wiki, creating, rewriting,
consolidating, or removing pages without approval; deterministic gates protect the corpus; search
and answers read only what the caller may see. It adds only what a team forces: cloud operation,
identity, audience layers, concurrent submissions, durable processing, binary evidence, Slack
capture, hybrid search, an administrator backoffice, and isolated workspaces.

Each release replaces the contract it changes, with no migrations or dual formats. A workspace
adopts a contract that reshapes its pages by refiling its captures into a fresh workspace
(`python -m stigmergy.ops.replay_workspace`).

## 2. Roles

| Role | Who | May |
|---|---|---|
| Member | a person or agent in a workspace's roster | read `public` and its own layers; file in `public` and the layers it belongs to; follow its own submissions |
| Unrestricted identity | a member of `brain-admins` | read every layer and submission; delete pages and sources; target a contradiction with `resolution_of` |
| Administrator | a person with `is_admin`, who must be in `brain-admins`; never an agent | the backoffice's Operations and Access sections: captures and retries, changes and diffs, contradiction resolution, entity rename/merge/delete, deletion, gardener and recompile, index health, people, tokens, layers, Slack channels, layer erasure |
| Platform operator | a `platform_operators` row signed in with an operator token | the master console (`admin/console.py`): create, suspend, reactivate, rebuild, and delete workspaces; access requests; the shared writer. Inside a workspace it sees the screens and manages the roster but makes no knowledge write |
| Writer | the one serialized worker process | execute validated jobs; it is not a product identity |

Membership is the right to publish; reading every layer never grants publishing elsewhere. Slack
and the local bridge act as the member they authenticate. The roster lives in Postgres per
workspace (`server/roster.py`); `stigmergy-admin operator bootstrap` creates the first operator.

## 3. Repository and data

Each workspace has one private Git repository in the platform's GitHub organization, which the
writer creates with its scaffold once an operator creates the workspace. It holds Markdown only:

```text
sources/YYYY/MM/<capture-id>.md  readable evidence of one capture
wiki/notes/                      dated events, meetings, decisions, commitments, operating states
wiki/concepts/                   reusable ideas, methods, and mechanisms a source explains
wiki/entities/                   people, organizations, products, tools, repositories, places
wiki/projects/                   ongoing initiatives: goal, current state, decisions, open questions
layers/<layer>/...               the same layout per restricted layer; the root is `public`
README.md, CLAUDE.md, .gitignore scaffold for people and agents who open the repository
```

Git holds current knowledge. The private object store holds original bytes, readable extractions,
and exact patch caches, all content-addressed by SHA-256. Postgres holds operational state (queue,
upload sessions, job runs, roster, workspaces, operators), the append-only change ledger
(`knowledge_changes`), and the search index, which rebuilds from Git.

## 4. Product invariants

1. **Current knowledge lives in Git and Markdown.** Postgres is operational state and a rebuildable
   index, never a second wiki.
2. **Original evidence is immutable.** Exact bytes stay in the object store; a source page is the
   immutable readable form of one capture, and `sources/` changes only by explicit deletion or
   layer erasure.
3. **There is one capture semantics.** Every adapter produces the same `CaptureEnvelope` before
   queueing; origin never selects a filing brief or page taxonomy.
4. **There is one Git writer.** Captures, gardening, recompiles, entity operations, resolutions,
   and deletions share one serialization, one set of gates, and one commit path; each landed
   operation records one commit and one change record. Layer erasure is the one history rewrite;
   it remaps the ledger and leaves a receipt.
5. **The librarian owns `wiki/`.** People contribute material; nobody hand-edits model-owned pages.
6. **Visibility is a write constraint.** A capture files in one layer and everything derived from it
   stays there; restricted material never shapes content a broader audience sees.
7. **No active pipeline waits for a human.** Uncertainty is kept honestly, contradictions are made
   explicit, and technical failures retry or fail visibly.
8. **A corpus-health finding is actionable.** It is prevented at write time or repaired
   autonomously; anything else is analytics, never a human task.
9. **No hidden product surface.** Every capability is reachable through Slack, MCP, or the
   backoffice; installed commands are service, bootstrap, security, index, or bridge entry points.
10. **Nothing crosses workspaces.** Every tenant-owned row carries `tenant_id` under forced
    row-level security, and a writer job touches exactly one workspace.

### 4.1 Runtime model boundary

Every model call is built by `kernel/llm.py` from a closed allowlist and goes through OpenRouter
with the single `OPENROUTER_API_KEY`. No direct Anthropic, OpenAI, Gemini, or other provider
credential is read, forwarded, or used as a fallback. A new model requires updating the allowlist
and this section together.

| Purpose | Model | Settings |
|---|---|---|
| Librarian filing, correction, and garden repair; cited answers | `openrouter:deepseek/deepseek-v4.1-flash` | reasoning `medium` (excluded from output), `max_tokens` 65536, temperature 0; strict native JSON Schema output for plans |
| Scanned-page and image OCR | `openrouter:qwen/qwen3-vl-8b-instruct` | temperature 0 |
| Embeddings | `openai/text-embedding-3-large` | 2560 dimensions |
| Typed decisions: capture layer, aliases | `openrouter:typesafe/jev-1.13` | OpenRouter decisions API, `choose` only |

- **Provider policy:** supported parameters required, data collection denied, zero data retention
  required, same-model provider fallback allowed, model fallback prohibited. `baseten`, which
  declares structured output but returns invalid JSON, is ignored on librarian and answer requests.
- **Routing:** the librarian starts on the cheapest measured host, Decart fp4, then DeepInfra fp8.
  A capture's automatic retry (its second attempt, whatever ended the first) starts on DeepInfra
  fp8, where a runaway ends in a truncated plan the schema retry can recover. Answers start on
  DeepInfra fp8.
- **Requests:** a filing attempt makes at most three requests (`STIGMERGY_LIBRARIAN_MAX_TURNS` is
  fixed at 3): the plan, one schema retry for invalid structured output, and one correction when a
  writer contract fails; a garden repair makes at most two. Each planning or correction phase is
  bounded by `STIGMERGY_LIBRARIAN_TIMEOUT_S` (300 s by default, 900 s in the shipped `fly.toml`).
  A request unanswered after 600 s (`HEDGE_AFTER_S` in `knowledge/planner.py`) is raced by one
  identical request; the first success wins and the other is cancelled.
- **Budget and retries:** a job's first claim fixes one absolute deadline: 180 s of extraction plus
  the librarian timeout plus 120 s of gates. An explicit transient failure gets one automatic retry
  after ten seconds within the remaining budget. A filing request (plan or correction) that times
  out or ends at its output limit is transient and retries on a fresh budget, as an administrator's
  retry does; an extraction timeout is transient too. Deadline exhaustion and deterministic failures
  are terminal. A worker whose lease expired gets one recovery-only claim that reconciles a
  published commit and never calls the model.
- **Typed decisions:** one request of at most ten seconds, no retry, at most two at once per
  process, and only the documented `choice` answer is read; any failure yields the safe default.
  Aliases share a ten-second window per capture and a sixty-second window per garden run.

## 5. System

### 5.1 Capture adapters

| Adapter | Accepted input | Adapter-only responsibility |
|---|---|---|
| Local MCP bridge (Codex, Claude Code) | text, one local path, or one URL | read local bytes; local Google OAuth and Drive export; upload through a short-lived URL |
| Slack `brain` reaction | the reacted thread and supported attachments | authenticate the reacting user, map the channel to its layer, snapshot messages, download attachments |
| Backoffice (administrators) | pasted text, an uploaded file, or a public URL | accept browser uploads; fetch public URLs safely |

The cloud MCP server also takes text and public URLs directly. Every adapter calls the same capture
service. Deletion is not a capture: it is an explicit write with its own authorization and
rationale, executed by the same writer and gates.

### 5.2 Local MCP bridge

`stigmergy-bridge` is one local stdio MCP server that proxies the cloud's tools and acquires local
material for `brain_submit`:

- a `path` is read locally; a remote server never interprets a client filesystem path;
- a public URL is fetched locally with the cloud's fetch defenses (§5.4);
- a private Google Drive URL starts Google's local OAuth flow; the refresh token stays in the
  operating-system keychain and never reaches Stigmergy, and browser cookies are never read. Docs
  export as DOCX and Slides as PPTX, with Drive file ID and media types recorded as provenance;
- bytes upload through a single-object presigned URL valid for 300 seconds, and the server verifies
  size and SHA-256 before finalizing. Files are never base64-encoded into tool arguments.

```text
brain_submit(exactly one of: text | path | url,
             optional: title, occurred_at, audience, resolution_of) -> receipt
```

There is no `kind`. A conversational save submits what the user asked for as `text`, stored exactly:
a synthesis is the synthesis, a transcript is the transcript. Unsubmitted history is never acquired.

### 5.3 Capture envelope, layer, and queue

Acquisition produces this object (`capture/schema.py`) before queueing:

```text
capture_id: <uuid>
idempotency_key: <opaque client key>
actor: {subject, display_name}
audience: null | [<layer>]          # null is `public`; otherwise exactly one layer
origin: {adapter: mcp | slack | admin, captured_at, occurred_at?, title?, locator?,
         participants[], acquisition?: {original_url, final_url, acquired_at,
                                        drive_file_id?, drive_media_type?, export_media_type?}}
artifacts[1..20]: {blob_ref: sha256/<aa>/<bb>/<sha256>, sha256, bytes, media_type,
                   original_name?, source_url?}         # at most 50 MiB in total
intent: {resolution_of?: con_<uuid>, rationale?}       # rationale only, and always, with a target
```

A Slack thread and its attachments stay one capture; identical attachment bytes are one artifact.
The queue stores the envelope and object references, never a second copy of the content. Each
adapter supplies an idempotency key (the bridge per submission, Slack per thread and reacting user);
repeating an actor's key returns the original receipt, while equal content submitted again is a
distinct capture.

**Layer choice** (`server/routing.py`, `specs/audience-layers.md` §3.6). The receipt reports the
layer and how it was decided:

| Decision | Rule |
|---|---|
| `explicit` | the caller named `public` or a layer it belongs to |
| `channel` | the Slack channel's mapped layer, which the reacting user must belong to; an unmapped channel captures nothing |
| `classified` | no layer named and the caller belongs to two or more existing layers besides `public`: the typed-decision classifier reads the title, file names, locator, and leading text, weighs those layers by their descriptions plus a "no single layer" option, and its pick wins at probability 0.75 or more |
| `default` | otherwise, or on any classifier failure: the caller's default layer; a caller without one must name a layer |

Nothing falls back to `public` silently and the classifier never picks it. The backoffice never
classifies. The writer checks membership again at filing.

**Queue.** `capture_queue` holds `capture`, `delete`, `entity`, and `garden` jobs in the states
`queued`, `processing`, `landed`, and `failed`; no state waits for review. Workspaces take turns at
the claim, so one backlog never starves another. A terminal failure has a safe error category,
leaves no partial commit, keeps its artifacts, and an administrator may retry it. An upload session
precedes `queued` and becomes a capture only when every declared object is verified; unconsumed
sessions expire and are purged.

### 5.4 Evidence and extraction

Objects are private, content-addressed, and read only server-side after an authorization check;
deduplication never reveals whether another audience submitted the same bytes. Secrets, OAuth
tokens, presigned URLs, document bytes, and restricted titles stay out of logs and errors.
Extraction is the only media-specific stage, and its readable output is stored content-addressed
before filing.

| Input | Readable extraction | OCR |
|---|---|---|
| Plain text, Markdown | the exact submitted text | never |
| HTML | title and main readable content | never |
| PDF | digital text per page | a page with too little alphanumeric text or too many replacement characters |
| DOCX | paragraphs, headings, and tables in document order | never |
| PPTX | per slide: number, title, body, tables, speaker notes | never |
| PNG, JPEG | transcription | always |
| Google Doc, Google Slides | the DOCX or PPTX export | never |
| Slack thread | speaker-attributed, timestamped messages with permalinks, then attachments | per attachment |

Sheets, XLSX, audio, video, archives, and directories are not supported. Detection uses magic bytes
and parser validation, never the extension or `Content-Type` alone. Limits: 50 MiB per artifact and
per capture, 200 pages or slides, 50 million image pixels, 2 MiB of readable text and 180 s of
extraction per capture; a Slack capture takes at most 20 artifacts, 500 messages, and 60 seconds of
acquisition with eight concurrent profile lookups. Parsers run in a separate process under resource
limits; plain text runs none. Protected, corrupt, oversized, unsupported, or unsafe files fail with
a typed error.

Public URL fetching accepts HTTP and HTTPS, validates DNS and each of at most five redirects, blocks
loopback, private, link-local, and metadata destinations, sends no cookies or credentials, and
enforces size and time limits while streaming. Authenticated private URLs belong to the bridge.

### 5.5 Source rendering

Every landed capture produces exactly one source page at `sources/YYYY/MM/<capture-id>.md` under its
layer's root; the path never encodes origin or kind. The renderer is deterministic: frontmatter
holds provenance, layer, artifact hashes and media types, extractor versions, OCR pages, and any
`resolution_of` with its rationale; the body holds each artifact's complete readable content,
unsummarized. Content OCR produced is headed as an OCR transcription: an image's whole content, or
each transcribed PDF page. Filing never changes an original object or an existing source page;
better extraction is a new capture. Deletion removes a source page and releases its artifacts, and
an object is collected only when no live capture references it.

### 5.6 Librarian and page model

Source rendering and filing run in one writer operation over an ephemeral worktree; every gate
passes before the branch advances, so a landed capture is one commit. The single librarian skill
ships in the platform package at `src/stigmergy/knowledge/librarian_skill.md`.

The librarian sees the capture's layer view (§5.7): the readable source with its provenance, a
catalog of every linkable page (title, role, aliases, one-line summary), in full the entity and
project pages the source names and the closest other pages with their filed contradiction markers,
and bounded evidence from those pages' sources, all as untrusted data. The prompt runs from what
changes least to what changes most (the skill, the catalog oldest page first, the pages and evidence
chosen for the capture, its provenance and source) and ends with the task, so a provider reuses its
cached prefix across captures and a correction reuses its plan's prompt. It returns one `FilingPlan`
(`knowledge/plan.py`): at most 32 page mutations (`create`, `update`, `delete`) plus contradictions
to file or resolve. It may create, rewrite, consolidate, or delete notes and concepts; create or
update entity pages with their aliases, and project pages for initiatives whose work outlasts one
source; file a contradiction; or return an empty plan whose summary says why, which the writer asks
it to confirm once. It never writes `sources/`, invents a page role, broadens a layer, or waits for
approval.

Knowledge pages carry the maturity `seed`, `developing`, `mature`, or `evergreen`; age alone is
never a defect. Every factual paragraph cites its local source, written
``(Source: `sources/YYYY/MM/<capture-id>.md`)``, and a page cites only sources it declares. An
update keeps every existing cited line and contradiction marker; an update that would drop cited
lines is corrected or not applied.

**Links and names.** The wikilink is the only relation between pages. A link resolves by title stem
to a note, concept, entity, or project, and an entity also answers to its aliases. A link to a
missing, ambiguous, or audience-forbidden page becomes plain text and the capture report records
it. A creation whose name another page already answers to (an entity's normalized title or alias,
or the stem of a page in another folder) is not made; the rest of the plan lands and the report
says so. Backlinks are derived by the index, never stored.

**Correction.** When a plan breaks a writer contract or a gate, the librarian gets the same source,
context, and structured failures once and returns a complete replacement plan. If that also fails,
the source lands alone and the report records the rejection.

### 5.7 Visibility and write guard

`specs/audience-layers.md` is the detailed contract. `public` is the root layer; every other layer
is a roster group stored under `layers/<layer>/`. The librarian never sees a layer: for a capture in
layer `L` the writer builds a single-layer view from `public` and `L`, files the capture there with
unchanged gates, and projects the result into `L` only. A public page is never changed by another
layer's write; an update or delete aimed at a broader page is dropped and reported, and a
resolution that would change a public page's own units is refused. Gardening and recompilation run
one layer view at a time, public first. Readers see the union of their layers fused by virtual
path, each restricted unit marked with its layer. Normal identities cannot infer hidden sources,
pages, aliases, captures, diffs, or contradictions. One policy (`kernel/acl.py`,
`knowledge/write_guard.py`, `knowledge/layered.py`) serves filing, entity operations, gardening,
deletion, indexing, and reads.

### 5.8 Entity pages

An entity is an ordinary librarian-owned page at `wiki/entities/<Title>.md` whose frontmatter adds
`entity_type` (person, organization, product, tool, repository, or place) and `aliases`, other
names copied verbatim from evidence. Its body follows the skill's template (`What / Who`, `Current
state`, dated and cited `Facts`, `Connections`); nothing is composed from a registry. The title is
the identity within a layer: titles and aliases are unique among a layer's entity pages after
normalization, and the librarian disambiguates in the title, for example `Dana (Initech)`.
Similarity alone never merges anything. Pages with one title in several layers read as one page.

`describe_entity` resolves a path, title, or alias to a visible entity page and returns it with a
bounded set of visible pages linking it; unknown, hidden, and ambiguous references read alike.

Rename, merge, and delete are administrator operations through the same writer, each one commit:

- **Rename** moves the page in every layer that holds it, keeps the old title as an alias, and
  rewrites every `[[Old]]` link to `[[New|Old]]`.
- **Merge** folds pages of one layer into the first listed page: one `## Formerly <Title>` section
  per absorbed page, the union of aliases (absorbed titles included) and sources, absorbed files
  removed, links rewritten. It requires an exact assertion, from an existing immutable source, that
  equates every selected page by name; a rationale alone is never evidence. Pages of different
  entity types or audiences cannot merge.
- **Delete** removes the page and turns links to it into plain text, keeping the pages that
  mentioned it.

### 5.9 Contradictions

A real conflict between plausible sources is preserved, never settled by choice. The librarian
proposes it and the writer renders a canonical marker on a page no broader than any source it cites:

```text
<!-- stigmergy-contradiction-start:con_<uuid> -->
> [!WARNING] Unresolved contradiction `con_<uuid>`
> <one-line neutral explanation>
> - **Claim:** <text>
>   **Date:** `<date>`
>   **Source:** `<source path>`
<!-- stigmergy-contradiction-data:<base64url JSON> -->
<!-- stigmergy-contradiction-end:con_<uuid> -->
```

A marker holds two to ten claims, each citing a source in the filing context. A valid unresolved
marker is a healthy state; prose listing both claims is not a contradiction. A proposal the writer
cannot place safely is dropped and reported while the rest of the plan lands. Contradictions never
block capture or gardening, and no broader page or reader learns that hidden evidence exists.

The backoffice derives a deduplicated Contradictions view from the markers; it is not a task table.
An administrator may submit a resolution later (claim A, claim B, neither, or custom text; a
required rationale; an optional file or public URL). It becomes an ordinary capture with
`intent.resolution_of` that passes through the same pipeline under the `contradiction_resolution`
trigger. Over MCP only an unrestricted identity may name a marker, and the server records a fixed
rationale. A resolution may change only its marker, only when the new source supports it; an
unauthorized request lands its source with no plan effect.

### 5.10 Linter, repair, gardener, and recompile

- **Linter** (`knowledge/lint.py`): a pure detector over a tree. It checks the page contract, ids,
  entity name collisions, links and sources that would leak a narrower audience, missing sources,
  contradiction markers, and, on pages a write changed, the title heading, real prose, and local
  source citations. A write may introduce no violation.
- **Repair:** bounded writer primitives: link sweeps after deletion, unresolved-link degradation,
  the filing correction, and a garden repair plan of replacement bodies for violated pages only.
- **Gardener:** a `garden` job the writer queues per active workspace daily at
  `STIGMERGY_LIBRARIAN_GARDEN_AT` (05:07 UTC by default; `off` disables it) and an administrator
  may trigger. Per layer view it detects, asks for one bounded repair plan, applies, and gates; on a
  clean corpus it then records as aliases the other names pages already link entities by. A clean
  candidate lands one commit under the `garden` trigger, a failed one advances nothing, and a run
  with nothing to change lands nothing. Findings are details of a run in `job_runs`.
- **Recompile:** an administrator's `garden` job in `recompile` mode that rebuilds every derived
  page and link from the immutable sources, `public` first and each layer on top. Each prior derived
  page is recreated, updated, or explicitly tombstoned; any failure abandons the whole candidate. It
  lands one `garden` commit and records `operation: recompile` in its run report.

Scheduling and upload cleanup continue while the queue is busy.

### 5.11 Change ledger and diffs

Every landed Git mutation writes one append-only `knowledge_changes` row (`changes/model.py`): a
trigger (`capture`, `garden`, `delete`, `contradiction_resolution`, or `entity`), the actor, the
capture or job run, parent and unique commit SHAs, a summary, and a per-path manifest of action
(`created`, `updated`, `deleted`), page role (`note`, `concept`, `entity`, `project`, `source`, or
`ops` for any other path), reason, before and after hashes, line counts, and contradictions added
or resolved, plus a reference to the exact patch with its hash and size. The exact patch is
stored gzip-compressed in the object store; a missing cache object is rebuilt from Git. Each commit
names its operation id, so a crash after publishing reconciles to that commit instead of a second.
The backoffice Changes view links every operation to its record and shows a short explanation,
counts, one card per path with its reason, inline additions and deletions with unchanged regions
and large sources collapsed, and on demand the exact patch with its SHAs. Diffs are
administrator-only.

### 5.12 Search and index reconciliation

`specs/retrieval-and-curation.md` holds the retrieval contract. Search fuses full-text ranking,
embeddings of page fragments, the pages linking every entity the query names (up to four), and the
graph neighbors of the best eight hits, damped by visible degree. Hits carry a one-sentence summary
and visible outbound links; `list_pages`, not search, is exhaustive. A three-second query-embedding
timeout degrades that request to full-text and reports `vector_search: "timed_out"`; indexing never
writes a partial vector state.

Two paths keep each workspace's index converged with its repository:

- **Incremental:** an authenticated GitHub push webhook applies a bounded linear push atomically; an
  overflowing, non-linear, truncated, or concurrent push, or one arriving while the index is dirty,
  marks the index dirty instead.
- **Full rebuild:** the writer rebuilds each active workspace's index daily at
  `STIGMERGY_INDEX_REBUILD_AT` (04:17 UTC by default; `off` disables it), and at once for an index
  never built (a new workspace, or an operator's console request), retrying at most every ten
  minutes while that fails. It indexes repository HEAD within 50 MiB per page, 512 MiB in total,
  500,000 tree entries, and 250,000 indexable entries, and restores source pages for `read_page`.
  Success records the commit, row count, and time and clears the dirty flag; a failure is a failed
  job run that leaves the last success unchanged.

Knowledge repositories run no workflow. The backoffice warns when the index stays dirty beyond 15
minutes or the last full rebuild is older than 26 hours.

### 5.13 Surfaces

**MCP** (`server/mcp_server.py`; the bridge proxies the same tools):

| Tool | Purpose |
|---|---|
| `search_brain` | hybrid ranked search over visible pages |
| `read_page` | one visible wiki or source page, or one fragment of a long page |
| `list_pages` | exhaustive listing by type, entity type, layer, source period, or link; 50 per call with a cursor |
| `describe_entity` | one entity page and the visible pages that link it |
| `ask` | a cited answer from visible pages |
| `brain_submit` | one capture (§5.2) |
| `brain_submissions` | capture progress: members see their own, unrestricted identities all |
| `brain_delete` | queue an explicit deletion of pages or sources with a rationale; unrestricted identities only |

**Slack** (Socket Mode, authenticated by its app token; redelivered events are idempotent): a
`brain` reaction captures a thread; `@brain <question>` answers in the channel at the channel's
layers narrowed to the asker's, and follows up privately when the asker may see more.

**Backoffice** (`/admin`): people sign in with a personal token and get a session cookie
(`HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/admin`, eight hours idle, seven days absolute).
Every member reads a sanitized wiki and graph of their layers through the same scoped service as
MCP. Administrators also see Operations (overview, captures, changes, contradictions, entities,
gardener, index, activity) and Access (people, layers, Slack channels); those routes answer anyone
else like an unknown route. Operators see the master console (workspaces, access requests, writer).

**Installed commands** (`pyproject.toml`): `stigmergy-server` (MCP, webhook, backoffice),
`stigmergy-librarian` (the writer), `stigmergy-librarian-boot` and `stigmergy-librarian-credential`
(writer checkout and Git credential helper), `stigmergy-slack` (Socket Mode adapter),
`stigmergy-index` (full rebuild of one checkout), `stigmergy-admin` (operator bootstrap and mint,
workspace registration, a workspace's first administrator, break-glass mint, and the one-time import
of a legacy file roster), and `stigmergy-bridge`. Reset, erasure, and refiling live in
`stigmergy.ops`. No user capability is available only as a command.

## 6. Acceptance criteria

### Capture and evidence

- **CAP-01:** `brain_submit` has no `kind` and takes exactly one of `text`, `path`, or `url`; the
  cloud server rejects `path`, and the bridge never sends bytes as base64 in tool arguments.
- **CAP-02:** Slack, backoffice, and bridge fixtures normalize to one `CaptureEnvelope` and queue;
  jobs hold artifact references, never a copy of the content.
- **CAP-03:** Repeating an actor's idempotency key returns one submission and never two commits.
- **CAP-04:** Text input is retrievable byte for byte from its original object and its source page.
- **CAP-05:** Each supported format passes an end-to-end extraction fixture; a scanned PDF page is
  transcribed and headed as an OCR transcription while a digital page skips OCR; spoofed,
  oversized, corrupt, encrypted, unsafe, and unsupported inputs fail with typed errors and no
  commit.
- **CAP-06:** URL fetching blocks loopback, private, link-local, metadata, DNS-rebinding, and unsafe
  redirect targets and records distinct sanitized original and final URLs; private Drive
  acquisition keeps the Google token local.
- **CAP-07:** A Slack source keeps speaker, timestamp, order, permalink, and attachment boundaries.
- **CAP-08:** Every landed capture creates exactly one `sources/YYYY/MM/<capture-id>.md` under its
  layer's root, which only deletion or erasure changes afterwards.
- **CAP-09:** A capture files in one layer decided as §5.3 states, and its receipt says how.

### Filing and visibility

- **WRITE-01:** A capture may create, rewrite, consolidate, and delete wiki pages without approval;
  its source and wiki changes land in one commit only after every gate passes, and a gate failure
  advances no branch and records no change.
- **WRITE-02:** A plan that stays empty, or is rejected after its one correction, lands the source
  alone with a report saying so.
- **WRITE-03:** Nothing chooses a filing brief or page role by origin; only the note, concept,
  entity, project, and source roles exist, and a project is titled after its initiative, never the
  bare name of an organization or product.
- **WRITE-04:** A caller cannot affect a page it cannot read, and a restricted capture never changes
  a `public` page; an update aimed at a broader page is dropped and reported.
- **WRITE-05:** Deleting a page or source sweeps its references in the same commit and removes it
  from search.
- **WRITE-06:** A link to a missing, ambiguous, or forbidden page becomes plain text and is
  reported; a restricted page is never linked from a broader one.
- **WRITE-07:** `list_pages`, `describe_entity`, and search serve only visible pages, and no read
  surface lets a scoped reader tell what other layers hold.

### Entities

- **ENT-01:** Two entity pages of one layer never share a normalized title or alias; a colliding
  creation is dropped and reported, and similarity alone merges nothing.
- **ENT-02:** Rename keeps the old title as an alias and rewrites every link; merge refuses a
  rationale alone and verifies an exact assertion in an immutable source; delete turns links into
  plain text; each lands as one commit.

### Contradictions and corpus health

- **CON-01:** Two credible incompatible sources land with both claims, dates, and citations in one
  valid marker that the linter accepts, on a page whose readers may see every cited source.
- **CON-02:** The Contradictions view is derived from current Markdown and has no task table.
- **CON-03:** A resolution is an ordinary capture with `resolution_of` and a rationale; it changes
  only its marker, only when the new material supports it, and lands its own commit.
- **CON-04:** `brain_submit` honors `resolution_of` for unrestricted identities only; any other
  request lands its source with no plan effect.
- **GARDEN-01:** The linter is pure and deterministic; no findings store, repair queue, or repair
  worker exists, and no state waits for a person.
- **GARDEN-02:** A garden run lands at most one commit after every gate and records its counts; a
  failed candidate or a run with nothing to change lands nothing.

### Ledger, index, and surfaces

- **OPS-01:** Every landed operation has exactly one `knowledge_changes` row linked to its commit.
- **OPS-02:** A change renders a friendly per-path diff with large sources collapsed, and its exact
  patch is hash-verified and reconstructable from Git.
- **OPS-03:** Operations and Access routes serve administrators only and answer others like an
  unknown route; platform operators make no knowledge write.
- **OPS-04:** A deferred webhook push marks the index dirty; the writer's daily rebuild, or an
  immediate one for an index never built, clears it, and a failure leaves the last success intact.
- **OPS-05:** The backoffice warns after 15 minutes dirty or 26 hours without a full rebuild.
- **OPS-06:** Golden retrieval and graph tests cover ranking.
- **OPS-07:** The installed commands are exactly those of §5.13.
- **OPS-08:** The packaged librarian skill describes only operations the plan schema and writer
  implement, and contract tests bind its promises to code.

## 7. Decisions

| Decision | Rationale |
|---|---|
| One normalized capture, no kinds | The medium changes extraction, not what knowledge means or how it is filed. |
| Local bridge for private files and Drive | It uses the user's machine and OAuth without placing personal credentials in the cloud; bytes travel by presigned upload, not base64. |
| Original bytes in the object store, one readable source in Git | Binary fidelity and readable provenance are different jobs. |
| Autonomous wiki mutation | Approval would reintroduce a personal loop that a team service cannot wait on. |
| Explicit delete operation | Deletion expresses intent, not new evidence, yet shares the writer and gates. |
| Entity pages related only by wikilinks | One relation mechanism is one the model can write; a registry beside the graph duplicated it. The accepted loss is one identity known by different names to different audiences. |
| Audience layers with read-time fusion | Derived knowledge stays in its capture's layer, so blended text cannot leak; readers still see one page. |
| Contradiction marker; resolution as a new capture | Honest uncertainty beats fabricated certainty, and later evidence flows through the normal pipeline instead of pausing a run. |
| Linter and repair as libraries, gardener as one job | Detection, repair, and scheduling stay testable without a human task store. |
| Change ledger beside Git | A readable audit view for people while Git stays the canonical history. |
| Webhook plus a daily rebuild in the writer | The webhook gives freshness; the rebuild guarantees convergence after deferred events, and repositories stay Markdown only. |
| Cheapest host first for filing, fastest for answers | Filing runs in the background; an answer keeps a person waiting. |
