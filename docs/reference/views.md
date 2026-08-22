# Views — `stigmergy-views` + the per-entity rollup

A view (`views/<entity-id>.md`) is the page that answers "what do we know about X, right
now" without a reader assembling N pages and cross-checking which figure is current. It is
**derived, never hand-maintained**: one page per entity, regenerated from the entity's own
anchored pages whenever they change.
Design record: [ADR 021](../decisions/021-views.md) (the intersection rule's two gates, one commit
per entity, the withheld-synthesis pattern, and the branch-tip contract change a meeting filing
produces). The pages a view's timeline is made of come from
[the meeting distiller](./meeting-distiller.md).
Code map: [`../../src/stigmergy/views/index.md`](../../src/stigmergy/views/index.md).

`stigmergy.views` is the **only** view generator in this codebase, and every live name is `views`
(`tests/gardener/test_checks_dossiers.py` keeps an older word in its filename only).

```
the worker is idle,      a meeting files          stigmergy-views regenerate
 its interval elapsed          │                   --entity/--stale/--all/--sweep
          │                    │                                  │
          ▼                    ▼                                  ▼
 regenerate.sweep(      regenerate.run(          regenerate.run(ids, guarded=True)
   guarded=False,        touched_ids,             or regenerate.sweep(guarded=True)
   max_changes=N)        guarded=False)                            │
          │                    │                                  │
          └────────────────────┴──────────────────┬───────────────┘
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
                     `member_hash:`/`backlink_hash:` frontmatter, and not --force?
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
`processing.MEETING_WRITE_PREFIXES`) never includes it, so this is a distinct, governed writer
beside the API — not a widened librarian.

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
  view's audience, or deletes outright, drops out of the list and moves the hash. Nothing
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

The staleness signals are the only thing that normally re-attempts a withheld synthesis, and they
only change when the member set or the rendered backlinks do — so a synthesis withheld for reasons
unrelated to either (the agent's run happened to need more budget than usual) has no automatic
retry. `--force` on `stigmergy-views regenerate` is the operator-triggerable lever that closes
this gap: it bypasses the staleness check and re-attempts synthesis against the *same* member
set.

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

## Three entry points, one guarantee

A view is DERIVED, so it goes stale the moment anything writes a page. The fix is deliberately not
a call at every door — two of the doors (an applied repair, an entity mint) run inside the HTTP
server process, and any new door would have to remember. It is a **state-based convergence pass**:
ask the corpus what diverges from `views/` right now, and fix that. The other two entry points are
latency optimisations on top of that guarantee, not the guarantee itself.

**The guarantee — the librarian worker's periodic sweep.** `worker.Worker`'s loop already has an
idle branch (the queue is empty), which is precisely where maintenance belongs. On its own
interval it calls `worker.run_view_sweep`, which materializes a fresh `gitcmd.ephemeral_worktree`
off a freshly-fetched `origin/<branch>` and runs `regenerate.sweep(guarded=False, max_changes=…)`
inside it. Three details are load-bearing:

- **It runs in the worker, not in a cron.** A view regeneration COMMITS AND PUSHES, and a GitHub
  Actions cron holding the librarian App's private key is a push credential sitting in a runner's
  environment. The worker already holds that credential and already runs continuously with
  `job_runs` bookkeeping, so this adds no credential surface — the same argument that moved the
  repair loop here (ADR 044) and the reason the remaining crons are the three that need no
  credential at all.
- **It builds its OWN worktree.** The post-meeting hook BORROWS the capture's, and that is where
  `guarded=False`'s justification comes from ("the librarian worker, whose ephemeral worktree is
  always a fresh checkout"). An idle pass has none to borrow, so it makes one — the justification
  stays literally true for the new caller instead of quietly becoming a claim nobody checks.
- **It has a per-run ceiling.** N changed entities are N model calls.
  `$STIGMERGY_LIBRARIAN_VIEW_SWEEP_CEILING` bounds one pass; what it defers is recorded in
  `job_runs.stats.skip_reasons` in `repair.proposer.RUN_CEILING_REASON`'s own wording, and picked
  up by the next pass, because the population is recomputed from state every time. A fault leaves
  a `job_runs` error row and is swallowed — filing must never depend on a rollup.

Both knobs, with their defaults, are in [`operator-runbook.md`](./operator-runbook.md).

**Latency, 1 — after a meeting files.** `librarian.processing._file_meeting` regenerates the
views of every entity the meeting's decision pages touched, in the same worker run, right
after the meeting's own page set is pushed. See [`librarian.md`](./librarian.md) and
[`../../src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md) for the contract
change this introduces to what "the branch tip after a filing" means. Ordinary (non-meeting)
captures still do **not** call it — they no longer need to, because the sweep covers them.

