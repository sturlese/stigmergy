---
title: "Stigmergy: a Karpathy-style team wiki"
status: ready
date: 2026-08-23
repositories:
  - /Users/marc/dev/stigmergy
  - /Users/marc/dev/stigmergy-brain
reference_implementation: /Users/marc/dev/hippocampus
---

# Stigmergy: a Karpathy-style team wiki

## 1. Purpose

Stigmergy is the team version of the wiki described by [Karpathy's gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) and implemented for one person by Hippocampus.

The product remains deliberately small:

- immutable source material enters the system;
- one librarian files that material into a Git-and-Markdown wiki;
- the librarian may create, rewrite, consolidate, or remove wiki pages without approval;
- deterministic gates protect the corpus;
- search and answers read only what the caller may see.

Stigmergy adds only the complexity forced by a team deployment: cloud operation, identity, visibility, concurrent submissions, durable processing, binary evidence, Slack capture, hybrid search, and a master-operated backoffice.

This document is the implementation contract. It is based on the executable code inspected on 2026-08-23, not on the repository's `index.md` prose. Where current code and documentation differ, the code described in the baseline below is the current behavior and this specification is the target behavior.

## 2. Explicit clean-cut decision

Stigmergy has only operated with test data. The new design replaces the experiment; it does not migrate it.

- Existing Postgres data, queues, index rows, object-store test objects, and test knowledge-repository content are discarded at cutover.
- The application schema is replaced by a fresh baseline.
- Old MCP arguments, capture kinds, persisted payloads, entity formats, views, findings, and repair records must not be supported by compatibility code.
- There are no dual reads, dual writes, backfills, compatibility flags, deprecation windows, or rollback adapters.
- Platform code, the knowledge-repository contract, clients, tests, and documentation change together.
- If a pre-launch deployment fails, fix it and recreate the test environment. Do not preserve the abandoned contract to make rollback possible.

This decision concerns software and persisted-format compatibility. Ordinary knowledge edits remain reversible through Git history.

## 3. Problem in the current code

The current implementation already has valuable team infrastructure: a durable Postgres queue, a single Git writer, identity and page ACLs, R2/S3 evidence storage, a Slack reaction adapter, hybrid full-text/vector search, an MCP server, and a master backoffice.

Its knowledge model has drifted away from the simple source-to-wiki loop:

1. `raw`, `page`, `meeting`, and `document` are exposed as different capture kinds even though `raw` and `page` currently behave alike and every kind eventually reaches the same librarian.
2. The queue duplicates submitted text that is also stored as evidence and then rendered into a source page.
3. Slack reduces a thread to newline-joined text, losing durable speaker and timestamp structure.
4. The MCP contract accepts strings only and tells callers to extract document text themselves.
5. Entity identity is tied to a title slug, aliases have no complete visibility/provenance lifecycle, entity pages accumulate facts, and generated views/registry machinery overlap with `describe_entity`.
6. The gardener stores permanent human-oriented findings and fixes nothing. The repair package is now mostly a separate deletion ledger plus compatibility for retired repair kinds.
7. Contradictions have no first-class, non-blocking, resolvable representation.
8. Normal capture commits do not have one friendly, exact, unified audit view in the backoffice.
9. Incremental indexing explicitly relies on a nightly full rebuild, but the scheduled rebuild workflow was removed.
10. User-like capabilities remain installed as CLI commands even when MCP or the backoffice is the real surface.
11. The live and frozen librarian instructions promise writers and maintenance behavior that the code does not have.

The target removes these parallel concepts rather than adding another layer over them.

### 3.1 Deliberate differences from Karpathy/Hippocampus

The final product keeps only team-enabling differences with a clear payoff:

| Difference | Decision | Why |
|---|---|---|
| Identity, groups, and ACL-aware reads/writes | keep | multiple people and confidential team knowledge require them |
| Durable queue and one serialized Git writer | keep | concurrent submissions must not race or lose work |
| Private object store and extraction/OCR | keep | a team submits binary evidence that Git Markdown cannot preserve exactly |
| Hybrid lexical/vector ranking | keep | corpus-wide team retrieval benefits from ranking beyond file search |
| Slack capture, local MCP bridge, and master backoffice | keep | these are the team's real contribution and operating surfaces |
| Master-visible change ledger and friendly diffs | keep | autonomous writes need understandable audit without approval |
| Scoped entity identity registry | keep, simplify | cross-team entity resolution has value, but facts/dossiers do not belong in it |
| Explicit contradiction index and optional later resolution | keep | an asynchronous team service must preserve uncertainty without blocking |
| Authorship/write-boundary enforcement | keep, reset | model-owned zones need one trusted writer, without historical baseline exceptions |
| Input kinds, generated views, human gardener findings, repair workflows, CLI search/queue/gardener | remove | they duplicate the core source-to-wiki loop without adding team value |

## 4. Product invariants

The following rules are non-negotiable.

1. **Current knowledge lives in Git and Markdown.** Postgres is operational state and a derived index, not a second wiki.
2. **Original evidence is immutable.** Exact submitted bytes live content-addressed in the private object store. A `sources/` page is the immutable, readable representation of one capture.
3. **There is one capture semantics.** Text, paths, URLs, Slack threads, and admin uploads differ only while acquiring or extracting bytes. They become the same `CaptureEnvelope` before filing.
4. **There is one Git writer.** Captures, autonomous gardening, entity merges, contradiction resolutions, and explicit deletion share the same serialization, gates, commit mechanism, and change ledger.
5. **The librarian owns `wiki/`.** It can create, rewrite, merge, or delete wiki pages without asking for approval. Humans contribute new material; they do not hand-edit model-owned pages through a parallel workflow.
6. **`sources/` is append-only except for explicit deletion.** Ingestion and gardening never rewrite an existing source page.
7. **Visibility is a write constraint, not only a read filter.** Restricted material can never influence content visible to a broader audience.
8. **No active pipeline waits for a human.** Ambiguity is preserved honestly, contradictions are made explicit, and technical failures retry or fail visibly.
9. **A reported corpus defect is actionable.** A health rule must be prevented at write time, deterministically repairable, or repairable by a bounded model operation. Otherwise it is analytics, not a health finding.
10. **Git commits explain every mutation.** Every landed writer operation has one commit and one master-visible change record with an exact diff.
11. **No origin-specific knowledge behavior.** Slack, Drive, PDF, pasted text, and meetings do not select different filing briefs or page taxonomies.
12. **No hidden product surface.** Every user capability is reachable through Slack, the official local MCP bridge, or the backoffice. CLI-only commands are operational or development tooling, never a separate product.
13. **Code prose is local and durable.** Comments and docstrings explain only a non-obvious invariant, contract, or mechanism visible in the code they accompany. Change history, rejected designs, migration narration, and implementation justification belong in this specification, tests, and Git rather than production code.

## 5. Target system

### 5.1 One writer, three adapters

There are three capture adapters:

| Adapter | Accepted input | Adapter-only responsibility |
|---|---|---|
| Local MCP bridge for Codex and Claude Code | text, one local path, or one URL | Read local bytes; perform optional local Google OAuth and Drive export; upload bytes with a short-lived URL |
| Slack brain reaction | reacted thread and supported attachments | Authenticate Slack, resolve the reacting user and channel audience, snapshot messages, download attachments |
| Master backoffice | pasted text, uploaded file, or public URL | Authenticate the master; accept browser uploads; safely fetch public URLs |

All three call the same capture service after acquisition. There is no Slack librarian, meeting distiller, document door, page writer, or Drive-specific filing path.

Deletion is intentionally not a capture adapter. It is an explicit write operation with its own authorization and rationale, executed by the same writer and gates.

### 5.2 Official local MCP bridge

The only supported private/local acquisition client is Codex or Claude Code running on the user's machine.

- Stigmergy ships one local stdio MCP bridge. The user installs and configures that bridge in Codex or Claude Code; no browser extension or second helper is required.
- The bridge proxies the existing read tools to the cloud and exposes the new `brain_submit` contract locally.
- For `path`, the bridge reads the local file. A remote server never interprets a client filesystem path.
- For a normal public URL, the bridge downloads the bytes locally and records the original and final URLs.
- For a private Google Drive file, the first use starts Google's local browser OAuth flow. The refresh token is stored in the operating-system keychain and is never sent to Stigmergy.
- A normal browser session cookie is neither read nor copied. Merely pasting a private Drive URL cannot authenticate the cloud service.
- Google Docs are exported as DOCX and Google Slides as PPTX. The exported bytes are the immutable acquired representation, with the Drive URL, file ID, export format, and acquisition time recorded as provenance.
- The bridge requests a short-lived, single-object presigned upload from Stigmergy, uploads bytes directly to R2, verifies size and SHA-256, then finalizes the capture.
- R2 credentials never reach the client. Google credentials never reach the cloud.
- Large files are never base64-encoded into MCP JSON arguments.
- Temporary local files are avoided when streaming is possible and deleted after a verified upload when they are necessary.

The local tool contract is:

```text
brain_submit(
  exactly one of: text | path | url,
  optional: title,
  optional: occurred_at,
  optional: audience
) -> submission receipt
```

There is no `kind` argument. `title`, `occurred_at`, and provenance help filing but do not select a different workflow. An authenticated caller may choose only an audience it is allowed to publish to.

For a conversational save:

- “save the conclusions” sends a self-contained synthesis as `text`; that exact synthesis is the immutable input;
- “save this whole conversation” sends the complete transcript as `text`;
- a pasted meeting transcript is stored exactly as supplied, then distilled into ordinary wiki pages.

The bridge does not silently acquire conversation history the user did not ask to submit.

### 5.3 Normalized capture envelope

Acquisition produces this domain object before queueing:

```yaml
capture_id: <uuid>
idempotency_key: <opaque client key>
actor:
  subject: <authenticated stable id>
  display_name: <audit label>
audience: null | [<group id>, ...]   # null means organization-wide; [] is invalid
origin:
  adapter: mcp | slack | admin
  captured_at: <RFC3339 UTC>
  occurred_at: <optional RFC3339/date>
  title: <optional string>
  locator: <optional non-secret URL/permalink>
  participants: [<optional display-safe participant records>]
  acquisition: null |                 # present only for URL acquisition
    original_url: <sanitized submitted URL>
    final_url: <sanitized final/canonical URL>
    acquired_at: <RFC3339 UTC>
    drive_file_id: <optional Drive ID>
    drive_media_type: <optional Drive MIME type>
    export_media_type: <optional exported MIME type>
artifacts:
  - blob_ref: <private content-addressed object reference>
    sha256: <lowercase hex>
    bytes: <integer>
    media_type: <detected MIME type>
    original_name: <optional filename>
    source_url: <optional URL>
intent:
  resolution_of: <optional contradiction id>
```

`origin.adapter` is provenance, never a behavioral switch in the librarian. One capture can contain multiple artifacts so a Slack thread and its supported attachments remain one unit of evidence. Public MCP and admin submissions normally contain one artifact.

The durable queue stores this envelope and object references, not a second full copy of the submitted text or binary. Client retries with the same actor and idempotency key return the original receipt; equal content submitted at a different time remains a distinct capture with distinct provenance.

The durable state machine is:

```text
queued -> processing -> landed
                    \-> failed
```

An upload session exists before `queued` but is not a capture until all declared objects have been verified and the request is finalized. Processing uses bounded automatic retries for technical failures. There is no `awaiting_review`, `needs_human`, or equivalent state. A terminal failure has a safe error category and is visible to the master; it leaves no partial Git commit.

Slack channel visibility is mapped to a configured Stigmergy audience. An organization-wide channel may map to organization-wide; a private channel maps to a restricted group and can never be broadened by the reacting user. An unmapped channel fails safely before capture. For MCP, omission of `audience` uses the authenticated identity's configured default and never silently falls back to organization-wide. The master may choose any configured audience in the backoffice.

### 5.4 Evidence and extraction

The object store earns its place by retaining exact original bytes, especially binary evidence. Objects are private and content-addressed by SHA-256. Deduplication is an implementation detail and must not reveal whether another audience already submitted the same bytes.

Extraction is the only media-specific stage. Its output is immutable readable text plus extraction metadata, after which every capture follows the same renderer and librarian.

| Input | Immutable original | Readable extraction | OCR rule |
|---|---|---|---|
| Plain text / Markdown | exact UTF-8 bytes | exact submitted text | never |
| HTML / public web page | fetched response bytes | main readable content plus title and URL | never |
| PDF | exact PDF bytes | layout-aware digital text | OCR only for scanned pages or failed/poor digital extraction |
| DOCX | exact DOCX bytes | paragraphs, headings, tables, and link text in document order | never |
| PPTX | exact PPTX bytes | slide number, title, body, table text, and speaker notes | never |
| PNG / JPEG | exact image bytes | OCR text with page/image boundary | always |
| Google Doc | local DOCX export | DOCX extraction | never |
| Google Slides | local PPTX export | PPTX extraction | never |
| Slack thread | canonical selected-message snapshot; attachment bytes are additional artifacts | speaker-attributed, timestamped transcript followed by extracted attachments | according to each attachment |

Supported first-release inputs are text/Markdown, HTML, PDF, DOCX, PPTX, PNG, JPEG, Google Docs, and Google Slides. Sheets/XLSX, audio, video, archives, directories, and arbitrary cloud-provider connectors are excluded.

Default limits are configurable but ship as:

- 50 MiB per artifact;
- 200 pages or slides per artifact;
- 20 artifacts per Slack capture;
- 500 messages, eight concurrent profile lookups, and 60 seconds of acquisition work per Slack capture;
- safe decompression and image-pixel limits for container formats;
- bounded fetch, parser, and OCR timeouts.

Slack fetches one validated root permalink and derives message permalinks locally. Capture does not issue one Slack API request per message, and a deadline failure releases the idempotency reservation for retry.

Detection uses magic bytes and parser validation, not filename extension or the remote `Content-Type` alone. Password-protected, corrupt, oversized, unsupported, or unsafe files fail with a typed error; the system never asks a processing-time question.

Public URL fetching accepts HTTP and HTTPS, revalidates DNS and every redirect, blocks loopback/private/link-local/metadata destinations, sends no ambient cookies or credentials, enforces size/time limits while streaming, and records distinct sanitized original and final URLs plus acquisition time. Private authenticated URL acquisition belongs only to the local bridge.

For binary inputs, the extracted derivative is stored content-addressed in R2 before filing so retries and extraction audits are deterministic. For plain text, the original and readable derivative are the same object. The immutable Markdown source remains the human-readable evidence in Git.

### 5.5 Source rendering

Every finalized capture produces exactly one neutral source page:

```text
sources/YYYY/MM/<capture-id>.md
```

The path does not encode `slack`, `meeting`, `document`, `raw`, or any other content kind.

The source renderer is deterministic. It writes provenance, ACL, artifact hashes/media types, extraction version, and the complete readable extraction. It does not summarize or improve the submitted text. For Slack it preserves message order, speaker attribution, timestamps, message permalinks, and attachment boundaries.

The source page and original object differ deliberately:

- R2 preserves exact bytes;
- `sources/` preserves readable evidence for the wiki and citations;
- `wiki/` preserves the conclusions worth retrieving later.

Filing never modifies the R2 original or an existing source page. If improved extraction is needed, it is a new capture with new provenance. An explicit master deletion may remove a current source page and its logical artifact references; a shared content-addressed object is collected only when no live capture references it.

### 5.6 Librarian behavior and page model

The source renderer and librarian run inside one serialized writer operation. The candidate tree contains the new source page and the librarian's wiki changes; all gates pass before the branch/ref is advanced. One landed capture therefore creates one Git commit.

The librarian receives:

- the new readable source;
- its provenance and visibility;
- only existing wiki material the submitting identity is allowed to read;
- deterministic search candidates and entity-resolution results;
- the actual operations it is allowed to return.

The librarian returns a structured change set. It may:

- create a `note` or `concept`;
- rewrite an existing `note` or `concept`;
- consolidate knowledge and delete a redundant wiki page;
- create or update minimal entity identity claims through the entity service;
- add or resolve a well-formed contradiction marker;
- decide that the source adds no durable wiki conclusion and create no wiki page.

The librarian never rewrites `sources/`, emits a generated `view`, invents a meeting/document/page type, changes ACL to expose material more broadly, or waits for approval.

The target corpus has four roles:

| Role | Location | Mutable by normal filing? | Meaning |
|---|---|---:|---|
| Note | `wiki/notes/` | yes | contextual conclusion, decision, or event worth retrieving |
| Concept | `wiki/concepts/` | yes | durable explanatory knowledge |
| Entity identity | `wiki/entities/` | only through entity primitives | minimal stable identity and scoped names; never a dossier |
| Source | `sources/` | no | immutable readable evidence for one capture |

`page`, `meeting`, `document`, `view`, and input-specific page roles disappear. Facts and connections about an entity live in normal notes/concepts and are composed at read time by `describe_entity`.

Notes and concepts use the Hippocampus-compatible editorial maturity values `seed`, `developing`, `mature`, and `evergreen`. Entity and source pages do not have editorial maturity. Age alone is not corruption: an old seed is not a health failure unless the corpus supplies enough evidence to merge, develop, or delete it.

There is no generic `deprecated` entity or fact state. A known replacement can use the existing explicit `supersedes`/`superseded_by` page relation; a factual disagreement uses a contradiction; an inactive organization/person is described as a dated fact; a bad page is rewritten, consolidated, or explicitly deleted.

### 5.7 Visibility and write guard

The master backoffice identity can read all material. Normal identities cannot infer hidden sources, pages, entities, aliases, captures, diffs, or contradiction records.

For a page with audience `P` and an incoming capture with audience `C`, an update may incorporate the capture only when every reader of `P` is allowed to read `C`. Organization-wide is the broadest audience. A restricted capture therefore cannot update an organization-wide page.

When otherwise useful restricted material cannot safely update a broader page, the librarian creates or updates a restricted companion page. It does not silently narrow the existing page, because doing so would remove knowledge from current readers.

Additional write rules:

- the submitter must be able to read an existing page before their capture can affect it;
- retrieval context for filing is scoped to the submitter and target visibility;
- output visibility can never be broader than any source material actually used in that output;
- ACL changes are explicit structured operations and pass the same non-leakage gate;
- gardening may inspect the whole corpus as a system identity, but each repair preserves claim/page visibility and cannot transfer content between scopes;
- deletion may sweep links across scopes without copying surrounding content between them;
- diffs and raw capture evidence are master-only in the backoffice.

This guard is shared by librarian filing, entity operations, gardening, explicit deletion, indexing, and read projection. There must not be copied, subtly different ACL predicates in each subsystem.

### 5.8 Entity identity lifecycle

Entity existence and names may themselves be confidential. The entity system therefore separates one internal identity graph from reader-specific projections.

#### Canonical representation

Each entity receives an opaque, immutable ID in the form `ent_<lowercase UUIDv4>`. The filename is the ID, never a name-derived slug:

```text
wiki/entities/<entity-id>.md
```

The machine-owned page is intentionally minimal. Its frontmatter contains:

- stable `id`;
- `type: entity`;
- `entity_type`;
- creation/update timestamps;
- scoped name claims;
- absorbed entity IDs, when merges have occurred.

Each name claim contains `value`, normalized comparison value, `preferred` or `alias`, ACL, source path/capture ID, introducing actor, and timestamp. The body contains no accumulated facts, connections, summaries, or generated dossier. `approved_by` disappears because there is no approval step.

Raw entity pages and the generated registry are internal machine data: ordinary `read_page` and general search do not expose or index them as normal pages. `list_entities` and `describe_entity` return an ACL-filtered projection.

#### Birth and matching

The librarian proposes an identity claim while filing normal material. A single entity service performs matching and mutation.

1. Normalize names without erasing meaningful distinctions.
2. Search the global internal registry without returning hidden candidates to the caller/model.
3. Reuse an existing ID only when identity evidence is strong enough; a name collision alone is not sufficient for ambiguous human or organization names.
4. Add the new scoped name claim with its provenance.
5. Otherwise mint a new opaque ID.

Automatic reuse during filing requires a shared stable external identifier or an explicit `same_as` reference that is uniquely visible to the writer. An explicit merge requires either one external identifier present on every selected identity or an exact same-entity assertion that is verified against a cited immutable source. A rationale alone is never evidence. Uncertain fuzzy similarity creates no merge task and reveals nothing; separate IDs are safer than a false merge.

If a hidden canonical ID is reused, the submission receipt reports only normal capture progress. It must not reveal the hidden preferred name, aliases, audiences, or prior existence.

Every normal page anchored to an entity must have at least one name claim visible to that page's audience. The gate enforces this invariant.

#### Rename and alias

A rename adds a new preferred name claim under the source's ACL and turns the previous preferred claim for that same visibility into an alias. The entity ID and path do not change. Multiple audiences can therefore know the same identity by different permitted names without seeing one another's aliases.

The projection chooses a display name deterministically from claims visible to the reader: the newest visible preferred claim, then the newest visible alias, with stable ID ordering as a final tie-break. It never falls back to a hidden name.

#### Read projection

`list_entities` returns only identities for which the reader can see a name claim or an anchored page, using a visible display name.

`describe_entity` follows internal redirects, emits only visible identity claims, and composes facts/connections from ACL-visible notes, concepts, and sources anchored to the stable ID. It does not read a stored dossier from the entity page.

Unknown, hidden, and unauthorized IDs return the same neutral response shape so the tool is not an existence oracle.

#### Merge

An explicit master operation or the gardener may merge entities only with strong evidence. The operation is atomic and uses one writer commit:

1. choose the earliest-created ID as canonical, breaking a tie lexically;
2. move all name claims to it without changing ACL or provenance;
3. rewrite every entity anchor from absorbed IDs to the canonical ID;
4. record absorbed IDs on the canonical page so internal lookups redirect;
5. remove absorbed entity pages;
6. regenerate and validate the registry;
7. record the verified evidence and exact diff in the change ledger.

Fuzzy suspicion alone does nothing. It does not create a human task.
Source evidence must be an exact assertion with a supported same-identity relation that binds every selected record by a complete, distinct name. Same-label, contained-name, and otherwise ambiguous pairs require a shared external ID instead.

#### Delete

Entity deletion is an explicit master operation. It removes the identity page and registry entry and sweeps its anchors and links across the corpus in the same commit. It does not silently delete the substantive notes containing those references; those pages are rewritten or removed only when the deletion request and remaining content justify it.

There is no inactive/deprecated entity state. Absorbed IDs are redirects, not visible entities. Git and the change ledger retain the audit history.

#### Derived registry

`ops/entity-registry.json` remains as a deterministic internal derivative of entity pages. It contains scoped claims and redirects, is regenerated in the same candidate tree as any entity change, and is never a second mutation API. The linter can reproduce it byte-for-byte from the entity pages.

### 5.9 Contradictions

A real contradiction between equally plausible sources is not repaired by choosing a convenient answer. The librarian preserves both claims, their dates, and their citations in a strict, reader-visible contradiction callout on the narrowest page whose audience may see both sources.

The callout has a stable opaque contradiction ID, `unresolved` status, two or more claim/citation entries, and a short neutral explanation. Its Markdown schema is deterministic and lintable. A valid unresolved contradiction counts as a healthy corpus state.

Contradictions never block capture or gardening. If the conflicting sources have different visibility, no broader page or reader is told that hidden evidence exists; the callout is placed only in a page visible to the intersection of permitted readers.

The backoffice derives a deduplicated Contradictions view by parsing/indexing these markers. It is not a permanent task table. The master can optionally contribute a later resolution through a structured form:

- choose claim A, claim B, neither/context, or a custom resolution;
- provide editable resolution text;
- provide a required rationale;
- optionally attach a supporting file or public URL.

Submitting the form creates an ordinary capture with `intent.resolution_of`. It passes through the same evidence, queue, librarian, ACL, gates, commit, and change ledger. The original processing run is never waiting for this action. The librarian removes or revises the marker only when the new source actually resolves it; otherwise the uncertainty remains explicit.

### 5.10 Linter, repair primitives, and gardener

These names remain, but their responsibilities become small and precise:

- **Linter:** a pure detector library. Given a candidate repository tree, it returns deterministic, structured violations. It has no database backlog and no side effects.
- **Repair:** pure, bounded transformation primitives used by the writer: deterministic rewrites, link/anchor sweeps, registry regeneration, explicit deletion, and a constrained model repair when semantics are required. It is not a daemon, queue, or user-facing workflow.
- **Gardener:** the single scheduled orchestrator that runs the linter and repair primitives autonomously inside the existing writer process.

There is no separate gardener worker and repair worker. The one knowledge writer performs:

```text
detect -> plan bounded repairs -> apply to candidate tree -> run all write gates
       -> create one commit -> advance repository ref -> rerun and record summary
```

If the candidate does not pass every gate, no repository ref is advanced and no partial repair lands. A clean run records zero changes. A failed run records a safe error and is retried according to the technical retry policy. Findings are ephemeral details of a run, not durable assignments to people.

The target disposition of current checks is:

| Current concern | Target disposition |
|---|---|
| Invalid frontmatter, links, source references, ACL, paths, or contradiction markers | prevent in every write gate; deterministically repair pre-existing drift |
| Registry drift or anchors to absorbed entity IDs | regenerate/re-anchor deterministically |
| Link from a broader page to a narrower page that leaks identity/context | prevent at write time and repair without copying restricted content |
| Entity placeholder/dossier body | impossible under the minimal entity renderer; remove the old check after reset |
| Duplicate pages or entities | bounded semantic consolidation only with strong evidence |
| Orphan page | repair/merge only when the corpus supports a correct destination; otherwise the rule must define why the page is invalid or disappear |
| Aging seed | remove as a time-only health failure; age is an analytic signal, not corruption |
| Anchor concentration | remove; it is not corpus corruption |
| Company-wide fraction | remove; it is not corpus corruption |
| Dead vocabulary | replace with deterministic entity claim/anchor/registry consistency |
| Date-bearing body-link style | enforce only if it expresses a real retrieval invariant; otherwise remove the style rule |

Contradictions expressed with the valid marker are not linter failures. A check that can only say “a person should inspect this” is not admitted into corpus health.

Gardening runs on a fixed schedule and can also be triggered by the master backoffice for operational testing. Scheduling checks and expired-upload cleanup continue while the writer has a non-empty queue; ordinary capture backlog cannot starve maintenance. The trigger is not installed as a product CLI command.

### 5.11 Unified change ledger and friendly diffs

Every landed Git mutation writes one append-only `knowledge_changes` record:

```text
id
trigger: capture | garden | delete | contradiction_resolution | entity
actor
capture_id / job_run_id when applicable
parent_commit_sha
commit_sha (unique)
human summary
per-path manifest:
  path, action, page role, reason,
  before_sha256, after_sha256, additions, deletions
exact_patch_ref, exact_patch_sha256, exact_patch_bytes
created_at
```

The exact unified patch is stored compressed and content-addressed in the existing private object store. Git remains authoritative; a missing cache object can be reconstructed from the parent and commit SHA. The ledger does not duplicate wiki content as mutable database state.

The current separate permanent gardener findings and repair ledger disappear. Delete operations and historical-style maintenance are simply filtered change records. Because this is a clean reset, no legacy records are converted.

The master backoffice replaces the Repairs view with a unified Changes view. A capture, gardener run, entity action, contradiction resolution, or deletion links to its change record. The default presentation is friendly to both technical and non-technical users:

- a short explanation of what the system learned or repaired;
- created, updated, deleted, and contradiction counts;
- one card per path with the librarian/gardener reason;
- before/after or inline colored additions and deletions;
- unchanged regions collapsed;
- large source additions collapsed initially to provenance, filename, media type, byte count, and hash;
- an expandable technical view containing the exact Git patch, commit SHA, and parent SHA.

All captures and diffs are master-only. The backoffice continues to use one master identity with global access; it does not implement per-operator ACL filtering.

### 5.12 Search and index reconciliation

The existing hybrid full-text plus embedding search and ranking remain. Search, `ask`, filing retrieval, `list_entities`, and `describe_entity` all apply the same visibility policy.

Incremental GitHub webhook indexing remains the fast path. A nightly full rebuild is restored in the private knowledge repository as the reconciliation path:

- GitHub Actions cron: `17 4 * * *` (04:17 UTC);
- manual `workflow_dispatch` for operators;
- pinned action SHAs and a pinned Stigmergy release;
- checkout of the private knowledge repository at the selected HEAD;
- full `stigmergy-index --rebuild` using the index DSN and embedding credential;
- no feature flag that exits successfully without doing the rebuild;
- a successful run records indexed commit SHA, row count, and completion time;
- a failed run is visibly failed and leaves the previous success timestamp unchanged.

Webhook overflow/defer paths mark the index dirty rather than pretending full convergence. A successful full rebuild clears the dirty marker. The backoffice warns when the indexed commit differs from repository HEAD beyond the incremental grace period or when the last successful full rebuild is older than 26 hours.

Entity machine pages are not indexed as ordinary documents. There is no separate entity-dossier or view index: `list_entities` and `describe_entity` combine the internal registry with the existing ACL-filtered page index.

### 5.13 Product and operational surfaces

The final MCP surface remains compact:

- `search_brain`
- `read_page`
- `list_entities`
- `describe_entity`
- `ask`
- `brain_submit` with the unified local contract
- `brain_submissions`
- `brain_delete`

The official local bridge proxies these tools. `path` and private Drive behavior exist only in the local bridge; cloud services receive verified blob references.

Installed commands are limited to real service or bootstrap operations:

| Keep | Reason |
|---|---|
| server/API entrypoint | deployed service |
| librarian/writer service entrypoint and boot/credential setup | deployed worker/bootstrap |
| Slack service entrypoint | deployed adapter |
| index rebuild/check command | deployment and scheduled operations |
| issue-token/admin-token/credential commands | security bootstrap and rotation |
| local MCP bridge entrypoint | supported user client |

The following installed product CLIs are removed:

- `stigmergy-search`: search is MCP/backoffice functionality;
- `stigmergy-queue`: queue inspection/retry/reclaim is backoffice or worker behavior;
- `stigmergy-gardener`: gardening is scheduled and master-triggerable in the backoffice;
- librarian `once`, manual queue claim, and similar debugging modes as installed product commands.

Manual queue claim, one-shot librarian, and one-shot gardener execution remain callable only as library-level test helpers; they are not installed scripts. Runbooks must distinguish service commands from user features. After cleanup, an audit of `project.scripts` and executable modules must prove that no user capability is available only through a CLI.

### 5.14 Librarian contract cleanup

The knowledge repository's live librarian skill and every platform-owned frozen/evaluation copy change in the same implementation:

- `/Users/marc/dev/stigmergy-brain/.claude/skills/librarian/SKILL.md`
- `tests/librarian/fixtures/repo/.claude/skills/librarian/SKILL.md`
- `evals/filing/repo/.claude/skills/librarian/SKILL.md`

The instructions must describe only real operations exposed by the structured response and implemented by the writer. In particular they remove claims about a meeting distiller, document door, view regenerator, human approval, a future repair loop, or an identity gardener that is not part of the same release.

Receipts and prompts must not claim that nothing was rewritten when rewrites or deletions are valid outcomes. Contract tests assert every mechanical promise in the skill against code. The knowledge-repository linter/templates/trust tooling are simplified at the same time to remove `views/`, input-specific source kinds, and obsolete writer identities.

The Stigmergy package is the single authoritative linter implementation. Knowledge-repository workflows invoke the pinned package; a repository-local file, if a tool launcher requires one, is a generated or thin version-checked wrapper and contains no independent rules. The clean reset removes historical authorship baselines and enforces the current trusted writer through ordinary branch/authorship gates.

### 5.15 Required code deletion and convergence

The build is not complete if the new path merely sits beside the old one. The inspected constructs below are replaced or removed:

| Current code area | Required target |
|---|---|
| `capture/schema.py` submit kinds and kind-sized payloads | one kind-free public contract and one envelope/artifact validator |
| `capture/queue.py` payload copy of full material | envelope metadata plus immutable object references only |
| kind/Slack-specific source directories in librarian processing | one neutral capture-ID source renderer |
| MCP string-only document instructions | local bridge acquisition plus cloud blob-finalization contract |
| Slack newline-joined thread material | canonical structured snapshot and attributed readable renderer |
| slug-derived entity birth and flat alias registry | opaque ID, scoped/provenanced claims, generated redirects |
| entity facts/connections and generated `views/` machinery | minimal identity pages plus ACL-scoped `describe_entity` composition |
| persistent `gardener_findings` and suggested human actions | ephemeral run findings plus `job_runs` summary |
| repair table/runtime compatibility for retired repair kinds | pure repair primitives and unified change ledger |
| separate Repairs backoffice view | unified Changes view with trigger filters |
| manual search/queue/gardener installed scripts | MCP/backoffice/service behavior and library-only test helpers |
| missing full-index scheduler | pinned nightly knowledge-repository workflow and index-health state |
| live/frozen/evaluation skill promises for nonexistent writers | one truthful, code-bound librarian contract |
| historical knowledge-repository authorship/view baselines | clean trusted-writer rules for the target zones only |

After replacement, repository-wide searches for the retired capture kinds, generated view writer, human gardener actions, retired repair kinds, and removed console scripts are part of validation. Historical changelog prose and Git history need not be rewritten; active runtime, fixtures, prompts, generated contracts, and current documentation must be clean.

## 6. End-to-end flows

### 6.1 Save conclusions from a local LLM conversation

1. The user asks Codex or Claude Code to save the conclusions.
2. The local client constructs a self-contained synthesis and calls `brain_submit(text=...)`.
3. The bridge submits exact UTF-8 bytes and authorized visibility.
4. The writer creates one immutable source page, files durable conclusions into the wiki, passes all gates, and lands one commit.
5. The receipt exposes submission state. The master can inspect the friendly and exact diff.

The unsubmitted chat transcript is neither assumed nor archived.

### 6.2 Save a private Drive PDF

1. The user passes its Drive URL to the local MCP tool.
2. On first use, local Google OAuth opens; its token remains in the OS keychain.
3. The bridge downloads the PDF, computes its digest, obtains a presigned upload, uploads, verifies, and finalizes.
4. The cloud worker detects PDF, extracts digital text, OCRs only poor/scanned pages, and renders one source.
5. The normal librarian flow runs. No Drive credential or Drive-specific brief exists in the cloud.

### 6.3 Capture a Slack thread

1. An authorized user reacts with the brain emoji.
2. The Slack adapter snapshots selected thread messages with speaker/timestamp/permalink structure and downloads supported attachments.
3. It creates one envelope with one thread artifact plus attachment artifacts under the reacting user's permitted visibility.
4. Extraction, source rendering, filing, gates, commit, audit, and indexing are identical to every other capture.

### 6.4 Submit through the backoffice

1. The master chooses paste, upload, or public URL.
2. The adapter acquires exact bytes, applying public-fetch security when relevant.
3. It sends the same envelope to the same queue.
4. Progress and the landed change appear on the capture detail page.

### 6.5 Encounter a contradiction

1. Filing detects two supportable, incompatible claims.
2. The commit retains both with dates/citations and adds a valid contradiction marker at a safe visibility.
3. The capture lands normally and the corpus is healthy.
4. The marker appears in the master Contradictions view.
5. If the master later supplies a resolution, that resolution is a new capture and a new commit.

### 6.6 Garden the corpus

1. The scheduled writer takes a consistent repository snapshot.
2. The linter detects only preventable or repairable violations.
3. Repair primitives build one candidate change set.
4. The normal schema, link, ACL, source-immutability, entity, contradiction, and trust gates run.
5. A clean candidate advances as one commit and one change record; a failed candidate advances nothing.
6. The linter reruns on landed HEAD and the job summary is recorded. No human task is created.

### 6.7 Delete knowledge

1. An authorized explicit delete names wiki/source paths and a rationale through MCP or the backoffice.
2. The writer validates authority, sweeps references, preserves ACL boundaries, and builds a candidate tree.
3. Normal gates run and one commit lands.
4. Search removes deleted current-state pages; Git and the master change record preserve audit and ordinary undo.

## 7. Shared semantic architecture

The implementation must centralize the following concepts rather than reimplement them per surface:

| Concept | Single owner | Consumers |
|---|---|---|
| Capture validation and normalization | capture domain service | MCP bridge upload finalization, Slack, admin |
| Media detection/extraction | artifact pipeline | all captures |
| Reader visibility predicate | authorization policy | search, read, ask, entity projection, admin where applicable |
| Safe write visibility predicate | write guard | librarian, gardener, entity operations, deletion |
| Repository mutation/gates/commit | knowledge writer | capture, garden, entity, contradiction resolution, delete |
| Entity resolve/mutate/project | entity service | librarian, gardener, list/describe, admin |
| Change record construction | change ledger | all landed Git operations |
| Indexable corpus selection | index corpus policy | full rebuild and webhook |

There is one operational writer process. Linter and repair are libraries; gardener is a scheduled operation inside that process. Origin adapters cannot mutate Git directly.

The permission matrix is also shared:

| Capability | Authenticated member | Master/unrestricted identity | Internal writer |
|---|---:|---:|---:|
| Submit material | authorized audiences only | any configured audience | executes validated job only |
| Search/read/ask | ACL-visible corpus | whole corpus | scoped to the operation's safe context |
| List/describe entity | ACL-filtered projection | full projection and provenance | global identity matching, no unsafe output |
| View captures/diffs/job runs | no | yes | writes audit records |
| Resolve contradiction | no | submits a new capture | files it without special approval |
| Merge/delete entity | no | explicit action | applies validated atomic primitive |
| Delete pages/sources | only with an explicit unrestricted delete capability | yes | applies validated sweep |
| Trigger gardener | no | yes | also runs on schedule |

Slack and the local bridge act as the authenticated member represented by their token/mapping; they do not become system or master identities merely because they are trusted adapters.

## 8. Data ownership

| Data | Authority | Mutable? | Rebuildable? |
|---|---|---:|---:|
| Original submitted bytes | private R2 object | no, except explicit deletion/GC | no |
| Readable source | Git `sources/` | no, except explicit deletion | from retained original plus pinned extractor, but treated as evidence |
| Current wiki | Git `wiki/` | yes, by writer only | Git history provides prior states |
| Entity registry | Git `ops/`, derived from entity pages | regenerated only | yes |
| Queue/write job state | Postgres | yes | no; operational only |
| Change ledger metadata | Postgres | append-only | mostly from Git commits and job metadata |
| Exact diff cache | private R2 object | no | yes, from Git |
| Search/vector index | Postgres | yes | yes, from Git |
| Contradiction list | parsed/indexed from wiki markers | yes, derived | yes |
| Gardener run summary | Postgres | append-only | no; operational audit only |

## 9. Security and failure requirements

1. Every submission has an authenticated actor and an authorized audience before upload finalization.
2. Presigned uploads are short-lived, restricted to one key, and verified against declared size and digest before queueing.
3. Object-store reads are server-side and authorization-checked; objects are never public.
4. Secrets, OAuth tokens, presigned URLs, document bytes, and restricted titles are excluded from logs and safe error messages.
5. Slack Socket Mode is authenticated by its app token, and event redelivery is idempotent before
   queueing.
6. Public URL acquisition implements SSRF and redirect defenses described above.
7. Parsers run with resource limits and reject unsafe containers, decompression bombs, and unsupported encryption.
8. The model receives only material permitted for the filing identity/target visibility.
9. Entity lookup, redirects, list, describe, and errors do not reveal hidden existence or aliases.
10. A Git operation is atomic from the reader's perspective: the repository ref moves only after all gates pass.
11. Retries are idempotent. A crash after Git commit but before database acknowledgement reconciles by commit/change ID rather than creating a second commit.
12. Technical terminal failures remain visible and retryable by the master after the infrastructure problem is fixed; content judgment is never required to unblock them.

## 10. Observability and backoffice

The master backoffice must provide:

- capture queue counts and oldest age by state;
- capture detail with actor, audience, provenance, artifacts, extraction outcome, retries, source path, commit, and change link;
- Changes with friendly and exact diffs;
- Contradictions derived from current Markdown plus the resolution form;
- Gardener run history with start/end, base/head commit, detected/fixed counts, final cleanliness, and safe failure;
- entity identity inspection with all scoped claims/provenance, redirects, and explicit merge/delete controls;
- index health with repository HEAD, indexed commit, dirty flag, incremental event time, full-rebuild time, and stale warning;
- service/worker heartbeat and last successful write.

It must not provide a permanent “things a human must fix” gardener inbox. Anchor concentration, company-wide fraction, and aging-by-itself analytics are removed from the target rather than carried into a separate subsystem.

## 11. Removed scope

The final implementation intentionally excludes:

- legacy capture kinds and kind-specific briefs;
- exact-edit `page` semantics;
- a cloud Google Drive OAuth integration;
- browser extensions or support for ChatGPT/Claude web as private-file bridges;
- direct remote filesystem paths;
- Sheets/XLSX, audio/video transcription, archive ingestion, and arbitrary connectors;
- base64 binary payloads in MCP calls;
- generated entity dossiers/views;
- human approval states in ingestion, gardening, or entity birth/merge;
- permanent gardener findings with suggested human actions;
- a generic model repair queue or repair daemon;
- a separate deprecated/inactive entity lifecycle;
- per-operator ACL in the master backoffice;
- CLI-only user features;
- preservation of experimental data or contracts.

Secure erasure from all historical Git objects is not part of ordinary page deletion. If regulatory erasure becomes a requirement, it needs a separate, explicit history-rewrite specification.

## 12. Acceptance criteria

### Unified input and evidence

1. **CAP-01:** The public/local MCP schema has no `kind` and rejects calls that provide zero or more than one of `text`, `path`, and `url`.
2. **CAP-02:** Slack, backoffice, and local MCP fixtures normalize to the same `CaptureEnvelope` schema and enter the same queue/service.
3. **CAP-03:** New durable jobs contain artifact references and metadata, not a duplicate full text/binary payload.
4. **CAP-04:** Retrying the same actor/idempotency key returns one submission and cannot create two Git commits.
5. **CAP-05:** Exact text input can be retrieved byte-for-byte from its private original object and appears byte-for-byte in its immutable readable source body.
6. **CAP-06:** Digital PDFs use extracted text without OCR when the quality threshold passes; scanned/poor pages use OCR, with the decision recorded.
7. **CAP-07:** DOCX, PPTX, PNG, JPEG, HTML, Google Doc export, and Google Slides export each pass an end-to-end extraction fixture with ordered readable output.
8. **CAP-08:** MIME spoofing, oversize, corrupt, encrypted, unsafe-container, and unsupported-format fixtures fail with typed safe errors and no Git commit.
9. **CAP-09:** Public URL tests block loopback, private, link-local, metadata, DNS-rebinding, and unsafe redirect targets while accepting a normal bounded public download whose immutable source records distinct sanitized original and final URLs.
10. **CAP-10:** A private Drive integration test proves the Google token remains local, bytes upload through a presigned URL, the cloud receives no Google credential, and typed Drive acquisition provenance reaches the immutable source.
11. **CAP-11:** The MCP bridge transmits large files as bytes through object upload, never as base64 in tool arguments.
12. **CAP-12:** A Slack thread source preserves speaker, timestamp, order, permalink, and attachment boundaries rather than newline-joining message text alone.
13. **CAP-13:** Every landed capture creates exactly one neutral `sources/YYYY/MM/<capture-id>.md` path regardless of origin or media.
14. **CAP-14:** Reprocessing or gardening cannot modify an existing source page or original object.
15. **CAP-15:** A pasted transcript stores the exact transcript as evidence, while its wiki changes contain conclusions rather than a mandatory transcript page type.
16. **CAP-16:** A conclusions-only conversational submission stores exactly the supplied synthesis and does not claim to have archived unseen conversation history.

### Filing, writing, and visibility

17. **WRITE-01:** A capture candidate may create, rewrite, consolidate, and delete `wiki/` pages without approval, and the source plus wiki changes land in one commit only after all gates pass.
18. **WRITE-02:** A valid capture that adds no durable conclusion still lands its source and an auditable no-wiki-change result.
19. **WRITE-03:** No production code or librarian contract dispatches on Slack/meeting/document/page/raw origin to choose a filing brief or page taxonomy.
20. **WRITE-04:** Only `note`, `concept`, minimal `entity`, and `source` roles are accepted; `meeting`, `document`, `page`, and `view` fixtures fail the corpus contract.
21. **WRITE-05:** A caller cannot affect a page they cannot read, including through a guessed path or entity ID.
22. **WRITE-06:** Restricted input cannot alter an organization-wide page; the tested outcome is a restricted companion or no wiki change.
23. **WRITE-07:** Filing never broadens an ACL and never includes narrower source content in a broader output.
24. **WRITE-08:** All writer operations use one shared repository mutation, gate, commit, and idempotent reconciliation path.
25. **WRITE-09:** A gate failure advances no repository ref, creates no landed change, and leaves the job safely retryable/failed.
26. **WRITE-10:** Deleting a wiki or source path through MCP/backoffice sweeps references, passes gates, lands one commit, and removes it from current search.
27. **WRITE-11:** No ingestion, gardener, entity, or deletion state can wait for human approval or a free-form answer.

### Entities

28. **ENT-01:** New entity filenames use opaque immutable IDs; renaming every visible name leaves the ID, path, and page anchors unchanged.
29. **ENT-02:** Entity pages contain only identity fields, scoped name claims, provenance, and absorbed IDs; facts/connections/dossier sections are rejected.
30. **ENT-03:** Entity pages and the raw registry are absent from ordinary search and `read_page` for non-master readers.
31. **ENT-04:** `list_entities` returns only identities with a visible claim or anchored page and always chooses a visible display name.
32. **ENT-05:** `describe_entity` composes current knowledge from visible anchored pages and sources rather than an entity-page body.
33. **ENT-06:** Hidden, nonexistent, and unauthorized entity identifiers yield the same neutral external response.
34. **ENT-07:** Reusing a hidden canonical ID cannot reveal its prior existence, names, aliases, facts, or audiences in the receipt or model context.
35. **ENT-08:** Name collision without strong identity evidence does not merge two ambiguous people/organizations.
36. **ENT-09:** A scoped rename creates a new preferred claim, retains the previous name as a same-scope alias, and reveals neither to unauthorized readers.
37. **ENT-10:** A merge rejects rationale-only requests, verifies either a shared external ID on every selected identity or an exact assertion in an existing immutable source, preserves every claim ACL/provenance, rewrites all anchors, emits redirects for absorbed IDs, removes absorbed pages, regenerates the registry, records the evidence, and lands atomically in one commit.
38. **ENT-11:** Fuzzy duplicate suspicion with insufficient evidence makes no change and creates no human finding.
39. **ENT-12:** Entity deletion removes the identity/registry entry and sweeps anchors without automatically erasing substantive page content.
40. **ENT-13:** The registry is byte-for-byte reproducible from entity pages and cannot be mutated through a separate API.
41. **ENT-14:** Every anchored page passes a gate proving at least one entity name claim is visible to that page's audience.

### Contradictions

42. **CON-01:** Two credible incompatible sources land normally with both claims, dates, citations, and one valid stable contradiction ID.
43. **CON-02:** A well-formed unresolved contradiction is a clean linter result, not a failed capture or gardener task.
44. **CON-03:** A contradiction involving restricted evidence is never rendered on a page visible to readers lacking either source.
45. **CON-04:** The backoffice Contradictions view is rebuilt from current Markdown/index data and deduplicates stable IDs without a task table.
46. **CON-05:** Resolution form submission creates an ordinary capture with `resolution_of`, required rationale, and optional evidence, and produces a new commit/change record.
47. **CON-06:** A resolution removes/revises a marker only when the new material supports it; otherwise the contradiction remains explicit and processing still completes.

### Autonomous corpus health

48. **GARDEN-01:** Linter calls are pure and deterministic for a fixed tree; repair primitives have no queue/daemon/database backlog of their own.
49. **GARDEN-02:** One scheduled writer run detects, repairs, executes all normal gates, lands at most one commit, and reruns the linter.
50. **GARDEN-03:** A failed repair candidate lands no partial commit and records a safe run failure.
51. **GARDEN-04:** A clean run records zero detected/fixed items and no Git commit.
52. **GARDEN-05:** Registry drift and absorbed-ID anchors are repaired deterministically.
53. **GARDEN-06:** ACL leakage and source mutation are prevented by gates and are not left as human instructions.
54. **GARDEN-07:** Aging seed, anchor concentration, and company-wide fraction are absent from health findings unless redefined as independently preventable/repairable invariants.
55. **GARDEN-08:** No open `gardener_findings`-style task store, suggested-human-action field, general repair queue, or repair worker exists in the target schema/runtime.

### Audit, admin, search, and surfaces

56. **OPS-01:** Every landed capture, garden, deletion, contradiction resolution, and entity operation has exactly one unique `knowledge_changes` row linked to its Git commit.
57. **OPS-02:** Each Changes entry renders a friendly summary and per-page diff, collapses large source bodies by default, and exposes the exact patch and commit SHAs on demand.
58. **OPS-03:** Exact patch bytes are hash-verified; if their cache object is absent, the same patch can be reconstructed from Git.
59. **OPS-04:** The backoffice is master-only and can inspect every capture, diff, contradiction, entity claim, gardener run, and index-health record.
60. **OPS-05:** The incremental webhook marks deferred/overflow work dirty, and a successful full rebuild clears it only after indexing repository HEAD.
61. **OPS-06:** The knowledge repository contains a pinned nightly `17 4 * * *` full-rebuild workflow plus manual dispatch, and a forced failure is visible rather than a successful no-op.
62. **OPS-07:** The backoffice warns when full rebuild age exceeds 26 hours or index convergence exceeds its grace period.
63. **OPS-08:** Hybrid lexical/vector search ranking remains covered by its current golden tests plus ACL and deletion/rebuild tests.
64. **OPS-09:** `stigmergy-search`, `stigmergy-queue`, and `stigmergy-gardener` are absent from installed scripts; retained commands are classified and tested as service/bootstrap/operations or local bridge commands.
65. **OPS-10:** A repository-wide executable audit finds no user capability available only through CLI.
66. **OPS-11:** The live, frozen-test, and evaluation librarian skills contain no claims about nonexistent writers, human approval, or maintenance, and contract tests bind every promised operation to code.
67. **OPS-12:** Runtime and tests contain no legacy capture-kind, generated-view, old entity-dossier, permanent-finding, retired-repair-kind, or compatibility branch.
68. **OPS-13:** A review of changed production comments and docstrings finds no migration narration, historical justification, review commentary, or prose that merely restates the implementation.

### Clean reset

69. **RESET-01:** A documented test-environment reset creates the target schema, empty private object namespace, empty queue/index, and target knowledge-repository scaffold from scratch.
70. **RESET-02:** The new system starts and passes its end-to-end suite without reading or converting any old database row, object metadata, page format, registry, or MCP request.
71. **RESET-03:** Current code, generated contracts, knowledge-repository tools/templates, tests, and operator documentation all describe the same target behavior in the same release.

## 13. Validation strategy

Implementation is complete only after sequential validation of the smallest relevant targets and then the full cross-system checks:

1. pure unit tests for envelope validation, media detection, ACL set relations, entity projection, contradiction parsing, linter, and patch manifests;
2. parser/OCR fixture tests with deterministic pinned outputs;
3. Postgres integration tests for queue idempotency, writer crash reconciliation, change ledger, index health, and job runs;
4. Git integration tests proving source immutability, one-commit atomicity, entity merge/delete sweeps, and reconstructable diffs;
5. adversarial ACL tests across capture, search, ask, entity tools, contradictions, gardener, and diffs;
6. MCP bridge tests with a fake cloud service and fake local Google OAuth/Drive export;
7. Slack Socket Mode, identity, thread, redelivery, and attachment end-to-end tests;
8. admin browser/API tests for paste/upload/public URL, friendly diffs, contradictions, entities, retries, and index warnings;
9. knowledge-repository linter/template/skill contract tests in both live-equivalent and frozen/evaluation fixtures;
10. a clean-environment system test that submits text, a digital PDF, a scanned PDF, a private-Drive fixture, and a Slack thread; rewrites and deletes pages; resolves a contradiction; merges entities; gardens; runs incremental indexing; then runs a full rebuild and compares searchable state.

Production code, tests, docs, workflows, and the private knowledge-repository contract must land as one coordinated release. No acceptance criterion can be waived by documenting current behavior.

## 14. Recommended implementation slices

The build should preserve a runnable target at the end of each slice, but it need not preserve the abandoned external contract between slices.

1. **Fresh foundations:** target database baseline, `CaptureEnvelope`, object references, shared write job/commit path, change ledger, and test-repository scaffold.
2. **Acquisition and extraction:** unified cloud service, admin and Slack adapters, format pipeline, source renderer, and local MCP bridge including private Drive.
3. **Librarian convergence:** one filing brief, target page roles, autonomous rewrites/deletes, shared ACL write guard, and truthful skills/contracts.
4. **Entity lifecycle:** opaque identity pages, scoped claims, registry projection, list/describe, merge/rename/delete, and removal of generated views/dossiers.
5. **Contradictions and corpus health:** strict markers, backoffice resolution capture, pure linter/repair primitives, scheduled autonomous gardener, and removal of findings/legacy repair runtime.
6. **Operations finish:** Changes UX, index reconciliation workflow/health, CLI cleanup, complete docs/runbooks, clean reset, and cross-system validation.

## 15. Decisions and rationale

| Decision | Rationale |
|---|---|
| Hard reset instead of migration | There is no production data or compatibility promise; transition code would become permanent accidental complexity. |
| One normalized capture, no kinds | Input medium affects extraction, not what knowledge means or how it is filed. |
| Local bridge for private Drive | It uses the user's existing machine and OAuth without placing personal cloud credentials in Stigmergy. |
| Presigned byte upload, not MCP base64 | It is bounded, streamable, retryable, and keeps credentials on the correct side. |
| Exact original in R2, readable source in Git | Binary fidelity and human-readable provenance have different jobs; neither substitutes for the other. |
| One source per capture | It preserves the Karpathy/Hippocampus mental model and stable provenance across every adapter. |
| Autonomous wiki mutation | The wiki is model-owned; approval would reintroduce the personal interactive loop that does not fit a team service. |
| Explicit delete operation | Deletion expresses intent rather than new source material, while still sharing the same writer and gates. |
| Minimal internal entity pages | Identity needs stable global resolution; facts belong in ACL-scoped knowledge pages and are composed for each reader. |
| Opaque entity IDs | Renames do not move paths and confidential names do not leak through filenames/anchors. |
| Contradiction marker, not forced answer | Honest unresolved uncertainty is healthier than fabricated certainty and does not need to block ingestion. |
| Structured later resolution as a capture | It adds evidence through the normal pipeline without turning the original run into human-in-the-loop work. |
| Linter/repair as libraries, gardener as one operation | Detection, transformation, and scheduling remain testable without three workflows or a human task store. |
| One change ledger plus Git | The backoffice gets a coherent audit UX while Git remains the canonical history. |
| Incremental index plus nightly rebuild | The webhook gives freshness; full rebuild guarantees eventual convergence after missed/deferred events. |
| Remove CLI-only product commands | Unsupported alternate surfaces hide dead features and multiply behavior; operators retain only real service/bootstrap tools. |
