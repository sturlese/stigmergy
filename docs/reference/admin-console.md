# The admin console (`/admin`)

The web control room over what already runs — the inbox of everything waiting on a steward, the
capture queue read-only beside the two levers over the whole of it, the identities and spellings
the librarian proposed and the verdicts on them (each one an App-authored commit, ADR 030), the
repair proposals derived from the gardener's findings (a governed Approve applies one, ADR 039),
corpus health, the index and the ops files it serves, the worker, the four scheduled jobs, the
digest, and who is using the brain — served by the SAME `app` process group that serves MCP,
behind its own token. Design record:
[ADR 029](../decisions/029-admin-console.md), [ADR 030](../decisions/030-server-side-entity-minting.md)
and [ADR 039](../decisions/039-governed-repair-loop.md); code map:
[`src/stigmergy/admin/index.md`](../../src/stigmergy/admin/index.md), which carries the full route
table.

What it deliberately is NOT: a brain client. No search, no page rendering, no `ask` — the
architecture tests enforce that boundary on the package. It reads page *paths* in four places
(the substrate check, the gardener's findings, a repair proposal's target paths, and a proposed
identity's own entity page beside the pages already filed against it), it reads the **entity
registry** (an `ops/` control file — the vocabulary, which every MCP identity already reads
through `list_entities`), and it fetches no page: everything it renders comes from rows in this
database, never from the knowledge repo. Two things on it read as page prose, and each is there
because deciding without reading it would be deciding blind. An identity proposal carries
the entity page's own What / Who paragraph, taken off the page index by
`server.review.items_for_doorbell` — the same read the Slack doorbell makes, which asks
`acl.visible()` of every page it lists (the console reads it unrestricted, like the doorbell, so
what bounds it is the operator token). An `entity-body` repair proposal carries the drafted body
in full: text a model wrote, sitting in `repair_proposals`, which is not a page yet and was never
a page this console fetched.

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
| The proposal inbox, the registry check on a name, the registry browser | a registry this server can read: the index's `ops/entity-registry.json` snapshot (refreshed by the push webhook and the nightly rebuild), or the `--entity-registry` file where there is no snapshot | the Inbox and the Entities desk list no identity or spelling proposal at all — a proposal IS a registry entry, and that list is read off the SNAPSHOT alone, so a stack answering from the `--entity-registry` file shows none either (repair proposals are a table of their own and still list). Every name check answers `unchecked` and the page says no registry is readable here; the birth gate still runs when the librarian files, inside that capture's own worktree, against the knowledge repo as it stands |
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
  legacy word), the repair kinds, the severities, the review item kinds, the verdicts each kind
  takes, the decision doors and the entity types — so the page never hardcodes a second copy that
  could drift; only the wording is the frontend's (`copy.js`).

Every page opens with a "How to read this page" explainer, collapsible and remembered per page in
the browser. Pages with a time axis (Dashboard, Captures, Repairs, Gardener, Index, Worker,
Activity) share one window picker (7, 30 or 90 days) in the top bar; every chart on the page
re-renders against the same slice. Every chart — the run strips included — has a table twin one
toggle away, and nothing on a chart is reachable only by hovering.

## What each page does

- **Dashboard** — the inbox count as the number that means work (everything owing a steward a
  decision, broken down into proposed entities, proposed spellings and repair proposals, each
  link opening the inbox already filtered to that kind), beside **the write path, live**: the
  window's captures flowing through the model's draft and code's gates into landed-in-git,
  refused and could-not-finish, with what is still moving named underneath and the legacy
  handled-by-hand outcome kept for as long as old rows carry it. Then captures per day by what
  became of them, questions per day by answer shape (answered with a citation, answered without
  one, honest refusal, errored), health tiles (index freshness with the incremental upserts
  sparkline, filings per day, the worker's lease, unresolved ingest errors, the gardener's latest
  findings), the capture→filed distribution beside the percentiles, the last known truth per
  scheduled job, and the two ledgers merged into one feed. The only page that polls: every 30 s,
  and only while the browser tab is visible.
