# digest — the week in one Slack post

Narrative doc: [`docs/reference/gardener-digest.md`](../../../docs/reference/gardener-digest.md)
(the how and why for an operator — the sections, the mrkdwn rules, `--dry-run`'s byte-identity, the
honest empty/stale states). Design records:
[ADR 024](../../../docs/decisions/024-gardener-digest.md) (deterministic assembly, never model
prose; the watermark; the ACL-scoped broadcast and its residual),
[ADR 026](../../../docs/decisions/026-the-purge.md) D6 (why this is command-only, with no workflow
of its own) and [ADR 027](../../../docs/decisions/027-the-contraction.md) (why one of its three
sections is gone).
Sibling: [`gardener`](../gardener/index.md) — this package reads its findings store and never
recomputes a check.

This file is the code map — for whoever is about to edit this package, not run it.

## Purpose

`stigmergy-digest` assembles a deterministic Slack message over a window (a watermark; `--since`
overrides it) and posts it, or previews it byte-identically with `--dry-run`.

**It renders TWO sections: corpus health and corpus deltas.** A third — "what the loop learned" —
died with `stigmergy.loop` ([ADR 027](../../../docs/decisions/027-the-contraction.md)), and with it
`gather_loop_learned`, `_render_learned`, the corrections-filed query and the `loop_candidates`
read. Worth knowing before anyone reads a two-section renderer as an incomplete one.

It owns no table. It READS `gardener_findings` / `job_runs` (corpus health), `capture_queue` /
`pages_index` (corpus deltas) and `review_decisions` (entities born), and WRITES exactly one thing:
its own `job_runs` row, the watermark the next default run starts from.

**The constraint the gardener never had to answer: this package broadcasts.** A Slack channel is a
broadcast surface, so every page title and path it renders passes `server.acl.visible()` at the
audiences resolved for the DESTINATION CHANNEL — never the operator's own, unscoped view.
`ops/slack-channels.json` does not exist in the real deployment yet, so today every channel
resolves to the empty audience set and this filter is indistinguishable from no scoping at all. It
becomes load-bearing the moment the first labelled page exists. Built now, deliberately, because
retrofitting a filter onto a shipped broadcast surface is how leaks happen.

## Key entry points

| Module | Owns |
|---|---|
| `cli.py` | `stigmergy-digest [--repo] [--channels] [--dsn] [--since YYYY-MM-DD] [--dry-run]` — one command, no subcommands. The only module here that imports `stigmergy.index.store`, `stigmergy.librarian.config` or `stigmergy.slack.bolt_gateway` |
| `run.py` | `run_digest` — the one function the CLI calls: resolve the window, gather, build, post (or preview), record. Plus `parse_since`, `_resolve_since`, `_watermark_since`, `_require_channel`, `DigestResult`, and the two job names |
| `sections.py` | The two section-data builders (`gather_corpus_health`, `gather_corpus_deltas`), each returning a plain dict; `_filed_page_paths` (the meeting-capture resolution) and `_visible_pages` (the ONE ACL-filtering seam) |
| `render.py` | `build_body` — pure Slack-mrkdwn assembly from the section dicts. No DB, no clock. `_render_health`, `_render_deltas` |
| `settings.py` | `DigestSettings.from_args` — `window_days` (`STIGMERGY_DIGEST_WINDOW_DAYS`, default 7) and `digest_channel_id`. Re-exports `DIGEST_CHANNEL_ID_ENV` / `SLACK_BOT_TOKEN_ENV` from `gardener.settings` rather than re-declaring either literal |
| `errors.py` | `DigestError` — a malformed `--since`, or a missing channel/token on a REAL post (never on `--dry-run`) |

**`cli.py` is not `run_digest`'s only caller.** `stigmergy.admin.service` imports `digest.run` and
`digest.settings` ([ADR 029](../../../docs/decisions/029-admin-console.md)) and awaits
`run_digest` twice — once with `dry_run=True` behind the console's preview button, once for real
behind its post button — reusing `DigestSettings.from_args` rather than assembling its own. So a
change to `run_digest`'s signature, or to what `DigestResult` carries, lands in two places at
once; see [`admin/index.md`](../admin/index.md).

## Use these

- **`sections.gather_corpus_health` / `gather_corpus_deltas`** — the two, and only two,
  section-data builders. Each returns a plain dict; `render.py` is the only reader and the only
  place any of it becomes text. That split is what makes the renderer testable with synthetic dicts
  and no Postgres at all.
- **`render.build_body(since=, until=, health=, deltas=)`** — the ONE function both a real post and
  a `--dry-run` preview call, unmodified. `since` / `until` are plain datetimes it never reads off
  the wall clock; determinism and byte-identity both rest on that.
- **`gardener.store.latest_completed_run` / `findings_for_run`** — the ONE read of "the latest
  gardener run and its findings". This package never runs a check and never writes a finding.
