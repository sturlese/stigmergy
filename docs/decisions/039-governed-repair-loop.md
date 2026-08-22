# ADR 039 — a finding gets a path to zero, and it runs through the covenant

- **Status**: accepted
- **Date**: 2026-08-17
- **Closes**: issues #39 (findings accumulate with no way to close one, and no memory of having
  declined) and #40 (the gardener detects and nothing acts); the first amendment at the foot of
  this document closes #36 (an entity page is minted with a body nothing ever writes), the
  second closes #38 (nothing in this system can remove a page, so the only way to is by hand,
  outside every gate), and the third carries the cleanup half of #77 (near misses mint duplicate
  identities and nothing can merge two of them)
- **Related**: [ADR 024](./024-gardener-digest.md) (the gardener, which produces the findings this
  loop answers and still fixes nothing), [ADR 015](./015-librarian.md) (the write path this reuses
  whole: declared-not-performed edits, the eight gates, the App as committer),
  [ADR 016](./016-human-loop-and-entity-governance.md) (the human loop this extends to a second
  kind of decision), [ADR 030](./030-server-side-entity-minting.md) (the two-door server-side
  approve whose ordering lesson this repeats exactly),
  [ADR 034](./034-agentic-pydantic-harness.md) (the rule this applies: deterministic code may seed
  context and must not replace the model's judgment).
  Narrative: [`docs/reference/repair.md`](../reference/repair.md).
- **Superseded in part by** [ADR 044](./044-the-capture-is-the-approval.md) D2: D1's "a HUMAN decides" clause is withdrawn — a repair is derived, validated, applied and recorded by the worker, and the reading nobody gave it beforehand is the stored diff. The kinds, the validators, the appliers and the nine-gate apply stand unchanged.

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
nobody has asked the gates yet, so widening the vocabulary is a decision with its own record —
which is exactly what the AMENDMENT at the foot of this document is: `entity-body` is a second
proposal KIND with its own validator, its own writer and its own gate branch, and the three
additive shapes above are untouched by it.

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

## Amendment — `entity-body`: the second kind (2026-08-17, closes #36)

The decisions above stand as written; this section records what the repair loop grew, and why the
one-sentence covenant did not have to change to accommodate it.

**The problem it answers.** ADR 016 made entity birth identity-only on purpose: a steward decides
that an entity exists, and `stigmergy-entities create` copies `ops/templates/entity.md` verbatim
into `wiki/entities/<Name>.md`. Nothing writes that page's body, and nothing ever counted the
pages that still carry the template's angle-marked placeholders — the gardener's orphan check
exempts entity pages by type, and no other check reads a body at all. So a brain accumulates
identities that say nothing about themselves, invisibly, and the one lane that could fix it is the
one a capture may never write into.

### A1 — birth stays identity-only; CONTENT gets a second owner

The alternative was to draft the body AT MINT TIME, and it is wrong for the reason ADR 016 gives:
a mint is a steward saying an entity EXISTS, and it happens at the moment the corpus knows least
about it — usually one capture, often before the first page about it is filed. A body drafted then
is a paraphrase of the capture that triggered the mint. So birth keeps its one job, and content
gets a second owner in the repair loop, where the drafting happens when there is something to
draft FROM and lands through a steward's approval rather than beside one.

The gardener gains `entity-placeholder-body` (deterministic, `info`) and the repair loop gains
`KIND_ENTITY_BODY`. The check is the producer, the kind is the answer, and they are the only pair
in the system where one check has a repair of its own.

### A2 — at least two anchored pages, or no model is asked at all

A body drafted from one page is that page's summary wearing an entity's name; from none it is the
placeholder with better grammar. The floor is enforced BEFORE the model call, not after — a run
that asked and then discarded the answer would reach the same outcome and pay for it every night.
Two is a floor and not a wall: demanding more would leave every young entity with a placeholder
forever, and the drafter's own instruction is to return an EMPTY body when the anchored pages turn
out not to say what the entity is.

Anchored pages are resolved from the CHECKOUT — the corpus rows whose `entity:` frontmatter
canonicalizes to this entity's id — and never from `pages_index`. Two reasons, and either is
sufficient: the index is a different tree from the one an apply commits against, and every reader
of `pages_index` must name an ACL predicate, which a nightly proposer has no business holding.

### A3 — the frontmatter is preserved byte for byte, minus two lines

This is the one op in the loop that REPLACES text, so it buys its safety by being unable to touch
anything else. Everything down to and including the page's own `# Title` survives byte for byte —
the frontmatter block, the template's comment, the H1 — and exactly two frontmatter lines may
differ, rewritten IN PLACE: `updated:` (the apply date) and `role:`, and `role:` only when the page
declares an EMPTY one. A role somebody wrote is a statement of identity and replacing it is not a
body draft.

`gate_body_rewrite` cannot judge this diff with its additive proof — the diff is not additive, by
design — so it gains ONE caller-declared exception: `GateContext.body_rewrite_allowed`, a set of
PATHS, empty by default and told by the apply. For a path in that set the additive proof is
replaced (never weakened) by three dedicated checks: the frontmatter is unchanged but for those two
keys, the page declares `type: entity`, and the path is inside this run's write lane. A path
nobody named is judged exactly as it was before the field existed, and the librarian's own flows
name none — `tests/test_architecture.py` pins the granting set to `repair/remote.py` alone, both
directions.

Why a set of paths rather than a flag: a flag would say "this run may rewrite bodies", and the
approval a steward gave was for ONE page. The permission is the thing that was approved.

### A4 — a rejection dismisses future drafts of the same page

`content_key` already hashes the kind with the ops, so a body draft and an additive edit about the
same page are two different questions. Within the kind, the key is `kind + path`: the body text is
not part of it, exactly as a callout's `note` is not part of an additive key. That is deliberate
and it is the same argument — **a re-drafted body is the SAME question**. A steward who read a
draft for a page and said no should not meet another draft of that page tomorrow; if the answer is
"this page needs writing by a person", saying it once has to be enough. `finding_subjects` is
`[[the entity page]]`, so the cheap pre-model skip recognises the question under a new finding id
too, which matters more here than on the additive road because this road's model call is per
entity.

### A5 — what is deliberately NOT here

- **No new write path.** The draft lands through the same clone, the same eight gates, the same
  cross-check and the same `gitcmd.commit(gated_entries=…)` as every other repair. The kind
  branches at exactly two points — which validator performs the ops, and which two caller-scoped
  facts the gates are told — and nowhere else.
- **No second op in a proposal.** One page, one draft, one approval: two drafts behind one button
  is two judgments a steward cannot separate.
- **No rewriting a page that already has a body.** The producer is a check that fires on
  placeholder lines. An entity page somebody has written is not a finding, and this kind has no
  way to reach one.
- **No body for `sources/`, `views/`, or any other zone.** The lane is narrowed to
  `wiki/entities/` for this kind's apply, and the permission names one page inside it.

## Amendment — `delete`: the third kind (2026-08-17, closes #38)

The decisions above stand as written; this section records what the repair loop grew, and the one
place where the covenant's first clause had to be stated more precisely than "a model proposes".

**The problem it answers.** Nothing in this system can remove a page. `gate_zone`'s oldest veto —
*"deleted {path}: the librarian never deletes a file"* — is right about the librarian and wrong as
a property of the whole system, because a corpus with no way to remove anything accumulates
superseded memos, duplicate filings of the same document and pages whose subject no longer exists.
The only alternative available until now was a human editing the knowledge repo by hand, outside
every gate this system has: no steward check, no secrets scan, no contract lint, no record of who
decided it — the one change with the largest blast radius, made in the one way with the least
governance.

### B1 — supersession and deletion are different questions, and this answers only the second

D8's `supersedes:`/`superseded_by:` fields already answer "this page has been overtaken": the old
page STAYS, demoted in search and reachable from the new one, because knowing what was believed in
March is often the point. That is history, and history is not deletion's business.

What this kind is for is the page that should never have been a page: a memo filed twice, a
document captured under two names, a note whose subject was a mistake. The test is not "is it
current" — supersession answers that — but "does the corpus lose anything if this is gone". A page
that has been superseded is not a candidate for deletion by virtue of having been superseded, and
nothing here proposes one.

**Entity pages are refused structurally, not by rule.** `wiki/entities/` is absent from the
deletable set because an entity page has no folder in `page.FOLDER_BY_TYPE` and therefore is not in
`gates.ALLOWED_WRITE_PREFIXES`, which the deletable set extends. An identity is retired through
governance (ADR 016): the pages anchored to a deleted entity would lose the thing they are about,
and a sweep that unlinked them would quietly rewrite somebody's understanding of the corpus. So
does `ops/`, `.claude/` and everything else outside the three content zones — the deletable set is
a whitelist, so a zone added tomorrow is undeletable by default.

### B2 — the deterministic duplicate road is the ONE exception to "a model proposes"

Two `sources/` pages declaring the same `content_hash:` are the same captured document filed twice.
Which one goes is not judgment — it is a lookup: the copy the corpus cites survives (deleting it
would scrub the citation off every page that made it), on a tie the older filing survives (the
later one is the accident), and on a tie in both the lexicographically first path survives so the
answer cannot depend on the order a directory was walked in. All three rules are total and
deterministic, so **the decision belongs to code**, and asking a model would be asking it to
re-derive a fact the frontmatter already states — sometimes wrongly.

Every OTHER deletion is typed by a person at `stigmergy-repair delete <path>... --why`. Judging
that a page is stale is exactly the judgment that is neither code's nor a model's, and the model
road is closed to it structurally: `validate_batch` drops an op naming a deletion in any spelling,
by name, with a sentence saying the road does not exist rather than the generic "not one of the
three kinds" — which reads as a spelling mistake and would send the one corrective retry hunting
for the right word.

Creation stays CLI-only in v1. The console and the review lane approve and reject a deletion
exactly as they do every other repair; what they do not have is a "propose a deletion" button,
because the CLI already reaches everybody who has a checkout and a button is a surface with its own
authorization question.

### B3 — the unit of approval is a SWEEP, not a file

Removing the file is the trivial part. What is not trivial is that the corpus afterwards still has
to be a graph: the knowledge repo's contract linter treats an unresolvable `[[wikilink]]` as an
ERROR, and `gate_contract` turns that into a veto. So a deletion proposal stores a PLAN — the pages
that go, and the **full planned bytes** of every page that mentions one of them, with its
`related:`/`sources:` entries dropped, its body wikilinks unlinked to the text they carried, and
its `supersedes:`/`superseded_by:` pointers removed.

`target_paths` therefore carries the FULL touched set, deleted and scrubbed alike, which is what
makes the review lane's existing per-path steward guard cover the whole blast radius: the steward
of the page being removed is not automatically the steward of every page the sweep would rewrite,
and that rewrite is a real change to somebody else's zone made in their absence.

The sweep unlinks rather than deletes: `[[X]]` becomes `X` and `[[X|alias]]` becomes `alias`, so
the sentence that cited a page survives the page. That is the whole difference between a sweep and
a shredder. And the link question is asked EXACTLY as the frozen contract linter asks it — code
fences and inline code blanked first, alias and anchor split off, `Path(target).stem` — because a
scanner that sees more links than the linter edits prose nobody asked about, and one that sees
fewer leaves a dead link and a veto at apply time.

### B4 — the plan is RECOMPUTED at apply time and refused unless it is identical

`entity-body` can re-run its validator against the fresh clone and know the draft still applies. A
sweep cannot, because what it would write depends on every OTHER page in the corpus: a page that
gained a link to the doomed page after the proposal was made is a DIFFERENT sweep, and performing
the approved one would leave exactly the dead link this kind exists to prevent.

So the apply derives the plan again from the clone's own bytes and refuses unless it is identical
to the stored one, op for op and byte for byte. The corpus moved — propose again. That single rule
is also what makes the stored `planned_after` bytes safe to hold at all: they are the only column
in this system that carries whole page CONTENT into an apply, so a row edited between Approve and
apply could otherwise write a sentence nobody proposed into somebody's page, additively, past every
one of the eight gates.

### B5 — byte-equality REPLACES the additive proof, and is stronger than it

`gate_body_rewrite` proves an edit additive: nothing that was there disappeared. A scrub answers
that "yes, deliberately" by construction, so for the pages a sweep rewrites the apply tells the
gates `expected_bytes` — `{path: the whole file it planned}` — and the gate proves the file on disk
IS those bytes. **That is a stronger statement than the additive proof, not a softer one**:
additive says "nothing disappeared", byte-equality says "this is precisely the file that was
approved, to the byte", and it is the only proof available when disappearing is the point.

`gate_zone` gains the sibling exception, `deletions_allowed`, a set of PATHS rather than a flag for
the reason `body_rewrite_allowed` is one: a flag would say "this run may delete", and the approval
a steward gave was for named pages. A sweep is also the first thing in this system that MODIFIES a
`sources/` or `views/` page, so it declares those as `provenance_pages` — the field the librarian's
own source-attachment flow already sets, making exactly the same claim: `content_hash`, `tier` and
`extracted_at` on those pages are the librarian's own stamps from when it filed them, and a scrub
only ever removes. Every one of them is empty by default; the two new ones are told only by
`repair/remote.py` and `provenance_pages` by that module and the librarian's own filing flow, and
`tests/test_architecture.py` pins the granting surface of each — plus a
classification check over every keyword any module passes to a `GateContext`, so a further exception
cannot arrive looking like the seventeen ordinary ones beside it. The librarian's own flows tell
neither, which is why *"the librarian never deletes a file"* is still literally true.

The one gate that could not be reused as it stands is `gate_contract`. It filters the linter's
findings to the pages a diff TOUCHED — right for every other kind, and blind for this one, because
a deletion's blast radius is the whole graph and a page the sweep never planned is exactly where a
missed reference would sit. So the delete apply pays for a second scan of the same clone and asks
the unfiltered report one question: does anything still link to a page this sweep removed? Scoped
to those stems rather than vetoing on any error, deliberately — a corpus that already carries an
unrelated contract error is not this steward's problem, and refusing their deletion for it would be
a gate bouncing work they cannot fix from there.

### B6 — what is deliberately NOT here

- **No new write path.** The sweep lands through the same clone, the same eight gates, the same
  cross-check and the same `gitcmd.commit(gated_entries=…)` as every other repair. The kind
  branches at exactly three points — which applier performs the ops, which caller-scoped facts the
  gates are told, and what shape the cross-check expects of the diff — plus the one extra lint.
- **No deletion a model can reach.** Not "does not today": `validate_batch` refuses it by name in
  every spelling, so a compromised skill or a confused model reaches nothing.
- **No entity page, no `ops/`, no `.claude/`, no page outside the three content zones.** A
  whitelist, so tomorrow's zone is undeletable until somebody decides otherwise.
- **No partial sweep.** A reference the sweep cannot rewrite — a `[[wikilink]]` in a frontmatter
  field this kind does not know, for instance — refuses the whole plan at propose time rather than
  becoming a question whose answer a gate would later veto.
- **No in-corpus deletion log.** What was removed lives in the commit message, the governance
  ledger row and `git log`, exactly where ADR 037's D2 put the same question: a page recording what
  used to be a page is a page, and it would be indexed, retrieved and cited.

## Amendment — `entity-alias`: the fourth kind (2026-08-18, part of #77)

The decisions above stand as written; this section records what the repair loop grew to close a
finding no kind could answer, and the one place where a constraint from ANOTHER repository decided
what this kind is allowed to be.

**The problem it answers.** A near miss of a registered name that gets minted anyway splits the
anchoring: pages land on two entity ids, each timeline is a fraction of the truth, entity-first
retrieval degrades — and none of that degradation is visible, because nothing counts it. The
gardener's identity pass (issue #77's second piece) now reports the pair, `model-duplicate-entity`,
and this kind is the answer. Without it the report would name a problem whose only remedy was a
human editing `wiki/entities/` and `ops/entity-registry.json` by hand, outside every gate — which
is exactly the state `delete` was added to end for pages.

