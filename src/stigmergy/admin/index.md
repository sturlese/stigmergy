# admin — code map

The operations console: one web surface over what already runs — the steward drain, the three
scheduled workflows, the gardener's findings, the digest, the index, pending entity situations,
activity and the worker's lease. Mounted as an ASGI branch in front of the MCP transport, inside
the `app` process group, behind its own single credential.
Narrative: [`docs/reference/admin-console.md`](../../../docs/reference/admin-console.md).

**It is a skin, not a subsystem.** Every act lands on a seam another package owns and tests:
`capture.dispositions`, `capture.retention`, `capture.queue.release_expired`, `gardener.store`,
`digest.run`, `index.check`, `entities.situations`, `entities.remote.mint_via_clone`,
`server.pilot_report`. The only state it owns is `admin_actions`.

**It is not a read surface over the corpus** — no search, no `ask`, no page bodies.
`test_admin_never_imports_the_read_path_or_the_mcp_adapter` bans `stigmergy.index.search`,
`stigmergy.answer`, `stigmergy.server.mcp_server`, `.service` and `.transport_http` from every
module here, so the rule is a property of the import graph. (The Activity tab does show `ask`
questions — user content, not corpus content.)

## Modules

| Module | What it is |
|---|---|
| `routes.py` | `compose(inner, *, conn, server_settings, admin_settings=None, gateway=None)` — the only door into this package, called by `server.transport_http.build_http_app`. Also `_Branch` (outermost ASGI router), `_AdminGate` (host → token → security headers), `_json_endpoint` (domain-exception → status map) and the route table |
| `service.py` | `AdminService`, one method per route; `CRON_WORKFLOWS`/`DISPATCHABLE` (the drivable-workflow allowlist); `worker_visibility_timeout_s()`/`WORKER_MAX_ATTEMPTS`; the domain errors `AdminBadRequest`/`AdminNotFound`/`AdminRefused` |
| `settings.py` | `AdminSettings` + `from_env`, the five `*_ENV` constants, `DEFAULT_ACTOR`, `DEFAULT_WORKFLOWS_REPO`, and the sha256-shape refusal that turns a malformed hash into a `StartupError` |
| `auth.py` | `token_matches` (sha256 + `hmac.compare_digest`), `bearer_token` (two `Authorization` headers → `None`), `host_allowed` |
| `github.py` | `ActionsGateway` — the crons tab's only reach out of this process (`workflows`/`runs`/`dispatch`/`set_enabled`), `urllib` with an injectable opener, a 60 s read cache that mutations clear, and `ActionsError` carrying the status and never the token |
| `schema.py` | `admin_actions`: `ensure_admin_schema` (behind `capture.schema.startup_ddl_lock`), `record_action` (never raises), `recent_actions` |
| `cli.py` | `stigmergy-admin-token` — mints the one credential: 32 random bytes, plaintext printed once beside its `STIGMERGY_ADMIN_TOKEN_HASH=` line, nothing stored |
| `static/` | the SPA, no build step: `index.html` + `app.js` (shell, hash router, login), `api.js` (the one fetch seam), `ui.js` (DOM helpers), `views.js` (the view renderers), `styles.css` |

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
| GET | `/admin/api/meta` | `meta()` | yes |
| GET | `/admin/api/overview` | `overview()` | yes |
| GET | `/admin/api/queue` | `queue_list()` — repeatable `?status=`, `?submitter=`, `?limit=` (default 50; non-integer is a 400) | yes |
| POST | `/admin/api/queue/reclaim` | `queue_reclaim()` — optional int `visibility_timeout_s`; omitted means the worker's derived lease, resolved per call; the horizon is clamped at 0 | yes |
| POST | `/admin/api/queue/purge` | `queue_purge()` — optional int `older_than_days`, `dry_run` | yes |
| GET | `/admin/api/queue/{id:int}` | `queue_show()` | yes |
| POST | `/admin/api/queue/{id:int}/requeue` | `queue_requeue()` | yes |
| POST | `/admin/api/queue/{id:int}/resolve` | `queue_resolve()` — non-blank `note` required | yes |
| POST | `/admin/api/queue/{id:int}/reject` | `queue_reject()` — non-blank `reason` required | yes |
| GET | `/admin/api/gardener` | `gardener_state()` | yes |
| GET | `/admin/api/digest` | `digest_state()` | yes |
| POST | `/admin/api/digest/preview` | `digest_preview()` (async) | yes |
| POST | `/admin/api/digest/post` | `digest_post()` (async) | yes |
| GET | `/admin/api/index` | `index_state()` | yes |
| POST | `/admin/api/index/check` | `index_substrate_check()` | yes |
| GET | `/admin/api/entities` | `entities_list()`, wrapped as `{"situations": [...]}` | yes |
| GET | `/admin/api/entities/{id:int}` | `entities_show()` | yes |
| POST | `/admin/api/entities/{id:int}/approve` | `entity_approve()` — `name`/`entity_type` required, `entity_id`/`aliases`/`role` optional, `requeue` boolean (default `true`) | yes |
| GET | `/admin/api/activity` | `activity()` | yes |
| GET | `/admin/api/worker` | `worker_status()` | yes |
| GET | `/admin/api/crons` | `crons_state()` | yes |
| POST | `/admin/api/crons/{workflow_file}/dispatch` | `cron_dispatch()` — `inputs` must be a JSON object | yes |
| POST | `/admin/api/crons/{workflow_file}/enable` | `cron_set_enabled(enabled=True)` | yes |
| POST | `/admin/api/crons/{workflow_file}/disable` | `cron_set_enabled(enabled=False)` | yes |

