# ADR 037 — the second-brain comparison: what stays, what is declined, and the seventh entity type

- **Status**: accepted
- **Date**: 2026-08-16
- **Closes**: issues #33 (frontmatter fields per shared page type), #34 (entity taxonomy versus
  governance), #35 (page type vocabulary)
- **Related**: [ADR 016](./016-human-loop-and-entity-governance.md) (governed entity birth — the
  model this record was asked to reconsider and keeps), [ADR 021](./021-views.md) (the per-entity
  view, which is why `project` is an entity), [ADR 026](./026-the-purge.md) D2 (the removed
  `verification` field, whose ghost the declined `confidence` is),
  [ADR 030](./030-server-side-entity-minting.md) (the three doors a mint now completes from).
  Narrative: [`docs/reference/page-contract.md`](../reference/page-contract.md),
  [`docs/reference/brain-page-contract.md`](../reference/brain-page-contract.md).

## Context

Three issues compared this system, field by field and type by type, against the reference
personal-second-brain pattern: an inbox drained into an LLM-structured wiki, six page types
(`source`, `entity`, `concept`, `project`, `note`, `meta`), flat frontmatter, no curated identity
registry, entities minted opportunistically during ingest and reconciled afterwards by a linter.

The comparison is worth answering rather than waving away, because **the two systems share an
ancestor**. The README names it: Andrej Karpathy's LLM wiki, the note this brain restates in its
own first section. The reference pattern is that note built for one person's capture; this is that
note built for a company's, where a name resolves the same way for everybody or it resolves for
nobody. Two descendants of one idea disagreeing is a finding; two unrelated systems disagreeing is
a coincidence.

And the taxonomies had already converged without either side coordinating. `entity_type` was the
same closed six on both — `person`, `organization`, `product`, `tool`, `repository`, `place` — and
the four shared page types carried the same universal base (`type`, `title`, `created`, `updated`,
`tags`, `status`, `related`, `sources`). **So the divergence actually up for evaluation was never
the vocabulary; it was the governance layer sitting on top of it,** plus a short list of fields
and two page types one side has and the other does not.

Each of those is settled below. Nothing here is a TODO: a divergence this record declines is a
recorded divergence, and the next reader comparing the two systems should find the answer here
rather than re-derive it.

## Decisions

### D1 — Governed entity birth stays (#34)

An entity is born from a human steward, through the registry, one at a time, from any of the three
doors ADR 030 opened. The reference pattern's alternative — the ingesting agent decides ad hoc
whether a name "clears the bar", and a linter catches the duplicates afterwards — is not imported.

**The argument is multi-writer, not rigor for its own sake.** In a one-person brain the coiner of
a name is also its only reader, so a bad coinage is a mistake the person who made it will meet
again and fix. In a company brain the coiner is somebody else: a name minted by the agent draining
one person's capture becomes the wikilink target every other person's page must spell, the
registry key every later anchoring decision resolves against, and the identity a view is generated
for. **A vocabulary that is agreed is a vocabulary; a vocabulary that is coined is a backlog.**
Lint-after-the-fact reconciliation is exactly the shape that does not survive the change of
audience — the duplicate is found after N pages already anchored to the wrong half of it, and
merging two identities is a decision this system deliberately does not have — `entities.generator`
says so in its own first paragraph: *"this module governs entity BIRTH; a MERGE is a different
decision."*

**The friction the issue worried about already has levers, and they are the ones to reach for
first.** A page that is genuinely about nobody in particular declares company-wide scope with a
written reason rather than inventing an identity to hang itself on; a capture that names one
unresolved thing parks with a single ask-back instead of guessing; a capture that names several
parks with all of them, since the plural park landed. Those three absorb most of what an
opportunistic mint would have absorbed, without the write.

**Re-open on measured pain, not on principle.** The signal that would justify revisiting this is a
triage backlog somebody can count — parked captures accumulating faster than stewards clear them,
visible in the admin console's own queue and in the weekly digest's governance section. "The other
system is more permissive" is not that signal, and this record exists so the next person arriving
with it starts from the measurement.

### D2 — No `meta` page type (#35)

The reference pattern's `meta` holds pages about the vault itself: lint reports, dashboards,
system commentary. It is declined, and not because `view` is a near-enough substitute.

**System state about this brain lives in Postgres, the admin console and the weekly digest — never
in the corpus.** The distinction the corpus draws is not "about the brain versus about the world",
it is **retrieval**: a corpus page is knowledge somebody may search for and be answered from. A
lint report is an operational fact with a shelf life measured in hours, and putting it in the
corpus makes it an indexed, ACL-scoped, wikilink-able document that `ask` may retrieve and cite —
so the brain would eventually answer a question about the company with the state of its own
gardener sweep. That is the failure mode, and it is not hypothetical prose: `pages_index` cannot
tell an operational page from a knowledge page, because nothing in the page says which it is.

The operational surfaces already exist and are already the right shape: the gardener's checks and
the digest report findings to humans on a schedule, the admin console shows queue and job state
live, and both read Postgres. **A `meta` page would be a fourth reporting surface whose only
advantage over those three is that it is retrievable — which is precisely its defect.**

### D3 — No `project` PAGE type; `project` becomes the seventh ENTITY type (#35)

Both halves of this are the decision, and the second half is what makes the first one honest.