- **`sections._visible_pages(conn, paths, audiences=)`** — the one ACL-filtering seam every
  page-shaped fact passes through. A path that is not indexed, or that fails the check, is silently
  absent from BOTH the count and the list — never counted without being nameable, because a count
  and a list that disagree is its own kind of dishonest report.
- **`sections._filed_page_paths(report, result_ref)`** — the one function that resolves a
  `capture_queue` row to every page it actually put in the repo. A meeting capture's `result_ref`
  names only the MEETING page; the source page(s) and every decision page live in
  `report['filed_meeting']`. Written after reading one real filed row, not from the column's name.
- **`slack.mrkdwn.escape_mrkdwn`** — every corpus-derived string `render.py` interpolates (a page
  title, a path) goes through this FIRST, before composition. An injected page must not be able to
  print Slack markup into a message a channel reads as the system speaking.
- **`stigmergy.text.parse_result_ref`** — the shared `'<path>@<sha>'` parser, which also refuses an
  absolute or `..` path outright.
- **`gardener.notice`'s shape as the model for a fail-closed check** — `run._require_channel`
  mirrors it exactly: name the variable, name the consequence, name the fix; and check it only at
  the moment a real post is about to happen, so `--dry-run` never needs it.

## Avoid / anti-patterns

- **Never import `stigmergy.views`, `stigmergy.entities`, `stigmergy.answer`, or `stigmergy.librarian`
  beyond `config` (CLI only).** Pinned by
  `test_digest_library_modules_stay_within_the_documented_edge` and
  `test_digest_never_touches_git_plumbing`. This package has no caller identity and no write path
  into the knowledge repo, so it has no business reaching into any package that serves or governs
  one.
- **Never import `stigmergy.server` beyond `acl.visible` and the three named `review` symbols**
  (`KIND_ENTITY_PROPOSAL`, `APPROVE`, `ensure_review_schema`) — a one-way read edge into the
  governed-birth log that creates no cycle.
- **Never hold a literal path fragment under `wiki/` anywhere in this package.** Pinned by
  `test_digest_holds_no_literal_path_under_knowledge`.
- **Never re-declare `STIGMERGY_DIGEST_CHANNEL_ID` or `SLACK_BOT_TOKEN` as a literal.** Both are owned
  by `gardener.settings` and re-exported from `digest.settings`, which every other module here
  imports them from — never straight from `gardener.settings`, so "which digest module reaches into
  gardener" stays answerable by reading one file.
- **Never read the wall clock inside `sections.py` or `render.py`.** `run_digest` resolves `now`
  once, at the top, and threads it down. A section calling `datetime.now()` itself would break both
  determinism and the contiguous-window guarantee.
- **Never let the `--dry-run` preview wrapper live inside `build_body`'s returned string.** The two
  marker lines are `cli.py`'s, printed OUTSIDE the function whose return value is the literal
  candidate for `text=`. That split is what makes byte-identity structural rather than a convention.
- **Never compose `#` / `##` headings or `-` bullets.** This is Slack mrkdwn: `*bold*` and `•` only,
  composed as such from the start. This is the OPPOSITE convention from `gardener.report`, which
  writes for a terminal — two readers, two dialects, each matching its own siblings.
- **Never interpolate a corpus-derived string without `escape_mrkdwn` first.**
- **Never render a page title or path this package read without `_visible_pages`**, even when the
  fact "feels" internal.

## Data & contracts

- **`job_runs` (`job='digest'` or `'digest-dry-run'`)** — the ONLY table this package writes.
  `stats` carries `dry_run`, `since` and `until` as ISO strings. A `--dry-run` logs under the
  separate job name specifically so previewing a window and later posting it stay two independent
  acts: `_watermark_since` reads only `job='digest'`, `status='ok'`.
- **The watermark is `stats['until']`, never `job_runs.started_at`.** `record_job_run` writes
  `started_at = now()` at INSERT time — after every section query AND the Slack post. An event
  landing between "the queries ran" and "the row committed" would fall into no window at all under
  a `started_at` watermark, because the next run's `since` would start strictly later.
  `stats['until']` is the instant the queries were actually bounded by, so consecutive windows are
  exactly contiguous.
- **The window resolution order** (`_resolve_since`): an explicit `--since` → the last completed
  `job='digest'` run's `stats['until']` → `now - window_days`. The last branch fires only on a
  genuine first-ever run.
- **`gather_corpus_health` returns one of three honest states**: `{"state": "never_run"}`,
  `{"state": "stale", "last_run_date", "days_before_window"}`, or `{"state": "ok", "run_date",
  "total", "counts_by_severity", "checks_by_severity", "sweep_incomplete"}`. `sweep_incomplete` is
  read straight off the run's own `stats` blob and exists so a reader is never left to interpret
  "no sweep findings this run" as "the sweep found nothing" when it may mean "the sweep did not
  complete" — `latest_completed_run` widens to `status IN ('ok','partial')`, which is what makes
  that case reachable.
- **`gather_corpus_deltas` returns** `{"pages_filed_count", "pages_filed_titles",
  "entities_born_count"}`. Both queries are bounded `[since, until)` on the same `now`.
