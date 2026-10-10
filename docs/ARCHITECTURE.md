# Architecture

## Authorities

| Data | Authority | Mutation rule |
|---|---|---|
| Tenants, platform operators, access requests, workspace deletions | Postgres platform tables (`server/platform.py`) | the master console and `stigmergy-admin`; every change in `platform_actions` |
| Original artifacts and exact patches | private object store | content-addressed; deleted only when no tenant still references them |
| Readable evidence | each tenant's Git `sources/YYYY/MM/<capture-id>.md` | append-only except explicit deletion |
| Current knowledge | each tenant's Git `wiki/notes`, `wiki/concepts`, `wiki/entities`, and `wiki/projects` | one serialized writer |
| Queue, runs, and change metadata | Postgres, per tenant | operational and append-only audit state |
| People, layers, tokens, sessions, Slack channel layers | Postgres roster (`server/roster.py`), per tenant | the tenant's administrators; every change audited |
| Search index and contradiction list | Postgres derived from each tenant's Git | rebuildable per tenant |
| Model spend | Postgres `model_usage`, per tenant | appended by filing and answers |

## Tenancy

One deployment serves several tenants (`specs/multi-tenancy.md`). Every tenant-owned table carries
`tenant_id` and forces one row-level security policy: a row is visible and writable when it
belongs to the connection's tenant (`app.tenant_id`) or the connection is in platform scope
(`app.scope = 'platform'`). `kernel.tenancy` is the only code that sets either; a connection with
neither reads no tenant row and cannot insert one. The service connects with a role that cannot
bypass the policy and owns the tables, so forced security binds it too.

Each entry point names its tenant before it reads anything else: a bearer token or session by its
own row (digests are unique across the platform), a push by its repository, a Slack event by its
team, a writer job by its queue row. Platform scope is used only to find that row and to sign
out, to claim and schedule the writer's work across tenants, to decide whether an evidence object
is still referenced anywhere, to expire uploads, audit rows, and access requests, to check the
index's embedding dimension, to delete a whole workspace, to record and complete a refiled
workspace's import and handover, and by the master console. Every process refuses to start on a
database role that bypasses row-level security. A suspended tenant's tokens, sessions, pushes, and
Slack events are refused and its work is not claimed, except a layer erasure already requested.

Each tenant has one private repository in the platform's GitHub organization; the librarian App's
installation there covers all of them, and its one webhook delivers every push. The writer keeps a
checkout per tenant (`librarian.workspaces`) and claims the queued item of the active tenant
served longest ago; one writer lock still serializes every Git mutation on the platform. A tenant
created in the master console starts `provisioning`; the writer creates its repository with the
scaffold (`librarian.provision`), marks it active, and builds its empty index. Garden and the full
index rebuild are due daily per tenant, and an index never built is rebuilt at once.

