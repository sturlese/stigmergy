# Views — the per-entity rollup

A view (`views/<entity-id>.md`) is the page that answers "what do we know about X, right
now" without a reader assembling N pages and cross-checking which figure is current. It is
**derived, never hand-maintained**: one page per entity, regenerated from the entity's own
anchored pages whenever they change.
Design record: [ADR 021](../decisions/021-views.md) (the intersection rule's two gates, one commit
per entity, the withheld-synthesis pattern, and the branch-tip contract change a meeting filing
produces), amended by [ADR 044](../decisions/044-the-capture-is-the-approval.md) D3, which left the
librarian worker as the only process that writes anything to the knowledge repo. The pages a view's
timeline is made of come from [the meeting distiller](./meeting-distiller.md).
Code map: [`../../src/stigmergy/views/index.md`](../../src/stigmergy/views/index.md).

`stigmergy.views` is the **only** view generator in this codebase, and every live name is `views`
(`tests/gardener/test_checks_dossiers.py` keeps an older word in its filename only).

```
 the worker's queue is idle —              a meeting files
  its interval elapsed, OR this is                │
  the first idle tick after it took               │
  a queued item to a terminal state               │
          │                                       │
          ▼                                       ▼
 regenerate.sweep(                    regenerate.run(touched_ids,
   guarded=False,                                 guarded=False)
   max_changes=N)                                 │
          │                                       │
          └───────────────────┬───────────────────┘
                              ▼
                  regenerate.regenerate_entity, once per entity id, in order:

                  1. is the id in the registry?
                       NO  + a view already exists for it  → removed, committed (de-registered)
                       NO  + no view ever existed          → refused-unknown-entity
                       YES → step 2

                  2. does the entity have any anchored members right now?
                       NO  + a view already exists for it  → removed, committed (last member vanished)
                       NO  + no view ever existed           → refused-no-members
                       YES → step 3

                  3. member_hash AND backlink_hash both unchanged since the view's own
                     `member_hash:`/`backlink_hash:` frontmatter?
                       YES → "unchanged", nothing written
                       NO  → skeleton (timeline, backlinks — pure code)
                               ║
                               ╚═ synthesis.write_synthesis (bounded agent, no verifier)
                                    draft finishes → shipped
                                    budget exhausted, or the wall clock
                                    (SYNTHESIS_TIMEOUT_S) fires first → withheld
                             │
                             ▼
                   render.render → write_text_atomic → writer.commit_and_push (App bot)
                             │
                             ▼
                   "written", one commit for this entity

  One `job_runs` row covers the WHOLE batch regardless of how many of the outcomes above it
  produced across however many entity ids were in it (`RegenOutcome.action` is exactly `written` ·
  `removed` · `unchanged` · `refused-unknown-entity` · `refused-no-members` ·
  `refused-unusable-id`, the last for an id no view file can be named from — refused before any
  repo work, counted, and named per id in `skip_reasons`; the run's `stats` folds
  every refusal into one `refused` count and reports `withheld` as a SUBSET of `written`, since a
  withheld synthesis still writes and commits the page). Stats are updated after every entity, not
  once at the end, so a fault at entity k of n leaves a `job_runs` row that admits the k-1 commits
  already pushed.
```

## Why derived, never hand-maintained

The fast lane has always been forbidden to write `views/`, so without this package "what do we know
about X" was answered by the single most-similar page — which does not resolve which of several
member pages is current, or assemble a timeline. A hand-maintained entity page would rot the moment
a member document changed, which is the exact drift the derived-view doctrine forbids.
`stigmergy.views` is the **only writer of `views/` anywhere in this
codebase**: the fast lane's confinement (`gates.ALLOWED_WRITE_PREFIXES`,
`processing.MEETING_WRITE_PREFIXES`) never includes it, so this is a distinct code path with its own
commit — reached only from the worker, which since ADR 044 D3 is the one process in this system that
writes to the knowledge repo at all.

## The skeleton — deterministic, and why it must not wait on the synthesis

