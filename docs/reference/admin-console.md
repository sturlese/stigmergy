# The admin console (`/admin`)

The web operations surface over what already runs — the steward drain, the three crons, the
gardener's findings, the digest, the index, entity situations (a governed Approve mints, ADR 030),
activity, and the worker's status — served by the SAME `app` process group that serves MCP, behind
its own token. Design record: [ADR 029](../decisions/029-admin-console.md) and
[ADR 030](../decisions/030-server-side-entity-minting.md); code map:
[`src/stigmergy/admin/index.md`](../../src/stigmergy/admin/index.md), which carries the full route
table.

What it deliberately is NOT: a brain client. No search, no page rendering, no `ask` — the
architecture tests enforce that boundary on the package. It reads page *paths* in two places
(the substrate check and the gardener's findings) and page *bodies* nowhere.

**The one thing that boundary does not cover, said plainly:** the Activity tab renders the `ask` QUESTIONS in `audit_log`. No answer, no page, no snippet — but a question is user content, and it sits behind the console's single shared credential with a free-text actor. Read "never a read surface over the corpus" as being about pages, because that is what it is about.

## Enabling it

The console is INERT until its token hash is configured — every `/admin` path answers 404, no
admin table is created, and nothing on the MCP surface reveals that the module is installed. To
enable:

```sh
.venv/bin/stigmergy-admin-token          # prints the token (once) + the hash line
# locally: put the printed STIGMERGY_ADMIN_TOKEN_HASH=... line in the gitignored .env
# staging:
fly secrets set STIGMERGY_ADMIN_TOKEN_HASH="<the printed hex>"    # triggers the redeploy
```

Open `https://<the app hostname>/admin/` (locally: `http://127.0.0.1:8080/admin/` once
`stigmergy-server --transport http` is running; `/admin` without the slash redirects there) and
paste the token. It lives in that browser session only — `sessionStorage`, never a cookie.
**Lost token = mint a new pair and set the new hash** — nothing is stored, there is nothing to
look up. **Revocation** = set a different hash (or unset it to turn the console off entirely);
the change lands with the redeploy the `fly secrets set` triggers.

The hash must be a 64-character sha256 hex digest. A non-empty value of any other shape stops the
server at startup with a sentence naming `stigmergy-admin-token` — fail closed AND loudly, because
a console no token can ever open is worse than no console.

The admin token opens `/admin/api/*` and NOTHING else — it is refused on the MCP endpoint like
any unknown bearer, and MCP tester tokens are refused on the console (pinned by
`tests/server/test_admin_branch.py`). The `/admin` branch never reaches the MCP bearer
middleware at all; it is a sibling with its own fail-closed gate, which is what keeps the
webhook's "one exemption, exact path match" rule literally true.

**What is not behind the token**: once the console is enabled, the SPA shell (`/admin/`) and its
static assets (`/admin/assets/*`) serve to anyone who can reach the host — a login screen that
needed a token to render could not ask for one. They are inert files that contain no data; every
byte of *content* comes from `/admin/api/*`, which is token-checked without exception. When
`$STIGMERGY_PUBLIC_HOST` is set, the `Host` allowlist runs *before* the token check and therefore
covers the shell and the assets too.

## The optional pieces, and how the console degrades without each

| Piece | Secret / env | Without it |
|---|---|---|
| Crons: Run-now, Enable/Disable, run history | `STIGMERGY_ADMIN_GITHUB_TOKEN` (fine-grained PAT: **Actions read+write on the repository the crons RUN IN — the knowledge repo, see the runbook — and on that one only**) + `STIGMERGY_ADMIN_GITHUB_REPO` (`<owner>/<repo>`; there is no default) | the crons tab shows the database truth (`job_runs`, `index_meta.built_at`) read-only, and says so; the buttons are disabled, with the reason stated in that same banner |
| Digest: Post now | `SLACK_BOT_TOKEN` (already an app-wide Fly secret) + `STIGMERGY_DIGEST_CHANNEL_ID` | the post button refuses naming the missing piece; Preview still works |
| Digest: audience scoping | `STIGMERGY_ADMIN_CHANNELS_PATH` → the baked `/app/slack-channels.json` (set in `fly.toml`, written by `scripts/deploy_staging.sh`) | every audience falls back to the safe empty default — same behavior as a repo with no channels file |
| Actor prefill on mutation forms | `STIGMERGY_ADMIN_ACTOR` (default `admin-console`) | forms prefill the default; every form field is editable |

PAT rotation is the standard drill: revoke on GitHub, `fly secrets set
STIGMERGY_ADMIN_GITHUB_TOKEN=...` with the new one. Between the two, the crons tab degrades
read-only — nothing breaks. A GitHub call that fails mid-session degrades the same way, with the
gateway's own sentence (status code, never the token, never an echoed body) shown as a banner.

## What each tab does, in one line each

- **Overview** — parked-on-a-human count (the number that means work), queue depth, in-flight
  count with the oldest claim's age, unresolved `ingest_errors`, per-cron last truth, gardener
  severity counts, index freshness, recent console actions. The only tab that polls: every 15s,
  and only while the browser tab is visible.
- **Queue** — `stigmergy-queue list/show` with the drain: requeue / resolve (with the
  missing-pointer warning) / reject, plus reclaim and the retention purge behind a dry-run
  preview. Reclaim sweeps against **the worker's own derived lease**, not the queue's 300 s and
  not the librarian's class default: the console resolves
  `$STIGMERGY_LIBRARIAN_TIMEOUT_S` through the same derivation the worker does (2× the agent budget
  + 120 s gates + 180 s headroom), per request, so staging's 600 s budget reads 1500 s here
  exactly as it does on the worker. The meter and this button therefore state one number, and an
  ordinary Reclaim no longer redelivers an item a long-budget worker legitimately holds. That
  works because `fly.toml`'s `[env]` is app-wide — the console's environment IS the worker's; an
  operator who splits them by hand fools the meter. The form's "release everything now" checkbox is what sends a
  horizon of 0, and it is only safe with no live worker mid-item. Status chips and a submitter filter; the three disposition buttons are disabled on any
  row that is not parked, with the reason shown beside them. The forms repeat `--help`'s own split, field
  by field: **resolve's note and reject's reason reach the submitter VERBATIM** (so: no secret, no
  personal data), while requeue's note is for the row's own history and is never shown to them.
- **Crons** — the three workflows, each with its schedule, its enabled state, its recent runs
  (linking out to the Actions logs), and three buttons: Run-now, Enable, Disable.
  `retention-purge` is the only one that takes a dispatch input (`dry_run`), and it is the only
  input any dispatch will accept — an undeclared key is refused by name before the GitHub gateway
  is touched, as is an unlisted workflow file. The truth column names its source: a `job_runs`
  row for retention and the gardener, `index_meta.built_at` for the rebuild, which writes none.
- **Gardener** — the latest completed run's findings, filterable by severity and by check; a
  `partial` run says the deterministic findings are complete and trustworthy and names the sweep
  failure; Run-now dispatches the workflow (real model spend, and the button says so).
- **Digest** — the configured-pieces checklist, Preview (the byte-identical dry-run body), Post
  now (names the duplicate-window risk before it posts). Still command-only: no schedule exists.
- **Index** — built_at + pages per zone + webhook upsert health; Substrate check in-process;
  Rebuild-now dispatches the workflow.
- **Entities** — pending identity situations with the material and the agent's reading, and a
  real Approve form: name (prefilled from the situation), type (the closed list, shipped from
  `/admin/api/meta` so the page never hardcodes a second copy of it), optional aliases/role, a
  pre-checked requeue box. Approve mints through the same server-driven door the review lane
  (MCP, Slack) walks (`entities.remote.mint_via_clone` -> `entities.mint.mint`, ONE commit,
  authored by the librarian App with an `Approved-by:` trailer naming the actor) — the CLI shares
  the same `entities.mint.mint` discipline but drives it from the steward's own clone, never
  through `entities.remote` — and records the decision in BOTH
  ledgers — `admin_actions` (this console's own bookkeeping) and the append-only
  `review_decisions` (ADR 030, superseding ADR 029's "writes stay CLI"). The console mints under
  the admin token with the actor as ATTRIBUTION, exactly like every other console mutation and
  like the CLI it replaces — MCP and Slack instead enforce a resolved identity's steward status
  and refuse self-approval; the console (like the CLI) does neither, because the shared admin
  credential cannot back a second-human rule. Reject is not duplicated here: the same row is
  reachable, and rejectable, from the Queue tab.
- **Activity** — the pilot-report numbers (answer shape, latency percentiles), per-identity/tool
  audit aggregates, the real `ask` questions (golden-set quarry), rate-limit refusals, and the
  console's own action log.
- **Worker** — `stigmergy-librarian status` live: depth, in-flight with the three-verdict lease
  reading and a lease meter, capture→filed percentiles. Read-only; draining and Fly scaling stay
  in the terminal.

## One control that is empty by construction

The gardener tab offers a severity filter with an `sla` chip. `sla` is a real member of
`gardener.schema.SEVERITIES`, but nothing in that package produces one — every deterministic check
emits `info` or `warn`, and all four model-sweep slugs map to `warn` (see
[gardener-digest](gardener-digest.md)). The chip mirrors a vocabulary with no producer, so it is
permanently empty by construction rather than by accident, and it goes live the day a check emits
an `sla` finding.

## What leaves a trace, and what does not

Every act that *changes state* goes through one wrapper and writes one `admin_actions` row —
actor, action, arguments, outcome, and the exception class on a failure — whether it succeeded or
not. That row is attribution, not authorization: the actor name is recorded and never checked,
exactly like `--by` on the steward CLIs. If the bookkeeping write itself fails it is logged
loudly and the work still lands; bookkeeping must never fail the work it records.

**Entities Approve is the one mutation that writes a SECOND row.** Alongside its `admin_actions`
row it also records into `review_decisions` — the same append-only governance ledger MCP's
`review_decide` and Slack's mint modal write into — so "who approved this identity" answers from
one table regardless of which door it came through (ADR 030). The `review_decisions` write and
the git push it follows happen inside the SAME `_mutate`-wrapped attempt as the `admin_actions`
row, not before it: a refusal anywhere in the mint leaves neither ledger touched.

Three POSTs write no such row, because none of them mutates anything:

| Call | Why it is not a mutation | What it *does* leave behind |
|---|---|---|
| Retention purge with **Dry run** | a preview, by construction the same row set the real purge would take | nothing |
| Digest **Preview** | dry-run render only; nothing is posted to Slack | a `digest-dry-run` row in `job_runs` — which is why the digest tab's history fills with them |
| **Substrate check** | an in-process lint over the live index | nothing |

## Security posture, in six lines

One credential, hashed at rest, compared in constant time, revoked by one secret change. No
cookies → no CSRF; the token sits in `sessionStorage` and any 401 clears it and returns you to
the login screen. Strict CSP + `textContent`-only rendering (a test greps the shipped files for
HTML-string sinks and for any external `src`/`href`) → captured text is inert, and the console
loads nothing from any other origin. Two `Authorization` headers on one request are refused
outright rather than resolved to whichever won. Foreign `Host` headers are 421'd when
`$STIGMERGY_PUBLIC_HOST` is set, before the token is even looked at. An unexpected server error
returns the exception *class name* and nothing else — a raised message can carry captured
content, so it never crosses the boundary.
