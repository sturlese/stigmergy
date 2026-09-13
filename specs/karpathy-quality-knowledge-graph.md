---
status: ready
owner: platform
date: 2026-09-13
---

# Karpathy-quality knowledge graph compilation

## Problem

Stigmergy reliably preserves immutable evidence and can produce substantial explanatory prose, but
its current filing contract optimizes the shape of a filing plan rather than the usefulness of the
resulting knowledge graph. It normally biases one explanatory source toward exactly one concept
mutation, treats entity recall as a count-and-anchor problem, permits pages without meaningful wiki
links, records ordinary provenance only at page granularity, and exposes entity descriptions as
navigation metadata rather than as readable knowledge.

The result can preserve all source text while still producing a graph that is less reusable,
connected, navigable, and enrichable than the local Hippocampus/Karpathy reference workflow. The
existing filing evaluation does not detect this failure because it scores mutation signatures,
entity membership, and anchors without scoring page bodies or the usefulness of entity reads.

## Objective

Make Stigmergy compile evidence into a small, rich, connected, source-grounded knowledge graph with
editorial quality at least equal to the Hippocampus/Karpathy reference on an identical corpus and
initial graph. The unit of graph structure is a reusable idea, not an input source. Preserve
Stigmergy's immutable evidence, stable opaque identities, scoped claims, write-time ACL safety,
serialized Git writer, and fast deterministic read boundary.

## Users and primary flows

### Filing new evidence

An authorized person or agent submits one source. The librarian reads the complete extracted source
and safe existing context, identifies durable conclusions and materially reusable identities, then
creates, updates, consolidates, or deletes as many normal knowledge pages as the evidence justifies.
It prefers enriching existing pages, but it does not collapse independently reusable ideas merely
because they arrived in one source. The candidate graph lands only after contract, provenance, link,
entity, ACL, and minimum editorial gates pass.

If the source contains no durable knowledge, the immutable source lands without a wiki mutation. If
a proposed knowledge mutation fails a gate and bounded repair cannot correct it, the source remains
available for retry and the processing report must not represent the knowledge graph as successfully
compiled.

### Reading a concept or note


A reader receives a self-contained page that can be understood without reopening its source. A
concept explains what the idea is, how it works, why it matters, appropriate examples, and its
meaningful connections when the evidence supports those sections. A note preserves a contextual
conclusion, decision, or event in declarative prose. Claims carry local source attribution, and
connections are readable relationships rather than unexplained link lists.

### Reading an entity

A reader asks for an entity by a visible name, alias, or stable ID. One `describe_entity` call returns
enough ACL-visible evidence to present a useful dynamic dossier with identity, relevant facts,
knowledge pages, relationships, and sources. The raw `wiki/entities/ent_<uuid>.md` record remains an
internal identity primitive and does not become a stored dossier.

### Empty, bounded, and error states

An unknown and a hidden entity retain the same absence shape. An entity with no visible anchored
knowledge returns its visible identity and an explicit empty-knowledge state. A large entity result
is deterministically capped, reports truncation, and provides paths for further reading. A failed
filing or repair records a typed operational failure without committing a partial graph.

## Editorial contract

1. A source may produce zero or more page mutations. No exact or default one-page quota exists.
2. A new page represents one independently reusable conclusion or concept that is likely to be
   searched, linked, or enriched again.
3. The librarian updates an existing relevant page before creating a parallel page.
4. Closely related supporting evidence remains in one cohesive page; independently reusable ideas
   become separate, explicitly connected pages.
5. Concept pages are self-contained and cover definition, mechanism, significance, examples, and
   connections when supported. Headings may adapt to the material; template words are not a content
   substitute.
6. Note pages are self-contained, declarative, and preserve the decision, event, or synthesis rather
   than the input's conversational or document structure.
7. Every newly introduced factual conclusion has local source attribution. Page-level source
   metadata remains authoritative but is not the reader's only provenance signal.
8. Every knowledge-page link has an intelligible relationship in prose. Relevant existing concepts
   are linked; lexical co-occurrence alone never creates a link.
