# The admin console (`/admin`)

The web control room over what already runs — the inbox of everything parking on a human, the
capture queue and its drain, identity decisions with a pre-mint registry check (a governed
Approve mints, ADR 030), the repair proposals derived from the gardener's findings (a governed
Approve applies one, ADR 039), corpus health, the index and the ops files it serves, the worker,
the four scheduled jobs, the digest, and who is using the brain — served by the SAME `app`
process group that serves MCP, behind its own token. Design record:
[ADR 029](../decisions/029-admin-console.md), [ADR 030](../decisions/030-server-side-entity-minting.md)
and [ADR 039](../decisions/039-governed-repair-loop.md); code map:
[`src/stigmergy/admin/index.md`](../../src/stigmergy/admin/index.md), which carries the full route
table.

What it deliberately is NOT: a brain client. No search, no page rendering, no `ask` — the
architecture tests enforce that boundary on the package. It reads page *paths* in three places
(the substrate check, the gardener's findings and a repair proposal's target paths), it reads the
**entity registry** (an `ops/` control file — the vocabulary, which every MCP identity already
reads through `list_entities`), and it reads no page out of the corpus at all: what it renders
comes from rows in this database, never from the knowledge repo. The one thing on it that LOOKS
like a page body is an `entity-body` proposal's draft — text a model wrote, sitting in
`repair_proposals`, shown because approving it without reading it would be approving prose nobody
has read. It is not a page yet, and it is not a page this console fetched.

**The one thing that boundary does not cover, said plainly:** the Activity page renders the `ask`
QUESTIONS in `audit_log`. No answer, no page, no snippet — but a question is user content, and it
sits behind the console's single shared credential with a free-text actor. Read "never a read
surface over the corpus" as being about pages, because that is what it is about.

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
covers the shell and the assets too. The shell and the assets carry `cache-control: no-cache`
(an ETag round trip on every load), so a deploy that renames a module never leaves a browser
running yesterday's `app.js` against today's imports; the API carries `no-store`.

## The optional pieces, and how the console degrades without each

| Piece | Secret / env | Without it |
|---|---|---|
| Jobs: Run now, Enable/Disable, run history | `STIGMERGY_ADMIN_GITHUB_TOKEN` (fine-grained PAT: **Actions read+write on the repository the crons RUN IN — the knowledge repo, see the runbook — and on that one only**) + `STIGMERGY_ADMIN_GITHUB_REPO` (`<owner>/<repo>`; there is no default) | the Jobs page shows the database truth (`job_runs`, `index_meta.built_at`) read-only and says so in a banner; the levers are not rendered at all, and the Run-now buttons on the Gardener and Index pages are disabled with the reason beside them |
| Digest: Post now | `SLACK_BOT_TOKEN` (already an app-wide Fly secret) + `STIGMERGY_DIGEST_CHANNEL_ID` | the post button refuses naming the missing piece; Preview still works |
| Digest: audience scoping | `STIGMERGY_ADMIN_CHANNELS_PATH` → the baked `/app/slack-channels.json` (set in `fly.toml`, written by `scripts/deploy_staging.sh`) | every audience falls back to the safe empty default — same behavior as a repo with no channels file |
| The pre-mint registry check, the registry browser | a registry this server can read: the index's `ops/entity-registry.json` snapshot (refreshed by the push webhook and the nightly rebuild), or the `--entity-registry` file where there is no snapshot | every check answers `unchecked` and the page says no registry is readable here; the mint gate still runs against the repo as it stands |
| Actor prefill on mutation forms | `STIGMERGY_ADMIN_ACTOR` (default `admin-console`) | forms prefill the default; every form field is editable |

PAT rotation is the standard drill: revoke on GitHub, `fly secrets set
STIGMERGY_ADMIN_GITHUB_TOKEN=...` with the new one. Between the two, the Jobs page degrades
read-only — nothing breaks. A GitHub call that fails mid-session degrades the same way, with the
gateway's own sentence (status code, never the token, never an echoed body) shown as a banner.

