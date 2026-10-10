# Multi-tenancy

One deployment serves several companies. Each is a **tenant** (a **workspace** in the product) with
its own repository, roster, layers, captures, index, and backoffice, where `audience-layers.md` and
`backoffice-users-and-wiki.md` apply unchanged. **Platform operators** manage workspaces from the
master console; **workspace administrators** (`is_admin`, in `brain-admins`) run one workspace.

## 1. Constraints

- **Nothing crosses tenants.** No read surface, write, job, cache, or error reveals or changes
  another tenant's data; reads answer byte-identically whether or not other tenants exist.
- **The librarian never sees a tenant**, and a job touches exactly one tenant.
- **One of everything**: one Fly app, Postgres schema, evidence bucket, model key, GitHub App, and
  host; no per-tenant process.

## 2. Model

| Object | Scope | Rules |
|---|---|---|
| Tenant | Platform | `tenants`: `id` (uuid), `slug` (`[a-z][a-z0-9-]{1,31}`, unique, not one of `RESERVED_SLUGS`: `admin`, `api`, `app`, `mcp`, `platform`, `status`, `www`), `name` (at most 120 characters), `status` (`provisioning`, `active`, `suspended`), `repo` (`owner/name`, unique), `slack_team_id` (unique, optional), `last_claimed_at` |
| People, agents, layers, tokens, sessions, Slack channels | Tenant | Roster tables keyed by `(tenant_id, …)`. Token and session digests are unique across the platform, so a credential names its tenant. Emails are unique per tenant |
| Platform operator | Platform | `platform_operators`, `platform_tokens` (expire after 90 days), `platform_sessions` with an optional acting tenant |
| Access request | Platform | An email left on the landing page, with optional company and note; kept 90 days after it is handled, at most a year |
| Workspace deletion | Platform | `workspace_erasures`: one open request per workspace; the request and its receipt outlive the workspace |

An operator session acts in at most one tenant at a time, as an administrator who reads every
layer and manages the roster. Captures, deletions, entity changes, gardener runs, recompiles, and
erasures are refused to operators: the tenant's own administrators make them.

## 3. Storage

### 3.1 Postgres

- Every tenant-owned table carries `tenant_id uuid NOT NULL REFERENCES tenants(id)`, defaulting to
  the connection's tenant, and forces row-level security with one policy (`tenant_isolation`): a
  row is visible and writable when its `tenant_id` equals `app.tenant_id`, or when the connection
  is in platform scope (`app.scope = 'platform'`). A connection with neither reads no tenant row and
  cannot insert one.
- `kernel.tenancy` is the only code that sets either setting. Platform scope is used only by
  credential lookup and sign-out, the writer's claim and schedule, evidence liveness, upload,
  audit, and access-request expiry, the index's embedding-dimension check, workspace deletion, the
  import and handover of a refiled workspace (`ops.replay_workspace`, the one sanctioned copy
  between workspaces), and the master console. A tenant scope inside it acts for that tenant alone.
- A connection whose role bypasses row-level security is refused. The service's role owns the
  tables, which it creates at start-up; forced security applies the policy to the owner.
- Platform tables carry no `tenant_id`: `tenants`, `platform_operators`, `platform_tokens`,
  `platform_sessions`, `platform_actions`, `access_requests`, `workspace_erasures`, `index_meta`,
  `worker_heartbeat`, `webhook_deliveries`.
- Every unique key of a tenant table includes `tenant_id`, except random identifiers and digests.
- The index is per tenant: a full rebuild replaces one tenant's rows, each `(tenant, audience)` has
  its own partial HNSW index, and `index_health` holds one row per tenant.

### 3.2 Git and evidence

- Each tenant has one private repository in the GitHub organization `STIGMERGY_GITHUB_ORG`. The
  master console names it `<org>/<slug>-brain`; the writer creates it through the GitHub App and
  pushes the shared scaffold; `stigmergy-admin tenant create --status active` registers an existing
  one. The App's one installation covers every repository, and its webhook's pushes map to tenants
  by repository. The writer keeps one checkout per tenant.
- Evidence objects are content-addressed in one bucket, read only through tenant-scoped rows, and
  deleted only when no tenant's rows reference them.

## 4. Entry points

