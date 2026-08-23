# admin — code map

The operations console: one web surface over what already runs — the entity registry and the door
for registering one, the captures (read-only), the removal ledger, page removal, the four
the night shift, the gardener's findings, the index and the ops files it serves,
activity and the worker's lease. Mounted as an ASGI branch in front of the MCP
transport, inside the `app` process group, behind its own single credential.
Narrative: [`docs/reference/admin-console.md`](../../../docs/reference/admin-console.md).

**It is a skin, not a subsystem.** Every act lands on a seam another package owns and tests:
`capture.retention`, `capture.queue` (reads, plus `release_expired`), `capture.latency`,
`gardener.store`, `index.check`, `index.store`, `repair.store`,
`server.review.commission_registration`, `server.review.queue_deletion`,
`kernel.registry`. The only state
it owns is `admin_actions`.

**Captures are READ-ONLY here.** Nothing drains a queue row: the two write buttons on that page
(Reclaim, Purge) act on the queue as a WHOLE, and a capture reaches its terminal state through the
librarian or the expiry sweep. Nothing on this console decides an identity either: a capture
introduces the entity it is about, confirmed by whoever captured.

**It is not a read surface over the corpus** — no search, no `ask`, no page bodies.
`test_admin_never_imports_the_read_path_or_the_mcp_adapter` bans `stigmergy.index.search`,
`stigmergy.answer`, `stigmergy.server.mcp_server`, `.service` and `.transport_http` from every
module here, so the rule is a property of the import graph. (The Activity page does show `ask`
questions — user content, not corpus content — and the Entities page reads the entity REGISTRY,
an `ops/` control file every MCP identity already reads through `list_entities`. The Repairs
page does carry page bytes: the `diff` a removal pushed, and on a row of a RETIRED kind the body it
never wrote — bytes a sweep produced and recorded in `repairs`, never a page this console
fetched, and read AFTER the push because nothing read them before it. A page removal hands back
the same thing, a unified diff per rewritten page.)

## Modules