- **Inbox** — everything waiting on a steward as ONE list across the three kinds (proposed
  entities, proposed spellings, repair proposals) — `server.review.items_for_doorbell`, the same
  read the Slack doorbell rings from, so the console and the doorbell cannot disagree about what
  is waiting on a person. The order is that read's own: the pending repairs first, oldest first,
  then the proposals in registry-id order. Chips filter by kind and carry its count; each item
  carries the ledger's latest decision when one exists, which is how a steward learns a second
  door got there first; a banner says so when more is waiting than the list can carry. Opening an
  item lands where it is decided — a proposed entity on its own detail, a proposed spelling on the
  Entities desk, a repair on its proposal. The sidebar badge is this list's count. **Nothing here
  waits on a submitter**: a capture never parks, so everything in this list is something the
  librarian PROPOSED after filing.
- **Captures** — **read-only**: nothing on this page acts on a single row, and that is the
  redesign rather than an omission. A capture files, is refused or fails on its own, and what a
  steward governs is the proposals it left behind. Every capture ever as a part-to-whole bar
  (click a segment to filter), arrivals per day by outcome, and the queue: status chips grouped as
  Moving (queued, being filed) and Done (landed in git, declined, could not finish, and the legacy
  handled-by-hand), a submitter filter, human labels on every state. Reclaim and Retention purge
  are the only levers, and both act on the WHOLE queue. The detail reads as a story — what arrived
  (the material, the placement hints, what was flagged), what the librarian says (its report with
  page, commit, anchor, links, the entities and spellings it proposed while filing, the agent's
  reading), and the row's own history, which holds operator acts from before captures stopped
  parking and nothing since. A row that proposed something says so at the top and links each
  proposal to the Entities desk, where it is decided.
  Reclaim sweeps against **the worker's own derived lease**,
  not the queue's 300 s and not the librarian's class default: the console resolves
  `$STIGMERGY_LIBRARIAN_TIMEOUT_S` through the same derivation the worker does (2× the agent
  budget + 120 s gates + 390 s Drive conversion + 180 s headroom), per request, so staging's 600 s
  budget reads 1890 s here exactly as it does on the worker; `fly.toml`'s `[env]` is app-wide, so
  the console's environment IS the worker's, and an operator who splits them by hand fools the
  meter. The form's "release everything now" checkbox is what sends a horizon of 0, and it is only
  safe with no live worker mid-item.
