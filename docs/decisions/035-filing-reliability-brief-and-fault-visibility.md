# ADR 035 — filing reliability: a symmetric brief, a corrective facts line, and a fault that names itself

- **Status**: accepted
- **Date**: 2026-08-12
- **Related**: [ADR 034](./034-agentic-pydantic-harness.md) (the agentic harness whose tool-holding
  run made this class of fault reachable), [ADR 032](./032-filing-port-and-pricing-seam.md) (the
  `AgentError`/`OutcomeShapeError` fault contract this ADR bounds rather than replaces),
  [ADR 015](./015-librarian.md) §3 (the agent judges, code vetoes — why the fix is not code writing
  the frontmatter block)

## Context

ADR 034 gave the ordinary filing run its tools back and made the librarian brief's "Writing the
page" section environment-conditional: one paragraph for a run that writes its own file, a
different one for a run that returns text for the worker to write. The two paragraphs coexisted in
one section, and the section's OLD emphasis — inherited from the structured, tool-less era —
still read as though returning text were the default case and writing the file the exception.

On the agentic backend that ambiguity resolved stochastically rather than deterministically:
across the frozen fixture and a corpus clone, 8 of 13 first-pass drafts wrote a page with a body
and NO frontmatter block at all — measured 4 of 8 on the fixture, 4 of 5 on the clone. Every one of
those failed the contract linter's `frontmatter` check with `missing required field: type`, which
is misleading on its own: the linter renders `REQUIRED_FIELDS` in a fixed order and `type` is
merely first in it — the whole block was missing, not one key. A staging capture reached this same
finding on its corrective retry and repeated the omission, because nothing told the agent WHOSE
fault it was to fix: the gate's message diagnoses the page as drafted, not the responsibility split
between what the agent owns and what the worker stamps afterward.

A second, unrelated finding came out of the same measurement pass: the model sometimes hard-wraps
prose across a line break inside `[[…]]`, producing a wikilink the contract linter correctly
scores as dead (a link split across lines names nothing).

Separately, a staging fault reached the worker as `UnexpectedModelBehavior` — the framework's own
name for an output the model could not be made to produce even after its retries — and every
downstream reader saw only that class name: the log line, the corrective-retry prompt and the
`failed` report all read `UnexpectedModelBehavior`, indistinguishable from every other occurrence of
it. The framework's own message, which would have named the actual fault (a validation failure, a
malformed tool call, a truncated response), reached nobody. Nineteen-plus instrumented passes since
have not reproduced it.

## Decisions

**D1 — the brief's "Writing the page" section becomes one symmetric statement, not two paragraphs
under one emphasis.** Knowledge-repo commit `03aab879` opens the section with **"Your preamble
decides who writes the file, and the two ways are not alike"**, then states both branches at equal
weight: a run that writes the file itself authors the WHOLE file, frontmatter block first, exactly
the template's fields minus the server-owned ones; a run that returns text for the worker returns
ONLY what goes below the H1. The standalone "write no frontmatter block at all" bullet — the
sentence that read as advice for the exceptional case — is folded into the branch it belongs to
rather than kept beside it. A **"a wikilink stays on one line"** bullet is added for the second
finding. Measured after the rewrite, same fixture and clone: 0 of 12. The two rates (8/13 vs.
0/12) are different enough that a Fisher exact test puts the chance of seeing that split by draw
alone at roughly p ≈ 0.0006. Every number above is a fixture-scale and clone-scale measurement, not
a production rate.

**D2 — the contract gate's `frontmatter` finding earns a facts line on top of its message,
regardless of which brief bytes produced the draft.** `gates.gate_contract` now composes
`Finding.brief` for a `check == FRONTMATTER_CHECK` finding as the linter's own message plus
`FRONTMATTER_FACTS` — a shape-neutral statement of the field split: the worker stamps `status`,
`as_of`, `submitted_by`, `entity` and `acl` after the draft; every other required field (`type`,
`title`, `created`, `updated`, `tags`) must already be in the page's frontmatter block, exactly as
`ops/templates/<type>.md` declares. This is a second, independent defense of the same fact D1's
rewrite states once in the brief: a corrective retry that reaches this gate has already drafted
once under whatever brief bytes were in effect, and telling it the split again, from the finding
itself, does not depend on the brief having said it clearly the first time. Earned by measurement —
the staging retry above reached this exact gate with this exact message and repaired nothing.

