# ADR 039 — a finding gets a path to zero, and it runs through the covenant

- **Status**: accepted
- **Date**: 2026-08-17
- **Closes**: issues #39 (findings accumulate with no way to close one, and no memory of having
  declined) and #40 (the gardener detects and nothing acts)
- **Related**: [ADR 024](./024-gardener-digest.md) (the gardener, which produces the findings this
  loop answers and still fixes nothing), [ADR 015](./015-librarian.md) (the write path this reuses
  whole: declared-not-performed edits, the eight gates, the App as committer),
  [ADR 016](./016-human-loop-and-entity-governance.md) (the human loop this extends to a second
  kind of decision), [ADR 030](./030-server-side-entity-minting.md) (the two-door server-side
  approve whose ordering lesson this repeats exactly),
  [ADR 034](./034-agentic-pydantic-harness.md) (the rule this applies: deterministic code may seed
  context and must not replace the model's judgment).
  Narrative: [`docs/reference/repair.md`](../reference/repair.md).

## Context

The gardener reads the corpus every night and writes down what looks wrong. It fixes nothing, on
purpose, and ADR 024 argued that at length. What it did not settle is what happens next, and the
answer turned out to be: nothing, forever.

Two things were missing, and they were different things.

**There was no ACT.** Every finding's `suggested_action` began "no command —" and named a human
procedure: read the two pages, decide whether they disagree, file a correction through the 🧠
gesture. That is honest and it does not scale past the first few dozen findings. The three checks
that a link or a callout would actually answer — `model-unlinked-mention`, `model-contradiction`,
`orphan-page` — described repairs the librarian's own `edits.apply_declared` already performs, in a
vocabulary the eight gates already judge. The machinery to make the change existed; nothing
proposed one.

**There was no MEMORY of having declined.** A finding a steward has read and judged not worth
acting on comes back the next night, and the night after, identical. The gardener's findings table
is append-only per run and carries no verdict, so "reviewed and declined" was not a state anything
could hold. That is what makes a findings list stop being read: the same twelve entries every
morning, most of them already judged.

The obvious shapes were both wrong. An autonomous fixer — the model edits and pushes — breaks the
covenant this whole system is built on, and would do it on the one lane where the writes are
irreversible and the diffs are small enough to look harmless. A silence list — findings a steward
mutes — is bookkeeping about a symptom that leaves the corpus exactly as it was.

## Decisions

### D1 — a MODEL proposes, CODE validates twice, a HUMAN approves one at a time, and code applies exactly what was approved (#40)

The four steps are the whole design, and each exists because the other three cannot see what it
sees.

- **The model proposes.** `stigmergy-repair propose` reads the latest completed gardener run's
  findings, filters to the three checks a repair shape can answer, hands the model those findings
  and the pages they name, and gets back a batch of concrete proposals: a set of ops, the finding
  ids they answer, and a rationale written for a steward. The model has exactly two tools and both
  READ. Its inability to write is structural, not promised.
- **Code validates twice, against two different trees.** At propose time `edits.validate` proves
  the ops apply to the checkout the proposer read. At apply time `edits.apply_declared` proves they
  still apply to a fresh clone — which may be hours newer. Neither validation trusts the other,
  because they are asking about different repositories, and a page deleted in between has to refuse
  at the second one.
- **A human approves one at a time.** There is no batch approve and there will not be one. A
  proposal is approved or declined whole, and `MAX_OPS_PER_PROPOSAL` (6) is how much one approval
  is allowed to be.
- **Code applies exactly what was approved.** `run_gates(ALL_GATES)` judges the resulting diff as
  it judges the librarian's own, and `gitcmd.commit(gated_entries=…)` closes the last window: the
  diff the gates approved is the diff that lands.

### D2 — the op vocabulary is the librarian's three declared-edit kinds, and nothing else

`backlink`, `overlap`, `contradiction` — the same three `edits.apply_declared` performs for a
filing capture, applied through the same function. Every one is strictly ADDITIVE: a name added to
a page's `related:` list, and for two of them a one-sentence callout below it. Nothing here
rewrites a sentence, deletes anything, moves anything, or creates or removes a page.

That is the whole safety argument, and it is why the vocabulary is closed rather than merely small.
The eight gates were written to judge these shapes; `gate_body_rewrite` is what proves a diff is
additive rather than promising it. A fourth op kind is not a bigger tuple, it is a new question
nobody has asked the gates yet — so `tests/test_architecture.py` pins the repair vocabulary equal
to `page.EDIT_KINDS`, and widening it is a decision with its own record.

The corollary a reader should not have to derive: a `model-contradiction` finding is answered by
FLAGGING the disagreement on both pages, never by correcting either. Deciding which page is right
is not a thing this loop does.

### D3 — a REJECTED row is the dismissal memory, and `content_key` is what identifies a proposal (#39)

A proposal is identified by WHAT IT WOULD DO — its kind plus its sorted `op:path:link` lines,
hashed — and the proposer skips a `content_key` held by a pending, approved, rejected or applied
row. "Reviewed and declined" is therefore a durable fact for the first time, and a steward who
says no once is not asked the same question by the next night's run.

The claim is worth stating precisely, because it has two halves and they answer different
questions. `content_key` is the AUTHORITATIVE memory and runs after the model: a declined repair
is never queued twice. It does not stop the finding reaching the model, though, and the cheap
pre-model skip that does needs its own key — which is why a proposal also stores
`finding_subjects`, the sorted page set each answered finding NAMED. `target_paths` alone was not
enough: an `orphan-page` finding names the page nothing links to while the repair edits the page
that ought to link to it, and a one-sided answer to a two-page finding names one page of two. Both
shapes matched nothing, so a declined repair cost a model call every night, invisibly. It is a list
of LISTS, one per finding answered rather than their union, because a proposal answering two
findings has to dismiss both.

`failed` is the one status neither half remembers, and the asymmetry is deliberate (D2's
consequence, made explicit): a rejection is a human saying no, while a failed apply is a human
having said YES to something that then hit a gate, a race or a fault. The row stays as the
operator-visible record; the SKIP does not, or the one repair a steward actively wanted would be
the one the loop can never offer again.

Three details of that are load-bearing:

- **`note` is excluded from the key.** Two proposals that would add the same callout to the same
  page with differently-worded sentences are the same question asked twice, and a steward who
  declined it should not meet a rephrasing of it tomorrow.
- **The reason is required on a reject, and it lands on the PROPOSAL as well as in the ledger.** A
  `rejected` row with an empty `notes` tells the next steward that somebody said no and nothing
  about why, which is a worse artifact than no row at all.
- **The UNIQUE index is narrower than the skip rule** — one PENDING row per key, not one row ever.
  Re-proposing after a rejection is a decision a human makes, and the index must not turn it into a
  database error.

### D4 — the proposer's PROCEDURE lives in the knowledge repo; the FRAME lives in code

The system prompt is a code-owned header plus `.claude/skills/repair-proposer/SKILL.md`, read at
run time from the checkout being repaired — the same arrangement the librarian's own filing skill
has, and for the same reason: which finding is worth repairing, which shape fits, and when a
finding has gone stale and deserves nothing are editorial judgments that belong to the people whose
brain it is.

What the skill CANNOT change is the header: two read tools and no third, the op vocabulary, "propose
only from the findings you were given and the pages you actually read", and the rule that a fenced
page body is data. A knowledge repo cannot widen the proposer's powers by rewriting its procedure.

A missing or empty skill is a NAMED configuration refusal, never a default: a proposer briefed only
by the header would know what it may not do and nothing at all about what is worth doing.

**The drift risk is accepted for v1 and named here.** The brief is versioned in the knowledge repo
and read at run time; this repository holds no frozen copy of it, unlike the contract linter and the
meeting brief, which have one because code parses their output. The two-sided pin is light — the
code owns the relpath constant, and a test asserts the file is where the constant says when a
sibling checkout is present. Revisit when the vocabulary grows past three ops, which is the point
at which the two could disagree about something that matters.

### D5 — the review lane grows a THIRD item kind, and its authorization is per-target-path

`repair-proposal` joins `entity-proposal` and `parked-capture` in `review_queue`/`review_decide`.
Its verdicts are `approve` and `reject` only: a proposal IS its edits, so the thing to change about
one is which edits it contains, which is a different proposal.

**Its steward guard is the first in this lane that asks a per-PATH question, and it has to.** The
other two kinds are anchored to no page — an entity proposal has no page yet, a parked capture never
got one — so `is_steward(service, "")` is the only scope they could resolve, and it can only match
the universal `"*"` key. A repair names the exact pages it would edit, and `ops/stewards.json`
exists to delegate zones. Asking the universal question would let the general steward apply an edit
inside a folder whose own steward never saw it: the delegation silently undone by the one verdict
that writes to those folders. So the rule is `all(is_steward(service, p) for p in target_paths)` —
and a proposal spanning two zones needs somebody who stewards both. That is not a deadlock: either
steward may still reject it, and the pair can be proposed as two one-sided repairs.

A repair proposal is listed in the MANAGEMENT read of the inbox only. It has no submitter, so there
is no "own" for an ownership-scoped caller to be shown, and a proposal names page PATHS —
`acl.visible()` decides who may know a page exists, and the inbox does not ask it.

**Self-approval is not asked of this kind, and that is a decision rather than an omission.** The
`entity-proposal` rule (a second, different steward) exists because a HUMAN submitted that row and
a second human has to agree with them. A repair proposal has no human submitter: a nightly job
derived it from the gardener's findings, and the model that wrote it approves nothing and is nobody
to be a second party to. The one steward IS the second party. Asking "did you file this?" of a
machine-authored row would refuse nobody and imply a submitter that does not exist.

### D6 — one apply ordering, two doors

`server.review.apply_repair_and_record` is the ONE function that records the verdict, applies
through the governed door, and writes the governance ledger row; the MCP/Slack review lane and the
admin console both run it. This is ADR 030 D2's lesson repeated verbatim, because the failure mode
is: two copies of an ordering rule are two places for it to be reordered, and the reordering is
invisible from either door's end state.

It takes no authorization argument, and the caller set is closed and pinned by test, for exactly the
reason `mint_and_record_approval`'s is: every caller is a surface that has already decided
authorization for itself — the review lane by resolving a steward for every target path, the console
by sitting behind the operator token.

Three properties of that ordering are decisions rather than implementation:

- **`mark_decided` is a conditional UPDATE** (`WHERE status = 'pending'`), and that one clause is
  the whole concurrency story. The second Approve sees zero rows and is told so, rather than a
  second clone-and-push of a repair that already landed. No lease exists for repairs because none is
  needed.
- **The ledger row is written AFTER the push**, like the mint's. A row claiming a decision whose
  commit never landed is worse than a missing row, and the commit is the irreversible half.
- **A failed apply does NOT revert to pending.** The status becomes `failed`, the `error` column
  says why, and the row stays operator-visible until somebody proposes again. A silent revert would
  hide that a gate refused, which is the one outcome an operator most needs to see.

### D7 — the cross-check: the diff must be what was approved, not merely a valid one

`run_gates` would pass a well-formed additive diff quite happily. What makes a tampered proposal
wrong is not its shape — it is that it is not the change a steward read. So the applier also
requires that the produced diff's paths EQUAL the proposal's stored `target_paths` and that every
entry is a modification.

That is why `target_paths` is stored separately from `ops` even though it is derivable from them:
the redundancy is the point. An `ops` blob edited after approval has to agree with a second stored
fact to reach `main`.

## What this deliberately does NOT do

- **No batch approval, and no autonomous apply.** Not "not yet" — the covenant is the product. A
  loop that applied its own proposals would be a model with commit rights to the corpus, which is
  the thing this system exists not to be.
- **No doorbell ring.** `repair-proposal` has no Block Kit card, so `slack.doorbell` skips it: a
  repair's ops and rationale are not a thing a DM can honestly compress into two buttons, and the
  doorbell's kind dispatch would otherwise render it as an entity-proposal card whose Approve
  button calls `review_decide` with the wrong item kind. Silence is the correct default for a kind
  with no renderer; giving one a card is a deliberate act.
- **No `stigmergy-repair apply`.** The CLI proposes, lists and shows, and there is no fourth
  command: a terminal knows who is typing and not what they are allowed to approve. Applying goes
  through a door that decides.
- **No new write path.** Every byte reaches the knowledge repo through `edits.apply_declared`,
  `run_gates(ALL_GATES)` and `gitcmd.commit(gated_entries=…)` — the librarian's own. A second road
  into the corpus would need its own gates, and there is no such thing as a second set of gates that
  stays equivalent.
- **No repair for the other five checks.** An aging seed needs somebody to write, a stale view needs
  a regeneration, an anchor that no longer fits is a judgment about a page's subject. None of them
  is a link or a callout, so none of them is proposable, and `PROPOSABLE_CHECKS` names the three by
  slug rather than excluding the rest by accident.

## Consequences

- A new package, `stigmergy.repair`, with its own `repair_proposals` table, its own CLI
  (`stigmergy-repair`), and an architecture allowlist from its first commit: only `cli.py` opens a
  connection, nothing reads the environment at import time, and only `proposer.py` may load a model
  stack — because `remote.py` runs inside the MCP server process.
- `gardener_findings` gains a `subjects JSONB` column (additive, `ADD COLUMN IF NOT EXISTS`). The
  proposer reads the LIST of subject pages and never re-splits the comma-joined display string,
  which is a filename with a comma away from being wrong.
- `stigmergy.review_kinds.ITEM_KINDS` becomes three, and the `review_queue`/`review_decide` tool
  docstrings — the MCP CLIENT CONTRACT — document the new kind and its verdicts.
- `server/review.py` gains a declared, reasoned import edge onto `stigmergy.repair` (store, schema,
  errors and the apply door), and `stigmergy.admin` gains one onto the store, the schema and the
  errors. The console reaches the apply through `apply_repair_and_record` and never directly, which
  is why `stigmergy.repair.remote` is absent from its allowlist — the same shape
  `stigmergy.entities.remote` has there.
- A fourth cron template, `deploy/workflows/repair-propose.yml`, an hour after the gardener's so the
  findings it reads are this morning's; the console's Crons tab and its `CRON_WORKFLOWS` table gain
  the row, and the console gains a Repairs panel.
- The knowledge repo gains `.claude/skills/repair-proposer/SKILL.md` and its own copy of the
  workflow.
