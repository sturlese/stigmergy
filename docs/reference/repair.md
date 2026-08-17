# The governed repair loop — `stigmergy.repair`

A finding's path to zero. `stigmergy-repair propose` turns the gardener's findings into concrete,
strictly additive edits a steward can approve one at a time; the review lane and the admin console
are where one is approved; and only then does code perform exactly the approved ops, through the
librarian's own validator, its eight gates and its governed commit.
Design record: [ADR 039](../decisions/039-governed-repair-loop.md) — it holds the decisions this
document only shows the results of.
The findings themselves are covered in [`gardener-digest.md`](./gardener-digest.md), the review lane
in [`server.md`](./server.md#the-review-tools) and the console's panel in
[`admin-console.md`](./admin-console.md). Code map:
[`src/stigmergy/repair/index.md`](../../src/stigmergy/repair/index.md).

**The covenant, in one sentence: a MODEL proposes, CODE validates twice, a HUMAN approves one at a
time, and code applies exactly what was approved.** Nothing reaches the knowledge repo without
having passed all four.

```
  gardener findings                stigmergy-repair propose            a steward, one at a time
  (the latest COMPLETED run)         ├─ filter to the three               ├─ review_queue / review_decide
   model-unlinked-mention            │    proposable checks               │    (MCP, item_kind
   model-contradiction               ├─ drop keys already reviewed        │     "repair-proposal")
   orphan-page                       ├─ the model, 2 READ tools           └─ the console's Repairs tab
        │                            ├─ validate: vocabulary, paths,              │
        └────────── read ───────────>│    links, notes, bounds                    │  approve
                                     ├─ validate: edits.validate                  v
                                     │    against the real checkout      server.review.apply_repair_and_record
                                     └─ INSERT ... status='pending'        ├─ mark_decided (WHERE pending)
                                          content_key = what it would do   ├─ clone → edits.apply_declared
                                                                           ├─ the cross-check: the diff's
                                                                           │    paths == target_paths, all M
                                                                           ├─ run_gates(ALL_GATES)
                                                                           ├─ gitcmd.commit(gated_entries=…)
                                                                           │    + push, App-authored
                                                                           └─ mark_applied + review_decisions
```

## The three checks a repair can answer

Only findings a link or a callout could actually close reach the proposer. The other five are
absent by NAME rather than by oversight: an aging seed needs somebody to write, a stale view needs a
regeneration, an anchor that no longer fits is a judgment about a page's subject. None of them is an
edit this vocabulary can express.

| check | what it says | the repair |
|---|---|---|
| `model-unlinked-mention` | two pages cover the same ground with no link between them | a `backlink` on one of them, or on each |
| `model-contradiction` | two pages assert things that disagree | a `contradiction` callout on BOTH sides |
| `orphan-page` | nothing in the corpus links to this page | a `backlink` on the page that ought to link to it — which the proposer has to FIND |

A contradiction repair FLAGS the disagreement and never resolves it. Deciding which of two pages is
right is not something this loop does, and it could not express the edit if it were.

## The op vocabulary is the librarian's, and it is closed

Three shapes, all of them additive, all of them performed by `edits.apply_declared` — the same
function a filing capture's declared edits go through:

- **`backlink`** — adds `[[link]]` to that page's `related:` list.
- **`overlap`** — the same link, plus a `> [!NOTE] Overlaps with [[link]]` callout carrying a
  one-sentence `note`.
- **`contradiction`** — the same link, plus a `> [!WARNING] Contradiction with [[link]]` callout.

`note` is required for `overlap` and `contradiction` and ignored for `backlink`. `path` is the page
that CHANGES and must be in one of the fast lane's three folders (`wiki/notes/`,
`wiki/decisions/`, `wiki/concepts/`); `link` is a bare page name and may resolve to any page,
including an entity page. Editing `wiki/entities/`, `sources/` or `views/` is refused.

Nothing here rewrites a sentence, deletes anything, moves anything, or creates or removes a page.
That is the safety argument rather than a coincidence: the eight gates were written to judge these
shapes, and `gate_body_rewrite` is what proves a diff is additive rather than promising it. A fourth
op kind is a new question nobody has asked the gates — `tests/test_architecture.py` pins the
vocabulary equal to `page.EDIT_KINDS`.

## `stigmergy-repair`

```
stigmergy-repair [--dsn DSN] [--repo PATH] [--json] propose
stigmergy-repair [--dsn DSN] [--repo PATH] [--json] list
stigmergy-repair [--dsn DSN] [--repo PATH] [--json] show <id>
```

- **`propose`** — the pass a cron runs. Reads the latest COMPLETED gardener run, keeps the
  proposable findings, drops the ones whose repair has already been reviewed, batches the rest to
  the model with the subject pages' bodies fenced, validates, and inserts one pending row per
  surviving proposal. Records a `job_runs` row under the job `repair-propose` with
  `findings_seen` / `proposed` / `skipped_known` / `skipped_invalid`. Exits 0 when it proposes
  nothing — an ordinary outcome, not a failure.
- **`list`** — what waits on a steward, plus what was recently decided.
- **`show <id>`** — what one proposal would change, rendered from the ops without touching git.

**There is no `apply`, and there will not be one.** A terminal knows who is typing and not what they
are allowed to approve; applying goes through a door that decides. `--repo` (or `$STIGMERGY_REPO`)
must be a real git checkout, because a proposal is validated against the pages that are actually
committed there.