- **"Entities born" counts an APPROVAL, not a mint.** `review_decisions` records who approved an
  entity proposal and when; the mint is a separate, later git commit through whichever of
  `stigmergy.entities`' doors the approval drove
  ([ADR 030](../../../docs/decisions/030-server-side-entity-minting.md)), with no ledger row of
  its own. It is a COUNT with no names: since ADR 030 the ledger's `extra` column DOES carry the
  minted entity id for a server-driven approval (MCP, Slack, the console), but this section's
  query is a bare `count(*)` that never reads it, and the CLI's own `stigmergy-entities approve`
  still writes no `review_decisions` row at all — a names-based count would still be incomplete by
  construction. The copy says "entity birth(s) approved" for exactly that reason, rather than the
  plainer "entities born" the section is still named after.
- **`DigestResult`** — `run_id`, `body` (the exact posted or would-post string), `posted`, `since`,
  `until`.
- **Post, then record — in that order.** The message reaches Slack BEFORE the `job_runs` row
  commits, so an interrupt between the two leaves a posted message with no recorded watermark. That
  is an accepted, NAMED risk, and both the interrupt handler and the `run_id is None` branch in
  `cli._run` warn the operator that tomorrow's run may re-post the same window.

## Tests

`tests/digest/` — 6 suites plus `conftest.py` and `support.py`, ~1,100 lines.

| Suite | Covers |
|---|---|
| `test_settings.py` | the window default, a malformed override, the re-exported channel/token env names |
| `test_sections.py` | `_filed_page_paths`, pure — the ordinary single-page case and the meeting shape |
| `test_sections_pg.py` | both `gather_*` functions against real Postgres, windowed on the right column per fact, with clock-injected boundaries |
| `test_render.py` | `build_body`'s determinism and every UX-named branch (a populated week, a zero-activity window, no gardener run ever, a stale run, `sweep_incomplete`'s own line) — pure, synthetic dicts, no DB, no clock |
| `test_run_pg.py` | the watermark priority chain, `--dry-run` never advancing it, post-then-record ordering, the honest failure and refusal paths — offline via `FakeSlackGateway` |
| `test_cli.py` | end to end in-process against real Postgres: exit codes, the dry-run marker lines, refusals |

`tests/test_architecture.py` carries this package's layering edges
(`test_digest_library_modules_stay_within_the_documented_edge`,
`test_digest_cli_stays_within_the_documented_edge_plus_its_own_db_connection`), the git-plumbing and
`wiki/`-path proofs, the threshold-literal scan for `DEFAULT_WINDOW_DAYS`, and a named,
declared-exception test for this package's TRANSITIVE reach: `sections.py` legitimately imports
`server.review`, which module-level-imports `librarian.gitcmd` / `.gates` / `.base_inputs`, so every
digest process loads part of the librarian's import graph invisibly to an AST-level check. That
test turns red on a WIDENING, not on the reach's mere existence.

`tests/test_workflows_config.py` pins `gardener.yml`'s shape. This package has no workflow of its
own: the four live workflows are `ci.yml`, `gardener.yml`, `index-rebuild.yml` and
`retention-purge.yml`.

## Common tasks

| Task | Touch |
|---|---|
| Change what a section shows | `sections.gather_*` (the data) AND `render._render_*` (the copy), in the same change |
| Change the window's default length | `settings.DEFAULT_WINDOW_DAYS` — never a literal at a call site; a grep-based test enforces this |
| Add a new page-shaped fact | route it through `sections._visible_pages` |
| Change the mrkdwn formatting | `render.py` only — bold and `•`, never `#` or `-` |
| Change what counts as an entity birth | `sections._entities_born_count` — and re-read why it is an approval count, not a mint count |
| Add a third section back | a `gather_*` in `sections.py`, a `_render_*` in `render.py`, and a line in `build_body`. Note that the removed one read `loop_candidates`, a table that no longer exists |

## Notes

- **`sections.py` reads `pages_index` / `capture_queue` / `review_decisions` by raw SQL over the
  injected `conn`**, not through a `stigmergy.index` or `stigmergy.capture.queue` import — the same
  "library modules take `conn` as a plain argument" posture `gardener.checks` takes for the same
  tables. No import edge is needed for a table a module never imports the owning package to reach.
- **Nor through a gardener-precomputed shape, which was proposed and rejected.** Rejected for a
  structural reason rather than a preference: the gardener has no caller identity
  and no destination channel, so a page title it precomputed into `job_runs.stats` would be rendered
  against no audience, or the wrong one, and would sit there unscoped for any future reader of
  `job_runs`. Reading the tables here, at post time, is what keeps the ACL decision at the one place
  it is ever made.
- **`cli._connect` ensures three schemas** — `capture`, `review` and `gardener` — because this run's
  sections read tables owned by all three, and this package owns no DDL of its own (which is why it
  never touches `startup_ddl_lock` directly).
