# Self-hosting

This guide takes you from empty accounts to your own Stigmergy answering a first question with
citations. It uses Fly.io for compute, a Postgres with pgvector (Supabase in the examples), an
S3-compatible bucket (Cloudflare R2 in the examples), a GitHub organization, and an OpenRouter key.
Where the code cannot tell you how a provider behaves, the guide says so. Day-to-day operation is
in [`OPERATIONS.md`](./OPERATIONS.md).

Placeholders: `<app>` is your Fly app, `<host>` a public host name it answers at (`<app>.fly.dev`
or your own domain), `<org>` your GitHub organization, `<slug>` a workspace's name.

## 1. What you will run

One image (`Dockerfile`) runs as three Fly process groups (`fly.toml`):

| Process | Command | Role |
|---|---|---|
| `app` | `stigmergy-server --transport http` | MCP at `/mcp`, bridge uploads, the GitHub webhook, backoffice and master console at `/admin`, `/health` |
| `worker` | `stigmergy-librarian-boot` | the only writer for every workspace: filing, gardening, index rebuilds, erasures, new workspaces' repositories |
| `slack` | `stigmergy-slack` | optional Socket Mode adapter for one Slack workspace |

| Service | What it holds |
|---|---|
| Postgres with pgvector | workspaces, operators, people, layers, token hashes, the queue, the change ledger, audit, usage, and the search index (rebuildable from Git) |
| S3-compatible bucket | the original bytes of every capture, upload staging, and compressed exact patches |
| GitHub organization | one private repository per workspace, `<org>/<slug>-brain`, with its Markdown knowledge |
| OpenRouter | every model call: filing, answers, embeddings, OCR, layer routing, alias decisions |
| Fly.io | the machines and the secrets; the writer's checkouts sit on the worker's disk and are cloned again when lost |

## 2. Prerequisites

- Accounts: Fly.io; Postgres 16 with pgvector 0.7 or later (the index uses `halfvec`); an
  S3-compatible bucket; a GitHub organization in which you can create GitHub Apps; OpenRouter with
  credits. Slack is optional.