## How the console is read

Two conventions run through every page, and both come from the README rather than from the
console:

- **Colour is who decides.** Amber is a human (a decision is waited on, or a person decided),
  violet is the model (it drafted, gathered or proposed — never the last word), grey is code (a
  gate, a lease, a schedule decided), green is git (it landed in the knowledge repo), red is
  something that could not finish. Every status, chart segment and timeline dot wears the key, and
  the sidebar carries the legend. The palette is validated for colour-vision deficiency in both
  light and dark mode; amber sits under 3:1 on the light surface, so it always ships with its word
  and every chart has a table twin one toggle away.
- **System words get a human label, and the system word stays reachable.** `needs_input` renders
  as "Waiting on its submitter", `triage` as "Waiting on a steward", `filed` as "Landed in git";
  hovering the pill shows the raw word and its one-line meaning. The closed vocabularies
  (statuses, situations, repair kinds, severities, item kinds, decision doors, entity types) ship
  from `/admin/api/meta`, so the page never hardcodes a second copy that could drift; only the
  wording is the frontend's (`copy.js`).

Every page opens with a "How to read this page" explainer, collapsible and remembered per page in
the browser. Pages with a time axis (Dashboard, Captures, Repairs, Gardener, Index, Worker,
Activity) share one window picker (7, 30 or 90 days) in the top bar; every chart on the page
re-renders against the same slice. Every chart — the run strips included — has a table twin one
toggle away, and nothing on a chart is reachable only by hovering.

## What each page does

- **Dashboard** — the inbox count as the number that means work (every item owing a person a
  decision, with the breakdown by kind), beside **the write path, live**: the window's captures
  flowing through the model's draft and code's gates into landed-in-git, parked-on-a-human,
  refused and could-not-finish, with real counts. Then captures per day by what became of them,
  questions per day by answer shape (answered with a citation, answered without one, honest
  refusal, errored), health tiles (index freshness with the incremental upserts sparkline, filings
  per day, the worker's lease, unresolved ingest errors, the gardener's latest findings), the
  capture→filed distribution beside the percentiles, the last known truth per scheduled job, and
  the two ledgers merged into one feed. The only page that polls: every 30 s, and only while the
  browser tab is visible.
- **Inbox** — everything parking on a human, oldest first, as ONE list across the three kinds
  (identity decisions, parked captures, repair proposals) — `server.review.items_for_doorbell`,
  the same read the Slack doorbell rings from, so the console and the doorbell cannot disagree
  about what is waiting on a person. Filters by kind and by "waiting on their submitter"; each
  item carries the ledger's latest decision when one exists, which is how a steward learns a
  second door got there first. Opening an item lands on the page that acts on it. The sidebar
  badge is this list's count.