The writer (`librarian.worker`) stops its own machine after `STIGMERGY_LIBRARIAN_IDLE_EXIT_S` idle
seconds, unless an active workspace has queued or retrying work or an index never built, or there is
an open layer erasure or workspace deletion, or a repository to create. Whatever queues work for it
(the capture queue, both erasures, and the master console's create, resume, and rebuild) calls
`kernel.wake`, which starts a stopped writer machine through the Fly Machines API with
`STIGMERGY_FLY_API_TOKEN` in the background, and starts it again if it stops within a minute of the
request. Without the token nothing wakes it, and nothing wakes it on a schedule: a daily run whose
time passed while the writer slept runs as soon as it wakes.

Two roles administer. Platform operators run the deployment from the master console
(`admin.console`): they create, suspend, resume, and delete workspaces, ask for an index rebuild,
answer access requests, and watch the writer. Workspace administrators run one workspace from its
backoffice: people with the administrator flag who are also members of `brain-admins`.

Platform operators are not roster principals. An operator signs in at `/admin` with an operator
token; the session carries the tenant it acts in, chosen in the master console, and inside it the
operator administers and reads every layer. It queues no captures, deletions, entity changes,
gardener runs, recompiles, contradiction resolutions, or erasures: those are the workspace
administrators', and the writer files them for the tenant's own people. Operator tokens open no MCP
session.

## Capture

The local bridge, Slack, and backoffice authenticate and acquire bytes. They then call the shared
capture service with actor, audience, provenance, artifact references, and optional contradiction
resolution intent. The durable request contains no duplicate document body or binary payload.

The queue holds captures, deletions, entity changes, and gardener runs. The state machine is
`queued -> processing -> landed|failed`; a retry, an expired lease, or a stopping writer returns a
claim to `queued`. The first processing claim starts one capture-wide budget whose absolute deadline
is persisted in the queue and shared by extraction, model calls, correction, gates, and at most one
automatic retry. Only explicit transient failures retry, after a fixed ten seconds and within the
remaining budget; a filing request that timed out or ended at its output limit spent the budget, so
its retry starts a new one. Deadline exhaustion and deterministic failures are terminal. A
backoffice retry resets the budget over the same immutable evidence. Idempotency is scoped to actor
and caller key. A crash after the Git commit receives a bounded recovery-only claim and is
reconciled by the operation marker and commit SHA rather than producing a second commit or another
model run. Queue leases are fenced by their exact claim timestamp, and expiry cannot race a writer
holding the publication lock.

Public URL acquisition resolves and revalidates every redirect, blocks non-public destinations,
streams under a byte limit, and sends no ambient credentials. Its source records sanitized original
and final URLs plus acquisition time. Private Drive acquisition is local: Google browser OAuth and
refresh tokens remain on the user's machine, while only exported bytes and typed non-secret Drive
provenance reach the cloud.

Slack acquisition has one whole-capture deadline, bounds profile lookup concurrency, and derives
message permalinks from one validated root permalink. Identical attachment bytes carried by several
messages are one artifact that each of those messages references. Timeout cleanup releases the
reservation so the same reaction can retry safely.

## Filing and gates

The worker extracts each artifact, stores readable derivatives, deterministically renders one source
page, retrieves only safe context, and requests one complete structured `FilingPlan`. Each capture
report records the size of that context against its byte budget, how many catalog lines, pages in
full, and evidence pages it held, and how many entries the budget dropped; the backoffice shows them
on the capture. The plan may create, update, or delete zero or more notes, concepts, entity, and
project pages (a consolidation updates one page and deletes the others); add or resolve
contradictions; or make no wiki mutation. A reusable idea, rather than the incoming source, is the
graph unit: related evidence stays cohesive, while independently reusable conclusions become
separate pages with meaningful links.

Once the plan passes the gates, the writer reads the `[[Entity|variant]]` links the plan added. A
variant that names no page yet, is not used for another entity, and does not share all its words
with another entity's name goes to the typed-decision classifier as one two-option question (`alias`
or `other`), at most eight per capture, one after another within ten seconds and inside the capture
budget. An answer at or above `aliases.THRESHOLD` writes the variant into the entity's `aliases`;
either every accepted alias lands or none does, and a gate violation undoes them all before the
layer projection and the commit. A restricted capture only names entities its layer stores whole, so
its variants never reach a public page. The capture report lists every decision, and a recompile
keeps the aliases no other entity claims.

The candidate tree must pass page, source immutability, reference, link, ACL-flow, entity-name,
contradiction, changed-path, and trusted-writer checks before the branch advances. A wikilink whose
target is missing, ambiguous, or outside the writing audience becomes plain text and is recorded in
the capture report; a page whose name another page already answers to (an entity's title or alias,
or a page of another kind) is not created and is recorded the same way. New and updated knowledge
pages must be cold-readable, use a matching H1, and place source attribution near their factual
conclusions. Only when the complete plan fails a deterministic writer contract may the same agent
return one corrected replacement plan over the same source and ACL-safe context. A filing makes at
most three model requests; the correction keeps the schema retry the plan did not use, so a
malformed correction is asked again once rather than failing the capture. A request still unanswered
after ten minutes is hedged by an identical one, and the first answer wins. A plan's summary or a
mutation's reason longer than the schema asks for is accepted whole. The correction replaces the
draft or nothing and is applied only after the usual writer gates. If it cannot pass, the immutable
source lands alone and the report sets `plan_rejected` with its `plan_rejection` reason. Every
landed operation records a friendly manifest plus a hash-verified exact patch.