The gardener's `stale-view` check is still DETECTION only, and that is now a division of labour
rather than a gap: it holds no git plumbing and no path under `wiki/`, so it names divergent
entities and the sweep is what acts on them. Its `suggested_action` remains a command a human can
run when they do not want to wait for the interval. See
[`gardener-digest.md`](./gardener-digest.md).

**Latency, 2 — `stigmergy-views regenerate`**, the operator's own front door:

```sh
stigmergy-views regenerate --entity <id>     # exactly this entity, even if not stale
stigmergy-views regenerate --stale           # every entity whose EXISTING view no longer matches its members
stigmergy-views regenerate --all             # every entity with at least one anchored page (the backfill flag)
stigmergy-views regenerate --sweep           # the UNION of the two: what the worker's periodic pass does
stigmergy-views regenerate --entity <id> --force   # bypass staleness; re-attempt synthesis
```

One required, mutually exclusive target — a bare `regenerate` never silently picks `--all` for
you. The targets check **different populations**, named explicitly in every report line so an
operator comparing two runs can tell whether they even looked at the same entities. `--force`
widens `--stale`'s population to every entity with an existing view (not just the stale ones);
`--all`'s own population already covers everything, so `--force` there changes only whether a
fresh view's synthesis is re-attempted, not which entities are visited.

### The populations, and why the sweep is a UNION

`--stale` and `--all` are not comparable, and **neither is a superset of the other**. This is the
crux of the whole design, so it is written down rather than left to be rediscovered:

| Target | Population | What it CANNOT see |
|---|---|---|
| `--stale` | entities with an EXISTING view whose `member_hash` OR `backlink_hash` no longer matches (`staleness.list_stale_entities`) | every entity that has never had a view — it iterates the views on DISK, so a newly-minted entity with one anchored page is invisible to it. Also a de-registered entity whose pages still anchor it: its member hash still matches |
| `--all` | every entity with ≥ 1 anchored page (`staleness.list_all_anchored_entities`) | an orphaned view whose members have ALL disappeared — that entity has no anchored pages left to be found by |
| `--sweep` | the union of both (`staleness.list_sweep_entities`) | — |

So a periodic pass built on `--stale` alone — the obvious choice, and the population
`gardener.checks.check_stale_views` reuses verbatim — would silently never CREATE a missing view,
and one built on `--all` alone would never REMOVE an orphaned one. `--sweep` is a fourth target
rather than a widening of `--stale` on purpose: `--stale` names a population another module reuses
by name, and `--stale --force` already carries a documented widening of its own; a third meaning on
the same flag is how two readers end up disagreeing about what "stale" means.

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
earlier in the SAME pass is noticed on the NEXT pass, one interval later.

## One writer, one commit per entity

