"""`stigmergy.gardener` — corpus health on demand.

Sibling of `views`, `capture`, `index`, `server`, `slack`, `digest`. Owns one table
(`gardener_findings`) and the `stigmergy-gardener` CLI: eight deterministic checks PLUS a bounded
model editorial sweep over the corpus/registry/queue tables, persisted as findings + a `job_runs`
row, printed as a severity-grouped report.

**Findings-only, by construction.** The gardener REPORTS; it never fixes, never writes a page,
never opens a PR or an issue, never edits the registry, never regenerates a view — it only
*names* the existing command for that (`stale-view`'s `suggested_action` is
`stigmergy-views regenerate --entity <id>`, never invoked here — one entity per finding, matching
`checks.check_stale_views`'s own per-entity population; `--stale` is the batch flag `views/
cli.py` offers a human, not what this package's own suggested command ever names). Ruled out
structurally, not by discipline alone: this package imports no git plumbing and holds no path
under `wiki/` (`tests/test_architecture.py` proves both mechanically).

**Layering** (enforced by `tests/test_architecture.py`): `gardener` may import
`stigmergy.capture.ops` (the shared `job_runs` writer) and `stigmergy.capture.schema`
(`startup_ddl_lock`, for this package's own DDL, plus `ensure_capture_schema`);
`stigmergy.index.store` from its own CLI only (the connection seam, mirroring `capture.cli`'s one
permitted edge — every library module here takes `conn` as a plain argument instead); the entity
registry loader (`stigmergy.kernel.registry`); exactly one symbol pair from `stigmergy.views`
(`staleness.list_stale_entities`/`staleness.list_all_anchored_entities` — reused by
`check_stale_views` and `check_dead_vocabulary`, never re-derived. `views.staleness`, not
`views.regenerate`: `regenerate.py` module-level-imports `views.writer`, the commit-and-push path,
so importing IT would load the full git write stack into every gardener process — `staleness.py`
is the read-only extraction of exactly these two functions, with none of that); exactly one symbol
from `stigmergy.librarian`'s library modules (`librarian.page.is_provenance_type`, imported as pure
policy: a provenance page's `entity: []` means "the extractor found no evidence", never a checked
company-wide declaration, and the checks that count anchoring must not read it the other way);
`stigmergy.server.errors` (`StartupError`/`IdentityError`, the shared settings-validation vocabulary
`SlackSettings`/`server.settings.Settings` already use); and `stigmergy.slack.gateway` (the
`SlackGateway` protocol + `FakeSlackGateway`, for the SLA notice — the daily cron's two steps are
sequential CLI invocations with nothing listening in between, so the notice has to be posted by
the run that found the `sla` finding).

**Two more edges, for the SLA notice: `stigmergy.server.acl` and `stigmergy.slack.channels`.** The
notice posts to the SAME Slack channel `stigmergy.digest` broadcasts to, so it needs the SAME ACL
scoping that package's own docstring requires of every page it renders. The mechanical guard
(`tests/test_architecture.py::ACL_REACHABILITY_EXCEPTIONS`) cannot see this on its own, because the
notice reads its own findings tables and never `pages_index` — so a page path could otherwise reach
a channel whose audience the digest scopes titles for, entirely unscoped. `run.py` resolves
`audiences = slack.channels.channel_audiences(channels_path, ...)` the identical way
`digest.run.run_digest` does, and `notice.scope_findings_to_channel` uses `server.acl` to redact
any SLA finding whose page is not visible there, before `compose_notice` ever sees it — narrow
grants, for exactly this one purpose; see `notice.py`'s own module docstring for the full
reasoning.

**The sweep's two edges are confined to `gardener/sweep.py`**: `stigmergy.kernel.llm`
(`build_processor` — the ONE fake/real PydanticAI dispatch every agent-building module in this
codebase shares, never reinvented) and `stigmergy.kernel.result` (`fake_result`, the offline-double
envelope every fake backend in this codebase returns). `stigmergy.text` (`fence`/`sanitize`/`clamp`)
is in the allowed set too — dependency-free, the bottom of the stack, the same edge `views` already
has for the identical reason: page bodies are untrusted input before they reach a prompt, and a
model's own rationale/excerpt echo them back before they reach `detail`.

Only `cli.py` additionally imports `stigmergy.server.review` (`ensure_review_schema` — so opening the
database ensures every schema a fresh one is missing, mirroring `digest/cli.py::_connect`),
`stigmergy.librarian.config` (the `--repo` default, mirroring `views/cli.py`) and
`stigmergy.slack.bolt_gateway`'s `build_gateway` factory (the real gateway, from just a bot token —
mirroring `stigmergy.slack.app` being the one process entry point that ever touches the Slack SDK;
nothing else in this package imports it directly).

It must NEVER import `stigmergy.server` beyond the declared `errors`/`acl`/`review` symbols above,
never `stigmergy.answer`, never `stigmergy.entities`, and never `stigmergy.librarian` beyond the two
declared symbols above (`page`, `config`) — the gardener has no caller identity and no write path,
so it has no business reaching into any of the packages that serve or govern one.
"""