### C1 — the model picks the survivor; code computes everything that follows

Which of two names is canonical is a JUDGMENT and not a count. The legal name is often the
less-used one, a former name usually loses to a current one, and an abbreviation usually loses to
what it abbreviates — none of which a backlink tally can see. So the model reads both entity pages
and the pages anchored to each and answers with exactly two things: **which page survives, and a
sentence saying why.** That sentence is what a steward reads beside Approve, which is the one place
this road differs from `entity-body`'s: there the DRAFT is the thing being judged, so a model's own
argument for its prose would be persuasion sitting beside it; here the visible result is four
rewritten files, and only the reasoning can tell a steward whether the two names are one company.

Everything else is code's: which pages carry the absorbed id in their `entity:` list, what each one
says afterwards, the survivor's new `aliases:` line, the `superseded_by:` pointer, the regenerated
registry. **A model never computes a file list** — B2's lesson, and the failure here is worse than a
wrong deletion: a page re-anchored to the wrong company has its whole history moved, silently, and
nothing later undoes it. `EntityMergeChoice` has two fields and neither is a list of paths, so the
road is closed structurally rather than by instruction.

The safe answer is a PARK, and it is a first-class one: an empty survivor means "these are not one
entity", nothing is proposed, and the finding stays in the report. Treating that as a validation
failure would spend the single corrective retry pushing a model off the answer it was told to give.

