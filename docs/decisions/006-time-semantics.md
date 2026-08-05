# ADR 006 — Time as a first-class dimension: provable as-of and a supersedes chain

**Status:** accepted · 2026-07-13

## Context

The #1 real-world failure of a company brain is serving stale truth with confidence. The time
model was thin: an optional LLM-chosen `date`, a `period` parsed from the folder path, and
`extracted_at`. Worse, only *exact* duplicates were deduplicated (sha256): "Q1 report FINAL"
with one corrected figure coexisted with its draft as two equally-ranked pages, and retrieval
picked whichever embedded better — the ambiguity the product exists to remove.

## Decision

Two mechanisms, each split along the usual doctrine line:

**1. `as_of` — content validity time, at the finest PROVABLE granularity.**
The LLM proposes a content date (judgment); pure code decides how much of it the evidence
supports: the full date only when it literally appears in extraction/filename/path, else
year-month when a compatible signal exists, else the quarter, else the bare year, else nothing —
falling back to the entity's deterministic path period. A trust field must never say more than
the document can prove. `as_of` lands in frontmatter and in the derived index; the answer layer
ranks current truth with it.

**2. Near-duplicate versions become an explicit `supersedes` chain.**
- *Deterministic candidates*: same entity/unit group + version-marker-stripped name similarity +
  extracted-content similarity. Cheap gates run first; nothing else reaches a model. Only pairs
  touching documents processed this pass are examined (steady state pays nothing), capped per
  pass.
- *An agent judges lineage* — the genuinely fuzzy question: same underlying document (draft →
  final, v1 → v2) or two documents that merely look alike (two different quarters' reports)?
  And which is current? Instructed to refuse when evidence is insufficient: a wrong link is
  worse than no link. The offline fake judged only on version markers and `as_of` recency, and
  otherwise refused — a heuristic must not invent lineage.
- *Deterministic application*: `supersedes:` / `superseded_by:` frontmatter (atomic rewrite,
  idempotent) on both pages. Nothing is deleted — the old version stays queryable history;
  consumers demote it.

**The automatic half of mechanism 2 no longer exists.** The candidate generator and the lineage
judge lived in the ingestion pipeline and were removed with it ([ADR 026](./026-the-purge.md)).
What outlived the producer is the part that was always the point: `as_of`, `supersedes` and
`superseded_by` are page-contract fields and indexed columns, ranking demotes a superseded page
and prefers a fresher `as_of` for "current"-style questions, and the chain is written by whoever
edits the two pages. The gardener's model sweep flags candidate pairs for a human to judge and
deliberately never links them itself.

## Consequences

- The answer layer can prefer current truth (demote `superseded_by` pages, rank by `as_of`) and
  answer "as of March 2026" questions honestly.
- Conflicting figures across versions (the draft's `$1.2M` vs the final's `$1.3M`) are
  *resolvable* rather than ambiguous: both pages stay addressable, and the chain says which
  document is current. Resolving the conflict is a reader's job — there is no fact store holding
  the two observations side by side.
- The frozen eval corpus ships drafts alongside their FINAL revisions with corrected figures, and
  the retrieval golden expects the superseding page, so the demotion is measured rather than
  asserted.

## Alternatives rejected

- **Marker-only heuristics in production** — "final"/"v2" conventions vary wildly across
  corpora; where markers are absent or contradictory, lineage is a judgment call. The heuristic
  survived only as the offline fake backend, which is its honest scope.
- **Deleting superseded pages** — destroys history and breaks "as of" questions; demotion keeps
  both truths addressable.
- **Embedding-similarity clustering** — threshold-tuning burden, no lineage direction, and the
  failure mode (wrong merge) is exactly the ambiguity we're removing.