## Visibility

Knowledge is stored in layers. `public` lives at the repository root; every other layer is a
roster group whose knowledge lives under `layers/<layer>/` with the same `wiki/` and `sources/`
layout. A capture files in exactly one layer and everything derived from it stays there.
Membership is the right to publish; `public` is open to every identity. `brain-admins` reads every
layer and may delete, but publishes only where it is a member.

The layer is decided once, at submission, before queueing (`server.routing`). A named layer wins,
and a Slack channel fixes its own. When the capture names none and the sender may publish to two or
more restricted layers, one call to the typed-decision classifier (`kernel.llm.choose`,
`typesafe/jev-1.13`) weighs those layers by their roster descriptions, plus a "no single layer"
option, against the capture's title, file names, address, and leading text readable without a
parser. A layer at or above `routing.THRESHOLD` wins; otherwise, and on any failure, after ten
seconds, or when two classifier calls are already in flight, the capture files in the sender's
default layer. The classifier is never offered `public` or a
layer the sender could not name. The decision and its probabilities are stored on the queue row
beside the envelope, so the librarian never sees them, and are reported to the caller.

The librarian never sees a layer. `knowledge.layered` builds, for the capture's layer, a scratch
tree shaped exactly like a single-layer repository: `public` pages as they are, the layer's own
pages with their audience cleared, and a page present in both merged into one. The unchanged writer
files the capture there. Projection then stores the result in the layer alone: its own pages in
full and, for a public page, only the units the public page does not already hold. A layer write
never changes a public page, and a public write never reads a restricted layer: a public name that
makes another layer's link ambiguous leaves that link unresolved for the layer's readers instead of
refusing the write. The writer re-checks the author's membership in the capture's layer before
filing. Garden, recompile, and contradiction resolution run through the same views.

Readers address pages by their virtual path (the path without `layers/<layer>/`). The index keeps
one row per stored page with its virtual path; an SQL audience clause
(`search.positional_audience_clause`) filters rows before any cap, and reads fuse the visible rows
of one virtual path into one page (`layers.fuse_rows`), marking units a restricted layer
contributed. Links resolve among a page's own layer and `public`; a name that reaches two different
pages there reads as plain text. A Slack channel answers at its layer narrowed to the asker's. A reader's responses are identical
whether or not other layers hold knowledge about the same names, and guessed hidden paths, entity
titles, aliases, captures, contradictions, or diff records reveal no existence.

A principal's layers come from the roster on every request: MCP bearer tokens, Slack emails,
the writer's actor scope, and backoffice sessions all read the same rows, so a revoked token, a
disabled person, or a changed group takes effect on the next request. Administering the backoffice
is a separate flag held only by people who are also in `brain-admins`; agents can never administer.

## Backoffice

A workspace identity has one kind of token; platform operators have their own (see Tenancy). It
connects the identity's MCP clients and signs it in at `/admin`. An agent is never an administrator,
so its session reaches only the wiki it may read; an agent should run under its own agent identity
rather than a person's token. Signing in yields an HttpOnly, Secure, SameSite=Strict session cookie
scoped to `/admin` (8 hours idle, 7 days absolute); the API itself accepts sessions, never a bearer
token. Every state change also needs the page's `X-Stigmergy-CSRF` header and a same-host `Origin`.
Operational and roster endpoints exist only for administrators and answer everyone else exactly like
an unknown route. Failed authentication is throttled per client address across MCP and the
backoffice; a valid credential always passes.

The console is a React application in `web/`, built by Vite into the package's static directory
(`make web`; CI and the image build it too). The shell is served with a per-page nonce that only the
components' runtime style elements may use; scripts load only from the console's own origin.

