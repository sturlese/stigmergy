# The governed repair loop — `stigmergy.repair`

A finding's path to zero. `stigmergy-repair propose` turns the gardener's findings into concrete
changes a steward can approve one at a time — additive edits to pages that already exist, and a
drafted BODY for an entity page that has never had one; the review lane and the admin console are
where one is approved; and only then does code perform exactly the approved ops, through the
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
  (the latest COMPLETED run)         ├─ split by check into TWO roads      ├─ review_queue / review_decide
   model-unlinked-mention            │   edits:  batch → 1 call/batch      │    (MCP, item_kind
   model-contradiction               │   entity-body: 1 page → 1 call,     │     "repair-proposal")
   orphan-page                       │     and only with >= 2 anchored     └─ the console's Repairs tab
   entity-placeholder-body           ├─ drop keys already reviewed                 │
        │                            ├─ the model, 2 READ tools                    │  approve
        └────────── read ───────────>├─ validate the answer, one retry             v
                                     ├─ validate against the real         server.review.apply_repair_and_record
                                     │    checkout (the kind's own          ├─ mark_decided (WHERE pending)
                                     │    validator, the applier's)         ├─ clone → the kind's applier
                                     └─ INSERT ... status='pending'         ├─ the cross-check: the diff's
                                          content_key = kind + what it      │    paths == target_paths, all M
                                          would do                          ├─ run_gates(ALL_GATES), told the
                                                                            │    lane and the permitted path
                                                                            ├─ gitcmd.commit(gated_entries=…)
                                                                            │    + push, App-authored
                                                                            └─ mark_applied + review_decisions
```

## The four checks a repair can answer

Only findings one of the two kinds could actually close reach the proposer. The other five are
absent by NAME rather than by oversight: an aging seed needs somebody to write, a stale view needs a
regeneration, an anchor that no longer fits is a judgment about a page's subject. None of them is an
edit or a body this vocabulary can express.

| check | what it says | the repair |
|---|---|---|
| `model-unlinked-mention` | two pages cover the same ground with no link between them | a `backlink` on one of them, or on each |
| `model-contradiction` | two pages assert things that disagree | a `contradiction` callout on BOTH sides |
| `orphan-page` | nothing in the corpus links to this page | a `backlink` on the page that ought to link to it — which the proposer has to FIND |
| `entity-placeholder-body` | an entity page still carries the placeholders it was minted with | an `entity-body` draft of that page's body, written from the pages anchored to the entity |

A contradiction repair FLAGS the disagreement and never resolves it. Deciding which of two pages is
right is not something this loop does, and it could not express the edit if it were.

## Two kinds, and both vocabularies are closed

A proposal's `kind` says which question it is. `edits` is three additive shapes, all performed by
`edits.apply_declared` — the same function a filing capture's declared edits go through:

- **`backlink`** — adds `[[link]]` to that page's `related:` list.
- **`overlap`** — the same link, plus a `> [!NOTE] Overlaps with [[link]]` callout carrying a
  one-sentence `note`.
- **`contradiction`** — the same link, plus a `> [!WARNING] Contradiction with [[link]]` callout.

`note` is required for `overlap` and `contradiction` and ignored for `backlink`. `path` is the page
that CHANGES and must be in one of the fast lane's three folders (`wiki/notes/`,
`wiki/decisions/`, `wiki/concepts/`); `link` is a bare page name and may resolve to any page,
including an entity page. Editing `wiki/entities/`, `sources/` or `views/` is refused.

Nothing in that kind rewrites a sentence, deletes anything, moves anything, or creates or removes a
page. That is the safety argument rather than a coincidence: the eight gates were written to judge
these shapes, and `gate_body_rewrite` is what proves a diff is additive rather than promising it. A
fourth ADDITIVE op is a new question nobody has asked the gates —
`tests/test_architecture.py` pins that vocabulary equal to `page.EDIT_KINDS`.

`entity-body` is the second kind, and the only one that REPLACES text
([ADR 039's amendment](../decisions/039-governed-repair-loop.md)). It carries exactly ONE op:

```json
{"op": "entity-body", "path": "wiki/entities/<Name>.md", "body_markdown": "…", "role": ""}
```

- **What it may touch.** Everything down to and including the page's own `# Title` survives byte
  for byte — the frontmatter block, the template's comment, the title line. Exactly two frontmatter
  lines may differ, rewritten in place: `updated:` (the apply date) and `role:`, the latter only
  when the page declares an EMPTY one. A role somebody wrote is a statement of identity.
- **When it is proposed at all.** Only for a page the gardener flagged, and only when at least two
  wiki pages are anchored to that entity — the floor is checked BEFORE the model call, so an entity
  nothing has been written about costs nothing every night. Anchored pages come from the CHECKOUT
  (`entity:` frontmatter, canonicalized through the registry), never from `pages_index`.
- **What the draft may contain.** Markdown sections and nothing else: no `---` line, no H1 of its
  own, no placeholder line left in it, every `[[wikilink]]` resolving to a page that exists (the
  knowledge repo's linter treats a dead link as an error, so a draft carrying one could never be
  applied), at most `MAX_BODY_BYTES` bytes and `MAX_BODY_LINES` lines, and a `role` of at most
  `MAX_ROLE_CHARS` on one line. Every rule is checked at propose time AND against the fresh clone
  at apply time, by the same function.
- **How the gates judge it.** `gate_body_rewrite`'s additive proof cannot admit a replaced body, so
  the apply TELLS the gates two caller-scoped facts: `write_prefixes=("wiki/entities/",)` — the
  lane this apply owns — and `body_rewrite_allowed={the one page}`. For a path in that set the
  additive proof is replaced by three dedicated checks (frontmatter unchanged but for those two
  keys, the page is an entity page, the path is in the lane); for every other path the gate is
  byte-identical to what it was. The librarian's own flows name no path, and
  `tests/test_architecture.py` pins the granting set to `repair/remote.py` alone.
- **Where the injection surface is.** A drafted body is model-written prose that becomes the page,
  where an additive op only ever contributed one callout sentence. The secrets, PII and contract
  gates run over it exactly as they run over a filed page, and a credential in a draft is vetoed at
  apply time with nothing pushed.

## `stigmergy-repair`

```
stigmergy-repair [--dsn DSN] [--repo PATH] [--json] propose
stigmergy-repair [--dsn DSN] [--repo PATH] [--json] list
stigmergy-repair [--dsn DSN] [--repo PATH] [--json] show <id>
```

- **`propose`** — the pass a cron runs. Reads the latest COMPLETED gardener run, keeps the
  proposable findings, drops the ones whose repair has already been reviewed, and sends what is
  left down whichever road its check belongs to — the additive findings in batches, each entity
  page on its own — then validates and inserts one pending row per surviving proposal. Both roads
  share ONE run ceiling: it is how many decisions a night may ask a person for. Stops at `STIGMERGY_REPAIR_MAX_PROPOSALS` — an answer carrying more than that
  is refused whole so the model re-cuts it, and a run that fills the ceiling stops batching and
  records what it left for the next pass. Records a `job_runs` row under the job `repair-propose`
  with `findings_seen` / `proposed` / `skipped_known` / `skipped_invalid`. Exits 0 when it proposes
  nothing — an ordinary outcome, not a failure.
- **`list`** — what waits on a steward, plus what was recently decided.
- **`show <id>`** — what one proposal would change, rendered from the ops without touching git. For
  an `entity-body` proposal that is the drafted body in full: the draft is the whole of what a
  steward judges, so a preview that summarised it would hide the only thing worth reading.

**There is no `apply`, and there will not be one.** A terminal knows who is typing and not what they
are allowed to approve; applying goes through a door that decides. `--repo` (or `$STIGMERGY_REPO`)
must be a real git checkout, because a proposal is validated against the pages that are actually
committed there.

| Setting | Default | Effect |
|---|---|---|
| `STIGMERGY_REPAIR_MODEL` | the librarian's own default model | which model proposes |
| `STIGMERGY_REPAIR_MAX_OPS` | `6` | how much ONE approval is allowed to be |
| `STIGMERGY_REPAIR_MAX_PROPOSALS` | `20` | how many approvals one RUN may ask for |
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
the header would know what it may not do and nothing at all about what is worth doing. A `SKILL.md`
that is a SYMLINK is refused the same way: both `getsize` and `open` follow one, so the size ceiling
would measure the target instead of guarding it, and whatever the link pointed at on the host would
become the system prompt.

## Deciding one

A pending proposal appears in the review inbox as `repair-proposal`, alongside `entity-proposal` and
`parked-capture`, and in the console's Repairs tab. It carries its rationale, the pages it would
edit, and a count of its ops with their kinds — never the ops themselves, because a list is a scan;
the ops, and a body draft in full, are one click or one `stigmergy-repair show` away.

- **Verdicts are `approve` and `reject` only.** A proposal IS its edits, so the thing to change
  about one is which edits it contains, which is a different proposal.
- **Approving needs a steward for EVERY page the proposal would edit.** `ops/stewards.json` exists
  to delegate zones, and this is the first verdict in the lane that can land inside one, so the
  question is asked per path rather than universally. A proposal spanning two zones needs somebody
  who stewards both — either steward may still reject it, and the pair can be proposed as two
  one-sided repairs.
- **Rejecting requires a reason**, and the reason lands on the proposal as well as in the ledger.
  A note on an APPROVE is optional and lands in both places too — it is the only record of why a
  repair was worth applying.
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
   entry must be a modification. The gates would pass a diff touching some other page quite
   happily — it is additive and well-formed — so this is the only thing that can say the diff is
   not the one the row describes. Its reach, stated exactly: an `ops` blob that disagrees with
   `target_paths` cannot reach `main`. Content is not compared, and a tamper that edited both
   columns consistently before the row was read is out of scope — write access to
   `repair_proposals` is the prerequisite either way, so this is a consistency check between two
   stored facts, not a defense against a database somebody else writes to.

Then `gitcmd.commit(gated_entries=…)` closes the last window: the diff the gates approved is the
diff that lands, bytes included. The commit is authored by the librarian App, its message names the
proposal and the findings it answers, and it carries an `Approved-by:` trailer naming the human.

**A failed apply stays failed.** The status becomes `failed`, the `error` column says why in a
sentence written to be read by a steward, and the approved status is not restored. A silent revert
to pending would hide that a gate refused, which is the outcome an operator most needs to see.

## The dismissal memory

A proposal is identified by WHAT IT WOULD DO — its kind plus its sorted `op:path:link` lines, hashed
into `content_key` — and the proposer skips a key held by a pending, approved, rejected or applied
row. "Reviewed and declined" is a durable fact, and a steward who says no once is not asked the
same question by the next night's run.

**A `failed` row is not a dismissal.** It is the one status the memory does not hold: a rejection is
a human saying no, while a failed apply is a human having said yes to something that then hit a
gate, a race or a fault. The row stays visible with its reason, and the next run may derive the same
repair again — which is the only way back for a repair somebody actually wanted.

`note` is deliberately excluded from the key: two proposals adding the same callout to the same page
with differently-worded sentences are the same question asked twice, and a rephrasing of a declined
repair is not a new one. The drafted body is excluded for the identical reason — **a re-drafted body
is the same question**, and a steward who decided a page needs writing by a person should not meet
another draft of it tomorrow. The UNIQUE index is narrower than the skip rule — one PENDING row per key,
not one row ever — so re-proposing after a rejection stays a human decision rather than a database
error.

**There are two halves and they answer different questions.** `content_key` is the authoritative
one and runs AFTER the model, so a declined repair is never queued twice. A cheap skip runs BEFORE
the model, so a declined repair does not cost a call every night either, and it keys on what the
finding NAMED — the `finding_subjects` column, one sorted page set per finding a proposal answers.
`target_paths` alone was not enough: an `orphan-page` finding names the page nothing links to while
the repair edits the page that ought to link to it, and a one-sided answer to a two-page finding
names one page of two.

## The cron

`deploy/workflows/repair-propose.yml` runs daily at ~06:07 UTC, an hour after the gardener's ~05:07,
so the findings it reads are this morning's. It is a template: copy it into your knowledge repo,
like the other three, because its log carries page paths and page names out of the corpus. It needs
`INDEX_DSN` and `OPENAI_API_KEY` (both already shared with `index-rebuild.yml` and `gardener.yml`)
and nothing else — no Slack token, and no App credential, because this job proposes and cannot
apply. The console's Crons tab can dispatch it, and its database truth is a `job_runs` row under
`repair-propose`.
