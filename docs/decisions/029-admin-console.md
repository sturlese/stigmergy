# ADR 029 — the admin console: an ASGI branch, a remote control, never a fourth process

Status: accepted. Narrative:
[`docs/reference/admin-console.md`](../reference/admin-console.md). Code map:
[`src/stigmergy/admin/index.md`](../../src/stigmergy/admin/index.md).
**Amended by** [ADR 044](./044-the-capture-is-the-approval.md) D6: the console's cron remote control is gone with the crons. The Jobs page reads `job_runs` and offers nothing to dispatch, and the fine-grained GitHub PAT it needed is no longer a credential this system holds. "No scheduled runs outside GitHub Actions" became no scheduled runs outside the deployment.

## Context

Every operational act lived in a terminal: the steward drain (`stigmergy-queue`), the three GitHub
Actions crons (driven by `gh workflow run` and read back through `job_runs` SQL), the gardener's
findings (terminal scrollback), the digest (command-only), the substrate check, token activity
(`stigmergy-pilot-report`, run rarely). None of this was a missing capability — every act already
had a tested seam — but the daily loop required a shell, muscle memory, and the runbook open in a
second window. The console covers exactly that set, under two constraints: the MCP tool surface
(search/ask/submit/review) stays out, and the platform must not grow new moving parts.

The standing rules that box the design in: the platform is capped at THREE always-on process
groups (`app` · `worker` · `slack`), and a fourth would have to amend that cap first; nothing
scheduled runs outside GitHub Actions; there is no public read site, permanently; there is exactly
one operator.

## Decision

1. **The console rides inside the `app` process group as an outermost ASGI branch**
   (`stigmergy.admin.routes.compose`, called at the end of `transport_http.build_http_app`).
   `/admin*` routes into the console's own sub-app; every other scope — lifespan included, which
   the MCP session manager depends on — flows to the existing app untouched. No new process
   group, no new deploy unit, no new hostname: the three-process cap is not amended because
   nothing needed amending.
2. **The branch is NOT a middleware exemption.** The webhook's "ONE exemption, exact path match,
   never a prefix" doctrine stays literally true: `_BearerAuthMiddleware` never sees `/admin`
   traffic at all, so its exemption list still names exactly one path. The console carries its
   own gate instead — inert 404s until `$STIGMERGY_ADMIN_TOKEN_HASH` is set (the webhook's
   inert-until-secret posture), then bearer-token auth over `/admin/api/*` with the same generic
   401 body, `hmac.compare_digest` over sha256, the MCP transport's Host allowlist mirrored, and
   a strict CSP on every response.
3. **One dedicated credential, not the MCP token store.** Brain identities and platform
   administration are different authorities; conflating them would let a tester token operate
   the platform or an admin token read the brain. `stigmergy-admin-token` mints the pair; the
   admin token opens `/admin/api/*` and nothing else (pinned by test), and revocation is one
   secret change. One operator is why there is exactly one credential and no user management.
4. **Heavy jobs are dispatched, never re-executed.** Anything needing the knowledge repo or a
   model (gardener, index rebuild, retention) is driven through the workflows that already run
   it, via a GitHub gateway (fine-grained PAT, Actions read+write, one repo, allowlisted workflow
   files). Nothing-scheduled-outside-Actions stays literally true — the console is a remote
   control for Actions, and its fail-soft mode (no PAT → database truth only) keeps the page
   honest rather than broken.
   In-process stays only what needs exactly this process's resources: the queue drain
   (`capture.dispositions` — the same seams, cleaning inherited), the substrate check (DB + the
   baked registry), the digest (DB + the app-wide bot token; still command-only — a button is a
   command with a nicer key, and no schedule was added).
5. **The no-read-site rule gets teeth on the console.** No route reads corpus content: the one
   `pages_index` query is an aggregate zone count (a named entry on the ACL reader-exception
   list), and the architecture test bans `index.search`/`answer`/`BrainService` imports from the
   package outright.
6. **The console's own audit is a table** (`admin_actions`): actor, action, args, outcome — the
   web's `--by`, with `capture.ops`' bookkeeping-never-fails-the-work posture.

## Consequences

- The MCP surface is byte-identical when the console is unconfigured, and the branch delegates
  attribute access so existing test seams (`app.user_middleware` introspection) keep working.
- A PAT joins the app-wide secret surface — the accepted residual of driving Actions from here,
  third instance (the App key and the Anthropic key precede it), bounded to one repo's Actions
  scope with a rotation drill in the runbook.
- An admin bug shares the server's process; accepted with the three-process cap as the reason,
  mitigated by thin handlers over already-tested seams, the no-cursor-across-await invariant
  restated in the package, and the coverage gate.
- Entity writes (`approve`/`reject`/`create`) stay CLI: web-native entity birth needs an
  authorship decision (operator identity vs App bot) that deserves its own ADR, not a side
  effect of this one. The console renders the exact command instead, filled only when the name
  passes `suggestable_entity_name` — the one shared safety predicate.
  **Superseded by ADR 030**: the console's Entities tab grows a real Approve form that mints
  through the governed door directly, under the admin token with the actor as attribution rather
  than authorization — `create` stays CLI-only (no situation to approve FROM), and `reject` stays
  the Queue tab's own, unduplicated.
- The librarian reach is `librarian.config` alone (the worker's lease numbers for the status
  panel) — declared and pruned-when-unused in the architecture tests, the webhook's
  githubapp-only shape one exception over.
