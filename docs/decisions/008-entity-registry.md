# ADR 008 — Entity identity: a curated registry, agent-proposed merges, human approval

**Status:** accepted · 2026-07-13

## Context

Entity canonicalization was purely mechanical (`normalize`: case, accents, legal suffixes).
That merges "Initech" with "INITECH, S.L." — and can never merge "Globex" with "GX Industries",
nor should any string rule try: whether two names denote the same real-world entity is a
judgment call, and a wrong merge corrupts every page that links either name.

## Decision

Identity gets the three-layer treatment the rest of the system uses:

1. **A curated registry** (`entity-registry.json`, read by `stigmergy.kernel.registry`): the
   human-owned identity file — canonical id, display name, type, aliases. Every resolution
   consults it FIRST; registered aliases join their canonical entity whatever `normalize()` would
   say, and the registered name/type win the entity page. Plain, diffable JSON — memory you can
   read, edit and revert in git. Missing file = empty registry (everything keeps working);
   malformed file = loud error (identity must never silently degrade).
2. **Agent-proposed merges**: deterministic candidates (similar or token-contained normalized
   keys) go to a merge-judge agent that sees each group's observed spellings, counts and mention
   types — and is instructed to refuse when unsure. The offline fake merged only on token
   containment and otherwise refused: a heuristic must not invent identity.
3. **A human approves**: proposals land in a pending file that an `approve` subcommand folds into
   the registry (all, or one by index), and `reject` discards. The same gate the rest of the
   system puts in front of irreversible judgment, applied to identity.

The corpus build itself stays pure (no LLM) — agency lives only in the opt-in `propose` step.

**Layers 2 and 3 no longer exist.** The merge lane was removed with the graph build it was part
of ([ADR 026](./026-the-purge.md)). The registry — layer 1, the part that carries the decision —
survived, and it now has exactly ONE writer: `stigmergy-entities`, the governed entity-birth door
([ADR 016](./016-human-loop-and-entity-governance.md)), where a steward authors the identity
metadata by hand. The human approval this ADR argued for did not go away; it moved to the front
of an entity's life instead of arriving after a machine proposed a merge.

## Consequences

- "Ask about Acme" works across spellings, abbreviations and renames — with identity decisions
  auditable in one file's git history.
- The registry is shared vocabulary for every layer above it (views, ACLs, the answer server's
  entity boosts, the librarian's anchoring gate) — one place where "who is who" lives.
- Cost: zero in the steady state; identity work happens only when a human sits down to do it.

## Alternatives rejected

- **Auto-applying judged merges** — a wrong merge is the identity equivalent of a hallucinated
  figure; it gets a human gate for the same reason.
- **Embedding-based clustering** — thresholds, no auditability, and reversibility is exactly
  what identity errors need most.
- **Folding the registry into the path-ownership conventions** — path-derived ownership and
  mention identity are different problems; the file lives with the stage that consumes it.