- On your machine: `git`, [`flyctl`](https://fly.io/docs/flyctl/) signed in (`fly auth login`),
  [`uv`](https://docs.astral.sh/uv/) and `make` (the deploy script imports the package from
  `.venv`), and `psql` or your database's SQL editor. The image is built by `fly deploy`; you need
  neither Docker nor Node locally.

Check out a release on a branch of your own, which will carry your deployment's `fly.toml`:

```bash
git clone https://github.com/sturlese/stigmergy.git && cd stigmergy
git switch -c deploy v0.12.0    # a release tag; see CHANGELOG.md
make venv
```

## 3. Database

As an administrator role (on Supabase, `postgres` in the SQL editor), run:

```sql
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE ROLE stigmergy_app LOGIN PASSWORD '<long random password>'
  NOSUPERUSER NOBYPASSRLS NOCREATEROLE;
GRANT USAGE, CREATE ON SCHEMA public TO stigmergy_app;
-- Supabase, or any host with a public data API: its API roles get nothing in this schema.
REVOKE USAGE ON SCHEMA public FROM anon, authenticated;
```

If `vector` already lives in another schema (Supabase's dashboard puts it in `extensions`), also
run `GRANT USAGE ON SCHEMA extensions TO stigmergy_app;` and
`ALTER ROLE stigmergy_app SET search_path = public, extensions;`.

The DSN, `STIGMERGY_INDEX_DSN`, is
`postgresql://stigmergy_app:<password>@<db-host>:5432/<database>?sslmode=require`. On Supabase, copy
the **session pooler** string (port 5432) and replace its user `postgres.<project-ref>` with
`stigmergy_app.<project-ref>`. The transaction pooler (port 6543) cannot work: a connection is bound
to one workspace with a session setting (`kernel/tenancy.py`).

**Why `stigmergy_app`.** Every table that holds a workspace's rows carries `tenant_id` under a
forced row-level security policy. Each process checks at connect that its role is neither a
superuser nor `BYPASSRLS` and refuses to start otherwise; Supabase's `postgres` role bypasses
row-level security and is refused. The name is a convention: the check is on the role's attributes.

**What creates the schema.** Nothing you run. Each process creates its tables on start with
idempotent DDL and forces row-level security on them, so `stigmergy_app` owns them; the writer
creates the index tables on a workspace's first index build. There are no migration files: a
release that changes tables is a clean cut ([`OPERATIONS.md`](./OPERATIONS.md), queue and index
schema changes).

## 4. Evidence bucket

1. Create a private bucket with no public access.
2. Create an access key limited to that bucket, with object read and write (R2: **Manage API
   tokens**, permission **Object Read & Write**, applied to this bucket).
3. Note the S3 endpoint without the bucket name; for R2 it is
   `https://<account-id>.r2.cloudflarestorage.com`.
4. Verify put, get, and delete:

   ```bash
   export R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com R2_BUCKET=<bucket>
   export R2_ACCESS_KEY_ID=<key-id> R2_SECRET_ACCESS_KEY=<secret>
   make r2-smoke
   ```

The service puts, gets, heads, and deletes objects and signs upload URLs; only the non-production
reset lists the bucket. The bridge uploads local files straight to the bucket through presigned
URLs valid for five minutes, so the endpoint must be reachable from people's machines. The
backoffice uploads through the app, so the bucket needs no CORS rule. The client signs with region
`auto`, fixed in `capture/evidence.py`; R2 and MinIO accept it, and whether another provider does
cannot be told from the code.

## 5. GitHub organization and the librarian App

The writer creates every workspace's repository in your organization, private, and is its only
author. Create the App under the organization: **Settings → Developer settings → GitHub Apps →
New GitHub App**.

| Setting | Value |
|---|---|
| GitHub App name | any; its slug may go in `STIGMERGY_LIBRARIAN_APP_LOGIN`, used only as the API `User-Agent` |
| Homepage URL | `https://<host>` |
| Callback URL, user authorization, device flow | none: the App never acts for a user |
| Webhook | active; URL `https://<host>/webhook/github`; secret: a random string (`openssl rand -hex 32`), which is `STIGMERGY_GITHUB_WEBHOOK_SECRET` |
| Repository permissions | **Administration**: read and write · **Contents**: read and write · **Metadata**: read-only |
| Organization and account permissions | none |
| Subscribe to events | **Push** |
| Where can this App be installed | only on this account |

| Permission | Used to |
|---|---|
| Administration | create a new workspace's repository; delete it when the workspace is deleted |
| Contents | clone, fetch, and push; force-push during a layer erasure; read changed files for the webhook |
| Metadata | look a repository up while deleting it |

Then:

1. Note the **App ID**: `STIGMERGY_LIBRARIAN_APP_ID`.
2. Generate a private key and keep the downloaded `.pem` outside the checkout. Its contents are
   `STIGMERGY_LIBRARIAN_PRIVATE_KEY`.
3. Install the App on the organization for **All repositories**, so it covers every repository the
   writer creates later. The installation id is the number at the end of the installation's
   settings URL, `https://github.com/organizations/<org>/settings/installations/<id>`:
   `STIGMERGY_LIBRARIAN_INSTALLATION_ID`.
4. Let the App push and force-push to every branch and tag of the workspace repositories: a layer
   erasure rewrites history and force-pushes all of them, so no branch protection or ruleset may
   block it.

Each use mints a short-lived installation token narrowed to the permissions, and where it can the
one repository, it needs; Git asks a credential helper for a fresh one per request, and no token is
stored on disk. Commits are authored as `Stigmergy Librarian <librarian@stigmergy.local>`.

## 6. OpenRouter

Create a key at <https://openrouter.ai/settings/keys> and add credits: `OPENROUTER_API_KEY`. The
platform has no spend cap of its own; set a credit limit on the key if you want one. The master
console shows each workspace's spend over seven days.

There is nothing to choose. The models are a closed allowlist in `kernel/llm.py`, the processes
refuse any other model, and `fly.toml` already sets the model variables.

| Purpose | Model |
|---|---|
| filing and cited answers | `deepseek/deepseek-v4.1-flash` |
| embeddings | `openai/text-embedding-3-large`, 2560 dimensions |
| OCR | `qwen/qwen3-vl-8b-instruct` |
| layer routing and filing-time alias decisions | `typesafe/jev-1.13`, through `https://openrouter.ai/api/alpha/decisions` |

Every request requires zero-data-retention providers that deny data collection; filing and answers
also name their providers (`decart/fp4`, `deepinfra/fp8`) and ignore `baseten`. If you restrict
providers on your OpenRouter account, keep these allowed. Whether a new account needs anything
enabled for the alpha decisions endpoint cannot be told from the code.

## 7. Fly app

### Create it

```bash
fly apps create <app>
```

On your `deploy` branch, edit `fly.toml` and commit it; the deploy refuses a dirty checkout:

- `app = "<app>"`, and `primary_region` near your database.
- Keep `STIGMERGY_LIBRARIAN_IDLE_EXIT_S = "600"` only if you set `STIGMERGY_FLY_API_TOKEN` below.
  Otherwise set it to `"0"`: the writer stops after ten idle minutes and, without the token,
  nothing starts it again.

```bash
git commit -am "chore(deploy): configure <app>"
```

For your own domain, run `fly certs add <domain> --app <app>`, create the DNS records it prints,
and list the domain in `STIGMERGY_PUBLIC_HOST`.

### Set the secrets

Every process group receives every secret; the `app` needs the App credentials too, to read
changed files for the webhook. Set them all before the first deploy:

```bash
fly secrets set --app <app> \
  STIGMERGY_INDEX_DSN='postgresql://stigmergy_app…' \
  OPENROUTER_API_KEY='…' \
  STIGMERGY_EVIDENCE_ENDPOINT='https://<account-id>.r2.cloudflarestorage.com' \
  STIGMERGY_EVIDENCE_BUCKET='<bucket>' \
  STIGMERGY_EVIDENCE_ACCESS_KEY_ID='…' STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY='…' \
  STIGMERGY_PUBLIC_HOST='<app>.fly.dev,<domain>' STIGMERGY_GITHUB_ORG='<org>' \
  STIGMERGY_LIBRARIAN_APP_ID='…' STIGMERGY_LIBRARIAN_INSTALLATION_ID='…' \
  STIGMERGY_GITHUB_WEBHOOK_SECRET='…'
fly secrets set --app <app> STIGMERGY_LIBRARIAN_PRIVATE_KEY="$(cat /path/to/librarian.pem)"
fly secrets set --app <app> STIGMERGY_FLY_API_TOKEN="$(fly tokens create deploy --app <app>)"
```

| Secret | Required | What it is |
|---|:---:|---|
| `STIGMERGY_INDEX_DSN` | yes | the `stigmergy_app` DSN (section 3) |
| `OPENROUTER_API_KEY` | yes | the OpenRouter key (section 6) |
| `STIGMERGY_EVIDENCE_ENDPOINT` | yes | the bucket's S3 endpoint; unset, it falls back to a local MinIO |
| `STIGMERGY_EVIDENCE_BUCKET` | yes | the bucket name |
| `STIGMERGY_EVIDENCE_ACCESS_KEY_ID` | yes | the bucket key id |
| `STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY` | yes | the bucket key secret |
| `STIGMERGY_PUBLIC_HOST` | yes | every host the app answers at, comma separated; MCP and the backoffice refuse any other `Host` |
| `STIGMERGY_GITHUB_ORG` | yes | the organization that holds workspace repositories; the console refuses to create a workspace without it |
| `STIGMERGY_LIBRARIAN_APP_ID` | yes | the App ID |
| `STIGMERGY_LIBRARIAN_INSTALLATION_ID` | yes | the installation id |
| `STIGMERGY_LIBRARIAN_PRIVATE_KEY` | yes | the App's private key, PEM text |
| `STIGMERGY_GITHUB_WEBHOOK_SECRET` | yes | the webhook secret; without it every delivery is refused and new pages wait for the nightly rebuild |
| `STIGMERGY_FLY_API_TOKEN` | with the idle stop | a deploy token limited to the app, which lets the app start a sleeping writer |
| `STIGMERGY_LANDING` | no | `on` serves your own landing page, built into the image, at the bare host (below) |
| `STIGMERGY_LIBRARIAN_APP_LOGIN` | no | the App's slug, sent as the GitHub API `User-Agent` |
| `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_TEAM_ID` | Slack only | section 9 |

The App credentials are all or nothing: a partial set is a configuration error. `fly.toml` holds
the non-secret defaults (models, timeouts, idle stop). Optional: `STIGMERGY_LIBRARIAN_GARDEN_AT` and
`STIGMERGY_INDEX_REBUILD_AT` move the daily garden (`05:07`) and index rebuild (`04:17`), in UTC as
`HH:MM` or `off`.

### Deploy

```bash
STIGMERGY_PARITY_GATE=off make deploy-staging
# or: STIGMERGY_PARITY_GATE=off bash scripts/deploy_staging.sh   (STIGMERGY_PYTHON names the venv)
```

`scripts/deploy_staging.sh` refuses a checkout with tracked or untracked changes, takes a lock,
checks that `.venv` imports the package, runs the filing parity gate unless
`STIGMERGY_PARITY_GATE=off`, runs `fly deploy --ha=false`, and scales to one `app`, one `worker`,
and `STIGMERGY_SLACK_MACHINES` (default 0) `slack` machines. The parity gate checks a recorded
real-model filing artifact ([`evals/filing/README.md`](../evals/filing/README.md)); it exists for
whoever changes the librarian. An operator deploying a released version turns it off explicitly;
keep it on if you change the librarian skill or filing code yourself.

The first deploy also creates a `slack` machine, which exits at start without Slack secrets before
the script scales it to zero. Whether `fly deploy` reports that as a failure cannot be told from the
code; if the script stops there, run `fly scale count app=1 worker=1 slack=0 --app <app> --yes`.

Check the deployment:

```bash
curl -fsS https://<host>/health
fly status --app <app>
fly logs --app <app>    # the worker logs "workspace checkouts live under /home/app/knowledge"
```

### Landing page and legal pages

The platform ships no landing page, and the bare host redirects to the backoffice sign-in. To
show your own, add `web/landing.html` and its sources under `web/src/landing/` (both ignored by git,
so the checkout stays clean) before deploying, and set `fly secrets set --app <app>
STIGMERGY_LANDING=on` (`1` and `true` work too). A landing page may post to the platform's
access-request endpoint (`POST /admin/api/access-requests`), whose emails operators see in the
master console.

Legal pages belong to whoever runs the deployment; none ship with the platform, and a page should
name your real company and contacts. Keep one Markdown file per page outside the checkout, named
like `privacy.md`, `terms.md`, or `sub-processors.md` (`^[a-z][a-z0-9-]{0,63}$`), and pass the
directory on every deploy, since the files and `STIGMERGY_LEGAL_DIR` are deploy arguments rather
than `fly.toml` settings:

```bash
STIGMERGY_LEGAL_SOURCE=/path/to/legal STIGMERGY_PARITY_GATE=off make deploy-staging
```

The script copies each `*.md` onto every machine under `/srv/legal`. `/legal/<name>` returns a
page's HTML, raw HTML escaped; with a landing page on, `/<name>` serves the landing, which can
fetch and render it.

## 8. First operator, workspace, person, and question

**Operator.** Create the first platform operator once:

```bash
fly ssh console --app <app> -C "stigmergy-admin operator bootstrap you@example.com \
  --name 'Your Name'"
```

It prints one token, once, and refuses to run when an operator exists. The token opens the master
console at `https://<host>/admin` and expires after 90 days; to issue another, run
`stigmergy-admin operator mint <email>` the same way. If `fly ssh console` finds no running
machine, open `/health` first: the `app` machine stops when idle and starts on a request.

**Workspace.** In the master console, create a workspace: the company, the workspace name (its
slug: 2 to 32 lowercase letters, digits, or hyphens, starting with a letter, and none of `admin`,
`api`, `app`, `mcp`, `platform`, `status`, `www`), and the first administrator's email and name.
The console shows that administrator's token once. The writer creates `<org>/<slug>-brain` with a
`README.md`, `CLAUDE.md`, `.gitignore`, and empty folders; the workspace then turns active and its
index is built on the writer's next pass. To use Slack with this workspace, create it with the
command line instead (section 9).

**Person and token.** Sign in at `https://<host>/admin` with the administrator's token. In
**People**, create a person or an agent, give them layers and a default layer, and issue a token;
it is shown once and only its SHA-256 is kept. Layers are described in
[`OPERATIONS.md`](./OPERATIONS.md#layers).

**Bridge.** On the person's machine, install the bridge from the release you deployed:

```bash
uv tool install git+https://github.com/sturlese/stigmergy.git@v0.12.0
export STIGMERGY_TOKEN="<token>"
```

Claude Code, `.mcp.json`:

```json
{
  "mcpServers": {
    "stigmergy-bridge": {
      "command": "stigmergy-bridge",
      "args": ["--url", "https://<host>"],
      "env": { "STIGMERGY_TOKEN": "${STIGMERGY_TOKEN}" }
    }
  }
}
```

Codex, `.codex/config.toml`:

```toml
[mcp_servers.stigmergy-bridge]
command = "stigmergy-bridge"
args = ["--url", "https://<host>"]
env_vars = ["STIGMERGY_TOKEN"]
```

The bridge calls `https://<host>/mcp` with the token as a bearer and uploads local files through
presigned URLs. Private Google Drive documents also need `STIGMERGY_GOOGLE_CLIENT_SECRETS` (see
the [README](../README.md#from-claude-code-or-codex)).

**First capture and question.** Wait until the console shows the workspace active and the
backoffice **Index** view shows a full rebuild. Then, in Claude Code or Codex:

1. *"Save this to the brain: …"* with a few self-contained sentences calls `brain_submit`.
2. *"How are my brain submissions doing?"* calls `brain_submissions`; wait for `landed`.
3. Ask a question about what you saved; `ask` answers with citations.

The writer commits to the repository, GitHub sends the push to `/webhook/github`, and the app
indexes the changed pages. If `ask` cannot find the new page, check the App's **Advanced → Recent
Deliveries** on GitHub and the backoffice **Index** view (repository HEAD against the indexed
commit); a workspace's index can be rebuilt from its row in the master console. Then run the
checks under "After deployment, validate" in [`OPERATIONS.md`](./OPERATIONS.md#deployment).

## 9. Optional: Slack

One deployment serves one Slack workspace, bound to one Stigmergy workspace.

1. At <https://api.slack.com/apps>, create an app **from an app manifest** with
   [`deploy/slack-app-manifest.json`](../deploy/slack-app-manifest.json) and install it in your
   Slack workspace. The **Bot User OAuth Token** (`xoxb-…`) is `SLACK_BOT_TOKEN`.
2. Under **Basic Information → App-Level Tokens**, generate a token with `connections:write`
   (`xapp-…`): `SLACK_APP_TOKEN`.
3. The Slack workspace id (`T…`) is `SLACK_TEAM_ID`; `auth.test` returns it as `team_id`:
   `curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" https://slack.com/api/auth.test`.
4. Bind a Stigmergy workspace. The adapter serves the workspace whose `slack_team_id` equals
   `SLACK_TEAM_ID` and refuses to start otherwise. No console or backoffice control sets it, so
   create that workspace with the command line rather than the console:

   ```bash
   fly ssh console --app <app> -C "stigmergy-admin tenant create --slug <slug> --name '<Company>' \
     --repo <org>/<slug>-brain --slack-team-id <T…>"
   fly ssh console --app <app> -C "stigmergy-admin --tenant <slug> bootstrap you@example.com \
     --name 'Your Name'"
   ```

   The first registers the workspace for the writer to create its repository; unlike the console,
   it does not wake a sleeping writer, so start it if needed (`fly machine list --app <app>`, then
   `fly machine start <id> --app <app>`). The second prints the first administrator's token once.
5. `fly secrets set --app <app> SLACK_APP_TOKEN=… SLACK_BOT_TOKEN=… SLACK_TEAM_ID=…`.
6. Start the adapter with `STIGMERGY_SLACK_MACHINES=1 STIGMERGY_PARITY_GATE=off make
   deploy-staging`, and pass `STIGMERGY_SLACK_MACHINES=1` on every later deploy: without it the
   script scales `slack` back to zero.
7. In the backoffice **Slack channels** screen, map each channel to one layer, and invite the bot
   (`/invite @brain`) to every mapped channel, public or private.

Slack users are matched to people by their Slack profile email, which must be the email they have
in **People**; anyone else is refused. The workspace bound to Slack cannot be deleted from the
console or refiled. Check the granted scopes after installation as
[`OPERATIONS.md`](./OPERATIONS.md#slack-application) describes.

## 10. Upgrading and backups

To upgrade:

1. Read [`CHANGELOG.md`](../CHANGELOG.md) from your version to the new one. A release that changes
   the queue or index tables needs the clean-cut steps in [`OPERATIONS.md`](./OPERATIONS.md).
2. Move your `deploy` branch onto the new tag: `git fetch --tags && git rebase <new-tag>`.
3. Deploy as before, with the same `STIGMERGY_LEGAL_SOURCE` and `STIGMERGY_SLACK_MACHINES`.
4. Upgrade people's bridges after the server; to roll back, restore the bridge first.

What to back up:

- **Postgres** holds everything that is not in Git: workspaces, operators, people and token
  hashes, the queue, the change ledger, and audit. Use your host's backups (on Supabase, its
  backups or point-in-time recovery). The search index is rebuilt from Git.
- **GitHub** holds the knowledge; a clone of a workspace repository is a full copy. After a layer
  erasure, delete or clone again every copy, or the layer comes back.
- **The bucket** holds the only copy of every capture's original bytes; the platform makes none.

A clean non-production reset is [`RESET.md`](./RESET.md); erasing one workspace or the whole
deployment is [`ERASE.md`](./ERASE.md); what goes where is
[`DATA-PROTECTION.md`](./DATA-PROTECTION.md).
