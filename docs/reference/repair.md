# The repair loop — `stigmergy.repair`

A finding's path to zero, unattended. The gardener reads the corpus and fixes nothing; the
librarian WORKER answers its findings on the same idle branch it sweeps views on — deriving a
concrete change for each, validating it, proving it through the nine gates and pushing it as ONE
commit. Additive edits to pages that already exist, a drafted BODY for an entity page whose own
body says nothing about it, a MERGE of two registry entries that turn out to be the same entity,
and the one deletion code can settle by lookup. Nobody is asked, before or after; the reading
happens afterwards, on the console, from the diff the ledger stored.

**A person's own deletion is the one repair a human decides.** It enters at an authenticated door —
`brain_delete` over MCP, or the console's own Remove pages button — and lands in that same call:
the judgment was already theirs, so what a second click would supply is an authentication, and it
runs in the act ([ADR 043](../decisions/043-a-sweep-is-written.md)).

Design record: [ADR 039](../decisions/039-governed-repair-loop.md), amended by
[ADR 043](../decisions/043-a-sweep-is-written.md) and
[ADR 044](../decisions/044-the-capture-is-the-approval.md) — they hold the decisions this document
only shows the results of.
The findings themselves are covered in [`gardener-digest.md`](./gardener-digest.md),
`brain_delete`'s own tool contract in
[`server.md`](./server.md#the-capture-tools-the-write-path), and the console's panel in
[`admin-console.md`](./admin-console.md). Code map:
[`src/stigmergy/repair/index.md`](../../src/stigmergy/repair/index.md).

**The covenant, in one sentence: a MODEL declares, CODE validates twice, code applies exactly what
it validated, and the diff is stored because nobody read it first.** Nothing reaches the knowledge
repo without having passed all three, and there is no fourth step where somebody says yes.

What stands where the approval stood is three mechanical things, and each of them was already
carrying part of the weight:

- **the memory.** `content_key` identifies a repair by what it DOES, and the derivation skips any
  key the ledger holds — for an `applied` row and for a `failed` one alike. It is permanent: a
  repair somebody reverted in git stays reverted, and a repair a gate refused is not retried
  tomorrow.
- **the ceilings.** `STIGMERGY_REPAIR_CEILING` (20) bounds how many repairs one pass may land and
  how many findings it may put in front of a model at all; `STIGMERGY_REPAIR_MERGE_CEILING` (3) is
  the tighter one for the kind that retires an identity.
- **the nine gates**, which judge a diff whatever produced it, and have never known who asked.

Deletion is the one kind the covenant reads differently for, and deliberately:

- **a model may never declare one, in any spelling** — the decision is a person's at an
  authenticated door, or, for exact-duplicate `sources/` pages where it is a lookup rather than a
  judgment, code's own;
- **a person's own deletion is decided by the call that asked for it** (ADR 043 D2): they already
  judged it, and asking them to approve their own request supplied an authentication, not a second
  opinion.

And its pages are WRITTEN. Code drops the frontmatter entries that named a removed page — a lookup
— and a model writes the bodies of the pages that referred to it, so a sentence that cited one
still reads and a callout that only existed because of one is gone (ADR 043 D1).

```
  the librarian worker, queue idle            gardener findings
   on its own interval, and only when          (the latest COMPLETED run, and only
   a gardener run has completed since           if it finished after the last pass)
   the last pass                                    │
        │                                           │
        ├────────────── read ───────────────────────┘
        │
        ├─ read at the BASE: the skill, the pages, the registry
        ├─ drop findings this ledger already answered   (BEFORE any model call)
        ├─ split by check into THREE model roads, plus one that asks none
        │    edits:        a BATCH of findings → 1 call
        │    entity-body:  1 entity page → 1 call, and only with >= 2 anchored
        │    entity-alias: 1 PAIR → 1 call, the model picks the survivor,
        │                  code computes the sweep
        │    delete:       duplicate sources/ pages, derived by CODE, no model
        ├─ validate each answer against a fresh worktree, one retry
        └─ drop keys the ledger already holds           (AFTER the model)
              │
              v   one repair at a time, its OWN worktree, at a base fetched for it
        apply.apply_and_record
              ├─ the kind's own applier performs the ops in THIS tree
              ├─ the cross-check: the diff's paths == target_paths, and its
              │    SHAPE, per kind
              ├─ run_gates(ALL_GATES), told the lane and what it may suspend
              ├─ gitcmd.commit(gated_entries=…) + push, App-authored,
              │    trailer `Repair: <check> #<finding>`
              └─ a `repairs` row EITHER WAY: applied, with the commit and the
                   DIFF that landed — or failed, with the sentence that refused it

  a person, at an authenticated door — and there is nobody left to ask, so it lands in this call
   brain_delete(paths, why)  ──>  an UNRESTRICTED identity, or the lane's refusal
   (MCP / the console)       ──>  clone
                             ──>  deletion.plan: the referring set, the frontmatter
                             ──>  sweep.write: a model writes their bodies, one retry
                             ──>  the bounds, run_gates(ALL_GATES), commit + push
                             ──>  trailer `Approved-by: <the person>`
                             ──>  an `applied` row like any other, and the DIFF per
                                    page back to the caller
```

## The six checks a repair can answer

Only findings one of the three MODEL-derived kinds could actually close reach the model — the
fourth kind, `delete`, answers no finding at all and is either a person's act or derived from
duplicate content hashes. The corpus has sixteen checks in all — ten deterministic and six model —
so the other TEN are absent by NAME rather than by oversight: an aging seed needs somebody to write,
a stale view needs a regeneration (the periodic sweep converges those, issue #76), an anchor that no
longer fits is a judgment about a page's subject. None of them is an edit, a body or a merge this
vocabulary can express.

**"This loop cannot express it" is not "nothing acts on it."** `stale-view` is answered by the
librarian worker's periodic view sweep — a state-based convergence pass that regenerates the page
whole, which is exactly the shape this vocabulary refuses and exactly why it belongs somewhere
else. See [`views.md`](./views.md).

| check | what it says | the repair |
|---|---|---|
| `model-unlinked-mention` | two pages cover the same ground with no link between them | a `backlink` on one of them, or on each |
| `model-contradiction` | two pages assert things that disagree | a `contradiction` callout on BOTH sides |
| `orphan-page` | nothing in the corpus links to this page | a `backlink` on the page that ought to link to it — which the model has to FIND |
| `entity-placeholder-body` | an entity page still carries the placeholders it was minted with | an `entity-body` draft of that page's body, written from the pages anchored to the entity |
| `model-empty-entity-body` | an entity page's body is written and says nothing about that entity | the same `entity-body` draft — one road, because it is the same question judged rather than matched |
| `model-duplicate-entity` | two registry entries are the same real-world entity, registered twice | an `entity-alias` merge: the model picks which name survives, code moves the spellings, re-anchors every page and regenerates the registry |

A contradiction repair FLAGS the disagreement and never resolves it. Deciding which of two pages is
right is not something this loop does, and it could not express the edit if it were.

## The kinds, and every vocabulary is closed

A repair's `kind` says which question it is. `edits` is three additive shapes, all performed by
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
page. That is the safety argument rather than a coincidence: the nine gates were written to judge
these shapes, and `gate_body_rewrite` is what proves a diff is additive rather than promising it. A
fourth ADDITIVE op is a new question nobody has asked the gates —
`tests/test_architecture.py` pins that vocabulary equal to `page.EDIT_KINDS`.

`entity-body` is the second kind, and the only one that REPLACES text
([ADR 039's first amendment](../decisions/039-governed-repair-loop.md)). It carries exactly ONE op:

```json
{"op": "entity-body", "path": "wiki/entities/<Name>.md", "body_markdown": "…", "role": ""}
```

- **What it may touch.** Everything down to and including the page's own `# Title` survives byte
  for byte — the frontmatter block, the template's comment, the title line. Exactly two frontmatter
  lines may differ, rewritten in place: `updated:` (the apply date) and `role:`, the latter only
  when the page declares an EMPTY one. A role somebody wrote is a statement of identity.
- **When it is derived at all.** Only for a page the gardener flagged, and only when at least two
  wiki pages are anchored to that entity — the floor is checked BEFORE the model call, so an entity
  nothing has been written about costs nothing every night. Anchored pages come from the CHECKOUT
  (`entity:` frontmatter, canonicalized through the registry), never from `pages_index`.
- **When the answer is "nothing yet".** An EMPTY body is the park, not a validation failure: both
  briefs tell the drafter to return one rather than invent prose, and the pass recognises it
  before validating — so the honest answer costs one call, not a call plus a retry restating the
  instruction the model just followed. Nothing durable is stored, so the page is reported and asked
  again the next night; that recurrence is deliberate, because the answer changes as soon as the
  corpus has something to say. The apply-time validator still refuses an empty body, and that is a
  different moment: there it would erase whatever prose the page already carries.
- **What the draft may contain.** Markdown sections and nothing else: no `---` line, no H1 of its
  own, no placeholder line left in it, every `[[wikilink]]` resolving to a page that exists (the
  knowledge repo's linter treats a dead link as an error, so a draft carrying one could never be
  applied), at most `MAX_BODY_BYTES` bytes and `MAX_BODY_LINES` lines, and a `role` of at most
  `MAX_ROLE_CHARS` on one line. Every rule is checked when the draft is derived AND against the
  worktree the commit is made in, by the same function.
- **How the gates judge it.** `gate_body_rewrite`'s additive proof cannot admit a replaced body, so
  the apply TELLS the gates two caller-scoped facts: `write_prefixes=("wiki/entities/",)` — the
  lane this apply owns — and `body_rewrite_allowed={the one page}`. For a path in that set the
  additive proof is replaced by three dedicated checks (frontmatter unchanged but for those two
  keys, the page is an entity page, the path is in the lane); for every other path the gate is
  byte-identical to what it was. The librarian's own flows name no path, and
  `tests/test_architecture.py` pins the granting set to `repair/apply.py` alone.
- **Where the injection surface is.** A drafted body is model-written prose that becomes the page,
  where an additive op only ever contributed one callout sentence. The secrets, PII and contract
  gates run over it exactly as they run over a filed page, and a credential in a draft is vetoed at
  apply time with nothing pushed — and, since nobody read it first, the stored diff is where a
  person finds out what the prose actually said.

`delete` is the third kind, and the only one that removes anything
([ADR 039's second amendment](../decisions/039-governed-repair-loop.md)). Its unit is a SWEEP, not
a file, so it carries two op shapes:

```json
{"op": "delete-page", "path": "wiki/notes/<Name>.md"}
{"op": "scrub-page",  "path": "wiki/decisions/<Other>.md",
 "expected_before_hash": "<sha256 of the bytes the plan was computed from>",
 "planned_after": "<the whole page, as it would be written>"}
```

- **Who may decide one.** A person, at `brain_delete(paths, why)` over MCP or the console's own
  button — and nothing else, except the one deterministic road below. It is decided, written, gated
  and pushed in that call, and what comes back is the commit and the per-page diff. **A model may
  never declare a deletion in any spelling**: `validate_batch` drops such an op by name with a
  sentence saying the road does not exist. Judging that a page is stale is the judgment that is
  neither code's nor a model's.
- **The one automatic road.** Two `sources/` pages declaring the same `content_hash:` are the same
  document filed twice, and the pass derives a deletion for the copy that goes: the page the corpus
  CITES survives, on a tie the OLDER filing survives, on a tie in both the lexicographically first
  path survives. All three rules are total lookups, so no model is asked WHICH page goes — the one
  deliberate exception to "a model declares", and the rationale on the ledger row is composed by
  code from the two facts that decided it. What a model still writes is the bodies of the pages
  that referred to the copy that goes, exactly as a person's deletion does.
- **What may be deleted.** `wiki/notes/`, `wiki/decisions/`, `wiki/concepts/`, `sources/`,
  `views/`. Never `wiki/entities/` — nothing in this system retires an identity, and the exclusion
  is by CONSTRUCTION rather than by rule: the entity type carries no folder, so it is not in the
  lane the deletable set extends. Never `ops/` or `.claude/`, and never anything outside the three
  content zones. A whitelist, so tomorrow's zone is undeletable by default.
- **What the sweep does to everything else**, and it is split down the middle (ADR 043 D1). CODE
  owns the frontmatter: `related:`/`sources:` entries naming a going page are dropped (an emptied
  field's line goes with it) and `supersedes:`/`superseded_by:` pointers at it are removed — a
  lookup, and a model asked to do it would be re-deriving what the parser already states. A MODEL
  owns the bodies: one call over the WHOLE referring set, returning each page's body reconciled, so
  a sentence that cited a removed page still reads, a callout that only existed because of one is
  gone, and a markdown link at its path goes with the rest. Every link question is asked exactly as
  the knowledge repo's contract linter asks it (code fences and inline code blanked first, alias
  and anchor split off, the last path segment minus `.md`) plus one shape the linter does not
  count — a markdown link at a going page's path — because a writer reconciles prose. A reference
  in a frontmatter field this kind does not rewrite refuses the whole plan rather than becoming a
  question a gate would later veto; a reference in a BODY never does, since a writer reconciles
  anything.
- **What bounds the writer.** The pages it returns must be exactly the pages that refer to a going
  one; each body keeps its `# Title` line, opens no `---` block, is never emptied and may grow by
  at most a sentence or two (a sweep reconciles, it does not write); each planned page's
  frontmatter must be byte-identical to code's own scrub of the page as it stands; and nothing it
  wrote may still refer to a going page. One retry carrying the reasons, then a refusal naming the
  page — **there is no deterministic fallback**, because two writers of one page are two
  implementations that can disagree about it.
- **What the row records.** `target_paths` carries the FULL touched set, deleted and scrubbed
  alike, so the ledger row, the console's history and the returned diff all name the whole blast
  radius — the pages someone asked to remove are never the whole of what changed.
  `STIGMERGY_REPAIR_MAX_PLAN_BYTES` bounds how much one stored plan may carry, shared with the
  entity-alias kind because both hold whole pages.
- **How the apply proves it.** A written sweep cannot be recomputed, so the recomputation ADR 039
  B4 ran is replaced by three questions asked of the tree the commit is made in (ADR 043 D3): the
  two bounds above, re-run there; the per-page base hash every scrub op carries, so a page that
  CHANGED since the plan was written refuses it; and a walk of the corpus for a page the plan never
  rewrote that now refers to a going one — the latecomer B4's recomputation used to catch. On the
  act road all three are a formality that costs one walk, because the plan was made in that very
  clone. Then the gates are told `deletions_allowed={the pages that go}` and
  `expected_bytes={path: the planned file}`; for a planned path the additive proof is replaced by
  byte-equality, which is a STRONGER statement than it (additive says "nothing disappeared";
  byte-equality says "this is precisely the file that was planned"). Finally the knowledge repo's
  own linter runs over the WHOLE tree — `gate_contract` filters to the pages a diff touched, which
  is blind to a deletion's global blast radius — and any surviving link to a removed page refuses
  the commit.


`entity-alias` is the fourth kind, and the only one that changes what a NAME resolves to
([ADR 039's third amendment](../decisions/039-governed-repair-loop.md)). Its unit is a merge, so it
carries four op shapes and every one of them holds the whole file it would write:

```json
{"op": "alias-survivor",      "path": "wiki/entities/<Survivor>.md",
 "expected_before_hash": "<sha256 of the bytes the plan was computed from>",
 "planned_after": "<the whole page, as it would be written>"}
{"op": "retire-absorbed",     "path": "wiki/entities/<Absorbed>.md",  "...": "…"}
{"op": "reanchor-page",       "path": "wiki/notes/<Some Page>.md",     "...": "…"}
{"op": "regenerate-registry", "path": "ops/entity-registry.json",      "...": "…"}
```

- **What the model decides, and it is one thing.** Which of the two entity pages SURVIVES, and a
  sentence saying what makes them one entity and why that name is canonical. Which name is
  canonical is a judgment — the legal name is often the less-used one, a former name usually loses
  to a current one — and it is not a backlink count. The rationale it writes is the one sentence on
  the ledger row that says WHY, unlike the `entity-body` road where code composes the rationale
  because the draft itself is the thing worth reading.
- **What code decides, and it is everything else.** Which pages carry the absorbed entity in their
  `entity:` list, what each one says afterwards, the survivor's new `aliases:` line, the
  `superseded_by:` on the absorbed page, and the regenerated registry. **A model never computes a
  file list** — that is the deletion sweep's lesson, and an error here moves a page's whole history
  onto the wrong company.
- **The absorbed page is never deleted.** Nothing in this system retires an identity, and no
  deletion may reach the entity zone. The page stays, marked `superseded_by:` the survivor,
  demoted by the index's ranking exactly as any superseded page is, and still answering to its own name — knowing that these two
  names were once two entities is the record of the decision.
- **The survivor gains the absorbed entity's ALIASES and never its own name, and that is a
  constraint rather than a choice.** The knowledge repo's contract linter refuses an alias that
  names an existing page (`alias 'X' collides with page wiki/entities/X.md`), because the wikilink
  namespace is keyed on page names and the absorbed page is still there. So the spellings that move
  are the ones the absorbed entity carried in its `aliases:` list, and the merge REMOVES them from
  that page in the same commit (two pages claiming one alias is that linter's other error). A claim
  it would refuse is refused at PLAN time with a sentence, never left for `gate_contract` to veto.
  What follows is a residual with two halves, and the second is the one that matters. The absorbed
  id STAYS registered — its page exists, so the generator still derives it — so a future capture
  spelling that name anchors to the retired identity. And this loop can never sweep those pages up
  afterwards: the pair's `content_key` and its `finding_subjects` are both permanent, so the
  question is skipped before the model forever; and even if it were re-derived, the cross-check
  refuses, because a second merge's `retire-absorbed` op is unchanged, `target_paths` drops it, and
  the absorbed page is then absent from the diff. The residual therefore accumulates monotonically
  with no remedy inside this loop. The filing-time half of issue #77 is the only closure — a page
  carrying `superseded_by:` is exactly the signal a resolution skill can act on.
- **What one commit covers.** `target_paths` carries the full touched set — both entity pages,
  every re-anchored page and the registry when it changes — so what the console shows beside the
  diff is the whole blast radius, not the pair that named it. An op whose planned bytes equal
  the bytes it was computed from names no page the diff will touch and is excluded, which is the
  ordinary case for the registry (an absorbed entity with no aliases changes nothing about it).
  `STIGMERGY_REPAIR_MAX_PLAN_BYTES` bounds how much one stored plan may carry, shared with the
  delete kind because both hold whole pages for the same reason.
- **How the apply proves it.** The plan is RECOMPUTED from the worktree the commit is made in and
  refused unless it is identical to the derived one, op for op and byte for byte — a page that
  gained the absorbed entity's anchor in the meantime is a different merge. The pages are written,
  then `entities.generator.regenerate` — the library the librarian's own birth fold runs, and the
  only writer of `ops/entity-registry.json` in this codebase — rebuilds the registry from the entity
  pages, and the result is refused unless it is byte-identical to what the plan predicted. Then the
  gates are told
  `expected_bytes={path: the planned file}` and `derived_files={ops/entity-registry.json}`, the
  fourth caller-declared exception: `gate_zone` otherwise refuses any in-lane write that is not a
  `.md` page, and it still requires a derived file's bytes to have been computed. No deletion and no
  body rewrite is granted at all — a merge removes nothing and replaces no prose.

## The pass, and when it runs

The repair pass is the librarian worker's second maintenance loop, beside the view sweep and under
the same rules: **only while the queue is idle**, on its own interval, skipped rather than blocked
when the interval has not elapsed, and its faults logged and swallowed because filing must never
depend on maintenance. There is no command and no cron — a repair pushes, and the credential that
pushes belongs to the worker.

Two conditions gate a pass, and the second is a watermark:

- `$STIGMERGY_LIBRARIAN_REPAIR_INTERVAL_S` has elapsed since the last one (default `3600`; `0`
  turns the pass off entirely, which is the lever an operator pulls to stop the corpus repairing
  itself while they investigate something);
- a gardener run has COMPLETED since the last pass finished. Without that check the loop would
  re-derive the same findings every interval — cheap only because the ledger's memory catches each
  one afterwards, which is paying for a model call to be told what a timestamp already knew.

Then: read the latest completed gardener run's findings, keep the ones a kind can express, drop the
ones this ledger has already answered, and send what is left down whichever road its check belongs
to — the additive findings in batches, each entity page on its own, each duplicate PAIR on its own,
and the duplicate-`sources/` road last because it reads no finding at all and a deletion is the
safest thing to defer when the ceiling is full. Every road shares ONE pass ceiling
(`STIGMERGY_REPAIR_CEILING`), and it bounds two different things: how many repairs the pass may
LAND, and how many findings a per-finding road may put in front of the model at all. The second
half is not redundant — a declined draft stores nothing and is remembered by nothing, deliberately,
so without it a corpus of thin entities could spend an unbounded number of calls a night while
storing nothing (issue #103). What a ceiling defers is not lost: it is counted in the pass's
`job_runs` row as `run-ceiling-reached(N)` or `ask-ceiling-reached(N)`, and the next pass sees it.

Derivation reads a fresh detached worktree at the pass's base — never the worker's own checkout,
which sits on whatever commit the last capture left it on — and writes nothing anywhere. Then each
accepted repair is applied ONE AT A TIME, in its OWN worktree, at a base fetched for it. Per repair
rather than per pass, because each apply PUSHES: deriving them all against one commit and applying
them against another is how a merge lands on a tip the gates never judged.

The pass records itself in `job_runs` under the job `repair`, with `findings_seen`, `applied`,
`failed`, `skipped_known`, `skipped_invalid`, `skip_reasons`, `failures` and `model_calls` — and it
records itself even when it dies, because an unattended pass whose failure is invisible has no
operator-facing surface at all. When it moved something, the worker prints one `repairs:` line on
its stdout; a pass that did nothing prints nothing, so the passes that changed the corpus are not
buried.

A cooperative stop is consulted BETWEEN repairs and never inside one: a repair is a model call, the
gates and a push, and abandoning it half-way is what leaves the corpus in a state nobody chose.
What a stop prevents is picking up the next one.

## `brain_delete` — a person removes pages

```
brain_delete(paths=["wiki/notes/Old Memo.md"], why="what makes it stale")
```

One call, and everything happens inside it. **The authorization is one question, asked before
anything is cloned: is the caller an UNRESTRICTED identity** — no audience restriction in
`ops/identities.json` (ADR 044 D3). It is the one fact the server can settle at the door, and the
right one: a removal touches the pages named AND every page that refers to them, a set nothing
knows until the clone exists, so only a caller who can already see the whole corpus may ask for it.
A scoped caller gets the lane's own anonymous refusal — *there is nothing for you to decide at that
id* — which is therefore no oracle about a referrer either. The console's Remove pages button runs
the same sequence under the console's own token.

Then: `deletion.plan` for the frontmatter and the referring set, `sweep.write` for the bodies, the
nine gates, one App-authored commit with the caller in an `Approved-by:` trailer, and a push that
never rebases.

- **What comes back** is the commit, the pages removed, and a unified DIFF per rewritten page,
  fenced as untrusted data because it carries both page bytes and fresh model output. Every diff
  still passes `acl.visible()` for the caller — one place decides read access, whatever the caller
  was allowed to remove — and a page this server's index does not carry is NAMED as withheld rather
  than dropped, so nobody reads "nothing happened to it" into a silence. Nobody read that prose
  before it landed — that is the trade ADR 043 D5 states rather than softens — so the diff is the
  reading, and `git revert` in the knowledge repo is the undo.
- **The row is `applied` like any other**, so the console's Repairs ledger and the metrics read one
  table whichever door removed the pages. What says a HUMAN decided it is not a column but the
  commit: `Approved-by:` names them, where a repair the worker derived carries
  `Repair: <check> #<finding>` instead — the commit log never claims a decision nobody made.
  `model_id` names the model that wrote the pages that stay, and is empty when nothing referred to
  the removed ones; it never means a model chose the deletion, because for this kind none ever
  does.
- **What it refuses, before anything is cloned**: an audience-restricted caller, no page, no
  reason, more than `MAX_DELETED_PAGES` (10) pages in one call, a reason matching a likely
  secret. And after the clone: an entity page, a path outside the
  corpus, a plan over `STIGMERGY_REPAIR_MAX_PLAN_BYTES`, a frontmatter reference the sweep cannot
  rewrite, and a body the writer could not reconcile in one retry. Every one of those lands
  nothing at all.

| Setting | Default | Effect |
|---|---|---|
| `STIGMERGY_LIBRARIAN_REPAIR_INTERVAL_S` | `3600` | how often the idle worker runs a repair pass. `0` turns the pass off entirely; a value below the worker's own poll interval is refused at startup |
| `STIGMERGY_REPAIR_MODEL` | the librarian's own default model | which model declares |
| `STIGMERGY_REPAIR_MAX_OPS` | `6` | how much ONE repair is allowed to be |
| `STIGMERGY_REPAIR_CEILING` | `20` | how many repairs one PASS may land — and how many findings a per-finding road may put in front of the model at all, so a night of declined drafts (which store nothing, deliberately) is a bounded bill rather than an invisible one |
| `STIGMERGY_REPAIR_MERGE_CEILING` | `3` | the same bound for the one kind that retires an identity. A second number rather than a fraction of the first: what makes a merge different is not its size but that it is the least reversible thing this loop does |
| `STIGMERGY_REPAIR_BATCH` | `3` | findings per model call — and, through that, how large the call's usage budget is. Capped at 32, because this is the one knob whose blast radius is a bill |
| `STIGMERGY_REPAIR_MAX_PLAN_BYTES` | `100000` | how much ONE stored plan may carry, in bytes — shared by the two kinds that hold whole pages, `delete` and `entity-alias` |
| `STIGMERGY_REPO` | — | the checkout the pass derives and applies from |
| `STIGMERGY_INDEX_DSN` | — | where the ledger lives |

### The model budget is sized for the batch

Each model call is given a tool-call ceiling derived from how many findings it carries — six per
finding, plus one finding's worth for the call to get its bearings — with the request ceiling kept
two above it so the runaway bound can never bind before the work bound. A call that spends the
ceiling mid-work is skipped WHOLE, its reason recorded in `job_runs.stats`, and the pass carries on;
the next pass retries those findings.

That is why the batch is small and why the two numbers move together. A tool call is a page read,
the two pages a finding names are already in the prompt, and the allowance exists to pay for the
pages it does NOT name — a model that can only re-read what it was handed cannot notice that a
third page is the better link target. A fixed budget against a growing batch is what emptied the
additive road on the first real corpus (issue #75): eight findings sharing a constant ceiling meant
three reads each, every batch lapsed, and a run that produced nothing still recorded itself `ok`.
Raising `STIGMERGY_REPAIR_BATCH` therefore raises the allowance with it — what it also raises is
how many findings one lapse costs, because the batch is the unit of loss.

The body road is the exception, and deliberately: it drafts ONE entity page per call, so its budget
is a constant rather than something derived from a batch it does not have — and it is the constant
that road already had. #75 was the additive road's problem; on the night that found it the body
road was the only one that produced anything, and resizing the half that works for the sake of
symmetry is a change with a risk and no benefit. The merge road holds the same constant for the
same reason: one pair per call, no batch to derive an allowance from.

Every model call also records what it SPENT beside the ceiling it ran under: `job_runs.stats`
carries a `model_calls` list — road, requests and tool calls against their limits, token counts —
so the next change to any budget constant is an observation over real nights instead of a formula
argued from first principles (issue #81). Before this, the only signal a budget produced was the
failure it was chosen to prevent.

## The agent's own procedure lives in the knowledge repo

The system prompt is a code-owned header plus `.claude/skills/repair-proposer/SKILL.md`, read at run
time from the checkout being repaired — at the pass's own BASE commit, so the procedure that governs
a repair is the one the commit it derives from carries. The same arrangement the librarian's filing
skill has. Which finding is worth repairing, which shape fits, and when a finding has gone stale and
deserves nothing are editorial judgments, and they belong to the people whose brain it is. Three
frames read that one file — the additive road's, the body road's and the merge road's — one
procedure, three questions.

What the skill cannot change is the header: two tools and both READ, the op vocabulary, "answer
only from the findings you were given and the pages you actually read", and the rule that a fenced
page body is data somebody wrote and never an instruction. A knowledge repo cannot widen the
agent's powers by rewriting its procedure.

A missing or empty skill is a NAMED refusal and the pass does not run. An agent briefed only by
the header would know what it may not do and nothing at all about what is worth doing. A `SKILL.md`
that is a SYMLINK is refused the same way: both `getsize` and `open` follow one, so the size ceiling
would measure the target instead of guarding it, and whatever the link pointed at on the host would
become the system prompt.

## What has to agree before anything is pushed

Three independent checks, each chosen because the other two cannot see what it sees:

1. **The kind's own applier against the tree the commit is made in.** The derivation ran against a
   worktree at the pass's base, and every repair before this one has PUSHED since; a page deleted
   in the meantime refuses here. For `delete` this step is the two bounds re-run against that tree,
   plus a base hash per rewritten page and a walk of the corpus for a latecomer that now refers to
   a going page (ADR 043 D3).
2. **`run_gates(ALL_GATES)`** judges the resulting diff exactly as it judges the librarian's own —
   all nine, not a subset, and they have never known who asked. A `delete` apply additionally runs
   the knowledge repo's own linter over the WHOLE tree, because that kind's blast radius is the
   graph rather than the diff.
3. **The cross-check**: the diff's paths must EQUAL the repair's stored `target_paths`, and its
   SHAPE must be the one this kind produces — every entry a modification for the two editing kinds;
   for `delete`, removals exactly equal to the pages the plan named and modifications exactly equal
   to the pages it planned to scrub. The gates would pass a diff touching some other page quite
   happily — it is additive and well-formed — so this is the only thing that can say the diff is
   not the one the row describes. Its reach, stated exactly: an `ops` blob that disagrees with
   `target_paths` cannot reach `main`. Content is not compared, and a tamper that edited both
   columns consistently before the row was read is out of scope — write access to
   `repairs` is the prerequisite either way, so this is a consistency check between two
   stored facts, not a defense against a database somebody else writes to.

Then `gitcmd.commit(gated_entries=…)` closes the last window: the diff the gates approved is the
diff that lands, bytes included. The commit is authored by the librarian App, its message names what
the repair did and the findings it answers, and its trailer says who decided — `Repair:` for a
repair the worker derived, `Approved-by:` for the deletion a person performed.

The push closes one more, for the two NON-ADDITIVE kinds: it never rebases. Their apply is a proof
against a base — the bounds and the base hashes, then perform — and a rebase would replay the
judged diff onto a tip the gates never saw: a delete can leave a dead link a fresh plan would have
scrubbed, a merge can leave a page anchored to the retired identity forever. A push that loses the
race (the view sweep pushes up to its ceiling every interval, so it is a race that happens) fails
CLEAN — nothing lands and the row is `failed`. The additive kinds keep `gitcmd.push`'s ordinary
rebase-and-retry: their gates judged content, not a position against a base, so a backlink replayed
onto the moved tip is exactly what was proven.

**A failed apply stays failed, and is never retried.** The status is `failed`, the `error` column
says why in a sentence written to be read by an operator, and the repair's `content_key` is
remembered like any other. That is the deliberate trade of an unattended loop: a gate's refusal is
deterministic for the same bytes, so retrying it every night would spend a model call to be refused
again — and the `error` column is the only place anybody will ever find out why a finding stopped
being answered. The finding itself stays in the gardener's report.

**Unless the refusal was about the corpus rather than about the repair.** A page deleted since the
derivation, or a plan whose bytes another repair from this very pass had already changed, raises
`CorpusMovedError` — recorded `failed`, so it is visible, with NO content key, so the next pass
derives it again against what is there now. Without that exception a race would retire a finding:
two duplicate-entity merges in one pass are derived against one base, and the first one to push
regenerates the registry under the second. (The merge road answers that twice over: its ops are
also RE-PLANNED in the tree they are about to be committed from, from the one thing the model
decided — which identity survives.)

The one residue this shape can leave is a crash between a successful push and the ledger write: the
commit is in the corpus and the ledger does not know it. The next pass re-derives that repair, finds
the ops already performed and refuses with "this repair changes nothing" — visible, harmless, and
self-clearing.

## The memory

A repair is identified by WHAT IT DOES — its kind plus its sorted `op:path:link` lines, hashed into
`content_key` — and the derivation skips any key the ledger holds. It is what stands where an
approval used to, so it is worth being exact about which rows carry one:

- **`applied` carries a key**, which is obvious for "do not do it twice" and less obvious for the
  case that matters: it is what makes a repair somebody REVERTED in git stay reverted rather than
  coming back the next night. A human's "no" is a commit (ADR 044 D5).
- **`failed` carries a key too**, and that is the deliberate one — see above — except when the
  refusal was a `CorpusMovedError`, which carries none.
- **`skipped` carries none.** A skipped row is a repair that was never derived: a finding no kind
  can express, a ceiling that bound, a model that declined. There is nothing to key on, so it is
  remembered by nothing and the next pass is free to try once the corpus has moved. That is why the
  UNIQUE index over `content_key` is PARTIAL (`content_key <> ''`), and that index is also the
  race: two workers deriving the same repair, one loses and is told.

`note` is deliberately excluded from the key: two repairs adding the same callout to the same page
with differently-worded sentences are the same question answered twice, and one already applied must
not land again tomorrow with the sentence reworded. A deletion's SCRUB set is excluded for a sharper
version of the same reason — a page that gained a link to the doomed page overnight changes the plan
and changes nothing about the question, so a deletion is keyed on the pages that GO and nothing
else. The drafted body is excluded for the identical reason — **a re-drafted body is the same
repair** — and a merge goes one step further still, dropping the op NAME as well so the two paths
key as an unordered PAIR: which of two entities survives may legitimately come out the other way
tomorrow, and a key carrying the direction would let the loop merge them back the moment the answer
flipped.

**There are two halves and they answer different questions.** `content_key` is the authoritative one
and runs AFTER the model, so a repair already in the ledger is never applied twice. A cheap skip runs
BEFORE the model, so it does not cost a call every night either, and it keys on what the finding
NAMED — the `finding_subjects` column, one sorted page set per finding a repair answers.
`target_paths` alone was not enough: an `orphan-page` finding names the page nothing links to while
the repair edits the page that ought to link to it, and a one-sided answer to a two-page finding
names one page of two. Both rules are EXACT — a finding id, or a page set — because anything looser
would suppress a legitimate second repair on a page that already has one, and an over-eager skip is
invisible while a missed one only costs a model call that `content_key` then throws away.

## Reading what it did

The reading nobody gave a repair before it landed happens on the console's **Repairs** page, which
is the `repairs` ledger and nothing else: every outcome the deployment ever produced as a
part-to-whole, the worker's own pass history as a run strip, and a bounded page of the most recent
rows, newest first, filterable by outcome. **Nothing on that page decides anything** — its one
write is Remove pages, which is the deletion a person performs and is a repair like any other in
everything but its trailer.

The three outcomes are three different readings rather than three colours of one:

- an **`applied`** row carries the commit and the DIFF that landed, rendered as a diff and not
  summarised — the reason the column exists at all is that nobody read those bytes first, so a page
  that summarised them would be showing a summary of prose a model wrote into somebody's corpus;
- a **`failed`** row wrote nothing, and what there is to read is the sentence that refused it beside
  the ops it never got to write;
- a **`skipped`** row never became a repair and carries only its reason.

The weekly digest carries the other half: how many repairs applied in the window, by kind. A count
and never a page list — a repair names the pages it edited, and that broadcast has no audience to
scope them against ([`gardener-digest.md`](./gardener-digest.md)).

The undo is `git revert` in the knowledge repo, by somebody with a checkout — and a reverted repair
is never derived again, because its key is permanent.
