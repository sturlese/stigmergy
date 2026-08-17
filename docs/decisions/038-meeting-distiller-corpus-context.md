# ADR 038 — the meeting distiller sees the corpus it files into, and may declare edits into it

- **Status**: accepted
- **Date**: 2026-08-17
- **Closes**: issue #37 (the meeting flow files blind into a brain it is never shown)
- **Related**: [ADR 020](./020-meeting-distiller.md) (the meeting flow's shape — one structured
  call, code writes every page), [ADR 033](./033-structured-filing-flow.md) (the deterministic
  gatherer this flow now reuses verbatim), [ADR 034](./034-agentic-pydantic-harness.md) (the rule
  this record applies: deterministic code may seed context and must not replace the model's
  judgment), [ADR 032](./032-filing-port-and-pricing-seam.md) (the filing port whose `run_meeting`
  gains one argument here).

## Context

The two filing flows had become asymmetric in a way neither of them decided.

The ORDINARY flow is handed, before the model is ever called, the pages its material most overlaps
with, the neighbourhood one link out from those, the resolved entity view and the repo's whole
wikilink vocabulary (ADR 033's `librarian/gather.py`, rendered by `agent.render_gathered`). It uses
that context for two things: judging whether a page it is about to write duplicates one that
exists, and declaring the reciprocal links the existing pages should gain — `edits`, performed by
`edits.apply_declared` and judged additive by `gate_body_rewrite`.

The MEETING flow was handed the transcript, the entity registry and its own source page's path,
and nothing else. Its brief said so in as many words — *"you do not need to explore, because there
is nothing left to find"* — which was true of the transcript and false of the brain. The
consequences were not subtle:

- a meeting could file a decision page duplicating one an earlier meeting filed, and neither the
  model nor any gate was in a position to notice;
- every decision page it filed was a **leaf**: the new page linked out, and nothing already in the
  corpus linked back, because this flow had no way to say that anything should.

The second half had also been *enforced*. `GateContext.edits_allowed=False` — a control added when
a leaked additive edit was found landing inside a meeting's own commit, reported on no surface a
human reads — made `gate_zone` refuse any status-`M` entry from this flow categorically. That was
the correct control for a flow with no edit mechanism. It is the wrong control for a flow that
should have one.

## Decision

**The meeting flow stays tool-less, and the worker hands it the same context the ordinary flow's
worker builds.** `processing._one_meeting_pass` calls `gather.gather` and `agent.render_gathered`
with no argument overrides — which is to say with `render_gathered`'s NO-TOOLS defaults, the
wording that tells a reader the block is its context and that there is nothing to look further
with. That wording was already written for exactly this kind of reader; it had simply never had
one. The block enters the prompt above the transcript and below the registry, fenced as
`UNTRUSTED DATA` by the same `agent.fence` the material goes through.

**The agent may DECLARE additive edits, in the ordinary flow's exact vocabulary.** `edits` on the
meeting account is the ordinary account's own field — same three kinds (`backlink`, `overlap`,
`contradiction`), same bounds, and now literally the same parser: `agent._parse_edits` was
extracted from `parse_outcome` and is called by `parse_meeting_outcome` too, so a malformed
declaration earns the SAME sentence on both flows. `edits.apply_declared` performs them,
all-or-nothing, and its findings join the gate findings.

