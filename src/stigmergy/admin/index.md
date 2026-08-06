# admin — the operations console: a web SKIN over seams other packages own

Narrative doc: [`docs/reference/admin-console.md`](../../../docs/reference/admin-console.md) (the
how and why for an operator). Design record:
[ADR 029](../../../docs/decisions/029-admin-console.md).
Import surface pinned by `tests/test_architecture.py`'s admin section.

This file is the code map — for whoever is about to edit this package, not run it.

## Purpose

One web surface over what already runs: the steward drain, the three scheduled workflows, the
gardener's findings, the digest, the index, pending entity situations (read), activity, and the
worker's lease. It is mounted as an ASGI **branch** in front of the MCP transport, inside the same
`app` process group, behind its own single credential.

**It is a skin, not a subsystem.** Every operational act lands on a seam another package already
owns and tests — [`capture.dispositions`](../capture/index.md) (the drain),
`capture.retention` (purge), `capture.queue.release_expired` (reclaim),
[`gardener.store`](../gardener/index.md) (findings), [`digest.run`](../digest/index.md) (the
digest), [`index.check`](../index/index.md) (the substrate lint),
[`entities.situations`](../entities/index.md) (the pending-identity read),
[`server.pilot_report`](../server/index.md) (the measurement table). The ONLY state it owns is
`admin_actions`, its own bookkeeping table.

**What it must never become is a read surface over the corpus.** (The Activity tab does show the
`ask` questions themselves — user content, not corpus content; `docs/reference/admin-console.md`
states the distinction.) No search, no `ask`, no
page bodies — `test_admin_never_imports_the_read_path_or_the_mcp_adapter` bans
`stigmergy.index.search`, `stigmergy.answer`, `stigmergy.server.mcp_server`, `stigmergy.server.service`
and `stigmergy.server.transport_http` from every module here, so the rule is a property of the
import graph rather than of anyone's memory.

## Key entry points