The **member set** is every page in `wiki/**` or `sources/**` whose `entity:` frontmatter
contains the id (`skeleton.members_of`), read from the repo through the same pure parser the
index build uses (`stigmergy.index.corpus`) — **never from the index itself**: a disposable,
rebuildable cache must not be a generator's input. `views/`
itself is deliberately excluded from the member zones, or a view would count as its own
member and its staleness hash could never converge.

From the member set, two sections are pure code, no LLM, and reproducible byte-for-byte across
two runs over an unchanged corpus:

- **Timeline** — member pages newest-first by `as_of`, capped at 10 with the truncation stated
  ("N older not shown" — never a silent cap).
- **Backlinks** — pages anywhere in the corpus whose wikilinks resolve to the entity's own page
  (not to any member), capped at 20.

A view is Timeline + Backlinks + Synthesis. There is no facts section: nothing in this system
stores per-entity extracted figures, so there is nothing to render.

**A view's own value does not depend on its synthesis succeeding.** The skeleton is
independent, cheap to recompute, and rendered first on the page (before Synthesis) precisely so a
reader encounters two solid, code-generated sections before reaching anything that might read
"withheld" — the reverse order would make a withheld synthesis look like the whole page failed.

**Staleness** is TWO hashes, one per feed the page renders, compared as a pair
(`staleness.view_is_current`) against the two the view recorded on itself:

- `skeleton.member_hash` — path, content hash, `type`, `as_of`, `superseded_by` and `acl` per
  member, stored as `member_hash:`. `acl` and `superseded_by` are hashed in addition to the
  (id, content hash, path) triple because a frontmatter-only edit to a member (an ACL narrowing,
  a new `superseded_by`) changes what the view should say without changing `content_hash` — the
  strengthened hash closes that false negative.
- `skeleton.backlink_hash` — (path, title) per backlink the section actually RENDERS, stored as
  `backlink_hash:`. The rows are the post-gate set, so a source a person narrows out of the
  view's audience, or removes outright, drops out of the list and moves the hash. Nothing
  body-shaped is in this key on purpose: a view is itself an indexed backlink source and its body
  carries its own regeneration date, so a content-sensitive key would make two views that cite
  each other regenerate each other every pass, forever.

Both unchanged is an honest no-op — nothing written, nothing committed. A view carrying **no**
`backlink_hash:` (every view generated before that field existed) reads as STALE rather than as a
match, so the first pass after this shipped regenerates each one exactly once, which is what
computes the missing signal. The second signal exists because `member_hash` covers the member set
and nothing else, and a view's Backlinks section is fed by pages that are not members: until #85
a source narrowed to `acl: [board-only]` after generation stayed cited — title and path — on an
otherwise open view, and every convergence pass reported `unchanged` forever.

## The synthesis — a bounded agent, judged by no verifier

`stigmergy.views.synthesis` is one bounded agent with one tool: `read_page`, over this entity's
member pages ONLY (any other path answers "not one of this entity's pages") and capped at
`MAX_PAGE_READS` = 4 real reads, with a call past that returning "budget exhausted — write the
synthesis with what you have" rather than failing the run. Every page body it sees is wrapped in
`stigmergy.text.fence` first, every tool result is recorded as the run's evidence, and the whole
pass sits under one budget (`VIEW_LIMITS`: 6 requests, 6 tool calls).

**No verifier runs over the draft.** Deterministic figure checking lives at ANSWER time
(`answer.verify_answer`, cites-or-refuses — see [answer.md](./answer.md)), because at write time it
taxes the model's own prose with false positives and cannot catch the dangerous class anyway (an
invented CLAIM passes every figure check). The agent's own instruction to write only figures it
saw in a tool result **stays** — it is an instruction, not a gate. The reader's protection here
is what it is for every other page in the corpus: the member pages are one click away, the
gardener reads the result, and a human reads it too. The synthesis caption states this plainly on
every view (`render.SYNTHESIS_CAPTION`): its figures are not machine-verified.

## The withheld state — a budget, not a verdict