`{workflow_file}` is a free path segment on the route and an allowlist check in the service
(`_require_workflow`, before `_require_gateway` and therefore before any network call): the refusal
must not depend on a converter, so an unlisted file is a 400 naming the allowed set.

## Reuse

- `routes.compose` — the only composition point. When `configured()` is false it returns
  `_Branch(inner, None)`: no service, no routes, no DDL.
- `AdminSettings.from_env(env=None)` — the only place this package reads the environment; `env` is
  injectable. Never `os.environ` at module scope here.
- `AdminService._mutate` / `_mutate_async` — every state-changing call goes through it: actor
  fallback, an `admin_actions` row on both outcomes, `CaptureError` → `AdminRefused`.
- `entities.remote.mint_via_clone` + `server.review.record_decision` — `entity_approve`'s whole
  seam: mint through a throwaway clone, then write the `review_decisions` row every mint door
  shares. `EntityError`/`CapabilityUnavailableError` map to `AdminRefused` with the library's own
  sentence.
- `auth.token_matches` / `bearer_token` / `host_allowed` — pure; never re-derive a header parse.
- `service._clean` (= `stigmergy.text.sanitize`) — the one cleaning seam for untrusted strings on
  the way out: control characters die, newlines and a literal `<script>` survive, because HTML
  inertness is the client's half. Every excerpt, error, note, finding and rationale crosses it.
- `schema.record_action` — bookkeeping that swallows and logs; it must never fail the work.
- `ui.el` / `ui.render` / `ui.clear` (frontend) — the only sanctioned way to build a node. `render`
  exists because `replaceChildren` stringifies `null` into the text `"null"`.
- `ui.confirmForm` (frontend) — every mutation goes through one, and its `consequence` sentence is
  a required argument: a button that spends money or posts to Slack says so first.

## Avoid

- Adding an exemption to `_BearerAuthMiddleware` for this console — `/admin*` never reaches that
  middleware, which is what keeps the webhook's "one exemption, exact path match" doctrine true.
- Importing outside `_ADMIN_ALLOWED_IMPORT_PREFIXES`. The librarian is reachable through
  `librarian.config` alone; `stigmergy.slack.bolt_gateway` may only be imported inside the posting
  handler, so a keyless console never loads the Slack SDK.
- Reading `pages_index` for anything but an aggregate. `_zone_counts` is the single read and a
  named entry in `ACL_REACHABILITY_EXCEPTIONS`; anything more names a `visible()` predicate.
