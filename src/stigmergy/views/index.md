# views — per-entity rollups: a deterministic skeleton plus a single-pass synthesis

Narrative doc: [`docs/reference/views.md`](../../../docs/reference/views.md).

`views/<entity-id>.md` answers "what do we know about X": a deterministic skeleton (timeline by
`as_of`, backlinks — pure code) followed by a single-pass synthesis written by a bounded agent.
One file per entity, regenerated when either of the two feeds it renders moves — its member set,
or the backlinks it is allowed to cite. This package is the ONLY writer of
`views/` anywhere in the codebase — `views/` appears in neither librarian write-prefix allowlist.

**Staleness is fixed by CONVERGENCE, not by a trigger per door.** The librarian worker runs
`regenerate.sweep` periodically on its idle branch: it asks the corpus which entities diverge from
`views/` right now and fixes those, so a view is never stale whatever wrote the page — an ordinary
capture, a 🧠 gesture, a submitted meeting or document, an applied repair, a page REMOVED, an entity
born, a hand edit. Two entry points, one guarantee and one latency optimisation on top of it: the
worker's sweep (the guarantee) and the post-meeting hook in `librarian.processing` (best-effort,
same run as the filing). There is no operator command — it was removed — and what replaces
it is that the sweep also runs on the first idle tick AFTER the worker did something, so the wait
is a poll interval rather than a sweep interval.

A view carries **no `acl:` at all**: it is the OPEN rollup. `skeleton.members_of` admits only
members that `flows_into` an open
page, and the backlink feed — a governed but NON-member source — passes the same gate. It used to
be the INTERSECTION of its members' audiences, which never widened access, correctly, and
COLLAPSED: two members with disjoint labels produced `acl: []`, which is *nobody*, so one
leadership-only note anchored to a popular entity deleted that entity's view for everyone while
its timeline went on naming every member. Both gates run at generation time AND inside the
staleness signal (`skeleton.member_hash` moves when a member drops out, `skeleton.backlink_hash`
hashes the post-gate rows), because a gate that ran only at generation time left a narrowed source
cited forever on an already-committed page — #85. The synthesis is
unverified by design: no figure check, no `verification:` field — the one withheld road is the
agent's budget (`UsageLimitExceeded`).

## Modules

| Module | What it is |
|---|---|
| `regenerate.py` | Orchestration: `regenerate_entity` (one entity, one commit), `run` (the shared batch base — one `job_runs` row per batch, the per-run ceiling, the cooperative `should_stop`) and `sweep` (the semantic wrapper: `run` over the union population off one corpus parse, under the deployment-wide advisory lock `VIEW_SWEEP_LOCK_KEY`). Owns `RegenOutcome`, `RunResult` and the whole `skip_reasons` vocabulary, because nothing here is deferred or skipped silently: `RUN_CEILING_REASON`, `STOPPED_EARLY_REASON` and `BRANCH_MOVED_REASON` (the three ways a batch stops early), `UNUSABLE_ID_REASON` (an id no view file can be named from) and `SWEEP_IN_FLIGHT_REASON` (another sweeper holds the lock) |
| `staleness.py` | The READ-ONLY half: `view_relpath`/`view_path`, `ViewSignals` with `existing_signals`/`current_signals`/`view_is_current` (the staleness definition itself), `existing_member_hash` (the existence probe), `existing_view_ids`, `list_stale_entities`, `list_all_anchored_entities`, `list_sweep_entities` (the union). Imports neither `writer` nor `synthesis` |
| `skeleton.py` | The deterministic half: `members_of`, `member_hash`, `backlinks_of`/`backlink_hash`, timeline and backlinks rendering, `entity_own_page` |
| `synthesis.py` | The bounded agent: `build_view_agent`, `write_synthesis` (count-bounded by `VIEW_LIMITS` AND wall-clocked by `SYNTHESIS_TIMEOUT_S` — a hung provider call becomes a withheld synthesis, never a hung worker loop), `FakeViewWriter`, `ViewContext` and its `read_page` tool |
| `render.py` | Assembles skeleton + synthesis into one page; owns the frontmatter shape, `WITHHELD_BLOCK`, `SYNTHESIS_CAPTION` |
| `writer.py` | The one commit path: `commit_and_push` (App-bot authored), the operator-clone guards `ensure_on_branch`/`ensure_clean`. The origin's `owner/name` comes from `librarian.githubapp.repo_slug`, the ONE parser, never a copy here |
| `errors.py` | `ViewError`. `writer.ViewWriteError` is a `librarian.errors.GitError` subclass, caught via `LibrarianError` |