### C2 — the absorbed identity is SUPERSEDED, never deleted

ADR 016 made an entity page undeletable and B1 kept it that way. A merge does not weaken that: the
absorbed page stays, marked `superseded_by:` the survivor, demoted by `index.rank` exactly as any
superseded page is, and still answering to its own name. Supersession is a vocabulary this system
already has (D8) and a merge is precisely what it is for — the page records what was believed
before the merge, which is the only durable account of the decision that is not a commit message.

Its own self-anchor is deliberately NOT re-anchored. Re-anchoring it would drop the absorbed id out
of `scoped_entities()` and turn a governed retirement into a silent disappearance: `describe_entity`
would answer "unknown entity" for a name somebody has been using for a year, rather than showing the
retired page that says what absorbed it.

### C3 — the survivor claims the absorbed entity's ALIASES and never its own name

This is the constraint that shaped the kind, it comes from the knowledge repo rather than from here,
and it was MEASURED rather than reasoned about: a merge that added the absorbed entity's name to the
survivor's `aliases:` is vetoed by `gate_contract`, because the frozen contract linter reports
`alias 'Cofers Holdings' collides with page wiki/entities/Cofers Holdings.md`. The wikilink
namespace is keyed on page STEMS, and the absorbed page is still there by C2. The sibling rule bites
too: two pages declaring one alias is `alias 'X' already declared by <page>`, so the spellings that
move must be REMOVED from the absorbed page in the same commit.