- Writing SQL for something a library already exposes. The only SQL owned here is read-side
  plumbing nothing else surfaces: `job_runs`/`ingest_errors`, the `audit_log` aggregates, the
  digest watermark, the zone counts, and `admin_actions`.
- Raising a message that could carry captured content across the HTTP boundary. The catch-all in
  `_json_endpoint` returns `the operation failed (<ClassName>)`; only the three domain errors and
  `ActionsError` cross with their sentence.
- Deciding in `views.js` when the Approve form's `Name` may carry a default. The one-vs-several
  rule is `entities.situations.mint_name_prefill`'s alone, and it is taken where `subject` and
  `subjects` are — in `situations._situation_view`, on the row BOTH entity routes read, so the
  list route and the detail route cannot answer differently. `_situation` only sanitizes what it
  is handed (`mint_name_prefill` beside `subject`/`subjects`) and never recomputes it from a row
  it preprocessed itself; `entityApproveFlow` renders it — an empty prefill with names still to
  place IS the several-names case, so the field stays empty and `subjects` is listed. Never
  prefill from the joined `subject` display string, and never count names here: this form mints
  ONE entity as ONE commit, and the Slack modal obeys the same decided value, so a second
  derivation is two doors that can disagree about whether a default is safe at all.
  `tests/admin/test_static_discipline.py` greps for exactly this.
- Building DOM from an HTML string in `static/`. `innerHTML`, `outerHTML`, `insertAdjacentHTML`,
  `document.write`, `eval(`, `new Function` and any `http(s)` `src`/`href` are grepped out of the
  shipped files by `tests/admin/test_static_discipline.py`; only `textContent` makes markup inert.
- Holding a cursor across an `await` — this service shares the process-wide autocommit connection,
  and the async digest methods await inside `digest.run`, between statements.
- Adding a CLI flag. The server's command line is pinned byte-identical between `fly.toml` and the
  Dockerfile `CMD`; configuration is env-only so that pin never moves.

## Data & contracts

`admin_actions` (`schema.py`): `id BIGSERIAL` · `ts TIMESTAMPTZ DEFAULT now()` · `actor` · `action`
· `args JSONB` · `outcome` (`ok`/`error`) · `error_class`, plus `admin_actions_ts_idx (ts DESC)`.
`actor` is **attribution, not authorization** — recorded, never checked.

Compose-time DDL, only when configured: `ensure_admin_schema` plus
`gardener.schema.ensure_gardener_schema` and `server.review.ensure_review_schema`, which the read
paths would otherwise meet as a bare `UndefinedTable` on a fresh database.

| Env var | Default | Effect |
|---|---|---|
| `STIGMERGY_ADMIN_TOKEN_HASH` | `""` | the master switch: unset → the console does not exist. Must be 64 sha256 hex (uppercase normalized); any other non-empty value raises `StartupError` at startup |
| `STIGMERGY_ADMIN_ACTOR` | `admin-console` | the `actor` fallback and the form prefill |
| `STIGMERGY_ADMIN_GITHUB_TOKEN` | `""` | unset → `gateway is None` → the crons tab is database-truth-only |
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
`base-uri`/`form-action`/`frame-ancestors` `'none'`), `x-content-type-options: nosniff` and
`referrer-policy: no-referrer`; `/admin/api/*` additionally `cache-control: no-store`.

`CRON_WORKFLOWS` — `index-rebuild.yml`, `retention-purge.yml`, `gardener.yml`, each naming its
`schedule_utc` and where the database truth lives (`job_runs:<job>`, or `index_meta.built_at` for
the rebuild, which writes none). `retention-purge.yml` declares the only dispatch input
(`dry_run`); an undeclared key is refused by name before the gateway is touched.
`test_the_console_schedule_table_matches_the_workflow_files` parses the real YAML.