**Rejected: `project` as an eighth page type.** `PAGE_TYPES` is a placement table, and every row
answers the same three questions: which folder it lands in, whether the fast lane may CREATE it or
it is governed (and if governed, the reason, in the submitter's own language), and — for the
governed ones — which single writer stamps it. An eighth row would owe all three answers, and the
honest answer to "who writes a project page" is "whoever is working on it, whenever": a
hand-maintained status document that goes stale silently and that no gate can judge, because
staleness is not a property of one diff.

**An ongoing initiative in a company brain is a governed identity, and it already behaves like
one.** People anchor notes to it, decisions are made about it, meetings are held on it, and its
name is a wikilink other pages must spell consistently. Every one of those is what `entity:`
means. So `project` joins `ENTITY_TYPES` as its seventh value and inherits the whole apparatus:
birth by a steward, a registry id nothing can hand-write, alias resolution, and anchoring.

**The part the reference pattern hand-maintains, this system regenerates.** ADR 021's per-entity
view is a derived rollup — the pages anchored to an entity, plus a written synthesis, rebuilt on a
schedule and never authored directly. A project entity therefore gets a living dashboard for free,
and the split is clean: **the notes, decisions and meetings anchored to the project are its
history; the view is its status page.** The reference pattern's `project` page is both at once,
which is why it goes stale — the same document is asked to be an append-only record and a current
summary. Here the record cannot go stale (it is history) and the summary cannot go stale (it is
regenerated).

**This deliberately diverges the entity enum from the reference pattern's six, and that is the
first place the two systems' `entity_type` values differ.** The divergence is the point: the
reference pattern has nowhere to put a project except a page type, because it has no governed
identity layer to put it in.

### D4 — The frontmatter fields, one at a time (#33)

Issue #33's table is answered field by field, because a table answered in aggregate is a table
nobody can act on:

- **entity `role`** — already exists. The issue's table predates it; `stigmergy-entities` writes
  `role` on every entity page and `birth._clean_role` validates it. No gap.
- **concept `domain` and `aliases`** — declined. `tags` already carries what `domain` would carry,
  with the advantage of being multi-valued and already indexed. `aliases` on a concept asks the
  retrieval layer to do identity matching, and retrieval here is hybrid: lexical plus vector, so a
  synonym is found by the half that was built for synonyms. **Identity machinery is reserved for
  entities** — that reservation is what keeps "resolve this name" a single question with a single
  answer, instead of a registry lookup plus a per-page alias scan over a second population.
- **source `confidence`** — declined. A human-asserted trust grade is the removed `verification`
  field's ghost, and ADR 026 D2 removed that one for a reason this proposal walks straight back
  into: *a field nothing computes is not stamped*, and a field that survives as a stale value on
  old pages is read as a fact about the page today. Trust signals here are **computed** (`tier`,
  written by `page.stamp_source_fields`) or **governed** (who submitted it, which door it came
  through) — never self-graded by the writer.
- **source `author`** — declined as frontmatter. `submitted_by` is the governed analogue and it is
  server-stamped, so it cannot be claimed. A document's own authorship is a fact *inside* the
  document, and it belongs in the page body where it can be attributed and quoted like any other
  extracted fact. A frontmatter `author` would be an unverifiable field sitting beside a verified
  one, which is worse than not having it.
- **source `origin`** — declined. The evidence store plus `content_hash` is the stronger answer to
  the same question: `origin` names a path back to a raw inbox file, which is a string that stops
  resolving the moment the inbox is cleaned, while the evidence blob is content-addressed and the
  hash proves the page and the blob are the same bytes. Replacing a proof with a pointer is not an
  improvement.
- **`note_type: decision`** — not ported. A decision is a full page type here, with its own folder,
  its own anchoring rule (per decision page, ADR 020) and its own place in the meeting flow's page
  set. Folding it back into a `note_type` value would demote a first-class thing to a label.

## Consequences

- **`ENTITY_TYPES` is seven values.** `entities.generator.ENTITY_TYPES` gains `project`;
  `review_kinds.ENTITY_TYPES` — the restatement that exists so the Block Kit renderers depend on
  nothing — follows, and the architecture test that pins the two to each other is what makes
  "follows" mechanical rather than remembered.
- **Every surface that offers a type picks the new one up without being edited**, because none of
  them spells the list: the Slack mint modal builds its `static_select` from the restatement, the
  admin console ships the list to its front end through `meta.entity_types`, and
  `stigmergy-entities --type` derives its `choices` from the constant. The sites that DID spell it
  are prose — the `review_decide` tool description, the librarian reference's writer table, and the
  `entity_type` comment in the knowledge repo's own `ops/templates/entity.md`, which is the
  human-facing source of truth the generator's comment defers to. All are corrected.
- **`PAGE_TYPES` is untouched**, and so are the fast lane's creatable types and the knowledge
  repo's contract linter. This record adds an entity kind, not a page kind — the one distinction
  D3 exists to draw.
- **No migration and no backfill.** The change is a widening of a closed set: every value that was
  accepted before is accepted now, every page already written parses unchanged, and no persisted
  registry entry, `review_decisions` row or `pages_index` column changes shape. A deployment
  running the previous image simply refuses `project` at the mint gate, by name, with the same
  actionable message it gives any unknown type.
- **Everything else in the three issues is a recorded divergence, not a backlog item.** D1's
  re-open criterion is the only thing here that names a future trigger, and it names a measurement.