So a merge moves the absorbed entity's alias list and nothing else, and `entity_alias.plan` refuses
a claim the linter would refuse — at plan time, with a sentence naming the colliding page — rather
than storing a proposal a steward can approve and a gate then vetoes.

**What this leaves open, stated rather than hidden.** The absorbed entity's own NAME keeps resolving
to its retired identity, so a future capture spelling it that way still anchors there — the absorbed
id stays REGISTERED, because its page exists and the generator derives the registry from the pages.

And this loop cannot clean that up afterwards, which is the half worth naming: the pair's
`content_key` and its `finding_subjects` are both permanent, so the question is skipped before the
model forever; and even if a second merge were proposed, `_cross_check` refuses it, because the
`retire-absorbed` op would be unchanged, `target_paths` drops an op whose planned bytes equal what
is on disk, and the absorbed page is then absent from the diff the cross-check judges. So the
residual accumulates monotonically and nothing inside this loop owns it. Closing it belongs to
#77's FILING-time piece — the agent
resolving a near miss against the registry it is handed, where a page carrying `superseded_by:` is
exactly the signal a skill can act on. The alternatives were both worse: dropping the absorbed
entity from the derived registry breaks the linter's page↔registry rule (an entity page it does not
register is an ERROR), and renaming or moving its page is a deletion wearing a different verb.

