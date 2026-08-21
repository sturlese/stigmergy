# admin — code map

The operations console: one web surface over what already runs — the inbox of everything waiting on
a steward, the identity decisions and the registry behind them, the captures (read-only), the repair
proposals, the four scheduled workflows, the gardener's findings, the digest, the index and the ops
files it serves, activity and the worker's lease. Mounted as an ASGI branch in front of the MCP
transport, inside the `app` process group, behind its own single credential.
Narrative: [`docs/reference/admin-console.md`](../../../docs/reference/admin-console.md).

**It is a skin, not a subsystem.** Every act lands on a seam another package owns and tests:
`capture.retention`, `capture.queue` (reads, plus `release_expired`), `gardener.store`,
`digest.run`, `index.check`, `repair.store`,
`server.review.items_for_doorbell`, `server.review.decide_and_record`,
`server.review.commission_registration`, `server.review.apply_repair_and_record`,
`server.pilot_report` (the report and its per-day
classifier), `capture.decisions.recent_decisions`, `repair.store.counts_by_status`,
`kernel.registry`, `entities.decide`. The only state it owns is `admin_actions`.

**Captures are READ-ONLY here.** Nothing drains a queue row: the two write buttons on that page
(Reclaim, Purge) act on the queue as a WHOLE, and a capture reaches its terminal state through the
librarian or the expiry sweep. What a steward decides in this console is an IDENTITY, after the
filing — Approve / Merge into… / Decline on the Inbox and the Entities desk.

**It is not a read surface over the corpus** — no search, no `ask`, no page bodies.
`test_admin_never_imports_the_read_path_or_the_mcp_adapter` bans `stigmergy.index.search`,
`stigmergy.answer`, `stigmergy.server.mcp_server`, `.service` and `.transport_http` from every
module here, so the rule is a property of the import graph. (The Activity page does show `ask`
questions — user content, not corpus content — and the Entities page reads the entity REGISTRY,
an `ops/` control file every MCP identity already reads through `list_entities`. A proposal's
`summary` is the What / Who paragraph the LIBRARIAN wrote on the entity page it created, carried
out by `server.review`, not a page body this console went and read.)

## Modules

