# The admin console (`/admin`)

The web control room over what already runs — the capture queue read-only beside the two levers
over the whole of it, the entity registry this stack serves and the one door for registering a name
nobody has captured about yet, the ledger of repairs the worker made of the gardener's findings —
each with the diff it pushed — and the Remove pages button beside it, corpus health, the index and
the ops files it serves, the worker, the three scheduled jobs, the digest, and who is using the
brain — served by the SAME `app` process group that serves MCP, behind its own token.
Design record: [ADR 029](../decisions/029-admin-console.md),
[ADR 039](../decisions/039-governed-repair-loop.md),
[ADR 043](../decisions/043-a-sweep-is-written.md) and
[ADR 044](../decisions/044-the-capture-is-the-approval.md); code map:
[`src/stigmergy/admin/index.md`](../../src/stigmergy/admin/index.md), which carries the full route
table.

**Nothing here decides an identity, because nothing is proposed.** A capture that meets a name the
registry does not know writes that entity's page in the same commit that files it, confirmed by
whoever captured (ADR 044) — so the Entities page reads the vocabulary and commissions the one
entity no capture has introduced, and it holds no verdict at all.

What it deliberately is NOT: a brain client. No search, no page rendering, no `ask` — the
architecture tests enforce that boundary on the package. It reads page *paths* in three places
(the substrate check, the gardener's findings, and a repair's target paths), it reads the
**entity registry** (an `ops/` control file — the vocabulary, which every MCP identity already
reads through `list_entities`), and it fetches no page: everything it renders comes from rows in
this database, never from the knowledge repo. Two things on it read as page PROSE, and each is
there because nobody read those bytes before they landed. An applied repair carries the unified
DIFF it pushed, out of the `repairs` row that recorded it — page bytes, but bytes this console's
own subject produced, and the whole reason the page exists (ADR 044). A removal hands back the same
thing per rewritten page, in a dialog, at the moment it performs it — the reading ADR 043 D5 moved
to after the push rather than before it.

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
| The registry browser, and the registry check on a name | a registry this server can read: the index's `ops/entity-registry.json` snapshot (refreshed by the push webhook and the nightly rebuild), or the `--entity-registry` file where there is no snapshot | the Entities page lists no entity and says no registry is readable here, and every name check answers `unchecked`. Register an entity still works — it queues a capture, and the birth gate runs when the librarian files, inside that capture's own worktree, against the knowledge repo as it stands |
| Actor prefill on mutation forms | `STIGMERGY_ADMIN_ACTOR` (default `admin-console`) | forms prefill the default; every form field is editable |

PAT rotation is the standard drill: revoke on GitHub, `fly secrets set
STIGMERGY_ADMIN_GITHUB_TOKEN=...` with the new one. Between the two, the Jobs page degrades
read-only — nothing breaks. A GitHub call that fails mid-session degrades the same way, with the
gateway's own sentence (status code, never the token, never an echoed body) shown as a banner.

## How the console is read

Three conventions run through every page, and the first comes from the README rather than from
the console:

- **Colour is who decides.** Amber is a human (a decision is waited on, or a person decided),
  violet is the model (it drafted, gathered or proposed — never the last word), grey is code (a
  gate, a lease, a schedule decided), green is git (it landed in the knowledge repo), red is
  something that could not finish. Every status, chart segment and timeline dot wears the key, and
  the sidebar carries the legend. The five colours sit inside the perceptual band the dataviz
  method asks for and clear 3:1 on both surfaces, with two deliberate deviations, each mitigated:
  grey is grey because grey IS the meaning (code decided), not a hue slot; and amber sits at 2.2:1
  on the light surface, so it never carries meaning alone — every status ships with its word, and
  every chart with a table twin one toggle away. Red and green are never adjacent in a stack,
  which is the pair colour-vision deficiency separates worst.
- **Light, dark, or whatever the device says.** The appearance picker in the sidebar (and on the
  login screen) offers Auto, Light and Dark; the choice is remembered in this browser and stamped
  before the first paint, so a chosen dark theme never flashes light on the way in. Auto follows
  the device's own setting, and an explicit choice beats it in both directions.
- **System words get a human label, and the system word stays reachable.** `claimed` renders as
  "Being filed now", `filed` as "Landed in git", the legacy `resolved` as "Handled by hand";
  hovering the pill shows the raw word and its one-line meaning. The closed vocabularies ship from
  `/admin/api/meta` — the statuses (with their terminal subset, and `resolved` named as the one
  legacy word), the repair kinds, the gardener's severities and the entity types — so the page
  never hardcodes a second copy that could drift; only the wording is the frontend's (`copy.js`).

Every page opens with a "How to read this page" explainer, collapsible and remembered per page in
the browser. Pages with a time axis (Dashboard, Captures, Repairs, Gardener, Index, Worker,
Activity) share one window picker (7, 30 or 90 days) in the top bar; every chart on the page
re-renders against the same slice. Every chart — the run strips included — has a table twin one
toggle away, and nothing on a chart is reachable only by hovering.

## What each page does