**D3 — a framework fault persists its real message, bounded, on both roads it travels.** In
`pydantic_backend.py`, both `run()` and `run_meeting()` gained the same treatment for their
`UnexpectedModelBehavior` arm: one bounded single-line `log.warning` (`MAX_FAULT_LOG_LEN` = 500,
wide enough for an operator's diagnosis) naming the exception's class, its own `str()`, and
`repr(ex.__cause__)` — usually the provider or validation fault pydantic-ai wrapped underneath, and
often the more informative of the two. The `OutcomeShapeError`'s `Finding.message` — the text that
reaches the corrective retry's prompt and a `failed` report — gains a second, shorter excerpt
(`MAX_FAULT_MESSAGE_LEN` = 200), fence-neutralized before it is bounded, since this text lands in a
prompt and a hostile-looking response could otherwise carry a stray fence token. The blanket
`except Exception` arm gets the same bounded log line but its WIRE message stays class-only, on
purpose: an arm that wide can catch a raw provider error, and a provider error can carry prompt
text — verbatim captured material — that a fence-neutralized excerpt does not protect against.
Before this, only the class name survived past either arm anywhere a human or the next retry could
read it; the staging `UnexpectedModelBehavior` fault described above is unrecoverable for exactly
that reason — there was nothing to look at afterward.

**D4 — one seam, not two hand-rolled ones.** `stigmergy.text.one_line(text, width)` composes
`sanitize` (control-character strip), whitespace collapse, then `clamp` (word-safe truncation) —
in that order, because collapsing first is what keeps a clamp's truncation point chosen against the
text a reader actually sees rather than against embedded newlines. It replaces two independent
hand-rolled versions: `gates._one_line`, now a thin wrapper over it, and the fault-log lines this
ADR adds to `pydantic_backend.py`. Composed at the bottom of the stack (`stigmergy.text` imports
nothing from this project) so both callers, and any future one, get the same guarantee rather than
a third copy that drifts from the first two.

**D5 — no deterministic frontmatter construction by code.** Code could build the frontmatter block
itself from `Outcome.title`/`page_type` and the template, sidestepping the omission entirely. This
is rejected on the standing rule this package already lives by
([ADR 034](./034-agentic-pydantic-harness.md)): *"deterministic code may seed context and implement
tools, and must not replace the model's ability to decide the context is not enough."* A drafted
frontmatter block is a JUDGMENT — which tags, which title casing, whether the template's optional
fields apply — not a mechanical fill-in, and code assembling it on the model's behalf is the same
substitution this package has already refused once, applied to a different field.

**Also rejected: a platform-side override note, layered in front of the brief, correcting the
emphasis the brief itself got wrong.** ADR 032/033 used exactly this shape — a per-backend preamble
carrying the one correction the shared brief could not yet state — for a genuine cross-backend
disagreement neither brief version could resolve alone. Here there is no disagreement to bridge:
one brief section was ambiguous under one run shape, and the brief is the one artifact the
knowledge-repo owner can read, diff and version. An override note would have shipped a permanent
platform-side patch for a brief defect the brief itself did not need to keep, and every future
backend reading the same section would have inherited the same silent contradiction between what
the brief says and what the preamble corrects.

**D6 — B2 (a tool-retry budget knob) stays deferred.** The `UnexpectedModelBehavior` fault D3 makes
diagnosable has not recurred in nineteen-plus instrumented passes since. Two candidate causes remain
open — a transient provider hiccup mid-tool-loop, and a genuine framework-side validation edge —
and they call for different knobs (a request-level retry budget versus a stricter tool-call
schema). Building either before a persisted occurrence names which one is the one this package
already refuses in D5's own terms: replacing a diagnosis with a guess wide enough to cover both
guesses.

## Consequences

- **The corrective-brief facts line and the fault persistence are shape-neutral.** Neither depends
  on which brief bytes produced the draft or on which of the two "Writing the page" branches a run
  took — they defend the same fact and the same visibility whether or not D1's rewrite is the brief
  in effect, which is what makes D2 and D3 worth having even though D1 measures as the larger fix.
- **The eval fixture is re-frozen a third time, deliberately.** `evals/filing/repo/`'s librarian
  `SKILL.md`, `PROVENANCE.json` and `FROZEN_SHA256` all move to knowledge-repo commit `03aab879`,
  recorded in `evals/README.md` beside the two prior re-freezes. Only the librarian brief moved; the
  linter and the meeting brief are byte-identical to the prior freeze. The bars already fixed for
  this instrument are not re-derived — a bar re-derived from a run nobody has made yet would be a
  number invented to be met — and the first row measured under these bytes lands in this same
  landing, read as a fresh baseline candidate rather than as a regression.
- **The repair channel only, gate semantics untouched.** D2 changes what a `Finding.brief` says; it
  changes no gate's verdict, no severity and no `repairable` flag. A capture that would have been
  vetoed before this ADR is vetoed after it, on the same finding — what changes is whether the one
  corrective retry it is owed has a chance of clearing it.