| Module | What it is |
|---|---|
| `routes.py` | `compose(inner, *, conn, server_settings, admin_settings=None, gateway=None, evidence=None)` — the only door into this package, `evidence` being the process's one evidence store, without which Register an entity has nothing to archive a capture's material into, called by `server.transport_http.build_http_app`. Also `_Branch` (outermost ASGI router), `_AdminGate` (host → token → security headers), `_json_endpoint` (domain-exception → status map) and the route table |
| `service.py` | `AdminService`, one method per route; `CRON_WORKFLOWS`/`DISPATCHABLE` (the drivable-workflow allowlist); `worker_visibility_timeout_s()`/`WORKER_MAX_ATTEMPTS`; the pre-registration check's verdict constants (`VERDICT_*`) and its advisory similarity (folded once per request); the read ceilings (`INBOX_LIMIT`, `DECISIONS_LIMIT`, `REPAIR_PENDING_LIMIT`, the metrics window bounds); `_clean_leaves`, the walk that cleans every string leaf of a JSON value; the domain errors `AdminBadRequest`/`AdminNotFound`/`AdminRefused` |
| `settings.py` | `AdminSettings` + `from_env`, the five `*_ENV` constants, `DEFAULT_ACTOR`, `DEFAULT_WORKFLOWS_REPO`, and the sha256-shape refusal that turns a malformed hash into a `StartupError` |
| `auth.py` | `token_matches` (sha256 + `hmac.compare_digest`), `bearer_token` (two `Authorization` headers → `None`), `host_allowed` |
| `github.py` | `ActionsGateway` — the Jobs page's only reach out of this process (`workflows`/`runs`/`dispatch`/`set_enabled`), `urllib` with an injectable opener, a 60 s read cache that mutations clear, and `ActionsError` carrying the status and never the token |
| `schema.py` | `admin_actions`: `ensure_admin_schema` (behind `capture.schema.startup_ddl_lock`), `record_action` (never raises), `recent_actions` |
| `cli.py` | `stigmergy-admin-token` — mints the one credential: 32 random bytes, plaintext printed once beside its `STIGMERGY_ADMIN_TOKEN_HASH=` line, nothing stored |
| `static/` | the SPA, no build step: `index.html` + `assets/app.js` (shell, grouped nav with the inbox badge, hash router with the old tab names as aliases, login), `theme.js` (the ONE classic script: it stamps the chosen theme on `<html>` before the first paint — a module would be deferred and flash, an inline script is refused by the CSP), `api.js` (the one fetch seam), `state.js` (the server's meta + the chart window), `copy.js` (the VOCABULARY — every system word's human label, meaning and who decides; the per-page explainers), `ui.js` (DOM helpers, pills, the confirm-with-form modal with live field checks, tooltips, the theme picker), `charts.js` (SVG charts built with `createElementNS`, each with a table twin), `views/` (one module per page: `dashboard`, `inbox`, `captures`, `entities`, `repairs`, `gardener`, `index`, `worker`, `jobs`, `digest`, `activity`, plus `common.js` for the loading wrapper, the mutation helper, the report renderer and the trace timeline), `styles.css` |

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
| GET | `/admin/api/inbox` | `inbox()` — `server.review.items_for_doorbell`, every string leaf cleaned, with per-kind counts and a conservative `truncated` flag | yes |
| GET | `/admin/api/metrics` | `metrics()`, in a worker thread — `?days=` (default 30, clamped to 1–365; non-integer is a 400): captures by arrival day and current status, capture→filed samples, `ask` outcomes per day (`pilot_report.answer_shape_by_day`, grouped in SQL), calls per day/tool/identity, each job's run history, the ledger's newest rows (`decisions.recent_decisions`, bounded in SQL), the repair table's status counts (`repair.store.counts_by_status`) | yes |
| GET | `/admin/api/queue` | `queue_list()` — repeatable `?status=`, `?submitter=`, `?limit=` (default 50; non-integer is a 400) | yes |
| POST | `/admin/api/queue/reclaim` | `queue_reclaim()` — optional int `visibility_timeout_s`; omitted means the worker's derived lease, resolved per call; the horizon is clamped at 0 | yes |
| POST | `/admin/api/queue/purge` | `queue_purge()` — optional int `older_than_days`, `dry_run` | yes |
| GET | `/admin/api/queue/{id:int}` | `queue_show()` | yes |
| GET | `/admin/api/gardener` | `gardener_state()` | yes |
| GET | `/admin/api/digest` | `digest_state()` | yes |
| POST | `/admin/api/digest/preview` | `digest_preview()` (async) | yes |
| POST | `/admin/api/digest/post` | `digest_post()` (async) | yes |
| GET | `/admin/api/index` | `index_state()` | yes |
| POST | `/admin/api/index/check` | `index_substrate_check()` | yes |
| GET | `/admin/api/entities` | `entities_list()` — `{"proposals": [...], "aliases": [...], "registry_check": {...}}`; each proposal carries the lane's `merge_candidates` plus `check`, the registry verdict on its own name | yes |
| GET | `/admin/api/entities/registry` | `entities_registry()` — the served registry, sorted by name, with `by_type` and freshness | yes |
| POST | `/admin/api/entities/resolve` | `entities_resolve()` — `names` must be a JSON list of strings (≤ 50); one verdict per non-blank name. Writes no `admin_actions` row | yes |
| POST | `/admin/api/entities/decide` | `entity_decide()` — `item_kind` (`identity-proposal` or `alias-proposal`), `item_id`, `verdict`, `into` (required for `merge`), optional `notes`; off the event loop, since a decision clones and pushes | yes |
| POST | `/admin/api/entities/create` | `entity_create()` — `name`/`entity_type`/`about` required, `entity_id`/`aliases` optional; commissions the entity by queueing a capture and answers the queued row (`id`, `status`, `entity_id`, `name`, `message`). The page is the librarian's to write, and the identity is born confirmed by the actor | yes |
| GET | `/admin/api/entities/{id}` | `entities_show()` — one proposed identity, with the same `check` the list attaches | yes |
| GET | `/admin/api/repairs` | `repairs_list()` — a bounded page of pending (`pending_truncated` says when it filled), the whole table's `counts` by status, recently decided, and the proposer's `job_runs` history | yes |
| GET | `/admin/api/repairs/{id:int}` | `repair_show()` | yes |
| POST | `/admin/api/repairs/{id:int}/approve` | `repair_approve()` — applies the proposal's ops as ONE commit through `server.review.apply_repair_and_record` | yes |
| POST | `/admin/api/repairs/{id:int}/reject` | `repair_reject()` — non-blank `reason` required | yes |
| POST | `/admin/api/pages/delete` | `pages_delete()` — a PERSON removes pages: `paths` (non-empty) + `why`, applied in the same call through `server.review.delete_and_record`, the same sequence MCP's `brain_delete` runs. The console passes NO steward guard: its token is the authorization (ADR 029/030 D2), which makes this its most consequential button | yes |
| GET | `/admin/api/activity` | `activity()` | yes |
| GET | `/admin/api/worker` | `worker_status()` | yes |
| GET | `/admin/api/crons` | `crons_state()` | yes |
| POST | `/admin/api/crons/{workflow_file}/dispatch` | `cron_dispatch()` — `inputs` must be a JSON object | yes |
| POST | `/admin/api/crons/{workflow_file}/enable` | `cron_set_enabled(enabled=True)` | yes |
| POST | `/admin/api/crons/{workflow_file}/disable` | `cron_set_enabled(enabled=False)` | yes |