### C4 — the fourth told fact, and the reason the other three could not carry it

The issue predicted `wiki/entities/` would need a new caller-declared exception. It does not: the
zone is grantable through `write_prefixes`, which `delete` already derives from its plan, and the
two frontmatter keys are proven by `expected_bytes`' byte-equality, which B5 established is stronger
than the additive proof rather than softer. **That half was tested, not assumed** — a real apply
through `run_gates(ALL_GATES)` against a real clone.

What the same test found is a different gate: `ops/entity-registry.json` is not a `.md` page, and
`gate_zone` refuses any in-lane write that is not one — `wrote ops/entity-registry.json, which is
not a page`. That refusal is right and stays: a `.gitattributes` carrying `* -diff` blinds every
content gate for the folder it lands in, which is why the check exists at all.

So the exception is `GateContext.derived_files`, a set of PATHS with the posture all three of its
siblings have: empty by default, told by the caller and never inferred, granted only by
`repair/remote.py`, and pinned in `tests/test_architecture.py` both directions with the pruning
check that watches for a FIFTH. It suspends exactly one proof — that an in-lane write is a page —
and it requires the other two to still hold: the path is inside this run's lane, and its whole
content is in `expected_bytes`, so a "derived" file nobody computed is refused by name
(`derived-file-unproven`). A permission with no proof behind it is a way to write an arbitrary
non-page into the corpus.