- **Captures** — every capture ever as a part-to-whole bar (click a segment to filter), arrivals
  per day by outcome, and the queue: status chips grouped by who is waited on (a human, the
  librarian, nobody), a submitter filter, human labels on every state. Reclaim and Retention
  purge are the list-level levers. The detail reads as a story — what arrived (the material, the
  hints, what was flagged), what the librarian says (its report with page, commit, anchor, links,
  the agent's reading), the one question a `needs_input` row got and the submitter's reply, and
  the row's own trace — and an identity decision parked here points at the Entities desk. The
  three dispositions live on a PARKED row's detail only, and the disabled-state sentence says why
  everywhere else. Reclaim sweeps against **the worker's own derived lease**, not the queue's 300 s
  and not the librarian's class default: the console resolves `$STIGMERGY_LIBRARIAN_TIMEOUT_S`
  through the same derivation the worker does (2× the agent budget + 120 s gates + 180 s
  headroom), per request, so staging's 600 s budget reads 1500 s here exactly as it does on the
  worker; `fly.toml`'s `[env]` is app-wide, so the console's environment IS the worker's, and an
  operator who splits them by hand fools the meter. The form's "release everything now" checkbox
  is what sends a horizon of 0, and it is only safe with no live worker mid-item. The forms repeat
  `--help`'s own split, field by field: **resolve's note and reject's reason reach the submitter
  VERBATIM** (so: no secret, no personal data), while requeue's note is for the row's own history
  and is never shown to them.
- **Entities** — the identity desk. Pending situations as a list, each unresolved name carrying a
  **registry check** computed server-side against the registry this server serves (the index's
  snapshot, or the `--entity-registry` file where there is none) with the mint gate's own folds:
  `registered` (the filing fold already resolves the spelling — nothing to mint, requeue),
  `collides` (the collision fold would refuse it — the same refusal the mint raises after the
  clone, delivered before it), `similar` (an ADVISORY listing of registered entities sharing a
  distinctive word or containing the name — nothing acts on it), `clear`, or `unchecked` when no
  registry is readable — and, beside the verdict, whether the name may be offered for a mint at
  all: the librarian's placeholder for a park that named nothing is listed and explained, never
  given a button, and the mint gate refuses it by value should any door hand it over. Beside the
  list: the registry by type and a searchable browser over every registered entity with its
  aliases. The detail shows one card per name with its verdict and the road it suggests — **Mint
  «name»** opens the form prefilled with exactly that name (a human pick, not a count; see
  below); a collision offers **How to alias it** (the three steps in the knowledge repo, with the
  aliases line and the `stigmergy-entities regenerate` command to copy) and **Mint under another
  name**, which opens the form with the Name EMPTY, because the one name the row carries is the
  one the gate will refuse; a registered name offers **Requeue — it resolves now**. A park naming
  several entities therefore shows several cards, one decision each — the form's own
  several-names listing (below) is what a door with no per-name cards shows, and the Slack modal
  still does. The
  Approve form checks the Name and every Alias live as the steward types, through the same
  `entities/resolve` call, and says so under the field; the gate runs again against the registry
  the commit will publish, so this is a warning that is right whenever the snapshot is fresh —
  never a permission. The form's Name prefills from `mint_name_prefill`, the value
  `entities.situations.mint_name_prefill` decides on the parked row and BOTH entity routes send
  beside `subject` and `subjects` — the single unresolved name, or `""` when several or none are
  unresolved, or when the one name is the librarian's placeholder for a park that named nothing;
  with an empty prefill and names left to place the field stays empty and those names are listed
  above it, because one submission mints one entity and the joined display string is none of
  their names — the same decided value the Slack mint modal reads. Type is the closed list shipped
  from `/admin/api/meta`; aliases and role are optional; the requeue box is pre-checked. Approve
  runs the same mint sequence the review lane (MCP, Slack) runs — literally the same function,
  `server.review.mint_and_record_approval`: mint, ledger row, then the requeue, which never
  precedes the push (`entities.remote.mint_via_clone` → `entities.mint.mint`, ONE commit,
  authored by the librarian App with an `Approved-by:` trailer naming the actor) — and records the
  decision in BOTH ledgers, `admin_actions` and the append-only `review_decisions` (ADR 030). The
  console mints under the admin token with the actor as ATTRIBUTION, exactly like every other
  console mutation and like the CLI it replaces — MCP and Slack instead enforce a resolved
  identity's steward status and refuse self-approval; the console (like the CLI) does neither,
  because the shared admin credential cannot back a second-human rule. Reject is not duplicated
  here: the same row is reachable, and rejectable, from the capture's own page — the "Other roads"
  card says so.
- **Repairs** — what the `repair-propose` cron made of the gardener's findings
  ([repair.md](./repair.md), ADR 039): proposals by outcome over the whole table, the proposer's
  run strip, a bounded page of the pending proposals (oldest first, filterable by kind, and it
  says when it filled), and the recently decided ones. The decided list is not decoration
  — a **rejected** row is the dismissal memory the proposer skips against, so "why has the nightly
  run stopped proposing this" is only answerable there, and a **failed** row is an apply a gate
  refused, kept visible with its reason rather than quietly returning to the queue. A proposal's
  detail shows what that KIND would change — one line per declared edit for the additive kinds;
  the drafted body in full, as plain text, for an `entity-body` proposal, because for that kind
  reading the draft IS the check; for a `delete` proposal two lists, the pages that STOP EXISTING
  and the pages rewritten so they no longer link to them; for an `entity-alias` merge, which
  identity survives and which is retired — with Approve and Decline. Decline demands a non-blank
  reason, because the reason is the whole of what stops the same repair being re-derived tomorrow.
  Approve runs `server.review.apply_repair_and_record`, the SAME ordering MCP's review lane runs,
  for the reason the Entities page shares its mint sequence: it applies exactly the approved ops
  through the librarian's own validator and its eight gates, as ONE App-authored commit with an
  `Approved-by:` trailer. `review_decide`'s per-target-path steward check is deliberately not
  reached here, exactly as the Entities page does not reach its own steward check — that guard is
  for a resolved identity, and the console's authorization IS the token.
- **Gardener** — findings per run over the last runs by severity (from each run's own
  `findings_by_severity`), the latest completed run with its findings, pages walked and model
  spend, the run strip, findings by check (click a bar to filter), what each check looks for, and
  the findings table filterable by severity and by check; a `partial` run says the deterministic
  findings are complete and trustworthy and names the model pass that failed; Run now dispatches
  the workflow (real model spend, and the button says so).
- **Index** — built_at, pages indexed, the embedding model, and which copy of each ops control
  file this stack is serving — the entity registry, the identity roster, the Slack channel map,
  each with its snapshot's age and the sha or `rebuild` that wrote it, or "no snapshot" when
  every server here is answering from its own baked file; pages per zone; incremental upserts per
  day; the substrate check in-process, over that same served registry copy; Rebuild now dispatches
  the workflow; the recent webhook deliveries.
- **Worker** — `stigmergy-librarian status` live: depth, the lease, the attempts budget, the
  capture→filed percentiles, each item in flight with its lease meter and the three-verdict
  reading, what the librarian finished per day, the latency distribution, and the unresolved
  ingest errors. Read-only; draining and Fly scaling stay in the terminal.
- **Jobs** — the four workflows, each with its purpose in a sentence, its schedule as "daily at
  HH:MM UTC · next in …", its enabled state in plain words (scheduled, paused by a person,
  auto-paused by GitHub), the last known database truth with that run's stats, the run strip
  (height is duration, colour the outcome), the recent Actions runs (linking out to the logs —
  and only to `github.com`), and the levers: Run now, Enable, Disable, each confirmed with a
  sentence that says what the workflow will do. `retention-purge` is the only one that takes a
  dispatch input (`dry_run`), and it is the only input any dispatch will accept — an undeclared
  key is refused by name before the GitHub gateway is touched, as is an unlisted workflow file.
  Its Run-now form starts with Dry run ticked, so the default path lists what would go and
  touches nothing. The truth column names its source: a `job_runs` row for retention, the gardener
  and the repair proposer, `index_meta.built_at` for the rebuild, which writes none. Other
  recorded work (the digest, the webhook, reclaims, the doorbell) is listed below the four.
