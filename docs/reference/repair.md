# The governed repair loop — `stigmergy.repair`

A finding's path to zero. `stigmergy-repair propose` turns the gardener's findings into concrete
changes a person approves one at a time — additive edits to pages that already exist, a drafted
BODY for an entity page whose own body says nothing about it, and a MERGE of two registry entries
that turn out to be the same entity. The console's Repairs page is where one is approved; and only
then does code perform exactly the approved ops, through the librarian's own validator, its nine
gates and its governed commit.

**A person's own deletion is not one of those.** It enters at an authenticated door —
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

**The covenant, in one sentence: a MODEL proposes, CODE validates twice, a PERSON approves, and
code applies exactly what was approved.** Nothing reaches the knowledge repo without having passed
all four. Deletion is the one kind that reads differently on two of them, and deliberately:

- **a model may never propose one, in any spelling** — the proposer is a person at an
  authenticated door, or, for exact-duplicate `sources/` pages where the decision is a lookup
  rather than a judgment, code itself;
- **a person's own deletion is decided by the call that asked for it** (ADR 043 D2): they already
  judged it, and asking them to approve their own request supplied an authentication, not a second
  opinion. What CODE derived overnight still waits for somebody on the console's Repairs page.

And its pages are WRITTEN. Code drops the frontmatter entries that named a removed page — a lookup
— and a model writes the bodies of the pages that referred to it, so a sentence that cited one
still reads and a callout that only existed because of one is gone (ADR 043 D1).

```
  gardener findings                stigmergy-repair propose            a person, one at a time
  (the latest COMPLETED run)         ├─ split by check into THREE model     └─ the console's
   model-unlinked-mention            │    roads, plus one that asks none         Repairs page
   model-contradiction               │   edits:  batch → 1 call/batch             │
   orphan-page                       │   entity-body: 1 page → 1 call,            │
   entity-placeholder-body           │     and only with >= 2 anchored            │
   model-empty-entity-body           │   entity-alias: 1 PAIR → 1 call,           │
   model-duplicate-entity            │     the model picks the survivor,          │  approve
        │                            │     code computes the sweep                v
        │                            │   delete: duplicate sources/ pages,        │
        └────────── read ───────────>│     derived by CODE, no model              │
                                     ├─ drop keys already reviewed        server.review.apply_repair_and_record
                                     ├─ the model, 2 READ tools             ├─ mark_decided (WHERE pending)
                                     ├─ validate the answer, one retry      ├─ clone → the kind's applier
                                     ├─ validate against the real           ├─ the cross-check: the diff's
                                     │    checkout (the kind's own          │    paths == target_paths, and
                                     │    validator, the applier's)         │    its SHAPE, per kind
                                     └─ INSERT ... status='pending'         ├─ run_gates(ALL_GATES), told the
                                          content_key = kind + what it      │    lane and what it may suspend
                                          would do                          ├─ gitcmd.commit(gated_entries=…)
                                                                            │    + push, App-authored
                                                                            └─ mark_applied, the verdict on
                                                                                 the row, admin_actions

  a person, at an authenticated door — and there is nobody left to ask, so it lands in this call
   brain_delete(paths, why)  ──>  an UNRESTRICTED identity, or the lane's refusal
   (MCP / the console)       ──>  clone
                             ──>  deletion.plan: the referring set, the frontmatter
                             ──>  sweep.write: a model writes their bodies, one retry
                             ──>  the bounds, run_gates(ALL_GATES), commit + push
                             ──>  the row is born `approved`, then `applied`
                             ──>  back to the caller: the commit, and the DIFF per page
```

## The six checks a repair can answer

Only findings one of the three MODEL-proposed kinds could actually close reach the proposer — the
fourth kind, `delete`, answers no finding at all and is proposed by a person or derived from
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
| `orphan-page` | nothing in the corpus links to this page | a `backlink` on the page that ought to link to it — which the proposer has to FIND |
| `entity-placeholder-body` | an entity page still carries the placeholders it was minted with | an `entity-body` draft of that page's body, written from the pages anchored to the entity |
| `model-empty-entity-body` | an entity page's body is written and says nothing about that entity | the same `entity-body` draft — one road, because it is the same question judged rather than matched |
| `model-duplicate-entity` | two registry entries are the same real-world entity, registered twice | an `entity-alias` merge: the model picks which name survives, code moves the spellings, re-anchors every page and regenerates the registry |