- **Dashboard** — the window's **captures filed** as the number that means work: a capture that
  landed is a page, and the identities it introduced were born in the same commit, so the number
  beside it is not a backlog but what did NOT land (refused by a gate, could not finish, still
  moving), with in-flight, queued and the repairs waiting on a decision on the statline. Beside it,
  **the write path, live**: the window's captures flowing through the model's draft and code's
  gates into landed-in-git, refused and could-not-finish, with the legacy handled-by-hand outcome
  kept for as long as old rows carry it. Then captures per day by what became of them, questions
  per day by answer shape (answered with a citation, answered without one, honest refusal,
  errored), health tiles (index freshness with the incremental upserts sparkline, filings per day,
  the worker's lease, unresolved ingest errors, the gardener's latest findings), the capture→filed
  distribution beside the percentiles, the last known truth per scheduled job, and this console's
  own action log. The only page that polls: every 30 s, and only while the browser tab is visible.
- **Captures** — **read-only**: nothing on this page acts on a single row, and that is the
  redesign rather than an omission. A capture files, is refused or fails on its own, and nothing it
  leaves behind is a decision. Every capture ever as a part-to-whole bar
  (click a segment to filter), arrivals per day by outcome, and the queue: status chips grouped as
  Moving (queued, being filed) and Done (landed in git, declined, could not finish, and the legacy
  handled-by-hand), a submitter filter, human labels on every state. Reclaim and Retention purge
  are the only levers, and both act on the WHOLE queue. The detail reads as a story — what arrived
  (the material, the placement hints, what was flagged), what the librarian says (its report with
  page, commit, anchor, links, the identities it introduced and the spellings it taught the
  registry while filing, the agent's reading), and the row's own history, which holds operator acts
  from before captures stopped parking and nothing since. A row that introduced an identity says so
  at the top, names who it is confirmed by, and says that all of it landed in the same commit.
  Reclaim sweeps against **the worker's own derived lease**,
  not the queue's 300 s and not the librarian's class default: the console resolves
  `$STIGMERGY_LIBRARIAN_TIMEOUT_S` through the same derivation the worker does (2× the agent
  budget + 120 s gates + 180 s headroom), per request, so staging's 600 s
  budget reads 1500 s here exactly as it does on the worker; `fly.toml`'s `[env]` is app-wide, so
  the console's environment IS the worker's, and an operator who splits them by hand fools the
  meter. The form's "release everything now" checkbox is what sends a horizon of 0, and it is only
  safe with no live worker mid-item.
- **Entities** — the vocabulary the brain has grown, and the door for a name nobody has captured
  about yet. There is no verdict on this page and nothing waiting on one: the registry by type, and
  a searchable browser over every entry with the spellings it answers to — each one there because a
  capture introduced it, or because somebody registered it here. Both read the registry this server
  serves (the index's snapshot, or the `--entity-registry` file where there is none).
  **Register an entity** is the one door, and it writes no page: the form's required
  **What is it?** field — everything the person knows about the thing, in their own words — becomes
  the MATERIAL of a capture carrying the registration
  (`server.review.commission_registration`), and the librarian writes the page from that material
  and from what the brain already holds, anchors the note to it, and the identity is born CONFIRMED
  by the actor on the form. The response is the queued row (`id`, `status: queued`, `entity_id`,
  `name` and a message saying what will happen), and the console goes straight to `captures/<id>` —
  the entity appears in the registry when the capture files, a few minutes later, not on the click.
  A name the SERVED registry already resolves is refused here before anything is queued: the entity
  exists, so the thing to do is capture about it. The door needs the evidence store the queue
  archives material into (`AdminService(..., evidence=)`, threaded from the HTTP transport through
  `admin.routes.compose`); a console composed without one refuses by saying so rather than queueing
  a row with no archive behind it. The form checks the Name and every Alias live as it is typed,
  through the `entities/resolve` call, with the birth gate's own folds — `registered` (this
  spelling already resolves to a registered entity), `collides` (the collision fold would refuse
  the name), `similar` (an ADVISORY listing of registered entities sharing a distinctive word or
  containing the name — nothing acts on it), `clear`, or `unchecked` when no registry is readable —
  and says so under the field. The birth gate runs again when the librarian files, so this is a
  warning that is right whenever the snapshot is fresh, never a permission. Type is the closed list
  shipped from `/admin/api/meta`; aliases are optional. The console registers under the admin token
  with the actor as ATTRIBUTION, exactly like every other console mutation — and that actor is the
  name `approved_by:` carries on the page the librarian writes.
- **Repairs** — what the worker's repair pass made of the gardener's findings
  ([repair.md](./repair.md), ADR 044). **This page decides nothing**; it is the reading nobody gave
  a repair before it landed. Repairs by outcome over the whole table, the pass's run strip, and a
  bounded page of the ledger newest first — every outcome together, because the three are one
  history and separating them would invite reading only the good half.
  An **applied** row's detail carries the unified DIFF that was pushed, which is the point of the
  page: it is the only place those bytes are ever read. A **failed** row carries the sentence that
  refused it — a gate, a validator, or a fault — and it matters more than it looks: a failed
  repair's key is remembered, so that finding has stopped being answered and this row is where an
  operator finds out why. A **skipped** row says what the pass could not express, and is remembered
  by nothing. Every row also shows what its KIND changed — one line per declared edit for the
  additive kinds; the drafted body in full for an `entity-body` repair; for a `delete`, the pages
  that stopped existing and the pages rewritten so they no longer link to them; for an
  `entity-alias` merge, which identity survived and which was retired.

  **Remove pages** is the one button here that writes anything. A person names the
  pages and says why, and it lands in that call —
  the pages go, every page that referred to them is rewritten (its frontmatter by code, its body
  by a model), the nine gates judge it, and one App-authored commit is pushed. There is no
  second click, because the judgment was the operator's when they typed it
  ([ADR 043](../decisions/043-a-sweep-is-written.md)); what the console's token buys here is the
  whole authorization — the MCP door asks for an UNRESTRICTED identity instead, and this token
  stands for the whole deployment — so this is its most consequential control and its confirm says
  so. The per-page diffs come back in a dialog: nobody read that prose before it landed, and a
  revert in the knowledge repo is the undo.

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
- **Jobs** — the three workflows, each with its purpose in a sentence, its schedule as "daily at
  HH:MM UTC · next in …", its enabled state in plain words (scheduled, paused by a person,
  auto-paused by GitHub), the last known database truth with that run's stats, the run strip
  (height is duration, colour the outcome), the recent Actions runs (linking out to the logs —
  and only to `github.com`), and the levers: Run now, Enable, Disable, each confirmed with a
  sentence that says what the workflow will do. `retention-purge` is the only one that takes a
  dispatch input (`dry_run`), and it is the only input any dispatch will accept — an undeclared
  key is refused by name before the GitHub gateway is touched, as is an unlisted workflow file.
  Its Run-now form starts with Dry run ticked, so the default path lists what would go and
  touches nothing. The truth column names its source: a `job_runs` row for retention and the
  gardener, `index_meta.built_at` for the rebuild, which writes none. Other recorded work — the
  digest, the webhook, reclaims, and the worker's own repair pass, which is not a workflow and has
  no lever here at all — is listed below the three.
