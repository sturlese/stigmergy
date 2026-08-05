# views — per-entity rollups: a deterministic skeleton plus a single-pass synthesis

Narrative doc: [`docs/reference/views.md`](../../../docs/reference/views.md) — what a view is, why
it is derived, the skeleton/synthesis split, the audience rules, the withheld state and the two
triggers. Design record: [ADR 021](../../../docs/decisions/021-views.md). Also
[ADR 026](../../../docs/decisions/026-the-purge.md) — **read D2 and D4 first**: D2 is why this
package does not verify a figure it writes, D4 is why it reads `stigmergy.kernel` rather than the
removed `stigmergy.pipeline`.

This file is the code map — for whoever is about to edit this package, not run it.

## Purpose

`views/<entity-id>.md` is the page that answers "what do we know about X" — a **deterministic
skeleton** (a timeline of anchored pages by `as_of`, plus backlinks; pure code, no LLM) followed by
a **single-pass synthesis** written by a bounded agent. One file per entity, regenerated when its
member set changes.

This is the **only writer of `views/` anywhere in this codebase**. The fast lane's confinement is
untouched — `views/` appears in neither `ALLOWED_WRITE_PREFIXES` nor `MEETING_WRITE_PREFIXES`. This
is a distinct writer, not a widened librarian.

Two triggers: the librarian worker calls `regenerate.run` after a meeting files (best-effort), and
an operator runs `stigmergy-views regenerate`.

**What the purge removed from this package.** A view once carried a fourth, "Current facts"
section (`views/facts.py`, reading the facts store), and its synthesis was judged by the same
ingest-time page verifier that judged every filed page: a failing verdict got one corrective retry
and a still-failing retry was withheld. Both are gone. A view is Timeline + Backlinks +
Synthesis, and `synthesis.write_synthesis` runs the agent exactly ONCE — no retry, no figure
checking of any kind. `render.SYNTHESIS_CAPTION` says so on the page itself. **The one withheld
road left is a budget, not a verdict**: `UsageLimitExceeded` against `VIEW_LIMITS`.

**The load-bearing audience rule survives untouched**: a view's `acl` is the **INTERSECTION** of its
members' audiences (`kernel.acl.view_acl`), never their union — a rollup that inherited labels the
obvious way would silently widen access to everything it summarizes. That covers MEMBERS only;
backlinks are a governed but non-member feed and pass a second, separate gate
(`kernel.acl.visible_to_view`) which is never folded into the intersection.

## Key entry points

| Module | Owns |
|---|---|
| `cli.py` | `stigmergy-views regenerate` with a required, mutually exclusive target (`--entity <id>` / `--stale` / `--all`) plus `--force`; the operator's front door. Exit 130 on Ctrl-C, `--json` first, one sentence per refusal — `stigmergy-queue`'s conventions, deliberately not a third dialect |
| `regenerate.py` | Orchestration: `regenerate_entity` (one entity, one commit) and `run` (the shared batch base both the CLI and the worker call, with one `job_runs` row per batch). Owns `RegenOutcome` and `RunResult` |
| `staleness.py` | The READ-ONLY half: `view_relpath` / `view_path`, `existing_member_hash`, `existing_view_ids`, `list_stale_entities`, `list_all_anchored_entities`. Imports neither `writer` nor `synthesis` |
| `skeleton.py` | The deterministic half: `members_of`, `member_hash`, `timeline_order` / `render_timeline`, `backlinks_of` / `render_backlinks`, `entity_own_page`, `all_anchored_entity_ids` |
| `synthesis.py` | The bounded agent: `build_view_agent`, `write_synthesis`, `FakeViewWriter`, `ViewContext` and its `read_page` tool |
| `render.py` | Assembles skeleton + synthesis into one page; owns the frontmatter shape, `WITHHELD_BLOCK` and `SYNTHESIS_CAPTION` |
| `writer.py` | The one commit path: `commit_and_push` (App-bot authored), plus the two steward-clone guards `ensure_on_branch` / `ensure_clean`, and `repo_slug` |
| `errors.py` | `ViewError` — this package's own refusal type. `cli.main` catches it together with `LibrarianError`, which is what carries `writer.ViewWriteError` (a `librarian.errors.GitError` subclass, deliberately not a `ViewError`) |