| Module | What it is |
|---|---|
| `routes.py` | `compose(inner, *, conn, server_settings, admin_settings=None, evidence=None)` — the only door into this package, `evidence` being the process's one evidence store, without which Register an entity has nothing to archive a capture's material into, called by `server.transport_http.build_http_app`. Also `_Branch` (outermost ASGI router), `_AdminGate` (host → token → security headers), `_json_endpoint` (domain-exception → status map) and the route table |
| `service.py` | `AdminService`, one method per route; `ADMIN_DOOR` (the `source` every governed call names); `NIGHT_SHIFT` (what runs unattended, and where each one's database truth lives) and `INDEX_REBUILD_COMMAND` (the one pass no process here can run); `worker_visibility_timeout_s()`/`WORKER_MAX_ATTEMPTS`; the registry check's verdict constants (`VERDICT_*`) and its advisory similarity (folded once per request); the read ceilings (`REPAIR_RECENT_LIMIT`, `MAX_RESOLVE_NAMES`, `LATENCY_SAMPLE_LIMIT`, `MAX_METRICS_DAYS`); `_clean`, the one sanitizing seam every untrusted string leaves through; the domain errors `AdminBadRequest`/`AdminNotFound`/`AdminRefused` |
| `measurements.py` | the Activity page's numbers, read from columns other code already wrote (`audit_log.result`, `capture_queue`, `job_runs`): `build_report` (questions per identity per week, answer shape, capture→filed and capture→searchable latency), plus `shape_of` and its SQL mirror `answer_shape_by_day` — the ONE reading of an `ask` result, so the table and the dashboard's chart cannot disagree. Reads only, and provisions nothing |
| `settings.py` | `AdminSettings` + `from_env`, the two `*_ENV` constants, `DEFAULT_ACTOR`, and the sha256-shape refusal that turns a malformed hash into a `StartupError` |
| `auth.py` | `token_matches` (sha256 + `hmac.compare_digest`), `bearer_token` (two `Authorization` headers → `None`), `host_allowed` |
| `schema.py` | `admin_actions`: `ensure_admin_schema` (behind `capture.schema.startup_ddl_lock`), `record_action` (never raises), `recent_actions` |
| `cli.py` | `stigmergy-admin-token` — mints the one credential: 32 random bytes, plaintext printed once beside its `STIGMERGY_ADMIN_TOKEN_HASH=` line, nothing stored |
| `static/` | the SPA, no build step: `index.html` + `assets/app.js` (shell, grouped nav, hash router with the old tab names as aliases, login), `theme.js` (the ONE classic script: it stamps the chosen theme on `<html>` before the first paint — a module would be deferred and flash, an inline script is refused by the CSP), `api.js` (the one fetch seam), `state.js` (the server's meta + the chart window), `copy.js` (the VOCABULARY — every system word's human label, meaning and who decides; the per-page explainers), `ui.js` (DOM helpers, pills, the confirm-with-form modal with live field checks, tooltips, the theme picker), `charts.js` (SVG charts built with `createElementNS`, each with a table twin), `views/` (one module per page: `dashboard`, `captures`, `entities`, `repairs`, `gardener`, `index`, `worker`, `jobs`, `activity`, plus `common.js` for the loading wrapper, the mutation helper, the report renderer and the trace timeline), `styles.css` |

Exactly one module imports this package — `server/transport_http.py`, pinned by
`test_only_the_http_transport_composes_the_admin_branch`.

## The HTTP surface

`ADMIN_PREFIX = "/admin"`, `API_PREFIX = "/admin/api/"`. The token is checked only under
`API_PREFIX`; the shell and its assets are deliberately tokenless (inert files, and a login screen
cannot need a token to render).

| Method | Path | Service call | Token |
|---|---|---|---|
| GET | `/admin` | 307 → `/admin/` | no |
| GET | `/admin/` | `static/index.html` | no |
| GET | `/admin/assets/…` | `StaticFiles(static/assets)` | no |
| GET | `/admin/api/meta` | `meta()` — configuration facts plus every closed vocabulary the frontend renders | yes |
| GET | `/admin/api/overview` | `overview()` | yes |
| GET | `/admin/api/metrics` | `metrics()`, in a worker thread — `?days=` (default 30, clamped to 1–365; non-integer is a 400): captures by arrival day and current status, capture→filed samples, `ask` outcomes per day (`measurements.answer_shape_by_day`, grouped in SQL), calls per day/tool/identity, each job's run history, and the repair table's status counts beside a bounded page of its newest rows (`repair.store.counts_by_status` / `recent`) | yes |
| GET | `/admin/api/queue` | `queue_list()` — repeatable `?status=`, `?submitter=`, `?limit=` (default 50; non-integer is a 400) | yes |
| POST | `/admin/api/queue/reclaim` | `queue_reclaim()` — optional int `visibility_timeout_s`; omitted means the worker's derived lease, resolved per call; the horizon is clamped at 0 | yes |
| POST | `/admin/api/queue/purge` | `queue_purge()` — optional int `older_than_days`, `dry_run` | yes |
| GET | `/admin/api/queue/{id:int}` | `queue_show()` | yes |
| GET | `/admin/api/gardener` | `gardener_state()` | yes |
| GET | `/admin/api/index` | `index_state()` | yes |
| POST | `/admin/api/index/check` | `index_substrate_check()` | yes |
| GET | `/admin/api/entities/registry` | `entities_registry()` — the served registry, sorted by name, with `by_type` and freshness | yes |
| POST | `/admin/api/entities/resolve` | `entities_resolve()` — `names` must be a JSON list of strings (≤ `MAX_RESOLVE_NAMES`); one verdict per non-blank name. Writes no `admin_actions` row | yes |
| POST | `/admin/api/entities/create` | `entity_create()` — `name`/`entity_type`/`about` required, `entity_id`/`aliases` optional; commissions the entity by queueing a capture and answers the queued row (`id`, `status`, `entity_id`, `name`, `message`). Off the event loop, for the archive write. The page is the librarian's to write, and the identity is born confirmed by the actor | yes |
| GET | `/admin/api/repairs` | `repairs_list()` — the newest `REPAIR_RECENT_LIMIT` ledger rows whatever their kind, each carrying the diff it pushed, plus the whole table's `counts` by status. Read-only: a removal is performed by the worker, and this page is the reading afterwards | yes |
| GET | `/admin/api/repairs/{id:int}` | `repair_show()` | yes |
| POST | `/admin/api/pages/delete` | `pages_delete()` — a PERSON removes pages: `paths` (non-empty) + `why`, QUEUED through `server.review.queue_deletion`, the same seam MCP's `brain_delete` runs, and performed by the librarian worker. It passes no per-path guard: the operator token is the authorization, which makes this the console's most consequential button. What comes back is a queue acknowledgement, and the per-page diffs are read afterwards on the capture | yes |
| GET | `/admin/api/activity` | `activity()` | yes |
| GET | `/admin/api/worker` | `worker_status()` | yes |
| GET | `/admin/api/jobs` | `jobs_state()` — the night shift, read-only | yes |

**There is no free path segment left in this table.** The four `crons/{workflow_file}/…` routes
were the only place a path was read as data, and they went with the crons themselves — the
passes run on the worker's idle branch, so there is nothing to dispatch. The two id routes are
`{id:int}`, which cannot swallow `reclaim` or `purge`, and no route under `entities/` takes a
converter — the three there are literal segments — so no declaration order here is load-bearing. Adding a catch-all under an existing prefix brings the
ordering hazard back with it.

## Reuse

- `routes.compose` — the only composition point. When `configured()` is false it returns
  `_Branch(inner, None)`: no service, no routes, no DDL.
- `AdminSettings.from_env(env=None)` — the only place this package reads the environment; `env` is
  injectable. Never `os.environ` at module scope here.
- `AdminService._mutate` / `_mutate_async` — every state-changing call goes through it: actor
  fallback, an `admin_actions` row on both outcomes, `CaptureError` → `AdminRefused`.
- `AdminService._served_registry` / `_check_name` — the registry check, asked BEFORE anything is
  queued: is this name already registered, or confusable with something registered? The registry is
  `index.check.served_registry`'s answer (the index's snapshot, else the `--entity-registry`
  file) parsed by `kernel.registry.registry_from_text` — the ONE loader, so a snapshot the server
  refuses is refused here too. The verdicts are the gate's own questions in the gate's own order:
  `Registry.canonical_id` (the filing fold — `registered`), then `Registry.collision_id` (the
  birth gate's fold — `collides`), then an ADVISORY similarity listing this module computes for a
  human to judge and nothing acts on. Never a second "collides": a looser fold here would stop an
  operator registering a legitimately distinct entity, and a stricter one would promise a write the
  gate refuses. `entities_resolve` and `entities_registry` let an unreadable snapshot refuse
  outright (the substrate check's posture); `_registry_or_none` is the swallowing variant and
  `entity_create` is its only caller, because a registry this server cannot read must not stop
  somebody registering — the birth gate checks again inside the commit either way. The similarity
  listing folds the registry ONCE per request (`_similarity_index`): N names against M entities is
  M + N folds, not N × M.
- `server.review.commission_registration` — `entity_create`'s whole seam, and it touches no git and
  writes no ledger row: it queues a capture carrying the registration and the LIBRARIAN writes the
  page, births the identity confirmed by the actor and records the approval after its own push.
What stays here is the
  pre-flight `entity_create` owns: the required `about`, the slug-of-the-name check on `entity_id`,
  and the refusal of a name the SERVED registry already resolves (the entity exists — capture about
  it instead). It needs the evidence store the queue archives into, passed as
  `AdminService(..., evidence=)` from `compose`. The exception mapping stays HERE by decision:
  nothing is caught inside `_do`, so `_mutate` records the library's OWN class name in
  `admin_actions` before the `except (EntityError, CaptureError)` outside it raises `AdminRefused`
  with the library's sentence.
- `server.review.queue_deletion` — `pages_delete`'s whole seam, and the same function the MCP
  deletion door runs, so which door a person acted from changes the recorded `source` and nothing
  else. It writes a `delete` capture row and no more: no clone, no commit, no credential — the
  worker performs it, and this package could not push if it wanted to. The exception mapping stays
  HERE for `commission_registration`'s reason: nothing is caught inside `_do`, so `_mutate` records
  the library's own class name in `admin_actions` before the `except` outside it raises
  `AdminRefused`. Every one of them is passed `source=ADMIN_DOOR` — this
  package's one spelling of its own name, so a console act is told apart from an MCP one on the row
  itself rather than by inference.
- `queue.outcomes_by_day` — the metrics' capture series, beside `counts_by_status` in the queue
  module because it is the same fact with a time axis; the console never carries its own query
  over `capture_queue`. `measurements.answer_shape_by_day` is the `ask` series: the Activity
  table's own classifier (`shape_of`) as SQL, pinned against the Python original by test, so a
  chart and the table cannot disagree about what an answer was. `repair.store.counts_by_status` is the removal ledger's
  histogram — aggregated in the database, because that table only grows.
- `auth.token_matches` / `bearer_token` / `host_allowed` — pure; never re-derive a header parse.
- `service._clean` (= `stigmergy.text.sanitize`) — the one cleaning seam for untrusted strings on
  the way out: control characters die, newlines and a literal `<script>` survive, because HTML
  inertness is the client's half. Every excerpt, error, note, finding, rationale, registry name and
  alias crosses it.
- `schema.record_action` — bookkeeping that swallows and logs; it must never fail the work.
- `ui.el` / `ui.svg` / `ui.render` / `ui.clear` (frontend) — the only sanctioned way to build a
  node. `render` exists because `replaceChildren` stringifies `null` into the text `"null"`.
  `el`'s `style` goes through the CSSOM (`node.style`), never a `style` attribute: the console
  ships under `style-src 'self'`, which refuses the attribute.
- `ui.confirmForm` (frontend) — every mutation goes through one, and its `consequence` sentence is
  a required argument, enforced: an empty one throws, so a new mutation without a sentence fails
  in development rather than shipping a blank line over a button that spends or deletes. A field's `live(value, setNote,
  allValues)` hook renders a node under the field as the user types — the Register form's registry
  check is one; its debounce is cancelled when the dialog closes. The dialog traps Tab and hands
  focus back to the control that opened it, and with no fields at all it is the plainest panel this
  console has, which is what a removal's diffs come back in.
- `views/common.js` `mutate(path, body, message, onSuccess?)` (frontend) — every state-changing
  button goes through it: one toast per outcome, the server's `warning` folded into a warning
  toast rather than a second, contradictory one, the result handed on for the flows that need a
  sha or a count. `runShape`/`runTable` — one shape for every run strip and its table twin.
- `copy.js` (frontend) — the vocabulary: `word()`, `status()`, `repairKind()`, `verdict()`,
  `check()`, `severity()`, `jobName()`, `jobConsequence()`, `page()`. Every lookup falls back to
  the raw word, so a new status renders ugly and never invisible. The words themselves come from
  `/admin/api/meta` or from the constant that produced them; this file only knows how to say them.
- `charts.js` (frontend) — `stackedColumns`, `hbars` (HTML, so labels stay legible at any card
  width), `partToWhole`, `sparkline`, `histogram`, `meter`, `runStrip`, and `chartCard` (title,
  the chart, its table twin one toggle away). A series' colour is its KEY role
  (`human`/`model`/`code`/`git`/`fail`) or a categorical slot (`s1`–`s6`), never its rank and
  never a value from data (`seriesColor` falls to the de-emphasis colour for anything else), so
  a filter never repaints the survivors.

## Avoid

- Adding an exemption to `_BearerAuthMiddleware` for this console — `/admin*` never reaches that
  middleware, which is what keeps the webhook's "one exemption, exact path match" doctrine true.
- Importing outside `_ADMIN_ALLOWED_IMPORT_PREFIXES`. The librarian is reachable through
  `librarian.config` alone; `stigmergy.slack.bolt_gateway` may only be imported inside the posting
  handler, so a keyless console never loads the Slack SDK. `stigmergy.kernel.registry` and
  `stigmergy.kernel.normalize` are in the set for the registry check and nothing else, and
  `stigmergy.entities.remote` is deliberately ABSENT — the governed door is reached through
  `server.review`, the same reason nothing that WRITES the corpus is reachable from here.
- Reading `pages_index` for anything but an aggregate. `_zone_counts` is the single read and a
  named entry in `ACL_REACHABILITY_EXCEPTIONS`; anything more names a `visible()` predicate.
- Writing SQL for something a library already exposes. The only SQL owned here is read-side
  plumbing nothing else surfaces: `job_runs`/`ingest_errors`, the `audit_log` aggregates (per
  identity/tool, per day, the `ask` outcome rows the console shapes in Python, the rate-limit
  trips), the zone counts, and `admin_actions`.
- Raising a message that could carry captured content across the HTTP boundary. The catch-all in
  `_json_endpoint` returns `the operation failed (<ClassName>)`; only the three domain errors
  cross with their sentence.
- Deriving a decision in the frontend. `views/repairs.js` sends Remove pages and nothing else —
  it renders a ledger of what the worker already did and computes no verdict of its own;
  `views/entities.js` renders the registry check's verdict beside the field it belongs to and acts
  on none of it. Whether a name may be born is the birth gate's answer inside the commit, and
  whether a removal lands is the nine gates' answer inside the worker — on every door alike. The
  frontend renders a decision and never derives one.
- Turning the `similar` verdict into a refusal, anywhere. It is a listing for a person's eyes;
  the only "collides" is `Registry.collision_id`, and the gate runs again after the clone.
- Offering a way to move a capture out of its state. The Captures page reads; a queue row is the
  librarian's and the sweep's to finish.
- Building DOM from an HTML string in `static/`. `innerHTML`, `outerHTML`, `insertAdjacentHTML`,
  `document.write`, `eval(`, `new Function` and any `http(s)` `src`/`href` are grepped out of the
  shipped files by `tests/admin/test_static_discipline.py`; only `textContent` makes markup inert.
- Putting `title:` and `disabled:` on the same element. A disabled control fires no pointer
  events and takes no focus, so the hint is unreachable; the same test greps for the pair. Render
  the reason as visible text beside the control instead.
- Holding a cursor across an `await` — this service shares the process-wide autocommit connection,
  and every method here is synchronous: a route that needs the loop free hands the whole call to a
  worker thread.
- Adding a CLI flag. The server's command line is pinned byte-identical between `fly.toml` and the
  Dockerfile `CMD`; configuration is env-only so that pin never moves.

## Data & contracts

`admin_actions` (`schema.py`): `id BIGSERIAL` · `ts TIMESTAMPTZ DEFAULT now()` · `actor` · `action`
· `args JSONB` · `outcome` (`ok`/`error`) · `error_class`, plus `admin_actions_ts_idx (ts DESC)`.
`actor` is **attribution, not authorization** — recorded, never checked.

Compose-time DDL, only when configured: `ensure_admin_schema` plus
`gardener.schema.ensure_gardener_schema` and `repair.schema.ensure_repair_schema`, which the read
paths would otherwise meet as a bare `UndefinedTable` on a fresh database.

| Env var | Default | Effect |
|---|---|---|
| `STIGMERGY_ADMIN_TOKEN_HASH` | `""` | the master switch: unset → the console does not exist. Must be 64 sha256 hex (uppercase normalized); any other non-empty value raises `StartupError` at startup |
| `STIGMERGY_ADMIN_ACTOR` | `admin-console` | the `actor` fallback and the form prefill |

Read but not owned: `STIGMERGY_PUBLIC_HOST` (`routes._public_hosts_from_env`, a two-line copy of
the transport's parser — importing it would close a cycle through the composition point).

**Wire contract**: JSON everywhere except the shell (`text/html`) and the assets. Errors are
`{"error": "<sentence>"}` at 400 (`AdminBadRequest`), 401 (generic, never a reason), 404
(`AdminNotFound`, and the inert console's blanket answer), 409 (`AdminRefused`), 421 (foreign
`Host`), 500 (class name only). No 502: this package reaches no other service — the GitHub Actions
gateway went with the crons, and nothing replaced it. Every response carries
`content-security-policy` (`default-src 'none'`; fetch directives `'self'`, `img-src` also `data:`;
`base-uri`/`form-action`/`frame-ancestors` `'none'`), `x-content-type-options: nosniff`,
`referrer-policy: no-referrer` and `strict-transport-security: max-age=31536000;
includeSubDomains`; `/admin/api/*` additionally `cache-control: no-store`, the shell and the
assets `cache-control: no-cache` (an ETag round trip on every load, so a deploy that renames a
module never leaves a browser running the old `app.js` against new imports).

**The registry check's wire shape** (`entities_resolve`'s `checks[]`):
`{"name", "verdict": registered|collides|similar|clear|unchecked, "match": <entry> | null,
"similar": [<entry> + "why"]}`, beside `registry`: `{"available", "road": snapshot|file|none,
"source", "refreshed_at"}`. An `<entry>` is `{id, name, type, aliases, approved_by}` — the same
shape `entities_registry` lists, `approved_by` being whoever's capture introduced the entity, empty
where the page records nobody.

**The vocabularies the frontend renders all ship from `meta()`**, never a second copy in JS:
`entity_types`, `statuses`, `terminal_statuses`, `legacy_statuses` (today just `resolved`),
`repair_kinds` and `gardener_severities`. `copy.js` knows only how to SAY them, and every lookup
falls back to the raw word — a new status renders ugly and never invisible.

`NIGHT_SHIFT` — the gardener, the retention purge and the index rebuild. Each row names where its
database truth lives (`job_runs:<job>`, or `index_meta.built_at` for the rebuild, which writes
none) and `runs_in`: `"worker"` for the two the librarian schedules itself on its idle branch, and
`"operator"` for the rebuild, which needs an embedding key no process here holds — so its row
carries `INDEX_REBUILD_COMMAND` instead of a time. A worker row also names the variable that moves
it, pinned against `librarian.config`'s own defaults by
`test_the_console_names_the_setting_that_actually_schedules_each_worker_pass`, and the command is
RUN by `test_the_pass_the_console_cannot_run_names_a_command_that_exists` — a page that names a
command is making a promise.

**Frontend theme**: every colour token is declared ONCE as `light-dark(light, dark)` on `:root`,
so a token added to one theme and forgotten in the other cannot exist; the three states are two
one-line rules (`:root[data-theme="light"|"dark"] { color-scheme: … }`) plus the absence of the
attribute for Auto, and `@supports not (color: light-dark(…))` keeps an old browser on the light
palette rather than on none. `theme.js` and `ui.js` each spell the storage key and the two state
names — a classic script cannot be imported by a module — and `test_static_discipline.py` pins the
two spellings against each other.

**Frontend**: each view module exports `render(host, params?) → cleanup?`, dispatched from
`app.js`'s `GROUPS` (the sidebar, grouped by the job a person came to do) and `DETAIL_ROUTES`
(`captures/…` and `repairs/…`, each naming its OWN id parser — a shared blanket `Number(...)` once
turned a non-numeric segment into `NaN` and asked the API for it); the old tab names (`overview`, `queue`) are aliases, so a bookmark still lands. The token
lives in `sessionStorage` under `stigmergy-ops-token` — no cookie, therefore no CSRF surface — and
any 401 clears it, stashes the reason for one reload, and lands on the login screen with that
reason shown. The chart window (7/30/90 days) lives in `sessionStorage` too, and the per-page
explainer's collapsed state in `localStorage`. Only the dashboard polls (30 s, skipped while
`document.hidden`), and it is the only view returning a cleanup function; `navigate()` carries a
token so a view that resolves after the next navigation started has its cleanup run at once.

## Behaviour worth knowing before editing

- **A branch, not a middleware exemption.** `_Branch` is the outermost ASGI object: `/admin*` HTTP
  requests go right, everything else — lifespan (the MCP session manager lives on it), websockets,
  every other path — flows to the inner app untouched, and `__getattr__` delegates so the branch
  stays transparent to Starlette introspection. Path matching is exact, so `/administration` is
  inner traffic.
- **Inert means inert.** With the hash unset there is no service, no route table and no
  DDL; from the MCP surface the module is undetectable.
- **The gate's order is load-bearing**: `Host` first (421), token second (401), headers on the way
  out of both — so the host check also covers the tokenless shell. With no `$STIGMERGY_PUBLIC_HOST`
  every host passes, which keeps local dev unchanged.
- **Not every POST is a mutation.** `queue/purge --dry-run`, `index/check` and
  `entities/resolve` write no `admin_actions` row.
- **Every read of a table that only grows has a ceiling**, applied in SQL: the repair ledger's
  page (`REPAIR_RECENT_LIMIT`, and every applied row on it carries a whole diff), the
  capture→filed sample the percentiles are cut from (`LATENCY_SAMPLE_LIMIT`), the metrics window
  (`MAX_METRICS_DAYS`), and the two `audit_log` ROW reads (`_ask_questions`, `_rate_limited`, each
  clamped again inside the method) — the per-identity/tool figures need none, being an aggregate
  bounded by cardinality. `entities/resolve` is the one that REFUSES instead of truncating
  (`MAX_RESOLVE_NAMES`): it is called as somebody types, so its input is a form's worth of names
  and a longer list is a caller doing something else.
- **The console reads page PATHS, never page BODIES.** `index/check` and `gardener` carry paths out
  of the corpus, both behind the operator token and both covered by the declared ACL exception.
- `_zone_counts` and `schema.record_action` are the only two places a swallowing `except` is right
  here — no index yet is a state, and bookkeeping must never fail the work.
- **The worker's lease is resolved per call** (`worker_visibility_timeout_s()` →
  `librarian_config.resolved_visibility_timeout_s()`), never frozen at import, and it governs both
  directions: a console comparing an old claim against `capture.queue`'s own 300 s would call every
  long agent item dead on the read path, and on the write path Reclaim would requeue captures out
  from under a running worker. `queue.release_expired` takes no default, so a caller must state the
  horizon; reading the env per call is correct because `fly.toml`'s `[env]` is app-wide — this
  process's environment is the worker's.
- `_mutate` records `outcome='error'` and re-raises, including for a domain refusal, so the action
  log answers "what was attempted", not only "what succeeded".
- **The registry check is a warning, never a permission.** It reads the snapshot this server
  serves; the birth gate re-checks against the registry the commit will publish, inside the
  capture's own worktree when the librarian files the registration. When the snapshot is fresh the
  two agree; when it is stale the gate wins, and the console shows the gate's own sentence as the
  capture's refusal on the row it navigated to.
- **The console's authorization IS its token** (one dedicated credential, revoked by one secret
  change), and `actor` is free text behind it —
  attribution, never checked. That is why `pages_delete` reaches the shared
  sequence with no authorization argument: authorization is per-surface, decided before the call,
  and the caller sets are pinned both ways in `tests/test_architecture.py`. A second surface added
  here without deciding who may is exactly what those pins exist to catch.
- **A read is cheap and a write to git is not.** `pages/delete` runs in a worker thread
  (`run_in_threadpool`) because it clones the knowledge repo, runs the nine gates — `git` and
  `gitleaks` subprocesses — and pushes, and the MCP tools share this process;
  `entities/create` rides the same thread for the archive write its capture pays for, and `metrics`
  does the same for its dozen aggregate queries. The service holds no cursor across the call
  boundary, so the autocommit connection is safe to use from the thread.
- **A removal's reading happens after the push, and so does a repair's.** `pages_delete` returns
  the unified diff of every page the sweep rewrote and the console shows them in a fieldless
  `confirmForm` UNRENDERED; an applied repair's diff is stored in `repairs` and read the same way
  on the Repairs page. What landed in the repo is those bytes, nobody read the model's prose before
  it landed, and `git revert` in the knowledge repo is the undo — the diff IS the reading, for
  every repair.

## Common tasks

| Task | Touch |
|---|---|
| Add a read endpoint | a method on `AdminService`, a `@_json_endpoint` handler, a `Route`. A new table means checking the import allowlist in `tests/test_architecture.py` first |
| Add a mutation | the same, but through `_mutate`/`_mutate_async` for the `admin_actions` row and the actor fallback — and the frontend flow through `confirmForm` with an honest consequence sentence |
| Report a new unattended pass | a row in `NIGHT_SHIFT` (file, title, `runs_in`, `truth`, and either the `at_setting`/`at_default` pair or a `command`), plus its purpose and truth sentences in `copy.js`'s `JOB`. The pass itself is scheduled in `librarian/schedule.py` — this table only reports it, and the settings test fails until the two agree |
| Reach a new package | add the SUBMODULE to `_ADMIN_ALLOWED_IMPORT_PREFIXES` with a stated reason, in the same diff |
| Add a config knob | a field on `AdminSettings` + a `*_ENV` constant + a line in `from_env`. Never a CLI flag |
| Change how untrusted text is cleaned | `service._clean` — never at a call site, and never by flattening newlines |
| Add a page | a module under `static/assets/views/` exporting `render(host)`, a route in `app.js`'s `GROUPS` (plus a `DETAIL_ROUTES` row if it has a detail), its title/purpose/explainer in `copy.js`'s `PAGE`, DOM built only through `ui.el`/`ui.svg` |
| Add a chart | a series whose colour is a KEY role or a categorical slot, inside `chartCard` with a `tableSpec` (the table twin is not optional); never a value on every point, never a second y-axis |
| Give a new system word a human label | `copy.js` — the list itself ships from `meta()` |
| Rotate or revoke the credential | `stigmergy-admin-token`, then set the new hash. There is no store and no list |

## Tests

`tests/admin/` runs against real Postgres through `tests.testdb` and real git wherever a claim
needs it (`conftest.build_bare_knowledge_repo` — the bare remote the registry fixtures publish an
entity page into, and the one a removal clones); the ONE network edge — Slack — is faked, and
there is no second one, because the console reaches no other service.
`test_settings_and_auth.py` and `test_cli.py` are keyless; `test_service_pg.py` is the largest
suite (queue reads, reclaim on both edges of the worker's lease, purge dry-run vs real, the night
shift's rows, a registry the loader refuses, `entity_create` against a throwaway bare remote, and
`pages_delete` through the shared sequence);
`test_console_reads_pg.py` covers the served registry, the registry check (every verdict beside its
benign twin, the gate's fold against a looser one) and the metrics window, through the service and
over the wire; `test_routes_pg.py` exercises the real `compose` product over
`httpx.ASGITransport` (inert 404s, the tokenless shell vs the 401 API, the security and cache
headers, the status mapping, and the handler that clones proving it never runs on the event
loop); `test_static_discipline.py` greps the shipped frontend files. Auth refusals are tested
beside their benign twins throughout.

`tests/server/test_admin_branch.py` proves the branch on the real `build_http_app` wiring: the MCP
token store does not open the console, the admin token opens nothing on `/mcp`, and
`rate_limiter_of(app)` still works through the branch's delegation.

`tests/test_architecture.py` holds the boundary: `test_admin_sources_found` (the anti-blindness
floor), `test_admin_imports_only_its_declared_set`,
`test_admin_reaches_the_librarian_through_config_alone`,
`test_admin_actually_uses_its_declared_librarian_exception` and
`test_every_declared_admin_import_prefix_is_actually_imported` (the pruning halves),
`test_admin_loads_the_slack_sdk_door_lazily`,
`test_admin_never_imports_the_read_path_or_the_mcp_adapter`,
`test_only_the_http_transport_composes_the_admin_branch`, the ACL-reachability parametrization
naming `admin/service.py`, and the caller pin that names this package as the surface deciding its
own authorization — `test_the_shared_mint_sequence_is_entered_from_exactly_the_authorizing_surfaces`.
The repair apply's own pin no longer names this package: no console route applies a repair.