`{workflow_file}` is a free path segment on the route and an allowlist check in the service
(`_require_workflow`, before `_require_gateway` and therefore before any network call): the refusal
must not depend on a converter, so an unlisted file is a 400 naming the allowed set.
`entities/registry`, `entities/resolve`, `entities/decide` and `entities/create` are declared BEFORE
`entities/{id}` in the route table, and that order is load-bearing now that an entity id is a slug
rather than an int: Starlette matches in order, so the four literal segments win before the
catch-all converter can read one as an id.

## Reuse

- `routes.compose` — the only composition point. When `configured()` is false it returns
  `_Branch(inner, None)`: no service, no routes, no DDL.
- `AdminSettings.from_env(env=None)` — the only place this package reads the environment; `env` is
  injectable. Never `os.environ` at module scope here.
- `AdminService._mutate` / `_mutate_async` — every state-changing call goes through it: actor
  fallback, an `admin_actions` row on both outcomes, `CaptureError` → `AdminRefused`.
- `server.review.items_for_doorbell` — the inbox's whole read, the SAME one the Slack doorbell
  rings from, so the two surfaces cannot disagree about what is waiting on a person. The console
  adds only its control-character strip — over EVERY string leaf, by `_clean_leaves`, never a list
  of keys that a field added upstream would miss — and the per-kind counts.