Read `regenerate.py` first when tracing a write end to end; read `staleness.py` first when tracing
`--stale`'s population or the gardener's checks, neither of which touches `regenerate`, `writer` or
`synthesis` at all.

**Who depends on this package**, and nothing else does: `librarian.processing` imports
`views.regenerate` (one symbol, pinned) to trigger regeneration after a meeting files;
`gardener.checks` imports `views.staleness` (also pinned) for its stale-view and dead-vocabulary
checks.

## Use these

- **`skeleton.members_of(repo, entity_id)`** — the ONE reading of "which pages anchor to this
  entity", from the repo. The index is a disposable cache and must never be a generator's input;
  this reads the repo through `index.corpus`'s parser, which is the SAME parser the index build
  uses, so a view's member set cannot drift from what a rebuild would compute.
- **`skeleton.member_hash(members)`** — the staleness signal, cheap enough that unchanged entities
  cost nothing. It hashes `(path, content_hash, superseded_by, acl)` per member. The last two are
  beyond the spec's literal triple on purpose: `content_hash` covers only title and body, so a
  frontmatter-only edit (a page gaining `superseded_by`, an ACL narrowing) would otherwise leave
  the hash unchanged and the view silently stale.
- **`regenerate.run`** — the shared batch base. Route ANY new caller through it rather than
  hand-rolling a `job_run` block: it updates `stats` incrementally after every entity (so a fault
  at entity k of n does not write an empty stats row over k-1 real commits) and writes its own
  error row for `KeyboardInterrupt`, which `ops.job_run` cannot see.
- **`regenerate.regenerate_entity`'s `force` parameter** — the ONE place `--force` is interpreted;
  the CLI has no second force-check. Note the three flags differ in what force MEANS:
  `--entity ID --force` forces that id; `--stale --force` widens the population to every entity
  with an existing view (because "stale" is defined by the very hash force bypasses); `--all
  --force` needs no widening, since `--all`'s population is already every anchored entity.
- **`kernel.acl.view_acl`** — the intersection rule, imported and never reimplemented.
  `regenerate_entity` computes it once for the two feeds; `render.render` computes the same pure
  value again for the frontmatter. That is a duplicated call, not a second implementation.
- **`kernel.acl.visible_to_view`** — the read gate a governed but NON-member feed must pass before
  rendering. `skeleton.backlinks_of` is the one call site; it defaults to `None` (open), which
  fail-closes to showing only equally-open backlinks if a caller forgets to pass the real audience.
- **`kernel.fsutil.write_text_atomic`** — the view file write, never a plain `open(..., "w")`.
- **`capture.ops.job_run` / `.record_job_run`** — the shared `job_runs` writer. `regenerate.run` is
  the one call site.
- **`writer.commit_and_push`** — the only commit path. `gitcmd.push` already provides the
  fetch-rebase-retry-never-force-push loop, so a caller never needs its own.

## Avoid / anti-patterns

- **Never read the member set from `stigmergy.index`.** A generator reading a disposable cache would
  make a derived view derive from a derivative. Use `index.corpus`'s pure parsing functions
  (`load_pages`, `split_frontmatter`, and the resolved `links`), never the index tables.
- **Never let `views/` count as its own member zone.** `MEMBER_ZONES` is `("wiki", "sources")`
  deliberately: a view declares `entity: [<id>]` too, so including it would change the staleness
  hash on every write and never converge.
- **Never let this package import `stigmergy.entities`.** Considered and rejected — the full argument
  is in `writer.py`'s module docstring. `librarian.processing` imports `views.regenerate`, so if
  this package imported `entities`, the unattended worker would transitively depend on the
  steward's CLI package.