9. Entity creation uses expected future reuse as its bar. Authors and identities with durable,
   entity-specific actions, claims, decisions, ownership, or results qualify. Incidental products,
   tools, examples, benchmarks, models, and named techniques remain prose unless the source adds
   reusable entity-specific knowledge.
10. Every selected entity anchor has a concise source-supported relationship stated in the page
    body. Stable IDs remain the machine relationship; readers see visible names.
11. The librarian performs a source-first inventory, drafts the smallest sufficient graph, then
    audits conclusion coverage, identity precision and recall, provenance, and connectivity before
    returning the structured plan.
12. Prompt guidance, not deterministic named-entity extraction or topic splitting, owns editorial
    judgment. Code enforces only objective contracts and obvious minimum-quality failures.

## Acceptance criteria

1. **KG-01:** The filing instructions and live knowledge repository contain no rule that defaults an
   explanatory source to exactly one concept mutation or otherwise treats the source as the unit of
   graph structure.
2. **KG-02:** A frozen evaluation with an existing related concept proves that the librarian can
   update that concept and create or update another independently reusable concept from one source,
   with reciprocal meaningful connections and no duplicate page.
3. **KG-03:** A frozen evaluation from an empty graph proves that closely related material remains
   cohesive when splitting would create a redundant stub; page count alone neither passes nor fails
   the case.
4. **KG-04:** Filing evaluations score required durable conclusions in page bodies, unsupported or
   forbidden conclusions, meaningful page connections, local source attribution, entity precision
   and recall, and entity-to-page relationship coverage.
5. **KG-05:** A plan with a heading-only or generic placeholder body fails evaluation and cannot pass
   merely by returning expected titles and entity anchors.
6. **KG-06:** Created and updated concepts and notes are readable cold, contain a matching H1, and
   express supported connections in ordinary prose. Objective violations enter the existing bounded
   repair path before commit.
7. **KG-07:** The Harness Engineering reference case retains every required durable conclusion,
   links every qualifying identity, rejects incidental identity nodes, and produces meaningful
   concept connectivity under both empty and seeded initial graphs.
8. **KG-08:** `describe_entity` returns the visible identity plus bounded content-bearing knowledge
   evidence, visible sources, and readable relationships sufficient for a client or agent to render
   `What / Who`, `Facts`, and `Connections` without a second retrieval call.
9. **KG-09:** Entity evidence returned by `describe_entity` is composed only from ACL-visible anchored
   notes, concepts, and sources. Hidden and unknown entities remain indistinguishable, and capped
   output discloses no hidden counts, titles, names, or timing-dependent branch.
10. **KG-10:** Entity machine pages remain minimal stable identity records; no fact dossier, generated
    entity view, new database table, or per-read LLM call is introduced.
11. **KG-11:** Search by a visible entity name continues to retrieve its anchored knowledge, while
    explicit concept links and backlinks make the same graph navigable by page relationships.
12. **KG-12:** The mechanical linter rejects unresolved or visibility-unsafe links, missing sources,
    entity anchors without a visible relationship in the body, placeholder or effectively empty
    knowledge bodies, and invalid local source references without attempting subjective topic
    extraction.
13. **KG-13:** The same source corpus and initial graph are run through Hippocampus and Stigmergy.
    Evidence records every current case inside every run rather than an aggregate score. Stigmergy
    must pass every hard faithfulness, coverage, provenance, connectivity, entity, and ACL criterion;
    Hippocampus is a comparative baseline whose complete evidence remains replay-verified even where
    it honestly fails Stigmergy-specific writer gates. A blind pairwise editorial review finds no
    material Stigmergy regression before deployment.
14. **KG-14:** Production continues to use `openai/gpt-5.4` through the approved Azure
    OpenRouter route. The lowest reasoning level that passes every hard reference case is selected
    only after at least three independent production-equivalent repeats per case under the configured
    two-request budget; runtime route, observed requests, derived schema retries, semantic repairs,
    elapsed time, available usage, raw gates, and content-addressed output references are recorded.
    Every result binds the case and fixture hashes and the exact brain-prompt commit/hash; its
    source-free original/effective plan payload is content-hashed and replayed by the admission gate.
    A stronger model may be used only as an offline evaluation judge.