**Frontend**: each renderer in `views.js` is `render(host, params?) → cleanup?`, dispatched from
`app.js`'s `ROUTES` and `DETAIL_ROUTES`. The token lives in `sessionStorage` under
`stigmergy-ops-token` — no cookie, therefore no CSRF surface — and any 401 clears it and reloads
into the login screen. Only `overviewView` polls (15 s, skipped while `document.hidden`), and it is
the only view returning a cleanup function.

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
- **Not every POST is a mutation.** `queue/purge --dry-run`, `digest/preview` and `index/check`
  write no `admin_actions` row. `digest/preview` still records a `digest-dry-run` row in `job_runs`,
  which is why the digest tab's history fills with them.
- **The console reads page PATHS, never page BODIES.** `index/check` and `gardener` carry paths out
  of the corpus, both behind the operator token and both declared ACL exceptions.
- `_zone_counts` swallows every exception and returns `{}` — "no index yet" is a state, not an
  error. It is the one place a bare `except` is right here.
- **The worker's lease is resolved per call** (`worker_visibility_timeout_s()` →
  `librarian_config.resolved_visibility_timeout_s()`), never frozen at import, and it governs both
  directions: a console comparing an old claim against `capture.queue`'s own 300 s would call every
  long agent item dead on the read path, and on the write path Reclaim would requeue captures out
  from under a running worker. `queue.release_expired` takes no default, so a caller must state the
  horizon; reading the env per call is correct because `fly.toml`'s `[env]` is app-wide — this
  process's environment is the worker's.
- `_mutate` records `outcome='error'` and re-raises, including for a domain refusal, so the action
  log answers "what was attempted", not only "what succeeded".
- **The gardener tab's `sla` severity filter is permanently empty** and that is not a console
  defect: `sla` is a real member of `gardener.schema.SEVERITIES` that nothing in that package
  produces. The chip becomes live the day a check emits one.
- **`entity_approve` mints server-side**, driving `entities` directly the way this package already
  drives `capture.dispositions` — never through `review_decide`/`BrainService` (both banned
  imports), whose steward check and self-approval refusal are for a resolved identity, not a
  free-text `actor` field.

## Common tasks

| Task | Touch |
|---|---|
| Add a read endpoint | a method on `AdminService`, a `@_json_endpoint` handler, a `Route`. A new table means checking the import allowlist in `tests/test_architecture.py` first |
| Add a mutation | the same, but through `_mutate`/`_mutate_async` for the `admin_actions` row and the actor fallback — and the frontend flow through `confirmForm` with an honest consequence sentence |
| Add a console-drivable workflow | a row in `CRON_WORKFLOWS` (file, title, `schedule_utc`, `truth`, `dispatch_inputs`); `DISPATCHABLE` derives from it and the schedule test fails until the YAML agrees |
| Reach a new package | add the SUBMODULE to `_ADMIN_ALLOWED_IMPORT_PREFIXES` with a stated reason, in the same diff |
| Add a config knob | a field on `AdminSettings` + a `*_ENV` constant + a line in `from_env`. Never a CLI flag |
| Change how untrusted text is cleaned | `service._clean` — never at a call site, and never by flattening newlines |
| Add a frontend view | a `render(host)` export in `views.js`, a `ROUTES` entry in `app.js`, DOM built only through `ui.el` |
| Rotate or revoke the credential | `stigmergy-admin-token`, then set the new hash. There is no store and no list |

## Tests

`tests/admin/` runs against real Postgres through `tests.testdb` and real git for `entity_approve`
(`conftest.build_bare_knowledge_repo`); only the two network edges — GitHub Actions and Slack — are
faked. `test_settings_and_auth.py` and `test_cli.py` are keyless; `test_github_gateway.py` drives
an injected opener; `test_service_pg.py` is the largest suite (queue reads, the drain, reclaim on
both edges of the worker's lease, purge dry-run vs real, cron paths, real minting against a
throwaway bare remote); `test_routes_pg.py` exercises the real `compose` product over
`httpx.ASGITransport` (inert 404s, the tokenless shell vs the 401 API, the security headers, the
status mapping); `test_static_discipline.py` greps the shipped frontend files. Auth refusals are
tested beside their benign twins throughout.

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