- **Never import `stigmergy.server`, `stigmergy.answer` or `stigmergy.capture` beyond `capture.ops`.**
  The edge list is pinned per module in `tests/test_architecture.py`
  (`test_views_never_imports_server_answer_capture_or_entities`,
  `test_views_library_modules_stay_within_the_documented_edge`, and a separate, wider allowance for
  `cli.py`'s own database connection).
- **Never add a `verification:` field to a view's frontmatter.** Nothing computes one — the
  "a field nothing computes is not stamped" rule. A synthesis that ran out of budget carries the
  withheld state in PROSE (`render.WITHHELD_BLOCK`), never in a frontmatter value.
- **Never put a write path into `staleness.py`.** Its whole reason for existing is that
  `gardener.checks` can import it without transitively loading `writer.py`'s git stack — which is
  exactly what happened when the gardener imported `regenerate` instead, leaving
  `writer.commit_and_push` one attribute access away inside a module that claimed by test docstring
  to rule out git plumbing by construction.
- **Never treat a view-regeneration fault as a meeting-filing fault.** The worker's hook is
  best-effort: the meeting's page set is already committed and pushed by the time it runs, so a
  view fault is caught, logged and recorded to `job_runs`, never re-raised.
- **Never skip `writer.commit_and_push`'s guards on the steward path** — but do not run them on the
  worker's. `guarded=True` (the CLI default) runs the dirty-tree and wrong-branch checks;
  `guarded=False` (the worker's ephemeral, always-detached worktree) skips them, because both would
  misfire there on conditions that are not problems.

## Data & contracts

- **`skeleton.Member`** (frozen) — `path`, `title`, `type`, `as_of`, `superseded_by`, `acl`,
  `content_hash`. Exactly what the skeleton and the synthesis's tools need.
- **The view's frontmatter** (`render.render`) — `type: view`, `title`, `entity: [<id>]` (a LIST,
  matching every other page type and the parity rule `index.corpus.entity_list` depends on),
  `tags: [view]`, `tier: 3`, `content_hash` (of the view's OWN rendered body), `generated_at`,
  `members`, `member_hash`, and an optional `acl`. `member_hash` is the persisted staleness signal,
  on the derived page itself rather than in a side-channel state file. An `acl` of `[]` is a legal,
  meaningful value — visible to unrestricted clients only — and is rendered, never omitted.
- **Section caps** — `TIMELINE_CAP` 10, `BACKLINKS_CAP` 20. No cap is silent: both
  rendering functions state "showing N of M" the moment a corpus exceeds one.
- **Timeline ordering** — newest first by `as_of`; undated members sort AFTER every dated one, in
  path order, rather than being dropped (losing a member from its own view would undercount the
  section against the frontmatter's `members:` total). Both renderers link by file STEM, never by
  title: every wikilink resolver in this codebase resolves by stem, and title and stem genuinely
  diverge for meeting-filed decision pages.
- **`regenerate.RegenOutcome.action`** — one of `written` · `removed` · `unchanged` ·
  `refused-unknown-entity` · `refused-no-members`. **`removed` fires for TWO distinct causes**: the
  entity is still registered but has no anchored members left, or the entity id is no longer in the
  registry at all while a view for it exists (a de-registration). Each commits a message naming
  which. The contrast with a refusal is whether a view ever existed: a `removed` view did, a
  refusal's target never did.
- **`regenerate.RunResult.stats`** — six keys: `checked`, `written`, `withheld`, `removed`,
  `unchanged`, `refused`. It is a property, not a stored field, and it is what both the CLI's
  `--json` output and the `job_runs.stats` column read; there is no second computation.
- **`synthesis.VIEW_LIMITS`** — `request_limit=6, tool_calls_limit=6`. `MAX_PAGE_READS` (4) bounds
  the agent's `read_page` tool independently, and `PAGE_EXCERPT` (5000) bounds each read. A member
  page's body is passed through `stigmergy.text.fence`, which neutralizes an in-band fence token so a
  hostile page cannot close the fence early.
- **`staleness._ENTITY_ID_RE`** — every view path is built through `view_relpath`, which refuses an
  entity id that is not lowercase letters, digits and hyphens. It is the one choke point, so an id
  carrying a separator or a `..` segment cannot escape `views/`.
- **`writer.py` runs ZERO of the librarian's gates, deliberately** — the module docstring rules on
  each of the eight (`gates.ALL_GATES`) by name, and the ruling holds because a view is generated
  from pages that already passed them. The trigger to re-open it is "a view reads something that is
  not a filed page", which today would be a change to `synthesis.py`'s inputs.

## Tests

`tests/views/` — 6 suites plus a conftest, ~1,000 lines, offline and keyless throughout: real git
(a `git init --bare` remote plus a steward's clone, `conftest.build_repo`) and no Postgres at all —
the `job_runs` write goes through `conftest.FakeConn`, which records the attempted write instead of
touching a database.

| Suite | Covers |
|---|---|
| `test_skeleton.py` | the member set, the staleness hash (including a frontmatter-only change moving it), timeline order and cap, backlinks |
| `test_render.py` | the frontmatter shape (no `verification:`), the withheld block's budget wording, and **the intersection ACL rule proven both ways plus its sabotage twin** — the load-bearing test of this package |
| `test_synthesis.py` | the single pass: ordinary output ships as-is, `UsageLimitExceeded` withholds (`shipped=False`, empty body). Both the shipped `FakeViewWriter` and the real `CLEAN_LLM=fake` / `fake-flawed` dispatch |
| `test_regenerate.py` | the staleness no-op, `--force`, both removal causes, refusals, one commit per entity in a batch over a REAL bare git remote, the `job_runs` row |
| `test_writer.py` | App-bot authorship (not the steward's) and the two steward-clone guards, over a REAL bare git remote |
| `test_cli.py` | the required mutually-exclusive target group, refusals, exit codes |

Two suites outside this directory cover the same contract from the other side:
`tests/server/test_service_acl.py`'s `test_view_*` cases prove the existence-leak guarantee at the
SERVER seam over the shared fixture, which is what shows it needed no view-specific code at all;
`tests/librarian/test_gates_unit.py` asserts `views/` is absent from both write-prefix allowlists.

Layering is pinned in `tests/test_architecture.py`:
`test_views_never_imports_server_answer_capture_or_entities`,
`test_views_library_modules_stay_within_the_documented_edge`,
`test_views_cli_stays_within_the_documented_edge_plus_its_own_db_connection`,
`test_librarian_may_only_import_views_regenerate`, and the gardener's own transitive-reach pin for
`views.staleness`.

`scripts/walk_views.py` is the end-to-end walk: drop a meeting transcript, watch the worker file it
AND regenerate the touched view in the same run, read the commit back, then run the CLI by hand and
watch the honest no-op — offline, real Postgres, real git.

## Common tasks

| Task | Touch |
|---|---|
| Change a skeleton section's cap or wording | `skeleton.py`'s `render_*` functions — every string is deliberate, and a cap's "showing N of M" line must move with it, never be dropped |
| Change the withheld state's copy | `render.WITHHELD_BLOCK` / `SYNTHESIS_CAPTION` — a budget message, never a verdict, and never a frontmatter field |
| Add a `stigmergy-views` flag | `cli.build_parser` plus the corresponding `regenerate.py` parameter — reuse `run` / `regenerate_entity`, never inline git or LLM logic in the CLI |
| Change what counts as stale | `staleness.list_stale_entities` / `existing_member_hash`, and `skeleton.member_hash` if the signal itself changes. Keep `--stale`'s and `--all`'s populations distinct |
| Change the worker trigger | `librarian.processing._file_meeting`'s regeneration block — keep it best-effort, and keep the import pinned to `views.regenerate` only |
| Reintroduce figure checking on a synthesis | a deliberate design decision, not a bug fix — read ADR 026 D2 first, and `writer.py`'s gate-by-gate ruling second |

## Notes

- **After a meeting filing, the branch tip is NOT necessarily the meeting's own commit.**
  `librarian.processing._file_meeting` pushes the page set, then this package's regeneration pushes
  a SECOND commit on top when it succeeds. Anything that wants to know what a capture filed reads
  `result_ref` or the returned sha — never "the current branch tip".
- **One commit per entity, deliberately** — a batch is N independent commits, not one. A run that
  fails halfway leaves a coherent repo and a statable outcome. This differs from the meeting flow's
  atomicity rule on purpose: one meeting capture is one indivisible page set, whereas each entity's
  view is independent of every other's.
- **The Ctrl-C message is honest about the window it cannot see**: `commit_and_push` commits
  locally and then pushes, so an interrupt between them leaves the entity genuinely committed but
  unpushed. The message names that range rather than asserting a state it cannot know, and points
  out that a re-run is safe because already-pushed entities no-op via the staleness hash.
- **`views/facts.py` does not exist and there is no facts store.** The "Current facts" section is
  not a degraded feature to watch — a view's contract has three sections, and adding a fourth needs
  a design decision, not a restoration.