`UsageLimitExceeded` against `VIEW_LIMITS` means no draft was finished, and the page
carries an explicit "withheld" block (`render.WITHHELD_BLOCK`) in place of the synthesis prose
instead of a synthesis section with nothing behind it — never silently omitted, and never claimed
as a failure: the skeleton (Timeline + Backlinks) is complete and current regardless, and the block
says so. That budget is the ONLY road to "withheld".

The view's frontmatter carries **no `verification:` field at all** — nothing computes a verdict, so
nothing stamps one, and there is no enum value this state could ever collide with. A `## Synthesis`
heading always renders — shipped or withheld — never omitted; an absent heading next to two
populated skeleton sections would read as a broken build, which is a worse and false story next to
the true one.

**A withheld synthesis is retried when the entity's inputs change, and not before.** The two
staleness signals are the whole of what re-attempts one: a member page filed, edited, narrowed or
removed moves `member_hash:`, and a page that starts or stops citing the entity moves
`backlink_hash:`. A synthesis withheld for reasons unrelated to either — the agent's run happened to
need more budget than usual — therefore waits for the entity's next real change. There is no lever
that re-attempts it on demand, and this document does not offer one: nothing in the deployment can
regenerate a view except the worker, and the worker converges from state rather than from a request.
The skeleton is complete and current the whole time.

## The audience rule — the intersection, not the union

**The load-bearing rule this package is the sole owner of**: a view's `acl` is the
**intersection** of its members' audiences
(`stigmergy.kernel.acl.view_acl`), never their union. Concretely: two members labelled
`["a"]` and `["b"]` respectively (disjoint audiences) intersect to `[]` — nobody below an
unrestricted client sees the view, which is correct, because a client scoped to only `a` or
only `b` can read one member but not the other, and a rollup summarizing both must not leak the
member it can't read. A union of the same two labels would instead produce `["a", "b"]`: a client
scoped to `a` alone would then pass the visibility check (`set(["a","b"]) & {"a"}` is non-empty)
and see a view built in part from a member it has no right to read — the union **silently
widens access to everything the view summarizes**, exactly the bug this rule exists to
prevent. The intersection is the only rule that guarantees a view never reaches an audience
that could not already read *every* member it summarizes: an open (label-free) member contributes
neutrally rather than narrowing, and an empty intersection (`acl: []`) is a legal, meaningful,
**restrictive-by-construction** value — rendered explicitly, never omitted (an omitted `acl:`
reads as open).

This computation covers **members only**. One other feed renders content from a governed source
that is *not* a member — backlinks (any page in the corpus that happens to link to the entity's
own page) — and a naive skeleton would let it leak a restricted string onto an open or narrower
view. `stigmergy.kernel.acl.visible_to_view` is the
**second, separate gate** this feed passes through before rendering (`skeleton.backlinks_of`): a
governed-but-non-member row must be excluded from a view it cannot read, but — critically — it must
never *narrow* `view_acl` itself. The two computations stay separate on purpose: `view_acl` answers
"what is this view's own audience, from its members," and `visible_to_view` answers "may this
other row be shown here," and folding the second into the first would let a backlink silently
restrict a view its own members never restricted.

## Two entry points, one guarantee

A view is DERIVED, so it goes stale the moment anything writes a page. The fix is deliberately not
a call at every door — every door that writes is now the same worker, but they are different flows,
and any new one would have to remember. It is a **state-based convergence pass**: ask the corpus
what diverges from `views/` right now, and fix that. The second entry point is a latency
optimisation on top of that guarantee, not the guarantee itself.

**The guarantee — the librarian worker's convergence sweep.** `worker.Worker`'s loop already has an
idle branch (the queue is empty), which is precisely where maintenance belongs. There it calls
`worker.run_view_sweep`, which materializes a fresh `gitcmd.ephemeral_worktree` off a
freshly-fetched `origin/<branch>` and runs `regenerate.sweep(guarded=False, max_changes=…)` inside
it. Four details are load-bearing:

- **It has TWO triggers, not one.** The interval (`$STIGMERGY_LIBRARIAN_VIEW_SWEEP_INTERVAL_S`) is
  the backstop; the other is `due_now` — the first idle tick after the worker took a queued item to
  a terminal state. That second trigger exists because the moment a filing, a meeting, a document or
  a **removal** lands is the moment a rollup is most likely to describe a page that is no longer
  there, and it is also the cheapest time to fix it: the queue has just gone quiet. Waiting out a
  whole interval after a `brain_delete` would leave a view citing pages the removal deleted.
  The repair pass runs after the sweep on the same idle tick and does not set that flag, so what a
  repair changed is picked up by the interval, or by the next queued item.
- **It runs in the worker.** A view regeneration COMMITS AND PUSHES, and a scheduled GitHub
  Actions run holding the librarian App's private key is a push credential sitting in a runner's
  environment. The worker already holds that credential and already runs continuously with
  `job_runs` bookkeeping, so this adds no credential surface — the same argument that moved the
  repair loop here and, in the end, everything else too (ADR 044: there is no scheduled job outside
  the deployment any more).
- **It builds its OWN worktree.** The post-meeting hook BORROWS the capture's, and that is where
  `guarded=False`'s justification comes from ("the librarian worker, whose ephemeral worktree is
  always a fresh checkout"). An idle pass has none to borrow, so it makes one — the justification
  stays literally true for the new caller instead of quietly becoming a claim nobody checks.
- **It has a per-run ceiling.** N changed entities are N model calls.
  `$STIGMERGY_LIBRARIAN_VIEW_SWEEP_CEILING` bounds one pass; what it defers is recorded in
  `job_runs.stats.skip_reasons` in `repair.run.RUN_CEILING_REASON`'s own wording, and picked
  up by the next pass, because the population is recomputed from state every time. A fault leaves
  a `job_runs` error row and is swallowed — filing must never depend on a rollup.

Both knobs, with their defaults, are in [`operator-runbook.md`](./operator-runbook.md).