Every view commit is authored by the App bot (`librarian.githubapp.identity()`). On an operator's
own clone (`guarded=True`, the CLI's default) `writer.py` first proves that checkout is on the
right branch and clean; both of the worker's paths pass `guarded=False`, because each runs in an
ephemeral, always-detached worktree where both guards would misfire on conditions that are not
problems — the post-meeting hook in the capture's, the periodic sweep in one it builds for itself.
A batch run (`--stale`/`--all`/`--sweep`, the periodic pass, or the
worker's touched-entity set) is **N independent commits**, one per entity, not one commit for the
whole run — deliberately different from the meeting flow's atomicity rule (one meeting capture is
one indivisible page set): here each entity's view is independent of every other entity's, so
there is no shared invariant a partial batch could violate. A run that fails or is interrupted
partway leaves a coherent repo, and re-running is always safe — already-regenerated entities
no-op via the staleness hash. One `job_runs` row covers the whole batch regardless of how many
commits it produced.

**None of the eight librarian gates runs over a view commit, and that is a ruling, not an
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
- **A withheld synthesis with no signal change has no automatic retry** — the periodic sweep
  converges on the two hashes, so an entity whose members and backlinks did not change is
  `unchanged` to it too. Only `--force` closes that gap, and only when an operator runs it by hand.
- **A backlink a view gains from a view written earlier in the SAME pass is one interval late.**
  The staleness signal is computed off the population's shared parse, which by construction cannot
  contain a page that pass has not written yet; the next pass sees it. This is the residue of #85,
  which closed the "never" (a narrowed, deleted or newly-added backlink now moves
  `backlink_hash:` and regenerates the view) and left this one bounded lag rather than paying a
  fresh corpus parse per entity CHECKED to close it. It converges on its own; `--force` is still
  there for an operator who will not wait an interval.
- **No cron regenerates a view — not here, and not in the gardener either.** The convergence pass
  lives in the librarian worker (which already holds the write credential), and this package owns
  both halves of view regeneration (the skeleton and the synthesis). The gardener owns DETECTION
  only: its daily run reports a `stale-view` finding per divergent entity and names the command a
  human can run without waiting for the interval. A findings-only package that quietly regenerated
  pages would be the thing its own architecture tests exist to forbid.

## Where the code lives

- `stigmergy.views` — the package itself: `skeleton.py` (the deterministic half — Timeline,
  Backlinks, and both staleness hashes: `member_hash`, `backlink_hash`), `synthesis.py` (the
  bounded agent),
  `render.py` (page assembly), `writer.py` (the one commit path), `regenerate.py` (orchestration —
  staleness, `--force`, removal, the shared `run()` with its ceiling, and the `sweep()` wrapper),
  `staleness.py` (the READ-ONLY extraction of `list_stale_entities`/`list_all_anchored_entities`/
  `list_sweep_entities`, so the gardener can ask "which views are stale" without importing
  `regenerate.py` and dragging the whole git write stack in with it),
  `cli.py` (`stigmergy-views`), `errors.py`. See
  [`../../src/stigmergy/views/index.md`](../../src/stigmergy/views/index.md) for the full code
  map.
- `librarian.worker.run_view_sweep` and `Worker.maybe_sweep_views` — the periodic pass and its
  schedule; `librarian.config`'s `VIEW_SWEEP_INTERVAL_ENV`/`VIEW_SWEEP_CEILING_ENV` are its two
  knobs. See [`../../src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md).
- `librarian.processing._file_meeting`'s `views_regenerate` block — the post-meeting hook, same
  code map.
- The operator's own quick-reference (commands, the entry points, the audience rule, all in
  brief) lives in [`operator-runbook.md`](./operator-runbook.md#a-view-that-did-not-catch-up);
  this document is the fuller narrative account.

## Tests

`tests/views/` covers the package end to end — member set and staleness (`test_skeleton.py`),
the budget-withheld outcome (`test_synthesis.py`), the frontmatter shape and the intersection rule
proven both ways plus its sabotage twin (`test_render.py`), App-bot authorship over a real bare git
remote (`test_writer.py`), staleness/`--force`/removal/refusals over that same real git — with the
`job_runs` write going through the conftest's offline `FakeConn`, so the suite needs no Postgres at
all (`test_regenerate.py`) — the convergence pass's union population, its single corpus parse, its
ceiling and the no-commit/no-model-call twin (`test_sweep.py`), and the CLI's argument handling
(`test_cli.py`). The worker's half — the interval, the fault posture, and the pass's own worktree —
is `tests/librarian/test_view_sweep_unit.py`. `tests/server/
test_service_acl.py`'s two `test_view_*` cases prove the existence-leak guarantee needed no
view-specific server code. `scripts/walk_views.py` is a narrated, offline, keyless walk of the
whole mechanism (drop a transcript, watch the post-meeting hook fire, read the commit back, run the
CLI by hand and watch the honest no-op) — it does not replace the live judgment step ("what do we know about
X?" against the real corpus, judged by the operator), which needs a real embedder and model call
and is out of scope for an offline script.
