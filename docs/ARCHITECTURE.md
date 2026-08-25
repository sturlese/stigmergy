# Architecture

## Authorities

| Data | Authority | Mutation rule |
|---|---|---|
| Original artifacts and exact patches | private object store | content-addressed; deleted only when no live reference remains |
| Readable evidence | Git `sources/YYYY/MM/<capture-id>.md` | append-only except explicit deletion |
| Current knowledge | Git `wiki/notes` and `wiki/concepts` | one serialized writer |
| Entity identity | Git `wiki/entities/<opaque-id>.md` | entity primitives only |
| Entity registry | Git `ops/entity-registry.json` | deterministic derivative of entity pages |
| Queue, runs, and change metadata | Postgres | operational and append-only audit state |
| Search index and contradiction list | Postgres derived from Git | rebuildable |

## Capture

The local bridge, Slack, and backoffice authenticate and acquire bytes. They then call the shared
capture service with actor, audience, provenance, artifact references, and optional contradiction
resolution intent. The durable request contains no duplicate document body or binary payload.

The state machine is `queued -> processing -> landed|failed`. Technical failures retry within a
bounded lease policy. Idempotency is scoped to actor and caller key. A crash after the Git commit is
reconciled by the operation marker and commit SHA rather than producing a second commit.

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

The worker extracts each artifact, stores readable derivatives, deterministically renders one
source page, retrieves only safe context, and requests one structured `FilingPlan`. The plan may
create, update, consolidate, or delete notes/concepts; propose identity claims; add contradictions;
or make no wiki mutation.

The candidate tree must pass page, source immutability, reference, link, ACL-flow, entity,
contradiction, registry, changed-path, and trusted-writer checks before the branch advances. Every
landed operation records a friendly manifest plus a hash-verified exact patch.

## Visibility

`server.acl.visible` owns reader visibility, `kernel.acl.flows_into` owns safe information flow, and
`knowledge.write_guard` composes them for mutations. Organization-wide knowledge has no ACL. A
restricted output may use only evidence visible to all of its readers. A restricted capture may
create a restricted companion page but cannot rewrite an open page. Guessed hidden paths, entity
IDs, aliases, captures, contradictions, or diff records reveal no existence.

## Entities

Entity IDs are immutable opaque UUIDs. Names are claims with scope, provenance, actor, and time.
Names may be confidential. Entity files contain no facts or dossier body; facts live in ordinary
notes and concepts anchored to the ID. Reader projections choose only visible claims, while
`describe_entity` composes visible anchored pages and sources.

Each filing proposal represents one identity: one current name, source-explicit aliases, and an
optional paired external namespace/ID. The filing context lists visible identities' external ids
and every registry name already in use, so one registry keeps one spelling across captures and
audiences. Alias claims are accepted only when one extracted readable
artifact contains the complete normalized alias; generated source markup is excluded. Proposal IDs
remain positional; textual names resolve to a set of candidate IDs and can anchor a page only when
that set has one member. Shared aliases are therefore ambiguous regardless of proposal order. Two
proposals that strongly match one identity reject the plan and preserve only the archived source.

Strong evidence can reuse or merge an identity. An explicit merge accepts only a shared external ID
verified on every selected record or an exact same-entity assertion verified in an immutable source.
Source assertions must bind every record through complete, distinct, non-contained names; ambiguous
names require external-ID evidence. A rationale, name collision, or fuzzy resemblance cannot
authorize a merge.
Rename adds a preferred claim and retains the prior same-scope name as an alias. Merge preserves
claim provenance and ACLs, rewrites anchors, records redirects, and removes absorbed entity files in
one commit. Explicit deletion removes the identity and anchors without automatically deleting
substantive knowledge.

## Contradictions and gardening

A strict Markdown marker stores an unresolved contradiction with a stable ID, explanation, dated
claims, and source citations at the narrowest safe audience. It is a healthy corpus state. Prose
listing both claims is not a contradiction; only the marker is, and the filing context lists each
candidate page's markers so the librarian judges by that list. A proposal the writer cannot place
safely is dropped and recorded in the capture report while the rest of the plan lands. The
backoffice derives its list from current indexed Markdown; a resolution form queues ordinary new
evidence.

The linter is pure. Repair primitives are bounded transformations. The scheduled gardener runs
both inside the writer, applies the normal gates, and lands at most one commit. It stores a run
summary, never a permanent assignment to a person. Due garden checks and expired-upload cleanup run
under continuous queue load as well as while idle.

## Read path

Full rebuild and webhook indexing share the same canonical corpus selector. Notes, concepts, and
sources are indexed; raw entity pages are not. Search combines Postgres full-text and embeddings
with reciprocal-rank fusion and contract factors. `search`, `read`, `ask`, `list_entities`, and
`describe_entity` all apply the same visibility policy. A timed-out query embedding degrades that
request to the same ACL-filtered full-text arm; indexing remains strict and never stores a partial
vector state. HTTP request and Slack connections bound each Postgres statement; a statement
cancelled while `ask` recovers evidence ends in the ordinary budget refusal and discloses nothing
the cancelled recovery gathered.