A contradiction repair FLAGS the disagreement and never resolves it. Deciding which of two pages is
right is not something this loop does, and it could not express the edit if it were.

## The kinds, and every vocabulary is closed

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
- **When it is proposed at all.** Only for a page the gardener flagged, and only when at least two
  wiki pages are anchored to that entity — the floor is checked BEFORE the model call, so an entity
  nothing has been written about costs nothing every night. Anchored pages come from the CHECKOUT
  (`entity:` frontmatter, canonicalized through the registry), never from `pages_index`.
- **When the answer is "nothing yet".** An EMPTY body is the park, not a validation failure: both
  briefs tell the drafter to return one rather than invent prose, and the proposer recognises it
  before validating — so the honest answer costs one call, not a call plus a retry restating the
  instruction the model just followed. Nothing durable is stored, so the page is reported and asked
  again the next night; that recurrence is deliberate, because the answer changes as soon as the
  corpus has something to say. The apply-time validator still refuses an empty body, and that is a
  different moment: there it would erase whatever prose the page already carries.
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

`delete` is the third kind, and the only one that removes anything
([ADR 039's second amendment](../decisions/039-governed-repair-loop.md)). Its unit is a SWEEP, not
a file, so it carries two op shapes:

```json
{"op": "delete-page", "path": "wiki/notes/<Name>.md"}
{"op": "scrub-page",  "path": "wiki/decisions/<Other>.md",
 "expected_before_hash": "<sha256 of the bytes the plan was computed from>",
 "planned_after": "<the whole page, as it would be written>"}
```

- **Who may propose one.** A person, at `brain_delete(paths, why)` over MCP or the console's own
  button — and nothing else, except the one deterministic road below. Theirs is not a proposal at
  all: it is decided, written, gated and pushed in that call, and what comes back is the commit and
  the per-page diff. **A model may never propose a deletion in
  any spelling**: `validate_batch` drops such an op by name with a sentence saying the road does
  not exist. Judging that a page is stale is the judgment that is neither code's nor a model's.
- **The one automatic road.** Two `sources/` pages declaring the same `content_hash:` are the same
  document filed twice, and the nightly propose run derives a deletion for the copy that goes: the
  page the corpus CITES survives, on a tie the OLDER filing survives, on a tie in both the
  lexicographically first path survives. All three rules are total lookups, so no model is asked —
  the one deliberate exception to "a model proposes", and the rationale a person reads on the
  console is composed by code from the two facts that decided it.
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
  `STIGMERGY_REPAIR_MAX_PLAN_BYTES` bounds how much one approval may be, shared with the
  entity-alias kind because both store whole pages.
- **How the apply proves it.** A written sweep cannot be recomputed, so the recomputation ADR 039
  B4 ran is replaced by three questions asked of the clone (ADR 043 D3): the two bounds above,
  re-run there; the per-page base hash every scrub op carries, so a page that CHANGED since the
  plan was written refuses it; and a walk of the corpus for a page the plan never rewrote that now
  refers to a going one — the latecomer B4's recomputation used to catch. On the act road all three
  are a formality that costs one walk, because the plan was made in that very clone. Then the gates
  are told `deletions_allowed={the pages that go}` and
  `expected_bytes={path: the planned file}`; for a planned path the additive proof is replaced by
  byte-equality, which is a STRONGER statement than it (additive says "nothing disappeared";
  byte-equality says "this is precisely the file that was approved"). Finally the knowledge repo's
  own linter runs over the WHOLE clone — `gate_contract` filters to the pages a diff touched, which
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
  to a current one — and it is not a backlink count. The rationale it writes is what a person reads
  beside Approve, unlike the `entity-body` road where code composes the rationale because the draft
  itself is the thing being judged.
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
  question is skipped before the model forever; and even if it were re-proposed, the cross-check
  refuses, because a second merge's `retire-absorbed` op is unchanged, `target_paths` drops it, and
  the absorbed page is then absent from the diff. The residual therefore accumulates monotonically
  with no remedy inside this loop. The filing-time half of issue #77 is the only closure — a page
  carrying `superseded_by:` is exactly the signal a resolution skill can act on.
- **What one approval covers.** `target_paths` carries the full touched set — both entity pages,
  every re-anchored page and the registry when it changes — so what the console shows beside
  Approve is the whole blast radius, not the pair that named it. An op whose planned bytes equal
  the bytes it was computed from names no page the diff will touch and is excluded, which is the
  ordinary case for the registry (an absorbed entity with no aliases changes nothing about it).
  `STIGMERGY_REPAIR_MAX_PLAN_BYTES` bounds how much one approval may be, shared with the delete
  kind because both store whole pages for the same reason.
- **How the apply proves it.** The plan is RECOMPUTED from the fresh clone and refused unless it is
  identical to the stored one, op for op and byte for byte — a page that gained the absorbed
  entity's anchor since the proposal was made is a different merge. The pages are written, then
  `entities.generator.regenerate` — the library the librarian's own birth fold runs, and the only
  writer of `ops/entity-registry.json` in this codebase — rebuilds the registry from the entity
  pages, and the result is refused unless it is byte-identical to what the plan predicted. Then the
  gates are told
  `expected_bytes={path: the planned file}` and `derived_files={ops/entity-registry.json}`, the
  fourth caller-declared exception: `gate_zone` otherwise refuses any in-lane write that is not a
  `.md` page, and it still requires a derived file's bytes to have been computed. No deletion and no
  body rewrite is granted at all — a merge removes nothing and replaces no prose.

## `stigmergy-repair`

```
stigmergy-repair [--dsn DSN] [--repo PATH] [--json] propose
stigmergy-repair [--dsn DSN] [--repo PATH] [--json] list
stigmergy-repair [--dsn DSN] [--repo PATH] [--json] show <id>
```

- **`propose`** — the pass a cron runs. Reads the latest COMPLETED gardener run, keeps the
  proposable findings, drops the ones whose repair has already been reviewed, and sends what is
  left down whichever road its check belongs to — the additive findings in batches, each entity
  page on its own, each duplicate PAIR on its own — then validates and inserts one pending row per
  surviving proposal. Every road shares ONE run ceiling: it is how many decisions a night may ask a
  person for. Stops at `STIGMERGY_REPAIR_MAX_PROPOSALS` — an answer carrying more than that
  is refused whole so the model re-cuts it, and a run that fills the ceiling stops batching and
  records what it left for the next pass. Records a `job_runs` row under the job `repair-propose`
  with `findings_seen` / `proposed` / `skipped_known` / `skipped_invalid`. Exits 0 when it proposes
  nothing — an ordinary outcome, not a failure.
- **`list`** — what waits on a decision, plus what was recently decided.
- **`show <id>`** — what one proposal would change, rendered from the ops without touching git. For
  an `entity-body` proposal that is the drafted body in full: the draft is the whole of what is
  being judged, so a preview that summarised it would hide the only thing worth reading. A
  `delete` proposal shows which pages stop existing and, for each page it rewrites, the page it
  would BECOME in full — for the same reason, since ADR 043 made those bytes a model's prose and a
  person deciding a pending deletion is the only reader they get before they land. An
  `entity-alias` proposal shows which identity absorbs which and how many pages move with it,
  never its planned bytes: what is judged there is the merge, and four whole files would
  bury it.
**There is no `apply`, and there will not be one. There is no `delete` either, since ADR 043.** A
terminal knows who is typing and not what they are allowed to approve, so neither verb can be
authorized from one: applying goes through a door that decides, and a deletion — which a person
judges and code then carries out — is now itself an act at such a door (`brain_delete`, below).
`--repo` (or `$STIGMERGY_REPO`) must be a real git checkout, because a proposal is validated
against the pages that are actually committed there.

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
- **The row is born `approved`** in the caller's name and applied at once, so the console's
  Repairs history and the metrics read one ledger whichever door removed the pages, and nothing is
  ever listed as pending. `model_id` names the model that wrote the pages that stay, and is empty
  when nothing referred to the removed ones — no model decides WHICH page goes, ever.
- **What it refuses, before anything is cloned**: an audience-restricted caller, no page, no
  reason, more than `MAX_DELETED_PAGES` (10) pages in one call, a reason matching a likely
  secret. And after the clone: an entity page, a path outside the
  corpus, a plan over `STIGMERGY_REPAIR_MAX_PLAN_BYTES`, a frontmatter reference the sweep cannot
  rewrite, and a body the writer could not reconcile in one retry. Every one of those lands
  nothing at all.

| Setting | Default | Effect |
|---|---|---|
| `STIGMERGY_REPAIR_MODEL` | the librarian's own default model | which model proposes |
| `STIGMERGY_REPAIR_MAX_OPS` | `6` | how much ONE approval is allowed to be |
| `STIGMERGY_REPAIR_MAX_PROPOSALS` | `20` | how many approvals one RUN may ask for — and how many findings a per-finding road may put in front of the model at all, so a night of declined drafts (which store nothing, deliberately) is a bounded bill rather than an invisible one |
| `STIGMERGY_REPAIR_BATCH` | `3` | findings per model call — and, through that, how large the call's usage budget is |
| `STIGMERGY_REPAIR_MAX_PLAN_BYTES` | `100000` | how much ONE approval may be, in the bytes its stored plan carries — shared by the two kinds that store whole pages, `delete` and `entity-alias` |
| `STIGMERGY_REPO` | — | the checkout to propose against |
| `STIGMERGY_INDEX_DSN` | — | where the proposals live |

### The model budget is sized for the batch

Each model call is given a tool-call ceiling derived from how many findings it carries — six per
finding, plus one finding's worth for the call to get its bearings — with the request ceiling kept
two above it so the runaway bound can never bind before the work bound. A call that spends the
ceiling mid-work is skipped WHOLE, its reason recorded in `job_runs.stats`, and the run carries on;
the next run retries those findings.

That is why the batch is small and why the two numbers move together. A tool call is a page read,
the two pages a finding names are already in the prompt, and the allowance exists to pay for the
pages it does NOT name — a proposer that can only re-read what it was handed cannot notice that a
third page is the better link target. A fixed budget against a growing batch is what emptied the
additive road on the first real corpus (issue #75): eight findings sharing a constant ceiling meant
three reads each, every batch lapsed, and a run that proposed nothing still recorded itself `ok`.
Raising `STIGMERGY_REPAIR_BATCH` therefore raises the allowance with it — what it also raises is
how many findings one lapse costs, because the batch is the unit of loss.

The body road is the exception, and deliberately: it drafts ONE entity page per call, so its budget
is a constant rather than something derived from a batch it does not have — and it is the constant
that road already had. #75 was the additive road's problem; on the night that found it the body
road was the only one that produced anything, and resizing the half that works for the sake of
symmetry is a change with a risk and no benefit.

Every model call also records what it SPENT beside the ceiling it ran under: `job_runs.stats`
carries a `model_calls` list — road, requests and tool calls against their limits, token counts —
so the next change to any budget constant is an observation over real nights instead of a formula
argued from first principles (issue #81). Before this, the only signal a budget produced was the
failure it was chosen to prevent.

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

A pending proposal appears on the console's Repairs page, and nowhere else — there is one door.
It carries its rationale, the pages it would touch, and a count of its ops with their kinds — never
the ops themselves, because a list is a scan; the ops, a body draft in full, and a deletion's two
lists are one click or one `stigmergy-repair show` away. For a deletion the op kinds are what say,
from the scan alone, that a page would be REMOVED rather than edited.

- **Verdicts are `approve` and `reject` only.** A proposal IS its edits, so the thing to change
  about one is which edits it contains, which is a different proposal.
- **The console's token IS the authorization.** There is no per-path guard and no second identity
  to resolve: the credential that opens `/admin` stands for the whole deployment, and the actor
  name on the form is attribution recorded beside the verdict, never checked.
- **Rejecting requires a reason**, and the reason lands on the proposal row (`notes`), which is
  what the proposer reads. A note on an APPROVE is optional and lands in the same column — it is
  the only record of why a repair was worth applying.
- **The verdict lives on the proposal.** `status`, `decided_by`, `notes` and, on a successful
  apply, `applied_commit` are the record, beside the console's own `admin_actions` row for the
  attempt. Nothing writes a separate governance ledger
  ([ADR 044](../decisions/044-the-capture-is-the-approval.md) D2).

The console runs one function for it, `server.review.apply_repair_and_record`, which owns the
ordering — mark decided first, as a conditional UPDATE on `status = 'pending'`, so a second Approve
loses rather than clones; then clone, apply, gate, commit and push. ADR 044 D2 retires the approval
itself and moves derive-validate-apply into the worker; until that lands, this is where the
ordering lives, once.

## What has to agree before anything is pushed

Three independent checks, each chosen because the other two cannot see what it sees:

1. **The kind's own applier against a fresh clone.** The propose-time validation ran against a
   checkout that may be hours old; a page deleted since then refuses here. For `delete` this step
   is the two bounds re-run against the clone, plus a base hash per rewritten page and a walk of
   the corpus for a latecomer that now refers to a going page (ADR 043 D3).
2. **`run_gates(ALL_GATES)`** judges the resulting diff exactly as it judges the librarian's own —
   all nine, not a subset. A `delete` apply additionally runs the knowledge repo's own linter over
   the WHOLE clone, because that kind's blast radius is the graph rather than the diff.
3. **The cross-check**: the diff's paths must EQUAL the proposal's stored `target_paths`, and its
   SHAPE must be the one this kind produces — every entry a modification for the two editing kinds;
   for `delete`, removals exactly equal to the pages the plan named and modifications exactly equal
   to the pages it planned to scrub. The gates would pass a diff touching some other page quite
   happily — it is additive and well-formed — so this is the only thing that can say the diff is
   not the one the row describes. Its reach, stated exactly: an `ops` blob that disagrees with
   `target_paths` cannot reach `main`. Content is not compared, and a tamper that edited both
   columns consistently before the row was read is out of scope — write access to
   `repair_proposals` is the prerequisite either way, so this is a consistency check between two
   stored facts, not a defense against a database somebody else writes to.

Then `gitcmd.commit(gated_entries=…)` closes the last window: the diff the gates approved is the
diff that lands, bytes included. The commit is authored by the librarian App, its message names the
proposal and the findings it answers, and it carries an `Approved-by:` trailer naming the human.

The push closes one more, for the two NON-ADDITIVE kinds: it never rebases. Their apply is a proof
against a base — the bounds and the base hashes, then perform — and a rebase would replay the approved diff
onto a tip the gates never judged: a delete can leave a dead link a fresh plan would have scrubbed,
a merge can leave a page anchored to the retired identity forever. A push that loses the race (the
view sweep pushes up to its ceiling every interval, so it is a race that happens) fails CLEAN —
nothing lands, the row is `failed`, and re-approving the re-proposed repair is the recovery. The
additive kinds keep `gitcmd.push`'s ordinary rebase-and-retry: their gates judged content, not a
position against a base, so a backlink replayed onto the moved tip is exactly what was approved.

**A failed apply stays failed.** The status becomes `failed`, the `error` column says why in a
sentence written to be read by an operator, and the approved status is not restored. A silent revert
to pending would hide that a gate refused, which is the outcome an operator most needs to see.

## The dismissal memory

A proposal is identified by WHAT IT WOULD DO — its kind plus its sorted `op:path:link` lines, hashed
into `content_key` — and the proposer skips a key held by a pending, approved, rejected or applied
row. "Reviewed and declined" is a durable fact, and somebody who says no once is not asked the
same question by the next night's run.

**A `failed` row is not a dismissal.** It is the one status the memory does not hold: a rejection is
a human saying no, while a failed apply is a human having said yes to something that then hit a
gate, a race or a fault. The row stays visible with its reason, and the next run may derive the same
repair again — which is the only way back for a repair somebody actually wanted.

`note` is deliberately excluded from the key: two proposals adding the same callout to the same page
with differently-worded sentences are the same question asked twice, and a rephrasing of a declined
repair is not a new one. A deletion's SCRUB set is excluded for a sharper version of the same
reason — a page that gained a link to the doomed page overnight changes the plan and changes
nothing about the question that was asked, so a deletion is keyed on the pages that GO and nothing
else. The drafted body is excluded for the identical reason — **a re-drafted body is the same
question**, and somebody who decided a page needs writing by a person should not meet another draft
of it tomorrow. The UNIQUE index is narrower than the skip rule — one PENDING row per key,
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