- **Digest** — the configured-pieces checklist, Preview (the byte-identical dry-run body), Post
  now (disabled until both Slack pieces are configured, with the checklist saying which is
  missing; it names the duplicate-window risk before it posts), the run strip and history. Still
  command-only: no schedule exists.
- **Activity** — calls in the window and questions asked, the answer shape (answered with a
  citation, without one, honest refusal — from the verifier's verdict, never the text), capture
  → searchable and capture → filed, calls per day by tool, who asks, per-tool calls/errors/latency,
  the real `ask` questions (golden-set quarry), rate-limit refusals, and the console's own action
  log — every attempted mutation, succeeded or not.

## What leaves a trace, and what does not

Every act that *changes state* goes through one wrapper and writes one `admin_actions` row —
actor, action, arguments, outcome, and the exception class on a failure — whether it succeeded or
not. That row is attribution, not authorization: the actor name is recorded and never checked, and
what authorizes the act is the token that opened the console. If the bookkeeping write itself fails
it is logged loudly and the work still lands; bookkeeping must never fail the work it records.

**`admin_actions` is the only ledger this console writes, and there is no second one anywhere.**
Nothing keeps a governance table: an identity is born in the commit that files the capture that
introduced it, so the page and the registry ARE the record of who stands behind it
([ADR 044](../decisions/044-the-capture-is-the-approval.md)), and a repair's whole record lives on
its own `repairs` row — `status`, `applied_commit`, and the `diff` it pushed. "What changed the
corpus, and why" is answered by that row and by `git log`, whose trailer says which kind of act it
was: `Approved-by:` when a person asked for it, `Repair: <check> #<finding>` when the worker
derived it.

One asymmetry is deliberate: a removal a gate refuses leaves a `failed` row carrying the sentence
that refused it, and the `admin_actions` row for the attempt is there already with the refusing
class name on it. Nothing is retried and nothing is put back — the operator reads both and decides
whether to ask again.

**`entities/create` decides nothing and touches no git.** It writes its `admin_actions` row and a
`capture_queue` row, and that is all this process does; the entity page, its `approved_by:` naming
the actor on the form, and the regenerated registry are the librarian worker's commit, minutes
later. The page and the registry are what say who introduced the identity.

Four POSTs write no such row, because none of them mutates anything:

| Call | Why it is not a mutation | What it *does* leave behind |
|---|---|---|
| Retention purge with **Dry run** | a preview, by construction the same row set the real purge would take | nothing |
| Digest **Preview** | dry-run render only; nothing is posted to Slack | a `digest-dry-run` row in `job_runs` — which is why the Digest page's history fills with them |
| **Substrate check** | an in-process lint over the live index | nothing |
| **Registry check** (`entities/resolve`) | a read of the served registry with the birth gate's own folds; it runs as somebody types into Register an entity, and stops the moment the dialog closes | nothing |

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