| Setting | Default | Effect |
|---|---|---|
| `STIGMERGY_REPAIR_MODEL` | the librarian's own default model | which model proposes |
| `STIGMERGY_REPAIR_MAX_OPS` | `6` | how much ONE approval is allowed to be |
| `STIGMERGY_REPAIR_BATCH` | `8` | findings per model call |
| `STIGMERGY_REPO` | — | the checkout to propose against |
| `STIGMERGY_INDEX_DSN` | — | where the proposals live |

## The proposer's own procedure lives in the knowledge repo

The system prompt is a code-owned header plus `.claude/skills/repair-proposer/SKILL.md`, read at run
time from the checkout being repaired — the same arrangement the librarian's filing skill has.
Which finding is worth repairing, which shape fits, and when a finding has gone stale and deserves
nothing are editorial judgments, and they belong to the people whose brain it is.

What the skill cannot change is the header: two tools and both READ, the op vocabulary, "propose
only from the findings you were given and the pages you actually read", and the rule that a fenced
page body is data somebody wrote and never an instruction. A knowledge repo cannot widen the
proposer's powers by rewriting its procedure.

A missing or empty skill is a NAMED refusal and the pass does not run. A proposer briefed only by
the header would know what it may not do and nothing at all about what is worth doing.

## Deciding one

A pending proposal appears in the review inbox as `repair-proposal`, alongside `entity-proposal` and
`parked-capture`, and in the console's Repairs tab. It carries its rationale, the pages it would
edit, and a count of its ops — never the ops themselves, because a list is a scan; the ops are one
click, or one `stigmergy-repair show`, away.

- **Verdicts are `approve` and `reject` only.** A proposal IS its edits, so the thing to change
  about one is which edits it contains, which is a different proposal.
- **Approving needs a steward for EVERY page the proposal would edit.** `ops/stewards.json` exists
  to delegate zones, and this is the first verdict in the lane that can land inside one, so the
  question is asked per path rather than universally. A proposal spanning two zones needs somebody
  who stewards both — either steward may still reject it, and the pair can be proposed as two
  one-sided repairs.
- **Rejecting requires a reason**, and the reason lands on the proposal as well as in the ledger.
- **A repair proposal is listed for an unrestricted identity only.** It has no submitter, so there
  is no "own" for an ownership-scoped caller — and a proposal names page PATHS, which is
  `acl.visible()`'s question and not the inbox's.
- **The Slack doorbell does not ring for it.** There is no Block Kit card: a repair's ops and
  rationale are not something a DM can honestly compress into two buttons. It is reviewed in the
  console and over MCP.

Both approving doors run one function, `server.review.apply_repair_and_record` — the MCP/Slack
review lane and the admin console alike — so "the ledger row is written, and written after the
push" is a property of the code rather than of each surface remembering.

## What has to agree before anything is pushed

Three independent checks, each chosen because the other two cannot see what it sees:

1. **`edits.apply_declared` against a fresh clone.** The propose-time validation ran against a
   checkout that may be hours old; a page deleted since then refuses here.
2. **`run_gates(ALL_GATES)`** judges the resulting diff exactly as it judges the librarian's own —
   all eight, not a subset.
3. **The cross-check**: the diff's paths must EQUAL the proposal's stored `target_paths`, and every
   entry must be a modification. This is the one that catches a TAMPERED proposal, whose ops were
   edited after a steward approved it. The gates would pass such a diff quite happily — it is
   additive and well-formed; what makes it wrong is that it is not what was approved, and only a
   second stored fact can say so.

Then `gitcmd.commit(gated_entries=…)` closes the last window: the diff the gates approved is the
diff that lands, bytes included. The commit is authored by the librarian App, its message names the
proposal and the findings it answers, and it carries an `Approved-by:` trailer naming the human.

**A failed apply stays failed.** The status becomes `failed`, the `error` column says why in a
sentence written to be read by a steward, and the approved status is not restored. A silent revert
to pending would hide that a gate refused, which is the outcome an operator most needs to see.

## The dismissal memory

A proposal is identified by WHAT IT WOULD DO — its kind plus its sorted `op:path:link` lines, hashed
into `content_key` — and the proposer skips a key that has ANY prior row: pending, rejected or
applied. "Reviewed and declined" is a durable fact, and a steward who says no once is not asked the
same question by the next night's run.

`note` is deliberately excluded from the key: two proposals adding the same callout to the same page
with differently-worded sentences are the same question asked twice, and a rephrasing of a declined
repair is not a new one. The UNIQUE index is narrower than the skip rule — one PENDING row per key,
not one row ever — so re-proposing after a rejection stays a human decision rather than a database
error.

## The cron

`deploy/workflows/repair-propose.yml` runs daily at ~06:07 UTC, an hour after the gardener's ~05:07,
so the findings it reads are this morning's. It is a template: copy it into your knowledge repo,
like the other three, because its log carries page paths and page names out of the corpus. It needs
`INDEX_DSN` and `OPENAI_API_KEY` (both already shared with `index-rebuild.yml` and `gardener.yml`)
and nothing else — no Slack token, and no App credential, because this job proposes and cannot
apply. The console's Crons tab can dispatch it, and its database truth is a `job_runs` row under
`repair-propose`.