### C5 — the registry is PREDICTED here and WRITTEN by the generator

`ops/entity-registry.json` has exactly one writer in this codebase and a merge does not become a
second. The proposal stores what the file WILL say — derived through `entities.generator`'s own
reader and `kernel.registry.registry_text`, the one serializer — because a steward approves bytes
and the apply byte-compares against them. The apply then writes the pages, runs the real
`generator.regenerate`, and refuses unless the file it produced is byte-identical to the prediction.
A prediction that turned out wrong is a fact about the corpus, not something to paper over by
writing the stored bytes instead.

That is the reason `stigmergy.repair` gains its first import edge into `stigmergy.entities`, and it
is narrow by design: the generator's reader and writer and the error type they raise, never
`mint`, `birth`, `remote` or `cli`, which are the mint DOOR and have their own authorization
question. The alternative — moving the derivation down into `kernel` — would refactor the governed
birth door to give a repair kind a constant, and re-implementing it here would be the second writer
this decision exists to prevent.

### C6 — one decision per PAIR, once, whichever way the model called it

`content_key` for this kind is the two entity pages as an UNORDERED pair, and nothing else. The
re-anchored pages are excluded for the reason B4's scrubs are: which pages happen to be anchored to
the absorbed entity is a fact about the rest of the corpus, and keying on it would re-ask a declined
merge every time somebody filed a page. The DIRECTION is excluded for a reason only this kind has:
which of two entities survives is a judgment that may legitimately come out the other way tomorrow,
and a steward who declined the merge declined the pair — a key carrying the direction would ask them
again the moment the answer flipped. `finding_subjects` carries the same pair, so the cheap
pre-model skip recognises the question under a new finding id too (D3).

### C7 — what is deliberately NOT here

- **No new write path.** The merge lands through the same clone, the same eight gates, the same
  cross-check and the same `gitcmd.commit(gated_entries=…)` as every other repair. The kind branches
  at exactly three points — which applier performs the ops, which caller-scoped facts the gates are
  told, and one extra shape assertion in the cross-check.
- **No deletion, and no body rewrite.** A merge grants neither, and both stay empty on its
  `GateContext`: it removes nothing and replaces no prose. Every byte it changes is a frontmatter
  line, and byte-equality is the whole of its proof.
- **No merge a person types.** Unlike `delete`, there is no CLI verb: this kind exists because a
  MODEL can see something a lookup cannot, and a hand-typed merge would be a person asserting the
  judgment the road was built to ask for. A person who wants one waits for the finding, or edits the
  pages by hand as they always could.
- **No three-way merge.** A finding names exactly two entity pages, enforced from both ends in
  `gardener.sweep` and asked again by the proposer. Three identities collapsing into one is three
  decisions a steward cannot separate, and it is two merges.

## Amendment — a lost race for the non-additive kinds fails clean (2026-08-19, part of #88)

`gitcmd.push` rebases and retries, and for a filed page that is correct: its gates judged content,
not a position against a base, so the same additive diff replayed onto the moved tip is exactly
what was approved. The two non-additive kinds are different in kind, not degree — their apply is a
proof against a base (recompute, byte-compare, then perform), and a rebase replays the approved
diff onto a tip the gates never judged. For `delete` that can leave a dead link a fresh plan would
have scrubbed; for `entity-alias` it can leave a page anchored to the retired identity, which C6's
permanent dismissal memory then makes unfixable inside this loop.

The two roads not taken, and why: re-running the gates after a rebase reaches the same refusal by
a longer road (`expected_bytes` was computed against the old base, so the byte-compare fails) —
and by then the commit is already on `main`, so the row would be marked `failed` for a change that
LANDED, which is dishonest. Detecting the rebase afterwards has the same flaw. So `push` gained a
way to not rebase at all, and `delete` and `entity-alias` use it: a rejected push fails clean,
nothing lands, the row is `failed`, and the next propose recomputes from state — the same shape
the view sweep's mid-batch stop takes, for the same reason.

The trade, stated honestly: the view sweep pushes up to its ceiling every fifteen minutes, so a
repair apply racing it is realistic and these applies will occasionally fail and need
re-approving. That is the correct side to fail on — a failed apply is recoverable, a wrong write
is not.