| Entry | Tenant from |
|---|---|
| MCP and bridge endpoints | The bearer token's row. Operator tokens do not open MCP |
| Backoffice | The session's row; an operator session's acting tenant |
| GitHub webhook | The pushed repository |
| Slack | The event's team, matched to `tenants.slack_team_id` |
| Writer | The claimed job's row |
| Landing request form | None; it writes only `access_requests` |
| `stigmergy-admin` workspace commands | `--tenant <slug>`, else `$STIGMERGY_TENANT`, else the only active workspace |

A suspended tenant's credentials, sessions, pushes, and Slack events are refused, and the writer
claims none of its captures; a layer erasure or workspace deletion already requested still runs.

## 5. Writer

One process serves every tenant under one writer lock. Each pass runs, in order, a due workspace
deletion; provisioning (create the repository, retry with backoff, mark the tenant active); layer
erasures and due index rebuilds; then the queued capture of the active tenant served longest ago,
in that tenant's checkout. Garden and the full index rebuild are due daily per active tenant.

## 6. Master console

Operators sign in with an operator token on the backoffice sign-in page and see:

- **Workspaces**: people, pending and failed captures, last landing, index state, last garden run,
  and estimated seven-day model spend. Create one (name, slug, first administrator's email and name;
  that token is shown once); suspend or resume it (not while provisioning or being deleted);
  request an index rebuild; open it, which makes it the session's acting tenant.
- **Access requests**, to mark handled, and the **Writer**: heartbeat, recent failed captures
  across workspaces, and recent platform actions.

Every console mutation, opening a workspace included, appends to `platform_actions`. Spend is
estimated from `model_usage`: per tenant, the tokens of each filing and answer (OCR, the classifier,
and embeddings are not recorded). `stigmergy-admin operator bootstrap <email> --name <name>` creates
the first operator while none exists; `operator mint <email>` issues an operator a new token.

## 7. Deleting a workspace

Only a suspended workspace that does not own the deployment's Slack app can be deleted. The
operator types its slug to confirm; the console warns that Supabase backups and GitHub's restorable
copy of a deleted repository are beyond its reach. The writer rechecks both conditions, then deletes
the repository (absent counts as deleted), every row of every tenant table and the `tenants` row in
one transaction (clearing it from operator sessions), its HNSW indexes, the evidence only it
referenced, and its checkout.

Each step resumes after a stop, with backoff. Until the repository is gone, a refused request or
one that fails eight attempts ends `failed` and can be made again; after that the request retries
until done. The receipt records what each step removed; slug, repository, and emails are then free.

## 8. Acceptance criteria

1. **TEN-01** Every tenant-owned table has `tenant_id`, forced row-level security, and exactly one
   isolation policy; every unique index on it includes `tenant_id` except random identifiers and
   credential digests; platform tables have no `tenant_id`.
2. **TEN-02** A connection without a tenant reads no row of any tenant table and cannot insert.
3. **TEN-03** For two tenants with byte-identical repositories, rosters, and layer names, every MCP
   read tool, the wiki, and the admin lists answer one identically whether or not the other exists.
4. **TEN-04** A token, session, or push of one tenant never reaches another tenant's rows; a
   suspended tenant's credentials are refused.
5. **TEN-05** A capture of each tenant lands one commit in its own repository and one ledger row in
   its own tenant; the planner prompt of the second holds no text of the first.
6. **TEN-06** Tenants take turns: one queued item of B is claimed before the second of ten of A.
7. **TEN-07** A full rebuild of one tenant leaves every row and index of the other unchanged.
8. **TEN-08** Erasing layer `x` in one tenant leaves another tenant's layer `x` untouched, and
   evidence another tenant still references survives.
9. **TEN-09** Creating a tenant creates its private repository with the shared scaffold
   (`README.md`, `CLAUDE.md`, `.gitignore`, and the empty page folders), its first administrator,
   and a token shown once; the tenant becomes active.
10. **TEN-10** Only an operator session reaches the master console; a workspace administrator meets
    it as an unknown route.
11. **TEN-11** The landing form stores an access request and answers the same whether or not the
    email was seen before.
12. **TEN-13** Deleting a workspace removes its repository, every one of its rows, its HNSW
    indexes, its checkout, and the evidence only it referenced; another workspace's rows, indexes,
    and shared evidence are unchanged; and a new workspace can then be created with the same slug
    and starts empty.
