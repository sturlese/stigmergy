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
a meeting files ────────┐                       stigmergy-views regenerate --entity/--stale/--all
                         │                                          │
                         ▼                                          ▼
         views.regenerate.run(touched_ids, guarded=False)   views.regenerate.run(ids, guarded=True)
                         │                                          │
                         └──────────────────┬───────────────────────┘
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

                  3. member_hash unchanged since the view's own `member_hash:` frontmatter,
                     and not --force?
                       YES → "unchanged", nothing written
                       NO  → skeleton (timeline, backlinks — pure code)
                               ║
                               ╚═ synthesis.write_synthesis (bounded agent, no verifier)
                                    draft finishes → shipped     budget exhausted first → withheld
                             │
                             ▼
                   render.render → write_text_atomic → writer.commit_and_push (App bot)
                             │
                             ▼
                   "written", one commit for this entity

  One `job_runs` row covers the WHOLE batch regardless of how many of the five outcomes above it
  produced across however many entity ids were in it (`RegenOutcome.action` is exactly `written` ·
  `removed` · `unchanged` · `refused-unknown-entity` · `refused-no-members`; the run's `stats` folds
  the two refusals into one `refused` count and reports `withheld` as a SUBSET of `written`, since a
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

**Staleness** is a hash over the member set (`skeleton.member_hash`: path, content hash,
`superseded_by`, and `acl` per member) stored on the view's own `member_hash:` frontmatter
field. An unchanged member set is an honest no-op — nothing written, nothing committed. `acl` and
`superseded_by` are hashed in addition to the (id, content hash, path) triple
because a frontmatter-only edit to a member (an ACL narrowing, a new `superseded_by`) changes
what the view should say without changing `content_hash` — the strengthened hash closes that
false negative.

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

The staleness hash is the only thing that normally re-attempts a withheld synthesis, and it only
changes when the member set changes — so a synthesis withheld for reasons unrelated to the member
set (the agent's run happened to need more budget than usual) has no automatic retry. `--force` on
`stigmergy-views regenerate` is the operator-triggerable lever that closes this gap: it bypasses
the staleness check and re-attempts synthesis against the *same* member set.

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

## Two triggers

**Trigger 1 — after a meeting files.** `librarian.processing._file_meeting` regenerates the
views of every entity the meeting's decision pages touched, in the same worker run, right
after the meeting's own page set is pushed. See [`librarian.md`](./librarian.md) and
[`../../src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md) for the contract
change this introduces to what "the branch tip after a filing" means. Ordinary (non-meeting)
captures do **not** trigger regeneration — a deliberate scope limit, not an oversight.

**Nothing on a schedule closes that window, and it is worth being exact about why.** The
gardener runs daily and its `stale-view` check (`views.staleness.list_stale_entities`, the same
function `--stale` uses) NAMES every entity whose view no longer matches its members — and then
stops there. The gardener is findings-only by construction: it holds no git plumbing and no path
under `wiki/`, and its `suggested_action` for that finding is the literal string
`stigmergy-views regenerate --entity <id>`, printed for a human to run, never invoked. So the loop is
detect-nightly, repair-by-hand; see [`gardener-digest.md`](./gardener-digest.md).

**Trigger 2 — `stigmergy-views regenerate`**, the operator's own front door:

```sh
stigmergy-views regenerate --entity <id>     # exactly this entity, even if not stale
stigmergy-views regenerate --stale           # every entity whose EXISTING view no longer matches its members
stigmergy-views regenerate --all             # every entity with at least one anchored page (the backfill flag)
stigmergy-views regenerate --entity <id> --force   # bypass staleness; re-attempt synthesis
```

One required, mutually exclusive target — a bare `regenerate` never silently picks `--all` for
you. `--stale` and `--all` check **different populations**, named explicitly in every report line
so a steward comparing two runs can tell whether they even looked at the same entities. `--force`
widens `--stale`'s population to every entity with an existing view (not just the stale ones);
`--all`'s own population already covers everything, so `--force` there changes only whether a
fresh view's synthesis is re-attempted, not which entities are visited.

## One writer, one commit per entity

Every view commit is authored by the App bot (`librarian.githubapp.identity()`). On the steward's
own clone (`guarded=True`, the CLI's default) `writer.py` first proves that checkout is on the
right branch and clean; the worker's trigger passes `guarded=False`, because its worktree is a
fresh, always-detached checkout where both guards would misfire on conditions that are not
problems. A batch run (`--stale`/`--all`, or the
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
filed page* — an external source, a fetched figure, a steward's free text. That is a change to
`skeleton.py`'s or `synthesis.py`'s inputs, which is where whoever needs to re-open it will be
standing.

## Limits, stated rather than assumed away

- **`views/` is already an indexed zone** (`stigmergy.index.corpus.ZONES`), so a regenerated
  view is searchable at the next rebuild/webhook upsert with no index schema change and no
  ranking change.
- **A withheld synthesis with no member-set change has no automatic retry** — only `--force`
  closes that gap, and only when an operator runs it by hand; nothing retries on a schedule.
- **No cron regenerates a view — not here, and not in the gardener either.** This package owns both
  halves of view regeneration (the skeleton and the synthesis). The gardener owns DETECTION only:
  its daily run reports a `stale-view` finding per divergent entity and names the command, and
  running that command is an operator's act. A findings-only package that quietly regenerated pages
  would be the thing its own architecture tests exist to forbid.

## Where the code lives

- `stigmergy.views` — the package itself: `skeleton.py` (the deterministic half — Timeline,
  Backlinks, `member_hash`), `synthesis.py` (the bounded agent),
  `render.py` (page assembly), `writer.py` (the one commit path), `regenerate.py` (orchestration —
  staleness, `--force`, removal, the shared `run()`), `staleness.py` (the READ-ONLY extraction of
  `list_stale_entities`/`list_all_anchored_entities`, so the gardener can ask "which views are
  stale" without importing `regenerate.py` and dragging the whole git write stack in with it),
  `cli.py` (`stigmergy-views`), `errors.py`. See
  [`../../src/stigmergy/views/index.md`](../../src/stigmergy/views/index.md) for the full code
  map.
- `librarian.processing._file_meeting`'s `views_regenerate` block — trigger 1. See
  [`../../src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md).
- The operator's own quick-reference (commands, the two triggers, the audience rule, all in
  brief) lives in [`operator-runbook.md`](./operator-runbook.md#a-view-that-did-not-catch-up);
  this document is the fuller narrative account.

## Tests

`tests/views/` covers the package end to end — member set and staleness (`test_skeleton.py`),
the budget-withheld outcome (`test_synthesis.py`), the frontmatter shape and the intersection rule
proven both ways plus its sabotage twin (`test_render.py`), App-bot authorship over a real bare git
remote (`test_writer.py`), staleness/`--force`/removal/refusals over that same real git — with the
`job_runs` write going through the conftest's offline `FakeConn`, so the suite needs no Postgres at
all (`test_regenerate.py`) — and the CLI's argument handling (`test_cli.py`). `tests/server/
test_service_acl.py`'s two `test_view_*` cases prove the existence-leak guarantee needed no
view-specific server code. `scripts/walk_views.py` is a narrated, offline, keyless walk of the
whole mechanism (drop a transcript, watch trigger 1 fire, read the commit back, run trigger 2 by
hand and watch the honest no-op) — it does not replace the live judgment step ("what do we know about
X?" against the real corpus, judged by the operator), which needs a real embedder and model call
and is out of scope for an offline script.