**Latency — after a meeting files.** `librarian.processing._file_meeting` regenerates the
views of every entity the meeting's decision pages touched, in the same worker run, right
after the meeting's own page set is pushed. See [`librarian.md`](./librarian.md) and
[`../../src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md) for the contract
change this introduces to what "the branch tip after a filing" means. Ordinary (non-meeting)
captures still do **not** call it — they no longer need to, because the sweep covers them, and
covers them on the very next idle tick.

The gardener's `stale-view` check is DETECTION only, and that is a division of labour rather than a
gap: it holds no git plumbing and no path under `wiki/`, so it names divergent entities and the
sweep is what acts on them. Its `suggested_action` names **no command**, because there is none for a
person to run — it says the worker regenerates the view on its next idle pass, and that an entity
still listed after several is worth checking the worker's `job_runs` for. See
[`gardener-digest.md`](./gardener-digest.md).

### The populations, and why the sweep is a UNION

The pass converges the union of two populations, and this is the crux of the whole design, so it is
written down rather than left to be rediscovered: **neither is a superset of the other.**

| Population | What it is | What it CANNOT see |
|---|---|---|
| stale (`staleness.list_stale_entities`) | entities with an EXISTING view whose `member_hash` OR `backlink_hash` no longer matches | every entity that has never had a view — it iterates the views on DISK, so a newly-registered entity with one anchored page is invisible to it. Also a de-registered entity whose pages still anchor it: its member hash still matches |
| anchored (`staleness.list_all_anchored_entities`) | every entity with ≥ 1 anchored page | an orphaned view whose members have ALL disappeared — that entity has no anchored pages left to be found by |
| the sweep (`staleness.list_sweep_entities`) | the union of both | — |

So a pass built on the stale population alone — the obvious choice, and the population
`gardener.checks.check_stale_views` reuses verbatim — would silently never CREATE a missing view,
and one built on the anchored population alone would never REMOVE an orphaned one. The union is its
OWN named function rather than a widening of `list_stale_entities`: that name is reused by another
module by name, and a second meaning on it is how two readers end up disagreeing about what "stale"
means.

**One corpus parse serves the whole population.** `list_sweep_entities` parses once and hands the
rows down through `list_stale_entities`, `list_all_anchored_entities`, `regenerate.run` and
`regenerate_entity` into `skeleton.members_of` — O(population x corpus) becomes O(corpus). That is
safe across the batch's own commits for exactly one reason: `views/` is deliberately excluded from
`skeleton.MEMBER_ZONES`, so nothing a view write or removal commits can change a member set.

`skeleton.backlinks_of` scans every indexed zone INCLUDING `views/`, where a view written earlier
in the same pass is a legitimate backlink source — so it is called at two moments with two
different parses, and the split is the whole design:

| Moment | Parse | Why |
|---|---|---|
| the STALENESS SIGNAL (`staleness.current_signals`), once per entity CHECKED | the population's shared one | a fresh parse per entity checked would undo the single-parse argument that makes a fifteen-minute pass affordable |
| what gets WRITTEN, once per entity REGENERATED | its own fresh one | a page must never cite a set that was already out of date when it was rendered |

The bounded consequence, stated rather than discovered: a backlink created by a view written
earlier in the SAME pass is noticed on the NEXT pass.

**Two sweepers are a supported shape and a broken run**, which is why the deployment-wide advisory
lock (`VIEW_SWEEP_LOCK_KEY`) sits inside `sweep()` rather than at its call site: N workers each on
their own checkout would derive the same divergence, pay for it twice, and rebase each other's
worktree out from under the batch it is reading. Losing that race is a SKIP with no `job_runs` row
and no error — the pass holding the lock is converging exactly the same state — reported as a
`skip_reason` so a worker's log never calls it silence.

## One writer, one commit per entity

Every view commit is authored by the App bot (`librarian.githubapp.identity()`). Both of the worker's
paths pass `guarded=False`, because each runs in an ephemeral, always-detached worktree where the
dirty-tree and wrong-branch guards would misfire on conditions that are not problems — the
post-meeting hook in the capture's worktree, the convergence sweep in one it builds for itself.
(`guarded=True` is still `writer.py`'s default, and it is what would run a regeneration from an
ordinary clone; nothing in the deployment takes that path.)
A batch run — the convergence sweep, or the worker's touched-entity set — is **N independent
commits**, one per entity, not one commit for the whole run — deliberately different from the
meeting flow's atomicity rule (one meeting capture is one indivisible page set): here each entity's
view is independent of every other entity's, so there is no shared invariant a partial batch could
violate. A run that fails or is interrupted partway leaves a coherent repo, and re-running is always
safe — already-regenerated entities no-op via the staleness hash. One `job_runs` row covers the
whole batch regardless of how many commits it produced.

**None of the librarian's nine gates runs over a view commit, and that is a ruling, not an
omission.** `writer.py` answers it gate by gate, because "run them anyway, it's cheap" would have
been wrong on this input for three of them: `gate_body_rewrite` exists to refuse exactly the
wholesale rewrite a regeneration IS, so it would refuse every legitimate run; `gate_zone`'s lane
whitelist is `wiki/`, so it would refuse the path outright; `gate_anchoring` treats `entity:` as
an anchor, but on a view it is self-reference. The rest have no work to do: the secrets and PII
scans already ran over every member page when it was filed, and the agent sees only those pages;
the file is UTF-8 composed by `views.render` and written atomically, never a blob from an agent;
the frontmatter is written by `render`, not declared by anyone, at a path derived from the entity
id. The contract linter is the one with a real argument for it, and it genuinely runs — in the
knowledge repo's own CI, over every commit including this one; running it here would only make
the same signal arrive sooner.

**The trigger that expires this ruling is written down**: *a view reads something that is not a
filed page* — an external source, a fetched figure, a person's free text. That is a change to
`skeleton.py`'s or `synthesis.py`'s inputs, which is where whoever needs to re-open it will be
standing.

## Limits, stated rather than assumed away

- **`views/` is already an indexed zone** (`stigmergy.index.corpus.ZONES`), so a regenerated
  view is searchable at the next rebuild/webhook upsert with no index schema change and no
  ranking change.
- **A withheld synthesis with no signal change is not retried** — the pass converges on the two
  hashes, so an entity whose members and backlinks did not change is `unchanged` to it too. What
  closes that gap is the entity's next real change, and nothing else: there is no on-demand road.
- **A backlink a view gains from a view written earlier in the SAME pass is one pass late.**
  The staleness signal is computed off the population's shared parse, which by construction cannot
  contain a page that pass has not written yet; the next pass sees it. This is the residue of #85,
  which closed the "never" (a narrowed, removed or newly-added backlink now moves
  `backlink_hash:` and regenerates the view) and left this one bounded lag rather than paying a
  fresh corpus parse per entity CHECKED to close it. It converges on its own.
- **Nothing outside the worker regenerates a view — not the gardener either.** The convergence pass
  lives in the librarian worker (which already holds the write credential), and this package owns
  both halves of view regeneration (the skeleton and the synthesis). The gardener owns DETECTION
  only: its daily run reports a `stale-view` finding per divergent entity and says, in place of a
  command, that the worker will take care of it. A findings-only package that quietly regenerated
  pages would be the thing its own architecture tests exist to forbid.

## Where the code lives

- `stigmergy.views` — the package itself: `skeleton.py` (the deterministic half — Timeline,
  Backlinks, and both staleness hashes: `member_hash`, `backlink_hash`), `synthesis.py` (the
  bounded agent),
  `render.py` (page assembly), `writer.py` (the one commit path), `regenerate.py` (orchestration —
  staleness, removal, the shared `run()` with its ceiling, and the `sweep()` wrapper with its
  advisory lock),
  `staleness.py` (the READ-ONLY extraction of `list_stale_entities`/`list_all_anchored_entities`/
  `list_sweep_entities`, so the gardener can ask "which views are stale" without importing
  `regenerate.py` and dragging the whole git write stack in with it),
  `errors.py`. See
  [`../../src/stigmergy/views/index.md`](../../src/stigmergy/views/index.md) for the full code
  map. **This package has no CLI and no entry point**: the worker is the only caller, which is the
  whole of ADR 044 D3 as it applies here.
- `librarian.worker.run_view_sweep` and `Worker.maybe_sweep_views(due_now=…)` — the convergence pass
  and its two triggers; `librarian.config`'s `VIEW_SWEEP_INTERVAL_ENV`/`VIEW_SWEEP_CEILING_ENV` are
  its two knobs. See
  [`../../src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md).