- `AdminService._served_registry` / `_check_name` — the registry check, used two ways: BEFORE a
  `create` (is this name already registered, or confusable with something registered?) and ON a
  proposal, where it is the Merge picker's strongest hint. The registry is
  `index.check.served_registry`'s answer (the index's snapshot, else the `--entity-registry`
  file) parsed by `kernel.registry.registry_from_text` — the ONE loader, so a snapshot the server
  refuses is refused here too. The verdicts are the gate's own questions in the gate's own order:
  `Registry.canonical_id` (the filing fold — `registered`), then `Registry.collision_id` (the
  birth gate's fold — `collides`), then an ADVISORY similarity listing this module computes for a
  human to judge and nothing acts on. Never a second "collides": a looser fold here would stop a
  steward registering a legitimately distinct entity, and a stricter one would promise a write the
  gate refuses. `_registry_or_none` turns an unreadable snapshot
  into `registry_check.error` rather than a blank page; `entities_resolve` (the live check as a
  steward types) refuses it outright, the substrate check's posture. `_with_registry_check` asks the
  question against the registry WITHOUT the proposal itself — a proposal always resolves to itself,
  which says nothing. The similarity listing folds the registry
  ONCE per request (`_similarity_index`): N names against M entities is M + N folds, not N × M.
- `server.review.decide_and_record` — `entity_decide`'s whole seam, and the SAME function MCP and
  Slack decide through: land the commit through the governed clone door, THEN the ledger row.
  `_decision_action` is this module's only per-verdict code, and it is a lambda factory: it maps
  `(item_kind, stored verdict)` onto one `entities.decide` call and the ledger `extra` that goes
  with it. `entities.remote` is reached by that sequence, never from this
  package — its import allowlist grants `decide`, `generator` and `errors` only.
  `server.review.commission_registration` is `entity_create`'s seam, and it is NOT that shape: it
  touches no git and writes no ledger row, it queues a capture carrying the registration and the
  LIBRARIAN writes the page, births the identity confirmed by the actor and records the approval
  after its own push ([ADR 042](../../../docs/decisions/042-an-entity-is-born-written.md)). What
  stays here is the pre-flight `entity_create` owns: the required `about`, the slug-of-the-name
  check on `entity_id`, and the refusal of a name the SERVED registry already resolves (the entity
  exists — capture about it instead). It needs the evidence store the queue archives into, passed
  as `AdminService(..., evidence=)` from `compose`. The exception
  mapping stays HERE by decision: nothing is caught inside `_do`, so `_mutate` records the
  library's OWN class name in `admin_actions` before the `except (EntityError, CaptureError)`
  outside it raises `AdminRefused` with the library's sentence.
  `server.review.apply_repair_and_record`/`reject_repair_and_record` — `repair_approve`/
  `repair_reject`'s whole seam, and the SAME pair the MCP review lane decides a `repair-proposal`
  with (ADR 039): record the verdict as a CONDITIONAL update, apply through the governed door,
  write the `review_decisions` row after the push. `repair.remote` is reached by that sequence,
  never from this package — its import allowlist grants `repair.store`, `repair.schema` and
  `repair.errors` only, the same shape the entities edge has. The exception mapping stays HERE for
  the same reason it does for a decision: nothing is caught inside `_do`, so `_mutate` records
  `RepairError` in `admin_actions` before the `except` outside it raises `AdminRefused`. Every
  write names this door with `server.review.SOURCE_ADMIN` — required on every ledger write, so a
  console row is told apart from an MCP or Slack one on the row itself rather than by inference.
- `server.review.VERDICTS_BY_KIND` — the ONE translation from a button's word to the stored verdict.
  `entity_decide` reads its keys to validate the request and its VALUES to act, so the console can
  never accept a verdict a kind does not take, nor store one under a spelling the ledger's other
  readers do not know.
- `queue.outcomes_by_day` — the metrics' capture series, beside `counts_by_status` in the queue
  module because it is the same fact with a time axis; the console never carries its own query
  over `capture_queue`. `pilot_report.answer_shape_by_day` is the `ask` series: the report's own
  classifier (`shape_of`) as SQL, pinned against the Python original by test, so a chart and the
  report cannot disagree about what an answer was. `decisions.recent_decisions` and
  `repair.store.counts_by_status` are the ledger feed and the proposal histogram — each bounded or
  aggregated in the database, because both tables only grow.
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
  a required argument, enforced: an empty one throws, so a new workflow without a sentence fails
  in development rather than shipping a blank line over Dispatch. A field's `live(value, setNote,
  allValues)` hook renders a node under the field as the user types — the Register form's registry
  check is one; its debounce is cancelled when the dialog closes. The dialog traps Tab and hands
  focus back to the control that opened it.
- `views/common.js` `mutate(path, body, message, onSuccess?)` (frontend) — every state-changing
  button goes through it: one toast per outcome, the server's `warning` folded into a warning
  toast rather than a second, contradictory one, the result handed on for the flows that need a
  sha or a count. `runShape`/`runTable` — one shape for every run strip and its table twin.
- `copy.js` (frontend) — the vocabulary: `word()`, `status()`, `decisionVerb()`, `itemKind()`,
  `repairKind()`, `verdict()`, `check()`, `severity()`, `jobName()`, `door()`, `page()`. Every
  lookup falls back to the raw word, so a new status renders ugly and never invisible. The closed
  LISTS come from `/admin/api/meta`; this file only knows how to say them.
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
  `server.review`, the same reason `stigmergy.repair.remote` is absent.
- Reading `pages_index` for anything but an aggregate. `_zone_counts` is the single read and a
  named entry in `ACL_REACHABILITY_EXCEPTIONS`; anything more names a `visible()` predicate.
- Writing SQL for something a library already exposes. The only SQL owned here is read-side
  plumbing nothing else surfaces: `job_runs`/`ingest_errors`, the `audit_log` aggregates (per
  identity/tool, per day, the `ask` outcome rows the console shapes in Python, the rate-limit
  trips), the digest watermark, the zone counts, and `admin_actions`.
- Raising a message that could carry captured content across the HTTP boundary. The catch-all in
  `_json_endpoint` returns `the operation failed (<ClassName>)`; only the three domain errors and
  `ActionsError` cross with their sentence.
- Deciding anything about a proposal in `views/entities.js` or `views/inbox.js`. A Merge picker
  offers `merge_candidates` first and the rest of the registry after, and hands the chosen id to
  `entities/decide` as `into`; whether that entity exists, is confirmed, or would collide is
  `server.review` and `entities.decide`'s to refuse, on every door alike. The frontend renders a
  decision and never derives one.
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
  and the async digest methods await inside `digest.run`, between statements.
- Adding a CLI flag. The server's command line is pinned byte-identical between `fly.toml` and the
  Dockerfile `CMD`; configuration is env-only so that pin never moves.

## Data & contracts

`admin_actions` (`schema.py`): `id BIGSERIAL` · `ts TIMESTAMPTZ DEFAULT now()` · `actor` · `action`
· `args JSONB` · `outcome` (`ok`/`error`) · `error_class`, plus `admin_actions_ts_idx (ts DESC)`.
`actor` is **attribution, not authorization** — recorded, never checked.

Compose-time DDL, only when configured: `ensure_admin_schema` plus
`gardener.schema.ensure_gardener_schema`, `repair.schema.ensure_repair_schema` and
`server.review.ensure_review_schema`, which the read paths would otherwise meet as a bare
`UndefinedTable` on a fresh database.

| Env var | Default | Effect |
|---|---|---|
| `STIGMERGY_ADMIN_TOKEN_HASH` | `""` | the master switch: unset → the console does not exist. Must be 64 sha256 hex (uppercase normalized); any other non-empty value raises `StartupError` at startup |
| `STIGMERGY_ADMIN_ACTOR` | `admin-console` | the `actor` fallback and the form prefill |
| `STIGMERGY_ADMIN_GITHUB_TOKEN` | `""` | unset → `gateway is None` → the Jobs page is database-truth-only |
| `STIGMERGY_ADMIN_GITHUB_REPO` | `""` | which repo's workflows (`<owner>/<name>`). Deliberately not `$STIGMERGY_GITHUB_REPO`, which names the knowledge repo |
| `STIGMERGY_ADMIN_CHANNELS_PATH` | `""` | the digest's audience-scoping map |

Read but not owned: `STIGMERGY_PUBLIC_HOST` (`routes._public_hosts_from_env`, a two-line copy of
the transport's parser — importing it would close a cycle through the composition point), and
`SLACK_BOT_TOKEN`/`STIGMERGY_DIGEST_CHANNEL_ID` through `digest.settings`.

**Wire contract**: JSON everywhere except the shell (`text/html`) and the assets. Errors are
`{"error": "<sentence>"}` at 400 (`AdminBadRequest`), 401 (generic, never a reason), 404
(`AdminNotFound`, and the inert console's blanket answer), 409 (`AdminRefused`), 421 (foreign
`Host`), 500 (class name only), 502 (`ActionsError`). Every response carries
`content-security-policy` (`default-src 'none'`; fetch directives `'self'`, `img-src` also `data:`;
`base-uri`/`form-action`/`frame-ancestors` `'none'`), `x-content-type-options: nosniff`,
`referrer-policy: no-referrer` and `strict-transport-security: max-age=31536000;
includeSubDomains`; `/admin/api/*` additionally `cache-control: no-store`, the shell and the
assets `cache-control: no-cache` (an ETag round trip on every load, so a deploy that renames a
module never leaves a browser running the old `app.js` against new imports).

**The registry check's wire shape** (`entities_resolve`'s `checks[]`, and `check` on each proposal):
`{"name", "verdict": registered|collides|similar|clear|unchecked, "match": {id, name, type,
aliases} | null, "similar": [{id, name, type, aliases, why}]}`, beside `registry` /
`registry_check`: `{"available", "road": snapshot|file|none, "source", "refreshed_at"}` (plus
`error` on the entity routes when the snapshot could not be read).

**The vocabularies the frontend renders all ship from `meta()`**, never a second copy in JS:
`entity_types`, `statuses`, `terminal_statuses`, `legacy_statuses` (today just `resolved`),
`repair_kinds`, `gardener_severities`, `item_kinds`, `verdicts_by_kind` (per kind, the stored words
`server.review.VERDICTS_BY_KIND` accepts) and `decision_sources`. `copy.js` knows only how to SAY
them, and every lookup falls back to the raw word — a new status renders ugly and never invisible.

`CRON_WORKFLOWS` — `index-rebuild.yml`, `retention-purge.yml`, `gardener.yml`,
`repair-propose.yml`, each naming its `schedule_utc` and where the database truth lives
(`job_runs:<job>`, or `index_meta.built_at` for the rebuild, which writes none).
`retention-purge.yml` declares the only dispatch input (`dry_run`); an undeclared key is refused by
name before the gateway is touched. `test_the_console_schedule_table_matches_the_workflow_files`
parses the real YAML.

**Frontend theme**: every colour token is declared ONCE as `light-dark(light, dark)` on `:root`,
so a token added to one theme and forgotten in the other cannot exist; the three states are two
one-line rules (`:root[data-theme="light"|"dark"] { color-scheme: … }`) plus the absence of the
attribute for Auto, and `@supports not (color: light-dark(…))` keeps an old browser on the light
palette rather than on none. `theme.js` and `ui.js` each spell the storage key and the two state
names — a classic script cannot be imported by a module — and `test_static_discipline.py` pins the
two spellings against each other.

**Frontend**: each view module exports `render(host, params?) → cleanup?`, dispatched from
`app.js`'s `GROUPS` (the sidebar, grouped by the job a person came to do) and `DETAIL_ROUTES`;
the old tab names (`overview`, `queue`, `crons`) are aliases, so a bookmark still lands, and a
page may carry a sub-path of its own (`#/inbox/entity` is the inbox, filtered). The token lives in
`sessionStorage` under `stigmergy-ops-token` — no cookie, therefore no CSRF surface — and any 401
clears it, stashes the reason for one reload, and lands on the login screen with that reason
shown. The chart window (7/30/90 days) lives in `sessionStorage` too, and the per-page explainer's
collapsed state in `localStorage`. Only the dashboard polls (30 s, skipped while
`document.hidden`), and it is the only view returning a cleanup function; `navigate()` carries a
token so a view that resolves after the next navigation started has its cleanup run at once. The
inbox badge refreshes every 60 s on the same visibility rule, and unconditionally on load and on
every navigation (`state.notify`).

## Behaviour worth knowing before editing

- **A branch, not a middleware exemption.** `_Branch` is the outermost ASGI object: `/admin*` HTTP
  requests go right, everything else — lifespan (the MCP session manager lives on it), websockets,
  every other path — flows to the inner app untouched, and `__getattr__` delegates so the branch
  stays transparent to Starlette introspection. Path matching is exact, so `/administration` is
  inner traffic.
- **Inert means inert.** With the hash unset there is no service, no route table, no gateway and no
  DDL; from the MCP surface the module is undetectable.
- **The gate's order is load-bearing**: `Host` first (421), token second (401), headers on the way
  out of both — so the host check also covers the tokenless shell. With no `$STIGMERGY_PUBLIC_HOST`
  every host passes, which keeps local dev unchanged.
- **Not every POST is a mutation.** `queue/purge --dry-run`, `digest/preview`, `index/check` and
  `entities/resolve` write no `admin_actions` row. `digest/preview` still records a
  `digest-dry-run` row in `job_runs`, which is why the Digest page's history fills with them.
- **Every read of a table that only grows has a ceiling**, applied in SQL: the inbox (`limit`,
  with a conservative `truncated`), the ledger feed (`DECISIONS_LIMIT`), the pending proposals
  (`REPAIR_PENDING_LIMIT`, with `pending_truncated`), the metrics window (`MAX_METRICS_DAYS`). The
  one unbounded per-item read, `latest_decisions`, is the doorbell's and is not reached from here.
- **The console reads page PATHS, never page BODIES.** `index/check` and `gardener` carry paths out
  of the corpus, both behind the operator token and both declared ACL exceptions.
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
- **The Gardener page's `urgent` (`sla`) chip is permanently empty** and that is not a console
  defect: `sla` is a real member of `gardener.schema.SEVERITIES` that nothing in that package
  produces. The chip becomes live the day a check emits one.
- **`entity_decide` decides server-side**, through `server.review.decide_and_record` — never through
  `review_decide`/`BrainService` (both banned imports), whose steward check is for a RESOLVED
  identity while `actor` here is free text behind the operator token (ADR 029/030 D2). The console's
  authorization IS that token.
- **The registry check is a warning, never a permission.** It reads the snapshot this server
  serves; the birth gate re-checks against the registry the commit will publish — inside the clone
  for a decision, inside the capture's own worktree when the librarian files a registration.
  When the snapshot is fresh the two agree; when it is stale the gate wins, and the console shows
  the gate's own sentence as a 409 (for a registration, as the capture's refusal).
- **A decision's own reads are cheap and its write is not.** `entities/decide` runs in a worker
  thread (`run_in_threadpool`) because it clones the knowledge repo and pushes, and the MCP tools
  share this process; `entities/create` rides the same thread for the archive write its capture
  pays for, and `metrics` does the same for its dozen aggregate queries. The service holds no cursor across the call boundary, so the autocommit
  connection is safe to use from the thread.
- **`repair_approve` applies server-side on the same terms.** `review_decide`'s per-target-path
  steward guard is likewise not reached: this console's authorization IS the operator token, and
  `actor` is attribution. What it does NOT skip is anything the apply itself proves — the clone's
  own re-validation, the nine gates, and the cross-check that the produced diff is exactly the
  proposal's stored `target_paths`. A failed apply comes back as a 409 with the gate's own
  sentence, and the row stays `failed` with its reason rather than returning to pending.

## Common tasks

| Task | Touch |
|---|---|
| Add a read endpoint | a method on `AdminService`, a `@_json_endpoint` handler, a `Route`. A new table means checking the import allowlist in `tests/test_architecture.py` first |
| Add a mutation | the same, but through `_mutate`/`_mutate_async` for the `admin_actions` row and the actor fallback — and the frontend flow through `confirmForm` with an honest consequence sentence |
| Add a console-drivable workflow | a row in `CRON_WORKFLOWS` (file, title, `schedule_utc`, `truth`, `dispatch_inputs`); `DISPATCHABLE` derives from it and the schedule test fails until the YAML agrees; its purpose, truth and Run-now `consequence` sentences in `copy.js`'s `JOB` (a workflow with no sentence gets a generic one; the pages that mirror a Run-now button look the row up in `meta().workflows` and render nothing when it is absent) |
| Reach a new package | add the SUBMODULE to `_ADMIN_ALLOWED_IMPORT_PREFIXES` with a stated reason, in the same diff |
| Add a config knob | a field on `AdminSettings` + a `*_ENV` constant + a line in `from_env`. Never a CLI flag |
| Change how untrusted text is cleaned | `service._clean` — never at a call site, and never by flattening newlines |
| Add a page | a module under `static/assets/views/` exporting `render(host)`, a route in `app.js`'s `GROUPS` (plus a `DETAIL_ROUTES` row if it has a detail), its title/purpose/explainer in `copy.js`'s `PAGE`, DOM built only through `ui.el`/`ui.svg` |
| Add a chart | a series whose colour is a KEY role or a categorical slot, inside `chartCard` with a `tableSpec` (the table twin is not optional); never a value on every point, never a second y-axis |
| Give a new system word a human label | `copy.js` — the list itself ships from `meta()` |
| Rotate or revoke the credential | `stigmergy-admin-token`, then set the new hash. There is no store and no list |

## Tests

`tests/admin/` runs against real Postgres through `tests.testdb` and real git for the identity
decisions (`conftest.build_bare_knowledge_repo`); only the two network edges — GitHub Actions and
Slack — are
faked. `test_settings_and_auth.py` and `test_cli.py` are keyless; `test_github_gateway.py` drives
an injected opener; `test_service_pg.py` is the largest suite (queue reads, reclaim on
both edges of the worker's lease, purge dry-run vs real, cron paths, and the five decisions plus a
`create` against a throwaway bare remote); `test_console_reads_pg.py` covers the inbox, the served
registry, the registry check (every verdict beside its benign twin, the gate's fold against a
looser one) and the metrics window, through the service and over the wire; `test_routes_pg.py` exercises the real
`compose` product over `httpx.ASGITransport` (inert 404s, the tokenless shell vs the 401 API, the
security and cache headers, the status mapping); `test_static_discipline.py` greps the shipped
frontend files. Auth refusals are tested beside their benign twins throughout.

`tests/server/test_admin_branch.py` proves the branch on the real `build_http_app` wiring: the MCP
token store does not open the console, the admin token opens nothing on `/mcp`, and
`rate_limiter_of(app)` still works through the branch's delegation.

`tests/test_architecture.py` holds the boundary: `test_admin_sources_found` (the anti-blindness
floor), `test_admin_imports_only_its_declared_set`,
`test_admin_reaches_the_librarian_through_config_alone`,
`test_admin_actually_uses_its_declared_librarian_exception` (the pruning half),
`test_admin_loads_the_slack_sdk_door_lazily`,
`test_admin_never_imports_the_read_path_or_the_mcp_adapter`,
`test_only_the_http_transport_composes_the_admin_branch`, and the ACL-reachability parametrization
naming `admin/service.py`.