Downstream: `librarian.processing` imports `views.regenerate` (the post-meeting hook) and so does
`librarian.worker` (the periodic sweep) — the one declared symbol either may reach;
`gardener.checks` imports `views.staleness`. Nothing else imports this package.

## Reuse

- `skeleton.members_of` — the ONE reading of "which pages anchor here", through `index.corpus`'s
  parser (the same one the index build uses). The index itself is a disposable cache and never a
  generator's input.
- `staleness.view_is_current` over a `ViewSignals` PAIR — the ONE definition of stale, read by
  `regenerate_entity` and by `list_stale_entities` (and so by the gardener). Never compare a
  single hash at a call site: `member_hash` hashes frontmatter fields beyond `content_hash` on
  purpose (that hash covers title+body only, and a frontmatter-only edit must not leave a view
  silently stale), and `backlink_hash` covers the feed `member_hash` says nothing about. A missing
  `backlink_hash:` is STALE, never a match — that is what regenerates the views a deployment
  already has.
- `regenerate.run` — route ANY new batch caller through it: incremental stats, its own error
  row for `KeyboardInterrupt` (which `ops.job_run` cannot see), `max_changes`, the per-run
  ceiling every unattended caller needs, and `should_stop`, the cooperative pause it asks BETWEEN
  entities (one entity is one commit, so any prefix of the loop is a valid repo state; inside one
  there is a synthesis call and a push that must not be torn in half). `None` means unbounded and
  never stopping, which is what an operator who typed a command already is.