The bare host opens the backoffice sign-in, or, where the operator built its own landing page into
the image (`web/landing.html` and `web/src/landing/`, never committed) and set `STIGMERGY_LANDING=on`,
that page as a second entry of the console build. A landing page may post to the access-request
endpoint, which stores an email in `access_requests` for operators to answer in the master console
and the worker to expire. The operator's own legal pages are Markdown
files in `STIGMERGY_LEGAL_DIR`, never shipped with the platform: `admin.routes` returns a page's
HTML, raw HTML escaped, at `/legal/<name>` and, with a landing page on, serves the landing at
`/<name>`. Every response carries `X-Robots-Tag: noindex, nofollow`.

The wiki reads the index through `BrainService.browse_pages`, `browse_page`, and `browse_graph`, the same scoped,
audited, rate-limited path MCP uses. Markdown is rendered on the server with raw HTML and images
disabled, only `https` links survive as external links, and the result is sanitized against an
allowlist. Wikilinks and source citations become internal links only when their target is visible
to the reader.

`index.measures` defines the graph measures once: pages by type, links per page, dead links, orphan
and multi-source entities, cited paragraphs, entities without aliases, thin entities, and hubs, for the
whole brain, `public`, and each layer fused with `public`. Every rebuild and incremental push that
moves the indexed commit stores one snapshot per scope; history is kept, and a failed snapshot
never fails indexing. The Index health screen reads current values, series, drill-downs, and a
constellation of the hubs from `/admin/api/graph-measures`, administrators only. The measures are
analytics: they create no task.

## Entities

An entity is an ordinary librarian-owned page under `wiki/entities/<Title>.md` (in its layer's
folder) with `entity_type` and `aliases` in its frontmatter and its layer as its only visibility. The wikilink is the only
relation: notes and concepts link the entities they involve, entity pages link the entities they
relate to and the pages they appear in, and the index derives backlinks. There is no registry, no
opaque ID, and no anchor list.

Titles and aliases are unique among the entity pages of one layer after normalization; the same
title may have a page in several layers, which readers see as one entity. The librarian receives a
catalog of every visible page with its aliases, reuses or links an existing page, and disambiguates
ambiguous names in the title. The writer drops a creation whose name is already claimed and reports
it; it never merges on similarity.

`list_pages` lists every visible page that matches its filters (type, entity type, layer, a
period of cited sources, or the pages linking a page or entity), fused by virtual path, 50 per call
with a cursor and the same answer whether or not hidden layers hold content. `describe_entity` resolves a path, title, or alias
among visible entity pages and returns the page with the bounded visible pages that link it and
their sources; unknown, hidden, and ambiguous references share one neutral response. Search resolves
up to four visible entity names in a query, boosts each entity page and the pages linking it, and
expands the lexical query with their titles and aliases.

Rename, merge, and delete are workspace administrators' operations, filed by the writer. Rename
keeps the old title as an alias and rewrites links. Merge folds the listed pages into the first one
(body sections, alias union, source union, link rewrite) and requires an exact same-identity
assertion present in an immutable source that binds every page by a complete distinct name; pages
with different types or visibility cannot merge. Delete removes the page and turns links to it into
plain text.

## Contradictions and gardening

A strict Markdown marker stores an unresolved contradiction with a stable ID, explanation, dated
claims, and source citations at the narrowest safe audience. It is a healthy corpus state. Prose
listing both claims is not a contradiction; only the marker is, and the filing context lists each
candidate page's markers so the librarian judges by that list. A proposal the writer cannot place
safely is dropped and recorded in the capture report while the rest of the plan lands. The
backoffice derives its list from current indexed Markdown; a resolution form queues ordinary new
evidence.

