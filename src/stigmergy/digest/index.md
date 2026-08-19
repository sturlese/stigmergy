# digest — the week in one Slack post

Narrative doc: [`docs/reference/gardener-digest.md`](../../../docs/reference/gardener-digest.md).
Sibling: [`gardener`](../gardener/index.md) — this package reads its findings store and never
recomputes a check.

`stigmergy-digest` assembles a deterministic Slack message over a window (a watermark; `--since`
overrides it) and posts it, or previews it byte-identically with `--dry-run`. Two sections:
corpus health and corpus deltas. It owns no table — it reads `gardener_findings`/`job_runs`,
`capture_queue`/`pages_index` and `review_decisions`, and writes exactly one thing: its own
`job_runs` row, the watermark. Because it BROADCASTS, every page title it renders passes
`server.acl.visible()` at the destination channel's audiences.

## Modules

| Module | What it is |
|---|---|
| `cli.py` | `stigmergy-digest [--repo] [--channels] [--dsn] [--since] [--dry-run]` — one command. The only module here that imports `stigmergy.index.store`, `stigmergy.librarian.config` or `stigmergy.slack.bolt_gateway` |
| `run.py` | `run_digest` — resolve the window, gather, build, post (or preview), record. Plus `parse_since`, `last_window_until` (the raw watermark, read by `admin`), `DigestResult`, the two job names |
| `sections.py` | `gather_corpus_health` / `gather_corpus_deltas` (plain dicts), `_filed_page_paths` (capture-row -> every page it filed), `_visible_pages` (the ONE ACL seam) |
| `render.py` | `build_body` — pure Slack-mrkdwn assembly from the section dicts. No DB, no clock |
| `settings.py` | `DigestSettings.from_args` — `window_days`, `digest_channel_id`. Re-exports the channel/token env names from `gardener.settings`, the one funnel |
| `errors.py` | `DigestError` — a malformed `--since`, or a missing channel/token on a REAL post (never on `--dry-run`) |

`cli.py` is not `run_digest`'s only caller: `stigmergy.admin.service` awaits it behind the
console's preview and post buttons, reusing `DigestSettings.from_args` — a signature change lands
in both places.

`cli.py`'s `_connect`/`_gateway`/`_repo`/`main` are a deliberate twin of `gardener/cli.py` —
change both or neither.

## Reuse

- `sections.gather_*` return plain dicts; `render.py` is the only place they become text — what
  makes the renderer testable with synthetic dicts and no Postgres.
- `render.build_body(since=, until=, health=, deltas=)` — the one function both a real post and a
  preview call, unmodified; it never reads the wall clock.
- `sections._visible_pages` — route every page-shaped fact through it; a path that fails is
  absent from BOTH the count and the list.
- `slack.mrkdwn.escape_mrkdwn` — every corpus-derived string, before composition.
- `gardener.store.latest_completed_run` / `findings_for_run` — the one read of gardener state.

## Avoid

- Import edges are pinned (`tests/test_architecture.py`): never `stigmergy.views`, `.entities`,
  `.answer`, or `.librarian` beyond `config` (CLI only); `stigmergy.server` only through
  `acl.visible` and `errors.StartupError` — the governed-birth ledger is read through
  `capture.decisions`, which sits below every door that writes it, never through the review lane;
  no git plumbing, no `wiki/` path literal.
- Never re-declare the channel/token env names — import them from `digest.settings`.
- Never read the wall clock in `sections.py` or `render.py`; `run_digest` resolves `now` once.
- Never move the dry-run marker lines into `build_body`'s returned string — they are `cli.py`'s,
  and the byte-identity is structural because of that split.
- Slack mrkdwn only: `*bold*` and `•`, never `#`/`##` or `-` (the opposite of `gardener.report`).
- Never "correct" the digest's body to the `finding(s)` spelling five sibling surfaces use
  (`gardener.report`, `gardener.cli`, `gardener.notice`, `gardener.sweep`, `index.check`). The two
  registers are deliberate: the digest's body is PROSE in a Slack post read by people who did not
  ask for it, so it pluralizes its counts; those five are operator output — a terminal table, a
  refusal, an exception — where the parenthesized form is the compact, count-agnostic convention.
  Spreading either spelling to the other side is a copy change nobody asked for.

## Contracts

- The watermark is `stats['until']`, never `job_runs.started_at` — `started_at` is written after
  the queries and the post, so a `started_at` watermark would drop events into no window;
  `stats['until']` keeps consecutive windows exactly contiguous. A `--dry-run` logs under
  `job='digest-dry-run'` and never advances it.
- Window resolution: `--since` -> the last completed run's `stats['until']` -> `now -
  window_days` (first run only).
- `gather_corpus_health` returns one of three states (`never_run` / `stale` / `ok`, the last with
  `model_passes_incomplete` — an aggregate over every model pass's recorded `error`, so a
  `partial` run can never render as a clean one); `gather_corpus_deltas` returns `pages_filed_count`/`pages_filed_titles`/
  `entities_born_count` — an approval COUNT with no names: every door writes the `review_decisions`
  row and every row carries `extra` (at minimum its `source`), but only a minting approve fills in
  `entity_id`, so a list of names would read as complete and would not be. The copy says "approved"
  for a second reason — the row records an APPROVAL, and one can exist without a mint.
- Post, then record — in that order: an interrupt between the two leaves a posted message with no
  watermark, a named risk both the interrupt copy and the `run_id is None` branch warn about.

Tests live in `tests/digest/`; layering and the transitive-reach pin in
`tests/test_architecture.py`.