15. **KG-15:** Existing immutable production sources are preserved while derived notes, concepts,
    links, and entity anchors are rebuilt with the new compiler. Source-backed identities remain
    retained even when no derived page anchors them; lifecycle removal is an explicit controlled
    operation, never a compiler liveness heuristic.
16. **KG-16:** Focused tests, the complete keyless suite, both repository CIs, deployment health, a
    live multi-page filing, entity description, search, read, and ACL-isolation probe all pass before
    the change is declared complete.

## Excluded scope

- Replacing Git and Markdown with a graph database or fact store.
- Persisting rich facts inside opaque entity identity pages.
- Materializing one entity dossier per audience or generating a `views/` tree.
- Calling an LLM during `describe_entity`, search, or ordinary page reads.
- Deterministic NER, ontology induction, automatic relation typing, or code-owned topic splitting.
- Reproducing Hippocampus metadata fields such as tags, domain, role, or `related` when body links and
  the existing index already express the required behavior.
- Byte-for-byte reproduction of Hippocampus page names or prose.
- Relaxing source immutability, ACL flow, identity evidence, contradiction, or Git writer invariants.

## Contract transition

### Old contract

`describe_entity` returns identity metadata plus a navigation-only list of anchored page and source
paths. Filing evaluation fixes expected mutation signatures and does not inspect body semantics.
The librarian normally emits one cohesive knowledge mutation for one explanatory source.

### New contract

`describe_entity` additively enriches each visible knowledge item with bounded content and readable
relationship evidence, plus explicit cap metadata. Existing identity, `knowledge`, `knowledge_note`,
and `sources` fields remain available. Filing permits zero or more semantically justified page
mutations and is evaluated on knowledge content and topology rather than exact page count alone.

### Consumers and compatibility

The cloud MCP server, local bridge, answer agent, master-only backoffice entity dossier, tests,
documentation, and Codex/Claude Stigmergy skills consume the entity description. The response
transition is additive so existing consumers remain valid; first-party consumers move to the richer
fields in the same release.
No persisted entity schema or database migration is required. Normal Markdown pages remain readable
by existing versions.

### Migration and backfill

The platform owner updates the frozen and live librarian instructions together. A controlled rebuild
recompiles derived knowledge from immutable sources in an isolated worktree, validates it, and then
advances the knowledge repository. Entity IDs are reused when identity evidence remains valid. The
process is idempotent and reports source count, page mutations, retained and removed identities, link
health, and final commit.

### Rollout and rollback

Land the additive reader contract and first-party consumers before or with the new filing prompt.
Update the live knowledge repository, rebuild derived knowledge, rebuild the index, then deploy every
affected process group. Rollback restores the prior platform image and prior knowledge-repository ref;
immutable sources make recompilation repeatable. Do not remove legacy response fields until all known
external consumers have explicitly migrated, which is outside this change.

### Security and observability

All entity content is fetched through the existing scoped page index and visibility policy. Caps are
applied after visibility filtering and reveal only visible totals. Filing reports and evaluation
history record model, reasoning, configured request budget, observed request count, derived schema
retries, semantic-repair count, elapsed time, available usage, raw gates, output hash, mutation count,
entities, links, and failure class without logging restricted content.

## Resolved decisions

- The reusable idea, not the submitted source, is the unit of graph structure.
- Anti-fragmentation is an editorial reuse test, not a page quota.
- Rich entity knowledge is a reader-scoped projection over normal pages, not a stored dossier.
- Page bodies carry human-readable provenance and relationships; frontmatter retains machine anchors.
- Production stays prompt-first and uses the fast approved open model.
- Quality is guaranteed operationally by hard reference gates and explicit failure, not by claiming
  that arbitrary model prose is infallible.