The linter is pure. Repair primitives are bounded transformations. The scheduled gardener runs both
inside the writer, records the other names the corpus already links entities by, applies the normal
gates, and lands at most one commit. It stores a run summary, never a permanent assignment to a
person. A recompile, asked by a workspace administrator, uses the same `GardenRequest` path in
`recompile` mode to rebuild derived notes, concepts, entities, projects, and links from the complete
immutable-source corpus in an isolated worktree. It preserves sources. Every ACL-safe prior page
must be explicitly reconstructed or tombstoned by each backing source before it may disappear;
sources with no prior derived page may still yield an empty plan. Recompile aborts the complete
candidate on any failed source or gate, and records its commit under the existing `garden`
change-ledger trigger. Due garden checks and expired-upload cleanup run under continuous queue load
as well as while idle.

## Erasure, deletion, and refiling

Erasing a layer (`specs/data-erasure.md` §3) is the one operation that rewrites history.
`ops.erasure` keeps requests in `layer_erasures`, at most one open per layer; while one is open the
layer is closed: capture refuses it and the queue claims none of its captures. The writer loop runs
`ops.erase_layer` under the writer lock before its next operation. The rewrite's commit map is stored
before a leased, atomic push. Every row change then happens in one transaction that also lists the
evidence objects left without a reference (`capture.references.is_live`), deleted once it commits.
A stopped job resumes from the stored map. Before the push a request fails after three attempts and
the layer reopens; after it the request is retried with backoff and never fails, and no other
erasure of that workspace starts while one's recorded rewrite may be pushed but unfinished.

Deleting a workspace (`ops.erase_workspace`) removes it whole and leaves every other as it was. An
operator asks from the master console for a suspended workspace that does not own the Slack app;
the request lives in `workspace_erasures`. The writer, under its lock, deletes the GitHub repository
first, so the slug cannot be reused while the old knowledge exists, then every tenant row and the
`tenants` row in one transaction, then the workspace's HNSW indexes, the evidence objects no other
workspace references, and its checkout. Every step is safe to run again; once the repository is gone
the request retries with backoff until it finishes.

Refiling (`ops.replay_workspace`) is the one sanctioned copy between workspaces. An operator exports
a workspace's layers, people, and landed captures whose sources nobody deleted, and imports them
into a fresh workspace, where the writer files every capture again over the same content-addressed
evidence. A handover then moves the live tokens and sessions of the people both share to the target
and suspends the source.

## Read path

Full rebuild and webhook indexing share the same canonical corpus selector, and both write one
tenant's rows. Notes, concepts,
entities, projects, and sources are indexed, with entity aliases in the lexical document. Search fuses four ranked lists with
reciprocal-rank fusion and contract factors: Postgres full-text, embeddings of page fragments, pages
linking every entity the query names, and the one-hop neighbors of the best hits, damped by visible
degree. A page of at most 400 words stays one fragment; longer notes, concepts, and projects are
cut at their H2 and H3 headings and longer sources at every heading, then into overlapping windows,
all bounded in bytes, so no embedded text exceeds the model's input. Entity pages stay whole unless
the embedding model refuses one for its length. Each
audience of each tenant has its own HNSW index, and a scoped reader's vector arm scans only the
indexes of its own tenant's audiences. The vector arm keeps each page's best fragment; a hit on a
page cut in several carries `fragment: <path>#<n>`, and `read_page` with that reference returns the fragment and its neighbours,
rendered as the fused page renders its rows. The answer verifier checks a quote against the head of
the page and the fragment windows the run read. Each
hit carries a one-sentence summary and its visible outbound links. `ask` runs the first search
before the model's first turn, and its agent reads up to five pages in one call; a search states
how many pages it ranked, and the agent enumerates with `list_pages`. Each index row carries the
dates of the sources it cites (a source: its own `occurred_at`, else `captured_at`).
`search_brain`, `read_page`, `ask`, `list_pages`, and `describe_entity` all apply the same
visibility policy. A query embedding that takes over three seconds drops that request's vector arm,
the other arms rank as usual, and `search_brain` reports `vector_search: "timed_out"`; indexing remains strict and never stores a partial vector state. HTTP requests check
out connections from one bounded serving pool. HTTP request and Slack connections bound each
Postgres statement; a statement
cancelled while `ask` recovers evidence ends in the ordinary budget refusal and discloses nothing
the cancelled recovery gathered.