- `librarian.processing._file_meeting`'s `views_regenerate` block — the post-meeting hook, same
  code map.
- The operator's own quick-reference (what to do about a view that did not catch up, the two knobs,
  the audience rule, all in brief) lives in
  [`operator-runbook.md`](./operator-runbook.md#a-view-that-did-not-catch-up);
  this document is the fuller narrative account.

## Tests

`tests/views/` covers the package end to end — member set and staleness (`test_skeleton.py`),
the budget-withheld outcome (`test_synthesis.py`), the frontmatter shape and the intersection rule
proven both ways plus its sabotage twin (`test_render.py`), App-bot authorship over a real bare git
remote (`test_writer.py`), staleness/removal/refusals over that same real git — with the
`job_runs` write going through the conftest's offline `FakeConn`, so the suite needs no Postgres at
all (`test_regenerate.py`) — and the convergence pass in three files: its union population, its
single corpus parse, its ceiling and the no-commit/no-model-call twin (`test_sweep.py`), the
property that repeated passes over an unchanged corpus stop writing (`test_sweep_convergence.py`),
and what a hostile corpus can do to it (`test_sweep_adversarial.py`). The worker's half — both
triggers, the fault posture, and the pass's own worktree — is
`tests/librarian/test_view_sweep_unit.py`. `tests/server/
test_service_acl.py`'s two `test_view_*` cases prove the existence-leak guarantee needed no
view-specific server code. `scripts/walk_views.py` is a narrated, offline, keyless walk of the
whole mechanism (drop a transcript, watch the post-meeting hook fire, read the commit back, ask for
the same entity again and watch the honest no-op, then let the convergence sweep create a view for
an entity nothing hooked) — it does not replace the live judgment step ("what do we know about
X?" against the real corpus, judged by the operator), which needs a real embedder and model call
and is out of scope for an offline script.