**Almost no new deterministic machinery.** Every part of the mechanism is an existing function
called from one more place: `gather.gather`, `agent.render_gathered`, `edits.apply_declared`,
`gate_body_rewrite`, `report.filed`'s `pages_edited`. What is genuinely new lives in the brief,
which is where the judgment belongs (ADR 034's rule): what the gathered context is, that there is
no looking past it, which pages may be edited, and that a guessed path costs the capture its one
retry.

**The one exception, and why it earned itself.** `processing._edits_with_resolved_links` translates
a `link` naming one of this capture's own decisions into the stem the worker filed it under. The
agent declares a `title`; `_decision_stems` slugifies that title into the basename a wikilink
resolves by — accents stripped, punctuation folded, truncated at 60 characters — so the only name
the agent has for the page it just described resolves to nothing. Reproduced against the real flow
before the translation existed: `declared an edit to wiki/decisions/an-earlier-acme-decision.md
linking [[Q3 sync — decision 1]], which resolves to no page in the graph`, the whole set refused,
BOTH agent attempts spent, the capture `failed`. Teaching the brief to slugify was the alternative
and was rejected: for a real title the rule is a guess, and a wrong guess costs the same retry.
The worker names the page, so the worker resolves the name — the same division that already has it,
not the agent, build the meeting page's `## Decisions` links out of `decision_stems`. A `link` that
already names a page is passed through untouched, so the translation can shadow nothing.

**`edits_allowed=False` is no longer declared by any caller.** The meeting flow grants the
mechanism, so it grants the posture that comes with it. The field and its `gate_zone` branch stay —
which flow grants an edit mechanism is a CALLER's declaration, not a fact about which flows happen
to exist — and their red proof moves to `test_gates_unit.py`'s explicit contexts. The finding code
keeps the name `meeting-edit-refused`: it is what preserved refused diffs on deployed stacks
already say in their `# refused by:` header, and renaming it would orphan those artifacts to no
end. Its MESSAGE now states the rule rather than the flow.

## What this deliberately does NOT do

- **No read tools for the meeting agent.** One structured call, everything handed over. The
  transcript is already whole in the prompt; the corpus context is what it lacked, and a worker
  that gathers deterministically buys that for one prompt instead of an iteration budget. This is
  ADR 034's rule read in the direction it also runs: seeding context is legitimate, and the flow
  that has no loop to spend is exactly where seeding is the whole answer.
- **No widening of the meeting write lane.** `MEETING_WRITE_PREFIXES` is the meeting BUILDER's
  range — `sources/meetings/`, `wiki/meetings/`, `wiki/decisions/` — pinned code-to-code against
  `_write_meeting_pages`' reachable paths. `edits.validate` admits the three fast-lane folders,
  which is wider, so the practical editable set for a meeting is the intersection: **decision
  pages**. An edit named anywhere else passes validation and is then refused by `gate_zone` as
  out-of-lane, and the brief tells the agent so up front rather than letting it spend the retry
  discovering it. A per-caller editable set is a real follow-up; it is not needed to close #37.
- **No entity-page enrichment.** An entity page never receives a backlink from what anchors to it —
  its view of what points at it is derived (the index's entity column, the regenerated views), not
  a hand-maintained link list. That stays with the repair loop.

## Consequences

- **The meeting run's JSON schema changed.** `MeetingAccount` gains
  `edits: list[OrdinaryEdit] = Field(default_factory=list)` — additive and optional, so a model
  that declares none is complete exactly as before. It is still a capability change to a paid
  path: **the meeting evals should be re-run before the next real filing**, since the golden's two
  meeting captures were scored under a brief and a schema that had neither of these fields.
- **The filing port's `run_meeting` takes `gathered`.** Unconditionally, unlike the ordinary call's
  `wants_gathered` branch: no backend on this flow holds a tool, so there is no second shape for
  the context to take. A backend predating this change raises `TypeError` on its first meeting
  item; the port conformance test now catches that for both calls rather than one.
- **A meeting's report names what it edited.** `report.filed_meeting`'s `pages_edited` line was a
  hardcoded `(none)`; it now carries the paths `edits.apply_declared` actually wrote, and the
  structured report gains the key. A page a commit changes and no surface names is a page nobody
  knows was touched — that was the harm `edits_allowed=False` existed to prevent, and reporting it
  is what makes granting the mechanism honest.
- **An UNDECLARED additive edit now files**, exactly as it does on the fast lane: `gate_body_rewrite`
  permits an additive change by design and nothing downstream asks whether a declaration produced
  it. This is a posture inherited, not invented, and it is pinned by a named test so that
  tightening it later is a deliberate act with a red test behind it. A non-additive rewrite is
  still refused, terminally, on `zone/body-rewrite`.
- **The stored distillation carries its edits.** `processing._outcome_to_raw` gains the field, so a
  parked meeting re-filed against a newer registry files the same set it parked. A row written
  before this key exists simply has none, and a stale declaration is re-validated against the
  current graph like any other — there is no migration and no backfill.
- **The brief and the frozen copy moved together.** The knowledge repo's
  `.claude/skills/meeting-distiller/SKILL.md` is rewritten and
  `tests/librarian/fixtures/repo/`'s copy is resynced with its recorded sha; the contract table in
  `test_meeting_brief_contract.py` gains three rows. The EVAL fixture's copy
  (`evals/filing/repo/`) is deliberately NOT resynced — it is a yardstick, and re-grading it would
  invalidate every score already recorded. The two copies are now expected to differ, which is what
  that fixture's own `FROZEN.md` has always said would happen.
