# views — per-entity rollups: a deterministic skeleton plus a single-pass synthesis

Narrative doc: [`docs/reference/views.md`](../../../docs/reference/views.md).

`views/<entity-id>.md` answers "what do we know about X": a deterministic skeleton (timeline by
`as_of`, backlinks — pure code) followed by a single-pass synthesis written by a bounded agent.
One file per entity, regenerated when its member set changes. This package is the ONLY writer of
`views/` anywhere in the codebase — `views/` appears in neither librarian write-prefix allowlist.

**Staleness is fixed by CONVERGENCE, not by a trigger per door.** The librarian worker runs
`regenerate.sweep` periodically on its idle branch: it asks the corpus which entities diverge from
`views/` right now and fixes those, so a view is never stale whatever wrote the page — an ordinary
capture, a Slack or Drive drop, an applied repair, an entity mint, a hand edit. Three entry
points, one guarantee and two latency optimisations on top of it: the periodic sweep (the
guarantee), the post-meeting hook in `librarian.processing` (best-effort, same run as the filing),
and `stigmergy-views regenerate` (the operator's).

A view's `acl` is the **INTERSECTION** of its members' audiences (`kernel.acl.view_acl`), never
their union — a rollup must not widen access to what it summarizes. Backlinks are a governed but
NON-member feed and pass a second gate (`kernel.acl.visible_to_view`), never folded into the
intersection. The synthesis is unverified by design: no figure check, no `verification:` field —
the one withheld road is the agent's budget (`UsageLimitExceeded`).

## Modules

| Module | What it is |
|---|---|
| `cli.py` | `stigmergy-views regenerate` with a required target (`--entity` / `--stale` / `--all` / `--sweep`) plus `--force` |
| `regenerate.py` | Orchestration: `regenerate_entity` (one entity, one commit), `run` (the shared batch base — one `job_runs` row per batch, the per-run ceiling) and `sweep` (the semantic wrapper: `run` over the union population off one corpus parse). Owns `RegenOutcome`, `RunResult`, `RUN_CEILING_REASON` |
| `staleness.py` | The READ-ONLY half: `view_relpath`/`view_path`, `existing_member_hash`, `existing_view_ids`, `list_stale_entities`, `list_all_anchored_entities`, `list_sweep_entities` (the union). Imports neither `writer` nor `synthesis` |
| `skeleton.py` | The deterministic half: `members_of`, `member_hash`, timeline and backlinks rendering, `entity_own_page` |
| `synthesis.py` | The bounded agent: `build_view_agent`, `write_synthesis`, `FakeViewWriter`, `ViewContext` and its `read_page` tool |
| `render.py` | Assembles skeleton + synthesis into one page; owns the frontmatter shape, `WITHHELD_BLOCK`, `SYNTHESIS_CAPTION` |
| `writer.py` | The one commit path: `commit_and_push` (App-bot authored), the steward-clone guards `ensure_on_branch`/`ensure_clean`. The origin's `owner/name` comes from `librarian.githubapp.repo_slug`, the ONE parser, never a copy here |
| `errors.py` | `ViewError`. `writer.ViewWriteError` is a `librarian.errors.GitError` subclass, caught via `LibrarianError` |

Downstream: `librarian.processing` imports `views.regenerate` (the post-meeting hook) and so does
`librarian.worker` (the periodic sweep) — the one declared symbol either may reach;
`gardener.checks` imports `views.staleness`. Nothing else imports this package.

## Reuse

- `skeleton.members_of` — the ONE reading of "which pages anchor here", through `index.corpus`'s
  parser (the same one the index build uses). The index itself is a disposable cache and never a
  generator's input.
- `skeleton.member_hash` — the staleness signal; it hashes frontmatter fields beyond
  `content_hash` on purpose (that hash covers title+body only, and a frontmatter-only edit must
  not leave a view silently stale).
- `regenerate.run` — route ANY new batch caller through it: incremental stats, its own error
  row for `KeyboardInterrupt` (which `ops.job_run` cannot see), and `max_changes`, the per-run
  ceiling every unattended caller needs. `None` means unbounded, which is what an operator who
  typed a command already is.
- `regenerate.sweep` — the ONE answer to "which population converges `views/`". `--sweep` and the
  worker's idle pass both call it; a caller assembling its own union would be the second answer.
- `staleness.list_sweep_entities` — that population, read-only and git-free, so a future
  findings-only reader can ask the same question the gardener already asks `list_stale_entities`.
- `rows=` on `members_of` / `list_*` / `regenerate_entity` / `run` — ONE `corpus.load_pages` for a
  whole batch. Safe across the batch's own commits ONLY because `views/` is not a `MEMBER_ZONE`;
  it is deliberately NOT threaded into `skeleton.backlinks_of`, which scans every zone including
  `views/` and would then miss a view written earlier in the same pass.
- `regenerate.regenerate_entity`'s `force` — the one place `--force` is interpreted.
  `--stale --force` widens the population to every entity with an existing view; `--all` needs no
  widening.
- `kernel.fsutil.write_text_atomic` — the view file write, never a plain `open(..., "w")`.
- `writer.commit_and_push` — the only commit path; `gitcmd.push` already rebases and retries.

## Avoid

- Never let `views/` count as its own member zone (`MEMBER_ZONES`) — the staleness hash would
  change on every write and never converge.
- Never import `stigmergy.entities` (the worker would transitively depend on the steward's CLI
  package), nor `stigmergy.server`/`answer`/`capture` beyond `capture.ops` — pinned in
  `tests/test_architecture.py`.
- Never put a write path into `staleness.py` — the gardener imports it and must never load the
  git stack.
- Never add a `verification:` field — nothing computes one; the withheld state is prose
  (`WITHHELD_BLOCK`), never a frontmatter value.
- Never treat a view-regeneration fault as a meeting-filing fault: the worker's hook is
  best-effort, caught and recorded, never re-raised.
- Never run the steward guards on the worker path (`guarded=False` — its ephemeral worktree is
  always detached and clean) or skip them on the steward path.

## Contracts

- Frontmatter (`render.render`): `type: view`, `title`, `entity: [<id>]` (a LIST), `tags`,
  `tier: 3`, `content_hash`, `generated_at`, `members`, `member_hash` (the persisted staleness
  signal, on the page itself), optional `acl` — `acl: []` is legal and rendered, never omitted.
- `RegenOutcome.action`: `written` / `removed` / `unchanged` / `refused-unknown-entity` /
  `refused-no-members`. `removed` fires for two causes: no anchored members left, or a
  de-registered entity whose view still exists. WHICH one travels on `RegenOutcome.message`
  (`REMOVED_NO_MEMBERS` / `REMOVED_DEREGISTERED`, the same pair the commit subject's tail is built
  from) — no caller re-derives it, and the CLI prints that message ALONE rather than naming a cause
  of its own: there is no closing sentence true of both roads, and the shared tail it used to
  append contradicted the de-registration one, where every page still anchors the entity.
- `RunResult.stats`: `checked`/`population`/`deferred`/`written`/`withheld`/`removed`/`unchanged`/
  `refused`/`skip_reasons` — a property, read by both `--json` and `job_runs.stats`. `checked` is
  what the run visited and `population` what it was asked about; they differ only when
  `max_changes` stopped it, and `skip_reasons` then carries `RUN_CEILING_REASON`, whose wording is
  `repair.proposer.RUN_CEILING_REASON`'s on purpose.
- `SWEEP_JOB_NAME` (`views-sweep`) — the periodic pass's own `job_runs.job`, distinct from `views`
  (an operator's run) and `views-on-meeting` (the post-filing hook), so a history says which of the
  three did the work.
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