- `regenerate.sweep` — the ONE answer to "which population converges `views/`". `--sweep` and the
  worker's idle pass both call it; a caller assembling its own union would be the second answer.
  It also holds the mutual exclusion: two sweepers are a supported SHAPE (N workers, plus an
  operator's `--sweep`) and a broken run, so the pass runs under one advisory lock and losing the
  race is a `skip_reason` with no `job_runs` row. The named populations (`--entity`/`--stale`/
  `--all`) are deliberately outside it.
- `staleness.list_sweep_entities` — that population, read-only and git-free, so a future
  findings-only reader can ask the same question the gardener already asks `list_stale_entities`.
- `rows=` on `members_of` / `list_*` / `regenerate_entity` / `run` — ONE `corpus.load_pages` for a
  whole batch. Safe across the batch's own commits ONLY because `views/` is not a `MEMBER_ZONE`.
  `skeleton.backlinks_of` takes `rows=` too and the two callers must NOT be confused: the
  staleness signal passes the shared parse (once per entity CHECKED — a fresh parse there would
  cost the pass its single-parse argument, and being one interval late about a backlink is a
  bounded, converging error), the WRITE passes `None` (once per entity REGENERATED — it must see
  a view written earlier in the same pass, which the shared snapshot cannot contain).
- `regenerate.regenerate_entity`'s `force` — the one place `--force` is interpreted.
  `--stale --force` widens the population to every entity with an existing view; `--all` needs no
  widening.
- `kernel.fsutil.write_text_atomic` — the view file write, never a plain `open(..., "w")`.
- `writer.commit_and_push` — the only commit path; `gitcmd.push` already rebases and retries.

## Avoid

- Never let `views/` count as its own member zone (`MEMBER_ZONES`) — the staleness hash would
  change on every write and never converge.
- Never import `stigmergy.entities` (the worker would transitively depend on the identity rules
  package), nor `stigmergy.server`/`answer`/`capture` beyond `capture.ops` — pinned in
  `tests/test_architecture.py`.
- Never put a write path into `staleness.py` — the gardener imports it and must never load the
  git stack.
- Never add a `verification:` field — nothing computes one; the withheld state is prose
  (`WITHHELD_BLOCK`), never a frontmatter value.
- Never treat a view-regeneration fault as a meeting-filing fault: the worker's hook is
  best-effort, caught and recorded, never re-raised.
- Never run the clone guards on the worker path (`guarded=False` — its ephemeral worktree is
  always detached and clean) or skip them on an operator's own clone.

## Contracts

- Frontmatter (`render.render`): `type: view`, `title`, `entity: [<id>]` (a LIST), `tags`,
  `tier: 3`, `content_hash`, `generated_at`, `members`, `member_hash` and `backlink_hash` (the two
  persisted staleness signals, on the page itself, one per feed it renders — both REQUIRED
  arguments, since a view written without one reads as stale on every pass thereafter). No `acl:`,
  ever — a view is open and its feeds are filtered to match.
- `RegenOutcome.action`: `written` / `removed` / `unchanged` / `refused-unknown-entity` /
  `refused-no-members` / `refused-unusable-id` (an id no view file can be named from — counted,
  and named per id in `skip_reasons`, so one typo'd anchor costs its own id and never the pass).
  `removed` fires for two causes: no anchored members left, or a
  de-registered entity whose view still exists. WHICH one travels on `RegenOutcome.message`
  (`REMOVED_NO_MEMBERS` / `REMOVED_DEREGISTERED`, the same pair the commit subject's tail is built
  from) — no caller re-derives it, and the CLI prints that message ALONE rather than naming a cause
  of its own: there is no closing sentence true of both roads, and the shared tail it used to
  append contradicted the de-registration one, where every page still anchors the entity.
- `RunResult.stats`: `checked`/`population`/`deferred`/`written`/`withheld`/`removed`/`unchanged`/
  `refused`/`skip_reasons` — a property, read by both `--json` and `job_runs.stats`. `checked` is
  what the run visited and `population` what it was asked about; they differ when the run stopped
  early, and `skip_reasons` then says WHICH of the three reasons stopped it — its ceiling
  (`RUN_CEILING_REASON`, worded as `repair.proposer.RUN_CEILING_REASON` is on purpose), the
  caller's own pause (`STOPPED_EARLY_REASON`, repeating the reason `should_stop` answered with),
  or a foreign commit landing mid-batch under a shared corpus parse (`BRANCH_MOVED_REASON`).
  Nothing is deferred silently: the population is recomputed from state, so the next pass sees
  whatever this one left.
- `SWEEP_JOB_NAME` (`views-sweep`) — the periodic pass's own `job_runs.job`, distinct from `views`
  (an operator's run) and `views-on-meeting` (the post-filing hook), so a history says which of the
  three did the work.
- `synthesis.view_model()` — the model a view is WRITTEN with, `$STIGMERGY_VIEWS_MODEL` or the
  librarian's own. NAMED rather than inherited: every unattended caller runs in the worker, whose
  boot strips `$OPENAI_API_KEY`, so an agent falling back to `CLEAN_MODEL` could only raise there.
- `synthesis.VIEW_LIMITS` (6 requests / 6 tool calls), `MAX_PAGE_READS` (4), `PAGE_EXCERPT`
  (5000); member bodies pass `stigmergy.text.fence`. Caps: `TIMELINE_CAP` 10, `BACKLINKS_CAP` 20,
  both announced with "showing N of M". Both renderers link by file STEM, never by title.
- `staleness._ENTITY_ID_RE` — every view path goes through `view_relpath`, which refuses an id
  that could escape `views/`.
- `writer.py` runs ZERO librarian gates, by ruling; the ruling expires the moment a view reads
  anything that is not a filed page.

Tests live in `tests/views/` (offline: real git, no Postgres); the ACL existence-leak guarantee
is proven at the server seam in `tests/server/test_service_acl.py`, and the layering pins in
`tests/test_architecture.py`. After a meeting filing the branch tip may be the view's SECOND
commit — read `result_ref` or the returned sha, never "the current tip".