- **Digest** — the configured-pieces checklist, Preview (the byte-identical dry-run body), Post
  now (disabled until both Slack pieces are configured, with the checklist saying which is
  missing; it names the duplicate-window risk before it posts), the run strip and history. Still
  command-only: no schedule exists.
- **Activity** — calls in the window and questions asked, the answer shape (answered with a
  citation, without one, honest refusal — from the verifier's verdict, never the text), capture
  → searchable and capture → filed, calls per day by tool, who asks, per-tool calls/errors/latency,
  the real `ask` questions (golden-set quarry), rate-limit refusals, the latest governance
  decision per item whichever door took it, and the console's own action log.

## One control that is empty by construction

The Gardener page's severity chips include `urgent` (`sla`). `sla` is a real member of
`gardener.schema.SEVERITIES`, but nothing in that package produces one — every deterministic check
emits `info` or `warn`, and all the model-sweep slugs map to `warn` (see
[gardener-digest](gardener-digest.md)). The chip mirrors a vocabulary with no producer, so it is
permanently empty by construction rather than by accident, and it goes live the day a check emits
an `sla` finding.

## What leaves a trace, and what does not

Every act that *changes state* goes through one wrapper and writes one `admin_actions` row —
actor, action, arguments, outcome, and the exception class on a failure — whether it succeeded or
not. That row is attribution, not authorization: the actor name is recorded and never checked,
exactly like `--by` on the steward CLIs. If the bookkeeping write itself fails it is logged
loudly and the work still lands; bookkeeping must never fail the work it records.

**Four mutations write a SECOND row: the two Entities verdicts and the two Repairs verdicts.**
On Entities, Approve does and so does Reject — the Entities page routes its Reject through the
ordinary capture rejection rather than growing a button of its own, and that path checks whether
the row is an entity situation and records the decision when it is. Without that, "who decided
this identity" answered from one table for approve and a different one for reject, on the one
door that has both. Alongside their `admin_actions` row both record into `review_decisions` —
the same append-only governance ledger MCP's `review_decide` and Slack's mint modal write into —
so "who approved this identity" answers from one table regardless of which SERVER-SIDE door it
came through (ADR 030) — and, since issue #51, regardless of which door at all:
`stigmergy-entities approve`/`reject` mint from the steward's own clone and still write the same
row, because the writer moved down to `capture.decisions`, below the `stigmergy.entities` ->
`stigmergy.server` edge that would otherwise forbid it. Which door wrote a row is on the row
itself: `extra->>'source'` is one of `mcp`/`slack`/`admin`/`cli`, required on every write. The
`review_decisions` write and the git push it follows happen inside the SAME `_mutate`-wrapped
attempt as the `admin_actions` row, not before it: a refusal anywhere in the mint leaves neither
ledger touched.

Repairs writes into that same ledger through the same shared functions the review lane runs —
`server.review.apply_repair_and_record` and `reject_repair_and_record`, an approve's row carrying
the commit sha and the edited paths in `extra` — so "who approved this edit to the corpus" answers
from the table "who approved this identity" already answers from, whichever of the two doors made
the decision. One asymmetry is deliberate there: a gate refusing the apply leaves the proposal
`failed` carrying the sentence that refused it and writes no `review_decisions` row at all — the
`admin_actions` row for the attempt is there already, with the refusing class name on it — because
a governance ledger claiming a decision whose commit never landed is worse than a missing one, and
a silent revert to pending would hide that a gate spoke.

Four POSTs write no such row, because none of them mutates anything:

| Call | Why it is not a mutation | What it *does* leave behind |
|---|---|---|
| Retention purge with **Dry run** | a preview, by construction the same row set the real purge would take | nothing |
| Digest **Preview** | dry-run render only; nothing is posted to Slack | a `digest-dry-run` row in `job_runs` — which is why the Digest page's history fills with them |
| **Substrate check** | an in-process lint over the live index | nothing |
| **Registry check** (`entities/resolve`) | a read of the served registry with the gate's own folds; it runs as a steward types, and stops the moment the dialog closes | nothing |

## Security posture, in six lines

One credential, hashed at rest, compared in constant time, revoked by one secret change. No
cookies → no CSRF; the token sits in `sessionStorage` and any 401 clears it and returns you to
the login screen. Strict CSP + `textContent`-only rendering (a test greps the shipped files for
HTML-string sinks and for any external `src`/`href`) → captured text is inert, and the console
loads nothing from any other origin; styles are applied through the CSSOM, never a `style`
attribute, because `style-src 'self'` refuses the attribute. Two `Authorization` headers on one
request are refused outright rather than resolved to whichever won. Foreign `Host` headers are
421'd when `$STIGMERGY_PUBLIC_HOST` is set, before the token is even looked at. An unexpected
server error returns the exception *class name* and nothing else — a raised message can carry
captured content, so it never crosses the boundary.