| Module | Owns |
|---|---|
| `routes.py` | `compose(inner, *, conn, server_settings, admin_settings=None, gateway=None)` — the ONE function `server.transport_http.build_http_app` calls, and the only door into this package. Also `_Branch` (the outermost ASGI router), `_AdminGate` (host → token → security headers), `_json_endpoint` (the domain-exception → status-code map) and the 26-entry route table |
| `service.py` | `AdminService` — one method per route. Also `CRON_WORKFLOWS` / `DISPATCHABLE` (the console-drivable workflow allowlist), `worker_visibility_timeout_s()` / `WORKER_MAX_ATTEMPTS` (the worker's OWN numbers — the lease RESOLVED per call, never a frozen constant), and the three domain errors `AdminBadRequest` / `AdminNotFound` / `AdminRefused` |
| `settings.py` | `AdminSettings` (frozen dataclass) + `from_env`, the five `*_ENV` name constants, `DEFAULT_ACTOR`, `DEFAULT_WORKFLOWS_REPO`, and the sha256-shape refusal that turns a malformed hash into a `StartupError` |
| `auth.py` | Three pure functions: `token_matches` (sha256 + `hmac.compare_digest`), `bearer_token` (extraction; two `Authorization` headers → `None`), `host_allowed` (the MCP transport's allowlist, mirrored) |
| `github.py` | `ActionsGateway` — the crons tab's only reach out of this process: `workflows` · `runs` · `dispatch` · `set_enabled`, `urllib` with an injectable `opener`, a `DEFAULT_CACHE_TTL_S = 60` read cache that mutations clear, and `ActionsError` carrying the status and never the token |
| `schema.py` | `admin_actions`: `ensure_admin_schema` (DDL behind `capture.schema.startup_ddl_lock`), `record_action` (never raises), `recent_actions` |
| `cli.py` | `stigmergy-admin-token` (`[project.scripts]` → `stigmergy.admin.cli:main`) — mints the one credential: 32 random bytes url-safe, plaintext printed ONCE, the `STIGMERGY_ADMIN_TOKEN_HASH=` line printed beside it, nothing stored |
| `static/` | The SPA, no build step: `index.html` + four ES modules (`app.js` shell/hash-router/login, `api.js` the one fetch seam, `ui.js` DOM helpers, `views.js` the eleven view renderers) + `styles.css` |

**Who depends on this package**: exactly one module, `server/transport_http.py`, and
`test_only_the_http_transport_composes_the_admin_branch` keeps it that way — a second importer
would be a second door onto the service layer.

### The HTTP surface, as `_build_admin_app` declares it

`ADMIN_PREFIX = "/admin"`, `API_PREFIX = "/admin/api/"`. The token is checked **only** on paths
starting with `API_PREFIX`; the shell and its assets are deliberately tokenless (they are inert
files, and a login screen that needs a token to render cannot ask for one).

| Method | Path | Service call | Token |
|---|---|---|---|
| GET | `/admin` | 307 → `/admin/` | no |
| GET | `/admin/` | `static/index.html` | no |
| GET | `/admin/assets/…` | `StaticFiles(static/assets)` | no |
| GET | `/admin/api/meta` | `meta()` | yes |
| GET | `/admin/api/overview` | `overview()` | yes |
| GET | `/admin/api/queue` | `queue_list()` — repeatable `?status=`, `?submitter=`, `?limit=` (default 50; a non-integer is a 400) | yes |
| POST | `/admin/api/queue/reclaim` | `queue_reclaim()` — optional int `visibility_timeout_s`; omitted means the worker's derived lease, resolved per call, never the queue's own 300 s — and the horizon is clamped at 0 either way | yes |
| POST | `/admin/api/queue/purge` | `queue_purge()` — optional int `older_than_days`, `dry_run` | yes |
| GET | `/admin/api/queue/{id:int}` | `queue_show()` | yes |
| POST | `/admin/api/queue/{id:int}/requeue` | `queue_requeue()` | yes |
| POST | `/admin/api/queue/{id:int}/resolve` | `queue_resolve()` — non-blank `note` REQUIRED | yes |
| POST | `/admin/api/queue/{id:int}/reject` | `queue_reject()` — non-blank `reason` REQUIRED | yes |
| GET | `/admin/api/gardener` | `gardener_state()` | yes |
| GET | `/admin/api/digest` | `digest_state()` | yes |
| POST | `/admin/api/digest/preview` | `digest_preview()` (async) | yes |
| POST | `/admin/api/digest/post` | `digest_post()` (async) | yes |
| GET | `/admin/api/index` | `index_state()` | yes |
| POST | `/admin/api/index/check` | `index_substrate_check()` | yes |
| GET | `/admin/api/entities` | `entities_list()`, wrapped as `{"situations": [...]}` | yes |
| GET | `/admin/api/entities/{id:int}` | `entities_show()` | yes |
| POST | `/admin/api/entities/{id:int}/approve` | `entity_approve()` — `name`/`entity_type` required, `entity_id`/`aliases`/`role` optional, `requeue` boolean (default `true`) — mints (ADR 030) | yes |
| GET | `/admin/api/activity` | `activity()` | yes |
| GET | `/admin/api/worker` | `worker_status()` | yes |
| GET | `/admin/api/crons` | `crons_state()` | yes |
| POST | `/admin/api/crons/{workflow_file}/dispatch` | `cron_dispatch()` — `inputs` must be a JSON object | yes |
| POST | `/admin/api/crons/{workflow_file}/enable` | `cron_set_enabled(enabled=True)` | yes |
| POST | `/admin/api/crons/{workflow_file}/disable` | `cron_set_enabled(enabled=False)` | yes |

`{workflow_file}` is a free path segment on the ROUTE and an allowlist check in the SERVICE
(`_require_workflow`, before `_require_gateway` and therefore before any network call) — the
refusal must not depend on a converter, so an unlisted file is a 400 with the allowed set named.

## Use these

- **`routes.compose`** — the only composition point. It resolves settings, and when
  `configured()` is false returns `_Branch(inner, None)`: no service, no routes, no DDL. A new
  caller is an architecture change, not a convenience.
- **`AdminSettings.from_env(env=None)`** — the ONE place this package reads the environment.
  `env` is injectable for tests (`webhook.webhook_settings_from_env`'s pattern). Never call
  `os.environ` at module scope here.
- **`AdminService._mutate` / `_mutate_async`** — the wrapper every state-changing call goes
  through: actor fallback, `admin_actions` row on both outcomes, `CaptureError` → `AdminRefused`
  translation. A new mutation that writes its own bookkeeping is a drift; add it here.
- **`entities.remote.mint_via_clone` / `server.review.record_decision`** — `entity_approve`'s
  whole seam (ADR 030): the first mints (a throwaway clone, ONE commit, the App's identity plus an
  `Approved-by:` trailer); the second writes the append-only `review_decisions` row every mint
  door — MCP, Slack, this one — shares, so the ledger never grows a third, hand-written `INSERT`
  that could drift from the other two's columns. `entities.errors.EntityError` (and its
  `CapabilityUnavailableError` subclass) is what a failure of either raises; `entity_approve` maps
  it to `AdminRefused` with the library's own sentence, the same posture `CaptureError` already
  gets from `_mutate`.
- **`auth.token_matches` / `bearer_token` / `host_allowed`** — pure, unit-tested on both edges.
  Reuse; never re-derive a header parse at a call site.
- **`service._clean` → `stigmergy.text.sanitize`** — the ONE cleaning seam for untrusted strings on
  the way out (control characters die; newlines and a literal `<script>` SURVIVE, because HTML
  inertness is the client's half). Every excerpt, error, note, finding and rationale crosses it.
- **`admin_schema.record_action`** — bookkeeping that swallows and logs, `capture.ops`' contract
  inherited on purpose: it must never fail the work it records.
- **`ui.el` / `ui.render` / `ui.clear` (frontend)** — the only sanctioned way to build a node.
  `render` exists because `Element.replaceChildren` stringifies a `null` argument into the literal
  text `"null"`, which is what makes `cond ? node : null` safe in every view.
- **`ui.confirmForm` (frontend)** — every mutation goes through one, and its `consequence`
  sentence is a required argument: a button that spends money or posts to Slack says so first.

## Avoid / anti-patterns

- **Never add an exemption to `_BearerAuthMiddleware` for this console.** The whole branch shape
  exists so the webhook's "ONE exemption, exact path match" doctrine stays literally true —
  `/admin*` never reaches that middleware at all. A second exemption there is the defect this
  design was chosen to prevent.
- **Never let this package import outside `_ADMIN_ALLOWED_IMPORT_PREFIXES`.** The librarian is
  reachable through `librarian.config` ALONE (the worker's lease numbers, never its machinery);
  `stigmergy.slack.bolt_gateway` may only be imported INSIDE the posting handler, so a keyless
  console never loads the Slack SDK. Four separate architecture tests hold these, including a
  pruning one that fails if the declared librarian exception stops being used.
- **Never read `pages_index` for anything but an aggregate.** `_zone_counts` is the single read,
  it is `SELECT zone, count(*) … GROUP BY zone`, and it is a NAMED entry in
  `ACL_REACHABILITY_EXCEPTIONS` (`"admin/service.py": "operator console; aggregate zone counts
  only, no content columns"`). The moment a route needs more than counts it names a
  `visible()` predicate like every other reader, in a diff a reviewer sees.
- **Never write SQL here for something a library already exposes.** The only SQL this package
  owns is read-side plumbing nothing else surfaces: `job_runs` / `ingest_errors` reads, the
  `audit_log` aggregates, the digest watermark, the zone counts, and `admin_actions`.
- **Never raise a message that could carry captured content across the HTTP boundary.** The
  catch-all in `_json_endpoint` returns `the operation failed (<ClassName>)` and nothing else.
  Only the three domain errors and `ActionsError` cross with their sentence, and each of those
  sentences is written by the library that owns the refusal.
- **Never build DOM from an HTML string in `static/`.** `innerHTML`, `outerHTML`,
  `insertAdjacentHTML`, `document.write`, `eval(`, `new Function` are all grepped out of the
  SHIPPED files by `tests/admin/test_static_discipline.py`, together with any `src`/`href` to an
  `http(s)` origin. The server strips control characters; only `textContent` makes markup inert.
- **Never hold a cursor across an `await`.** Same invariant as `AuditWriter`: this service shares
  the process-wide autocommit connection, and the two async digest methods do their awaiting
  inside `digest.run`, between statements. A future connection pool must preserve this explicitly.
- **Never add a CLI flag for the console.** The server's command line is pinned byte-identical
  between `fly.toml` `[processes]` and the Dockerfile `CMD`
  (`tests/test_deployment_config.py`); configuration is env-only so that pin never moves.

## Data & contracts

**Owned state — `admin_actions`** (`schema.py`, the console's whole footprint):
`id BIGSERIAL` · `ts TIMESTAMPTZ NOT NULL DEFAULT now()` · `actor TEXT` · `action TEXT` ·
`args JSONB` · `outcome TEXT` (`ok` / `error`) · `error_class TEXT DEFAULT ''`, plus
`admin_actions_ts_idx (ts DESC)`. `actor` is **attribution, not authorization** — recorded,
never checked, exactly `--by`'s contract on the steward CLIs.

**Compose-time DDL** (only when configured): `ensure_admin_schema`, plus
`gardener.schema.ensure_gardener_schema` and `server.review.ensure_review_schema` — the two the
console's read paths depend on and would otherwise meet as a bare `UndefinedTable` on a fresh
database. `ensure_capture_schema` / `ensure_audit_table` already ran in `build_http_app`.

**Configuration** — env only, resolved once, `AdminSettings.from_env`:

| Env var | Default | Effect |
|---|---|---|
| `STIGMERGY_ADMIN_TOKEN_HASH` | `""` | **the master switch.** Unset → the console does not exist. Must be 64 lowercase sha256 hex (uppercase is normalized, not refused); any other non-empty value raises `StartupError` at startup |
| `STIGMERGY_ADMIN_ACTOR` | `admin-console` | the `actor` fallback and the form prefill |
| `STIGMERGY_ADMIN_GITHUB_TOKEN` | `""` | unset → `gateway is None` → the crons tab is database-truth-only |
| `STIGMERGY_ADMIN_GITHUB_REPO` | `""` | which repo's workflows (`<owner>/<name>`). Deliberately NOT `$STIGMERGY_GITHUB_REPO`, which names the KNOWLEDGE repo for the index webhook — different fact, different variable |
| `STIGMERGY_ADMIN_CHANNELS_PATH` | `""` | the digest's audience-scoping map; staging sets it in `fly.toml` to the baked `/app/slack-channels.json` |

Read but not owned: `STIGMERGY_PUBLIC_HOST` (`routes._public_hosts_from_env`, a deliberate two-line
copy of the transport's parser — importing it would close an import cycle through the composition
point), and `SLACK_BOT_TOKEN` / `STIGMERGY_DIGEST_CHANNEL_ID` through `digest.settings`, whose
constants are imported rather than re-typed.

**The wire contract**: every response is JSON except the shell (`text/html`) and the assets.
Errors are `{"error": "<sentence>"}` at 400 (`AdminBadRequest`), 401 (unauthorized — generic,
never a reason), 404 (`AdminNotFound`, and the inert console's blanket answer), 409
(`AdminRefused`), 421 (misdirected — foreign `Host`), 500 (class name only), 502 (`ActionsError`).
Every response carries `content-security-policy` (`default-src 'none'`; the fetch directives
`'self'`, with `img-src` also allowing `data:`; `base-uri`, `form-action` and `frame-ancestors`
`'none'`), `x-content-type-options: nosniff` and `referrer-policy: no-referrer`;
`/admin/api/*` additionally carries `cache-control: no-store`.

**`CRON_WORKFLOWS`** — three rows (`index-rebuild.yml`, `retention-purge.yml`, `gardener.yml`),
each naming its `schedule_utc` and, crucially, **where the database truth lives**: `job_runs:<job>`
for the two that write one, `index_meta.built_at` for the rebuild, which writes none.
`retention-purge.yml` is the only one declaring a dispatch input (`dry_run`), and it is the only
input any dispatch will accept — an undeclared key is refused BY NAME before the gateway is
touched. `test_the_console_schedule_table_matches_the_workflow_files` parses the real YAML, so
this table cannot drift from the files it describes.

**The frontend contract**: eleven renderers in `views.js` — the nine nav views `app.js`'s `ROUTES`
lists, plus the two detail views its `DETAIL_ROUTES` patterns dispatch to — each
`render(host, params?) → cleanup?`. The
token lives in `sessionStorage` under `stigmergy-ops-token` — no cookie, therefore no CSRF surface
— and any 401 clears it and reloads into the login screen. Only `overviewView` polls (15 s,
skipped while `document.hidden`), and it is the only view that returns a cleanup function.

## Tests

`tests/admin/` — six modules plus `conftest.py`, against real Postgres through `tests.testdb`
(never a faked queue) and, since ADR 030, real git for `entity_approve`
(`conftest.build_bare_knowledge_repo`, this package's own minimal fixture — never a faked commit).
Only the two network edges — GitHub Actions and Slack — are faked.

| Module | What it actually covers |
|---|---|
| `test_settings_and_auth.py` | Keyless. `from_env` across unset / full / malformed / uppercase-hex; then every auth refusal **beside its benign twin** — right token vs wrong/empty/None, empty configured hash, bearer case-insensitivity, `Basic`, empty bearer, and the doubled `Authorization` header; host allowlist with no public host, localhost spellings on any port, the configured host bare and on `:443`, a foreign host, a suffix-attack host (`…fly.dev.evil.example`), zero headers and two |
| `test_cli.py` | Keyless. The printed hash IS the sha256 of the printed token; two runs mint two different tokens |
| `test_github_gateway.py` | Keyless, injected opener. Exact URLs for all four calls, the `Authorization` header, `conclusion: null` normalized to `""`, read caching, mutation-invalidates-cache, the TTL on an injected clock, HTTPError → `ActionsError(status)` with the token absent from the message, OSError → status 0 |
| `test_service_pg.py` | Postgres. The largest suite: queue reads (withheld pending material, the `needs_input` reply invocation, the whole trace, ANSI stripped while `<script>` survives as text), the drain (requeue leaves `attempts` alone, the resolve pointer warning and its absence, a refusal on an unparked row recording `outcome='error'`, `dispositions.clean` inherited, reclaim on both edges of the worker's lease — a flagless Reclaim leaves a 400 s-old claim alone and still releases a 1000 s-old one — purge dry-run vs real **and that a dry run writes no `admin_actions` row**), a bookkeeping failure that does not fail the work, the blank-actor fallback, worker lease verdicts, an empty-world overview, gardener `partial` honesty, digest preview + the refusal without its pieces, index state and the substrate check over the real store, activity aggregates, every cron path including the allowlist refusal firing before any gateway call, and — real git, real gitleaks, no double — `entity_approve` minting for real against a throwaway bare remote (both ledgers recorded, the entity-id slug default, `requeue=False` leaving the capture parked, a missing `librarian_repo_url` naming the capability while `admin_actions` keeps the ORIGINAL exception class, a no-longer-parked row's own refusal) plus the transition pin that `entities_show` no longer carries a `commands` key |
| `test_routes_pg.py` | Postgres + `httpx.ASGITransport` over the REAL `compose` product. Inert = 404 on all four path shapes **and no `admin_actions` DDL at all**; the tokenless shell vs the 401 API; wrong token and smuggled headers; the security headers on a 200; the `/admin` → 307; foreign `Host` → 421 with the configured host and localhost as twins; the queue flow end to end; the 400/404/409/500/502 mapping; a non-JSON body as a 400 rather than a traceback; `entities/{id}/approve` minting for real over HTTP, token-gated like every other route, its 400/409 error mapping |
| `test_static_discipline.py` | Greps the SHIPPED files: the three required files exist inside the package, no HTML-string sink in any `.js`/`.html`, the sanctioned mechanism (`textContent`/`createTextNode`) IS used — the benign twin — and no `http(s)` `src`/`href` anywhere |

`tests/server/test_admin_branch.py` proves the same branch on the REAL production wiring
(`build_http_app`): without the env, `/admin/` and `/admin/api/meta` are 404 while MCP stays
fail-closed; with it, the shell serves, **the MCP token store does not open the console**, **the
admin token opens nothing on `/mcp`**, and `rate_limiter_of(app)` still works — the branch
delegates attribute access precisely so `tests/server/conftest`'s introspection seams survive
being wrapped.

`tests/test_architecture.py` holds the boundary: `test_admin_sources_found` (≥ 6 modules, the
anti-blindness floor), `test_admin_imports_only_its_declared_set`,
`test_admin_reaches_the_librarian_through_config_alone`,
`test_admin_actually_uses_its_declared_librarian_exception` (the pruning half),
`test_admin_loads_the_slack_sdk_door_lazily`,
`test_admin_never_imports_the_read_path_or_the_mcp_adapter`,
`test_only_the_http_transport_composes_the_admin_branch`, and the ACL-reachability parametrization
that accepts `admin/service.py` as a named exception.

## Common tasks

| Task | Touch |
|---|---|
| Add a read endpoint | a method on `AdminService`, a `@_json_endpoint` handler, a `Route`. If it reads a new table, check the import allowlist in `tests/test_architecture.py` first |
| Add a mutation | the same, but the service method MUST go through `_mutate` / `_mutate_async` so it gets its `admin_actions` row and the actor fallback — and the frontend flow MUST go through `confirmForm` with an honest consequence sentence |
| Add a console-drivable workflow | a row in `CRON_WORKFLOWS` (file, title, `schedule_utc`, `truth`, `dispatch_inputs`) — `DISPATCHABLE` derives from it, and `test_the_console_schedule_table_matches_the_workflow_files` will fail until the YAML agrees |
| Reach a new package | add the SUBMODULE to `_ADMIN_ALLOWED_IMPORT_PREFIXES` with a stated reason, in the same diff. That test names submodules, not packages, on purpose |
| Add a config knob | a field on `AdminSettings` + a `*_ENV` constant + a line in `from_env`. Never a CLI flag |
| Change how untrusted text is cleaned | `service._clean` (which is `stigmergy.text.sanitize`) — never at a call site, and never by flattening newlines |
| Add a frontend view | a `render(host)` export in `views.js`, a `ROUTES` entry in `app.js`, and DOM built only through `ui.el` |
| Rotate or revoke the credential | `stigmergy-admin-token`, then set the new `STIGMERGY_ADMIN_TOKEN_HASH`. There is no store and no list — revocation is one secret change |

## Notes

- **Why a branch and not a middleware exemption.** `_Branch` is the outermost ASGI object:
  `/admin*` HTTP requests go right, and *everything* else — lifespan (the MCP session manager
  lives on it), websockets, every other path — flows to the inner app untouched. `__getattr__`
  delegates to the inner app so the branch stays transparent to Starlette introspection. Path
  matching is exact (`/admin` or `/admin/…`), so a path like `/administration` is inner traffic.
- **Inert means inert.** With `$STIGMERGY_ADMIN_TOKEN_HASH` unset there is no service, no route
  table, no gateway and **no DDL** — `test_an_unconfigured_console_runs_no_admin_ddl` drops
  `admin_actions` and proves `compose` does not recreate it. From the MCP surface the module is
  undetectable.
- **The gate's order is load-bearing**: `Host` first (421), token second (401), headers on the way
  out of both. The host check therefore also covers the tokenless shell and assets. With no
  `$STIGMERGY_PUBLIC_HOST` configured every host passes, which is what keeps local dev unchanged;
  the check is defense in depth, since a bearer token carries no ambient credential a DNS
  rebinding could ride.
- **Not every POST is a mutation.** `queue/purge` with `dry_run`, `digest/preview` and
  `index/check` are POSTs that write no `admin_actions` row, because none of them changes state
  through `_mutate`. `digest/preview` is the subtle one: it is side-effect-free with respect to
  Slack, but `digest.run` records a `digest-dry-run` row in `job_runs` for every preview, which is
  why the digest tab's history fills up with them.
- **The console reads page PATHS, never page BODIES.** Two payloads carry paths out of the
  corpus: `POST /admin/api/index/check` (whose findings come from `index.check.run_checks`, a
  whole-index unscoped lint — its own declared ACL exception, because a scoped lint is blind to
  out-of-scope corruption) and `GET /admin/api/gardener` (whose findings were produced by
  `gardener/checks.py` and `gardener/sweep.py`, likewise declared exceptions). Both are behind
  the operator token, and neither ever returns page content. Say this plainly rather than claiming
  the console touches nothing ACL-bearing — it does, in the shape the exception list describes.
- **`_zone_counts` swallows every exception and returns `{}`** — "no index yet" is a state, not an
  error, and the tile renders empty. This is the one place a bare `except` is the right answer
  here; anywhere else it would hide a defect.
- **The worker numbers come from `librarian.config`, not from `capture.queue`'s own 300 s** — and
  the lease is RESOLVED per call (`worker_visibility_timeout_s()` →
  `librarian_config.resolved_visibility_timeout_s()`), never frozen at import. It governs BOTH
  directions. On the read path a console that compared a 700 s-old claim against 300 s would call
  every long agent item dead — the mistake `query_in_flight`'s docstring names. On the WRITE path
  it is worse and it shipped: `queue_reclaim` fell back to the queue's 300 s, so the ordinary
  Reclaim button requeued every capture held between the two numbers out from under a running
  worker, and failed it outright once its attempts were spent — telling the submitter it failed
  while the worker was still filing it. `queue.release_expired` now takes no default at all, so a
  caller must state the horizon; this function is what the console states, and it is why the one
  librarian import exists. It was a static CLASS default until it was derived, which is the same defect
  one layer over: staging's `$STIGMERGY_LIBRARIAN_TIMEOUT_S=600` derives a 1500 s lease while the
  constant read 900, so the meter called a healthy in-flight item expired and the button swept it.
  Reading the env per call is correct because `fly.toml`'s `[env]` is app-wide — this process's
  environment is the worker's.
- **`_mutate` records `outcome='error'` and re-raises** — including for a domain refusal, so the
  action log answers "what was attempted", not merely "what succeeded".
- **The gardener tab's `sla` severity filter is permanently empty**, and that is not a console
  defect. `sla` is a real member of `gardener.schema.SEVERITIES`, but as
  [`gardener/index.md`](../gardener/index.md) records, **nothing in that package can produce an
  `sla` finding**: every deterministic check emits `info` or `warn`, and `MODEL_CHECK_SEVERITY`
  maps all four model slugs to `warn`. The console faithfully mirrors a vocabulary with no
  producer; the chip becomes live the day a check emits one.
- **`entities` is no longer read-only from the web** — [ADR 029](../../../docs/decisions/029-admin-console.md)'s
  "writes stay CLI" consequence is superseded by
  [ADR 030](../../../docs/decisions/030-server-side-entity-minting.md): `entity_approve` mints
  through the same server-driven door the review lane walks
  (`entities.remote.mint_via_clone` -> `entities.mint.mint`), directly — the CLI reaches the same
  `entities.mint.mint` discipline from the steward's own clone instead, never through
  `entities.remote`. Never through
  `review_decide`/`BrainService` (both banned imports here, `test_admin_never_imports_the_read_
  path_or_the_mcp_adapter`). Driving `entities` directly, the way this package already drives
  `capture.dispositions`, is what D2's authorship ruling requires: the console mints under the
  admin token with the actor as ATTRIBUTION, not authorization, and `review_decide`'s steward
  check / self-approval refusal are for a RESOLVED identity (a bearer token, a Slack profile) —
  routing through `review_decide` itself would either wrongly enforce that check against a
  free-text `actor` field or require bypassing it, neither of which is what D2 asks for. The one
  ledger write this needs beyond its own `admin_actions` row — `review_decisions`, so "who
  approved this identity" answers from one table regardless of door — reuses
  `server.review.record_decision` (public since this change, previously `_record_decision`)
  rather than a second hand-written `INSERT` that could drift from the MCP/Slack doors' own.
  `entities.cli`/`entities.birth` left this package's declared import set the same change: neither
  is reached from here any more (`entities.cli.suggestable_entity_name`, the bracket-placeholder
  command's shell-safety predicate, has no caller left in this package — `_entity_commands` is
  gone whole).