- **Entities** — the identity desk, and the only door on this console where an identity is
  decided. Each identity the librarian PROPOSED while filing is a card: its name and type, the
  spellings it carried, the entity page's own What / Who paragraph, the pages already filed
  against it, and a **registry check** computed server-side against the registry this server
  serves (the index's snapshot, or the `--entity-registry` file where there is none) with the
  birth gate's own folds — `registered` (this spelling already resolves to a registered entity, so
  the proposal IS that entity under another name: merge it), `collides` (the collision fold would
  refuse the name), `similar` (an ADVISORY listing of registered entities sharing a distinctive
  word or containing the name — nothing acts on it), `clear`, or `unchecked` when no registry is
  readable. The check runs against the REST of the registry, never the whole of it: a proposal
  always resolves to itself, and that says nothing. Three verdicts sit on the card and on its
  detail. **Approve** confirms the identity — `approved_by` becomes the actor and the registry
  stops calling it proposed; the page and everything filed against it are untouched. **Merge
  into…** says the proposal IS a registered entity: its name and every spelling it carried become
  that entity's aliases, its page goes, and every page anchored to it is re-anchored to the
  survivor; the picker offers the lane's `merge_candidates` first and checks a typed registry id
  live against the served registry, saying so when the id is unknown or is itself a proposal —
  `entities.decide` refuses both against the repo as it stands. **Decline** deletes the proposed
  page and leaves the pages that anchored to it holding everything but that anchor — and the ledger
  remembers, which is exactly what stops the librarian proposing the same identity again.
  Proposed SPELLINGS are the same desk one size down (Approve moves the spelling onto the
  entity's `aliases`; Decline drops it from `proposed_aliases`). Every verdict is ONE commit
  through `server.review.decide_and_record` — the same ordering function MCP and Slack run:
  `entities.remote.decide_via_clone` → `entities.decide` in a throwaway clone, authored by the
  librarian App with a `Decided-by:` trailer naming the actor, the push first and the ledger row
  after — and it lands in BOTH ledgers, `admin_actions` and the append-only `review_decisions`
  (ADR 030). Beside the proposals: the registry by type, and a searchable browser over every entry
  with its aliases, a proposed one marked as such and its proposed spellings listed.
  **Register an entity** is the door for a name nobody has captured about yet, and it writes no
  page: the form's required **What is it?** field — everything the steward knows about the thing,
  in their own words — becomes the MATERIAL of a capture carrying the registration
  (`server.review.commission_registration`), and the librarian writes the page from that material
  and from what the brain already holds, anchors the note to it, and births the identity CONFIRMED
  by the steward. The response is the queued row (`id`, `status: queued`, `entity_id`, `name` and a
  message saying what will happen), and the console goes straight to `captures/<id>` — the entity
  appears on this desk when the capture files, a few minutes later, not on the click. A name the
  SERVED registry already resolves is refused here before anything is queued: the entity exists, so
  the thing to do is capture about it. The door needs the evidence store the queue archives material
  into (`AdminService(..., evidence=)`, threaded from the HTTP transport through
  `admin.routes.compose`); a console composed without one refuses by saying so rather than queueing
  a row with no archive behind it. The form still checks the Name and every Alias live as the
  steward types, through the `entities/resolve` call, and says so under the field — the birth gate
  runs again when the librarian files, so this is a warning that is right whenever the snapshot is
  fresh, never a permission. Type is the closed list shipped from `/admin/api/meta`; aliases are
  optional. The console decides and registers under the admin token with the actor as ATTRIBUTION,
  exactly like every other console mutation — MCP and Slack instead enforce a resolved identity's
  steward status; the console does not, because the shared admin credential cannot back a
  second-human rule.
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
  for the reason the Entities desk shares `decide_and_record`: it applies exactly the approved ops
  through the librarian's own validator and its gates, as ONE App-authored commit with an
  `Approved-by:` trailer. `review_decide`'s per-target-path steward check is deliberately not
  reached here, exactly as the Entities desk does not reach its own steward check — that guard is
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

**Every governed verdict writes a SECOND row.** Three routes take one — `entities/decide` (an
identity's Approve, Merge or Decline, and a spelling's Approve or Decline) and the two Repairs
verdicts — and each writes into `review_decisions` beside its `admin_actions`
row. That is the same append-only governance ledger MCP's `review_decide` and Slack's review card
write into, so "who decided this identity" answers from one table regardless of which SERVER-SIDE
door it came through (ADR 030) — and, since issue #51, regardless of which door at all:
`stigmergy-entities approve`/`merge`/`decline` decides from the steward's own clone and still
writes the same row, because the writer moved down to `capture.decisions`, below the
`stigmergy.entities` -> `stigmergy.server` edge that would otherwise forbid it. A DECLINE is not
bookkeeping there: the librarian reads that ledger and refuses to propose an identity a steward
has already declined, so the row is the memory that stops the loop. Which door wrote a row is on
the row itself: `extra->>'source'` is one of `mcp`/`slack`/`admin`/`cli`, required on every write.
The `review_decisions` write and the git push it follows happen inside the SAME `_mutate`-wrapped
attempt as the `admin_actions` row, not before it: a refusal anywhere in the clone leaves neither
ledger touched.

**`entities/create` is the one mutation whose ledger row is not the console's to write.** It
touches no git and decides nothing here: it writes its `admin_actions` row and a `capture_queue`
row, and that is all this process does. The `review_decisions` row for the entity — recorded as
the steward's own APPROVE, so "entities born" still counts every door alike — is written by the
LIBRARIAN worker (`librarian.processing`) after the commit that births the entity has been pushed,
carrying the commit sha, the door (`admin`) and the capture id in `extra`. A ledger fault there is
logged and never turns a landed commit into a failed capture; the page and the registry are what
say the identity is confirmed.

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
| **Registry check** (`entities/resolve`) | a read of the served registry with the birth gate's own folds; it runs as a steward types into Register an entity, and stops the moment the dialog closes | nothing |

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
