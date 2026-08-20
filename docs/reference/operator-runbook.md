# Operator runbook

Everything below is for the operator running the system. The live world this runbook covers:
**three zones** in the knowledge repo (`wiki/` · `sources/` · `views/`), the librarian's
**8 gates**, **10 MCP tools** (`search_brain`/`read_page`/`list_entities`/`describe_entity`,
`ask`, `brain_submit`/`brain_submissions`/`brain_reply`, `review_queue`/`review_decide`),
**one Fly app** with three process groups (`app` · `slack` · `worker`), four GitHub Actions
crons (`index-rebuild` · `retention-purge` · `gardener` · `repair-propose`), the optional `/admin`
console on the `app` group, and the golden evals under `evals/` with the release gates
(`make gates`) over them.

Organized by OPERATION: Deploy · Wipe & re-seed · Capture from Drive · Index rebuild ·
Recovery · Revocation · Release gates & drills · Troubleshooting.

There is no read site: navigation happens through `read_page` and the entity tools
([ADR 022](../decisions/022-entity-navigation.md)).

## Deploy

### One-time setup (before the first deploy)

Nothing in this repo's scripts creates a cloud resource; all of them assume you did this once:

1. **Fly app** — `fly launch --no-deploy` (or `fly apps create <your-app>`), then edit
   [`fly.toml`](../../fly.toml). It ships with **placeholders**: `app = "CHANGEME-your-fly-app"`,
   `STIGMERGY_PUBLIC_HOST = "CHANGEME-your-fly-app.fly.dev"` and
   `STIGMERGY_LIBRARIAN_REPO_URL = "https://github.com/CHANGEME/stigmergy-brain.git"` all have
   to be replaced before anything deploys. Set `primary_region` nearest your Postgres, not
   nearest you. `STIGMERGY_PUBLIC_HOST` must equal the app's real hostname as a **bare hostname,
   no scheme** — see Troubleshooting for what breaks when these drift.

   Every `fly` command below writes `$FLY_APP` where your app name goes;
   `export FLY_APP=<your-app>` once per shell. (`make` derives it from `fly.toml` when unset.)
2. **Fly secrets** (never in `fly.toml`, never in this repo). The full inventory, by consumer:

   ```sh
   # the server (`app` group)
   fly secrets set STIGMERGY_INDEX_DSN="postgresql://...supabase.../stigmergy"
   # The READ path's two model keys, and which ones you set follows what you configured:
   # OPENAI_API_KEY serves both defaults (the embedder and `ask`); an EMBED_BASE_URL host takes
   # EMBED_API_KEY instead, and a provider-prefixed ANSWER_MODEL takes that provider's own key
   # (`openrouter:` -> OPENROUTER_API_KEY).
   fly secrets set OPENAI_API_KEY="sk-..."
   fly secrets set STIGMERGY_TOKEN_STORE='{"<sha256hex>": "steward@example.com"}'
   # the evidence plane (R2 — server AND worker read it)
   fly secrets set STIGMERGY_EVIDENCE_ENDPOINT="https://<account-id>.r2.cloudflarestorage.com"
   fly secrets set STIGMERGY_EVIDENCE_BUCKET="stigmergy-evidence-staging"
   fly secrets set STIGMERGY_EVIDENCE_ACCESS_KEY_ID="..."
   fly secrets set STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY="..."
   # the librarian worker (`worker` group)
   fly secrets set STIGMERGY_LIBRARIAN_APP_ID="123456"
   fly secrets set STIGMERGY_LIBRARIAN_INSTALLATION_ID="87654321"
   fly secrets set STIGMERGY_LIBRARIAN_PRIVATE_KEY="$(cat ~/.config/stigmergy/librarian.private-key.pem)"
   # Only if your App is not named `stigmergy-librarian` — its slug is what the bot commits as.
   fly secrets set STIGMERGY_LIBRARIAN_APP_LOGIN="my-librarian"
   # The FILING model's own provider key — whichever STIGMERGY_LIBRARIAN_MODEL names
   # (`anthropic:` -> ANTHROPIC_API_KEY, `openrouter:` -> OPENROUTER_API_KEY,
   # `google-gla:` -> GEMINI_API_KEY). A missing one is refused at startup, by name.
   fly secrets set ANTHROPIC_API_KEY="sk-ant-..."
   # OPTIONAL, worker only: the Drive door's OCR fallback for a scanned PDF. Two forms: this key
   # serves the bare Gemini VISION_MODEL; a provider-prefixed VISION_MODEL secret
   # ("openrouter:qwen/qwen3-vl-8b-instruct", with OPENROUTER_API_KEY) OCRs rasterized pages
   # instead. Neither set means a scanned deck refuses honestly (see "Capture from Drive").
   fly secrets set GEMINI_API_KEY="..."
   # the Slack transport (`slack` group) — or `make slack-secrets` to stage all three from .env
   fly secrets set SLACK_APP_TOKEN="xapp-..."
   fly secrets set SLACK_BOT_TOKEN="xoxb-..."
   fly secrets set SLACK_TEAM_ID="T..."
   # the incremental index webhook (`app` group) — the KNOWLEDGE repo, the one being pushed to
   fly secrets set STIGMERGY_GITHUB_WEBHOOK_SECRET="$(openssl rand -hex 32)"
   fly secrets set STIGMERGY_GITHUB_REPO="<owner>/stigmergy-brain"
   # the admin console (`app` group, ADR 029) — OPTIONAL; unset = the console does not exist.
   # Hash from `stigmergy-admin-token`; PAT = fine-grained, Actions read+write, one repo only.
   # STIGMERGY_ADMIN_GITHUB_REPO is WHEREVER THE CRON WORKFLOWS RUN, which is the knowledge repo
   # (see step 4) — so in practice it holds the same value as STIGMERGY_GITHUB_REPO above, and
   # the PAT must be scoped to THAT repo. They are still two settings because they answer two
   # questions (which repo is pushed to vs which repo's Actions the console drives), and there is
   # no default: without it the crons tab is read-only. The digest channel id is not sensitive;
   # secrets are simply Fly's env mechanism.
   fly secrets set STIGMERGY_ADMIN_TOKEN_HASH="<from stigmergy-admin-token>"
   fly secrets set STIGMERGY_ADMIN_GITHUB_TOKEN="<fine-grained PAT>"
   fly secrets set STIGMERGY_ADMIN_GITHUB_REPO="$STIGMERGY_GITHUB_REPO"   # same repo as above
   fly secrets set STIGMERGY_DIGEST_CHANNEL_ID="C..."
   ```

   **Every secret here is read once, at process startup** — `STIGMERGY_TOKEN_STORE` included, which
   is why revoking a token is not instantaneous (see Revocation). A plain `fly secrets set`
   triggers the deploy that applies it; `make slack-secrets` uses `--stage` instead, so
   `make slack-secrets && make deploy-staging` is one rollout, not two.
3. **Supabase Postgres** must already have the index built at least once
   (`.venv/bin/stigmergy-index --rebuild --repo $STIGMERGY_REPO` against `STIGMERGY_INDEX_DSN`) — the
   server refuses to serve an empty index.
4. **GitHub Actions secrets and variables** (for the four crons), on the **knowledge repo**:

   **The crons run from the knowledge repo, not from this one, for privacy.** Actions logs on a
   PUBLIC repository are world-readable, and these jobs describe the corpus out loud —
   `stigmergy-gardener` prints its whole report, entity ids and page paths included, and
   `stigmergy-repair propose` names every page it proposed an edit against. Repository *variables*
   are not masked either (only secrets are). This repo carries the four workflow files as adopter
   templates, **disabled**; copy them into your private knowledge repo and run them there.

   | Settings → Secrets → Actions | Used by |
   |---|---|
   | `INDEX_DSN` | all four workflows |
   | `OPENAI_API_KEY` | `index-rebuild`, `gardener`, `repair-propose` |
   | `SLACK_BOT_TOKEN` | `gardener`, for the SLA notice — and nothing else: the proposer posts nowhere, and it holds no GitHub App credential either, because it proposes and cannot apply |

   | Settings → Variables → Actions | Used by |
   |---|---|
   | `STIGMERGY_CRONS_ENABLED` (`true`) | all four — **and it is the on/off switch** |
   | `STIGMERGY_PLATFORM_REF` (a release tag; default `main`) | all four — which platform version `pip` installs |
   | `STIGMERGY_DIGEST_CHANNEL_ID`, `STIGMERGY_GARDENER_MODEL` | `gardener` (a channel id and a model name are not credentials) |
   | `STIGMERGY_REPAIR_MODEL` | `repair-propose` (a model name, for the same reason: not a credential, and not masked — a second argument for a private repo) |
   | `STIGMERGY_PLATFORM_REPO` (default `sturlese/stigmergy`) | all four — **set it if you forked.** Leave it unset on a fork and every cron silently `pip install`s the UPSTREAM CLI, so your crons run somebody else's code against your knowledge |

   **No cross-repo PAT is involved.** The knowledge repo is the workflow's own repository, so the
   job's read-only `GITHUB_TOKEN` covers the checkout; the CLI arrives by
   `pip install git+https://github.com/<owner>/stigmergy.git@$STIGMERGY_PLATFORM_REF`.

   **Every scheduled job is guarded by `if: vars.STIGMERGY_CRONS_ENABLED == 'true'` and skips
   cleanly when it is unset.** That is also the failure mode to check first when "the crons stopped
   running": a skipped job is green, so read `job_runs`, not the Actions tab (below).
5. **R2 bucket** + scoped API token in the Cloudflare dashboard, and the lifecycle rule
   `evidence-retention-30d` (delete after 30 days — the physical floor behind the queue purge's
   own `DEFAULT_RETENTION_DAYS`). Verify credentials with the smoke check:

   ```sh
   export R2_ENDPOINT_URL="https://<account-id>.r2.cloudflarestorage.com"
   export R2_ACCESS_KEY_ID="..."
   export R2_SECRET_ACCESS_KEY="..."
   export R2_BUCKET="stigmergy-evidence-staging"
   make r2-smoke        # put+get+delete one throwaway object; prints `r2-smoke: OK`
   ```
6. **GitHub App** (`stigmergy-librarian`) — Contents: Read and write, installed on the **knowledge**
   repo only. Its variables are in [librarian.md](./librarian.md#configuration):
   `STIGMERGY_LIBRARIAN_APP_ID`, `STIGMERGY_LIBRARIAN_INSTALLATION_ID` and
   `STIGMERGY_LIBRARIAN_PRIVATE_KEY` (or `STIGMERGY_LIBRARIAN_PRIVATE_KEY_FILE`) — **all three or
   none**. Locally they live in the gitignored root `.env`, which `make` loads and a directly
   invoked binary inherits nothing from — use the `make` targets for anything that files.
7. **Slack App** — Socket Mode, three secrets, four event subscriptions, the App Home Messages Tab
   toggle. The exact list, and the Web API methods the scopes have to cover:
   [slack.md](./slack.md#what-the-slack-app-has-to-be-configured-with).

### The deploy loop

```sh
make deploy-staging          # = scripts/deploy_staging.sh, STIGMERGY_REPO defaults to ../stigmergy-brain
```

Bakes **four** files out of `$STIGMERGY_REPO/ops/` into the `deploy/` directory, which the
Dockerfile then `COPY`s to `/app/`:

| Baked from | To | Missing in the knowledge repo |
|---|---|---|
| `ops/identities.json` | `/app/identities.json` | **the script exits 2** — this is the one required file |
| `ops/entity-registry.json` | `/app/entity-registry.json` | `{"entities": {}}` — `ask` searches without entity-first resolution. The baked copy is the FALLBACK: a server whose index carries a registry snapshot (the webhook refreshes it on every push that touches the file) answers from that instead |
| `ops/slack-channels.json` | `/app/slack-channels.json` | `{}` — every audience falls back to the safe empty default |
| `ops/stewards.json` | `/app/stewards.json` | `{}` — no scope resolves to a steward, so the doorbell records an undeliverable and every review decision fails closed (see Troubleshooting) |

All four are always written, so the unconditional `COPY` can never fail on a missing source.

**`deploy/` is TRACKED, not gitignored.** The `COPY`s are unconditional, so a fresh clone has to
build, so the four files are committed as EMPTY defaults (`{}` · `{"entities": {}}` · `{}` · `{}`).
What the script bakes over them is a whole deployment's identity roster, one `git add -A` from being
published, so it restores the committed defaults on the way out through an EXIT trap that fires on
**every** path out, a failed `fly deploy` included. `tests/test_deploy_defaults.py` holds both
halves: that `fly deploy` saw the real files, and that nothing but the defaults outlived the script.
If you ever find real data under `deploy/`, restore the empty defaults before committing.

The script touches **only those four names**, never the `deploy/` directory itself, because
`deploy/` holds tracked files it does not bake — `workflows/` today, whatever is added next
tomorrow. It used to clear the directory outright, and since the EXIT trap knew how to rebuild the
four JSON files and nothing else, one `make deploy-staging` deleted the four files under
`deploy/workflows/` from the working tree; a routine `git add -A` afterwards would have committed
their removal. The delete set and the restore set are now derived from one list in the script, so
they cannot drift apart again.

Then it runs `fly deploy` (one image, all three process groups) and pins both singleton groups:
`fly scale count slack=1 --yes` — Socket Mode has no leader election and `fly deploy` creates two
machines by default for a NEW process group — and `fly scale count worker=1 --yes`, because the
worker's default second machine is a standby one `fly machine start` away from a second paid poller
that nothing refuses. The trade is deliberate: with no standby, a worker host failure stalls queue
draining until an operator redeploys or starts a machine. The knowledge repo's `ops/` stays the
single source of truth; the script takes a deploy-time snapshot, which is why a scope change needs a
redeploy (see Revocation).

**A deploy is not complete without two verifications**:

1. **A release carrying an index schema change rebuilds the index right after the deploy —
   never wait for the nightly cron.** Until the index catches up, every `ask`/`search_brain`
   fails `UndefinedColumn`. After any deploy whose diff touches `index/store.py`'s DDL or the
   columns `index/corpus.py` parses: `gh workflow run index-rebuild.yml` (or
   `make rebuild-staging`), before calling the deploy done.
2. **The deploy check ends with ONE real `ask`, end to end.** `fly status` showing every
   process group healthy proves nothing about the read path a schema-skewed index breaks
   silently.

### Configuration: what changing something takes

Four different mechanisms carry configuration into a running process, and they answer "what do I
do to change this" differently enough that guessing costs a redeploy or a silent no-op.

| Kind | Example | What changing it takes |
|---|---|---|
| Baked into the image at deploy time | `ops/stewards.json` — the copy `fly.toml` points the `app` group at (`--stewards`); the fallback copies of the three cached files below are baked too, but only answer where the index has no snapshot | a commit and a push in the knowledge repo, then `make deploy-staging` to re-bake `deploy/` and redeploy |
| Cached in the index, refreshed by the push webhook | `ops/entity-registry.json`, `ops/identities.json`, `ops/slack-channels.json` — `ops_file_snapshot` rows every process group reads through its database connection. The console's Index panel shows each file's freshness and source sha | a commit and a push — no deploy; the webhook writes each pushed file within seconds (fetched at the branch ref, so replays are inert), and the nightly index rebuild reconciles per file — it never clears the two access files over an absent checkout copy |
| A Fly secret, read once at process startup | `STIGMERGY_TOKEN_STORE`, `OPENAI_API_KEY`, `STIGMERGY_INDEX_DSN`, the `STIGMERGY_EVIDENCE_*` group, the librarian App triple, `ANTHROPIC_API_KEY`, `STIGMERGY_ADMIN_TOKEN_HASH`/`STIGMERGY_ADMIN_GITHUB_TOKEN`, the three `SLACK_*` tokens, `STIGMERGY_GITHUB_WEBHOOK_SECRET` | `fly secrets set …` — triggers the redeploy that applies it; effective once the new machines are healthy, not before |
| A non-secret env var in `fly.toml`'s `[env]`, app-wide | `STIGMERGY_LIBRARIAN_TIMEOUT_S` — the worker's per-item agent budget, and what its lease (`config.resolved_visibility_timeout_s`) is derived from | edit `fly.toml`, then `make deploy-staging`/`fly deploy` — every process group's machines restart on the new value together. The admin console re-derives the DEPENDENT lease fresh on every request rather than caching a class default, so its meter and Reclaim horizon can never disagree with the worker's own once the new machines are up — see "A dead worker mid-item" below |
| Committed to the knowledge repo, read at a base commit, wherever a checkout exists | `ops/acl.json`, `ops/entity-registry.json` and the contract linter (the WORKER's own reads — distinct from the `app`/`slack` groups' baked copies above), `ops/stewards.json` (same distinction, for the worker and any locally-run server passed `--repo`) | a commit and a push — no deploy; picked up at the very next item the worker claims, or the very next decision a checked-out server resolves |

The rows that name `ops/stewards.json` are one file read two ways: on the deployed `app`/`slack`
groups a steward's authority is a deploy-time snapshot, while the worker (and any process holding a
checkout) sees a push immediately. See Revocation. `ops/entity-registry.json` used to work that way
too and no longer does — a mint that pushed a new entity left `describe_entity` serving it with no
name and no aliases until the next deploy, so the registry moved into the index where the webhook
can refresh it for every group at once.

### The three process groups

| Group | Command | Exposed |
|---|---|---|
| `app` | `stigmergy-server --transport http …` | the public HTTPS service (+ `/webhook/github`) |
| `slack` | `stigmergy-slack` (Socket Mode) | nothing — outbound socket only |
| `worker` | `stigmergy-librarian-boot` | nothing — no ports |

```sh
fly status -a $FLY_APP                  # PROCESS column: expect app, slack, worker
fly logs -a $FLY_APP                    # everything
fly logs -a $FLY_APP -i <machine>       # one machine (fly status lists them by group)
fly machine stop <worker machine id>                 # drain and stop the worker only
fly scale count worker=0 -a $FLY_APP    # ...or take the group down entirely
fly scale count worker=1 -a $FLY_APP    # only if a deploy did not create it
```

Four standing rules:

- **`STIGMERGY_LIBRARIAN_BACKEND` must say `pydantic`, and its default does not.** The default is
  `double`, the offline test double: on ordinary material it fabricates a plausible page with no
  model involved at all. `fly.toml` sets `pydantic` explicitly, together with the
  provider-prefixed `STIGMERGY_LIBRARIAN_MODEL` it requires; a deployment assembled any other way
  inherits `double` and looks perfectly healthy while committing invented knowledge — the one
  configuration mistake here whose symptom is *pages that read fine*.
- **A deployment carrying `sdk` is refused, not silently downgraded.** The value survives in a
  `fly.toml` or a `.env` that a `git pull` does not touch. The worker refuses at startup naming
  the two edits it takes (this variable AND a provider-prefixed model id) and the image rollback
  below. The queue is durable, so nothing is lost while it is down.
- **Never scale `slack` past 1.** No leader election; a second machine double-handles every
  event. The deploy script pins it, and the advisory lock
  (`stigmergy.slack.app.acquire_singleton_lock`) refuses a second process at startup. The lock is
  per DATABASE: a local bot on the docker Postgres and a staging bot on Supabase hold different
  locks — stop the local bot before staging goes live.
- **`fly scale count worker=2` does NOT give you two workers.** Fly creates a *standby* for a
  group with no service — `fly status` marks it `†`, it sits `stopped` and claims nothing, but
  the count includes it. Two workers actually draining is `worker=3`. Read the STATE column,
  never the count. On a pinned deployment the standby does not exist at all.
- **The kill window is shorter than one item.** Fly caps `kill_timeout` at 300s; one item's
  worst case on the deployed worker is 1710s (two agent attempts at the deployed
  `STIGMERGY_LIBRARIAN_TIMEOUT_S=600`, plus 120s for the gates, the commit and the push, plus
  390s for a drive conversion — a scanned deck rasterizes and OCRs before its first agent pass).
  A deploy
  or `fly machine stop` SIGTERMs (the worker stops claiming and exits at the next terminal
  state), but an item still running at 300s is SIGKILLed — the row returns after the derived
  1890s visibility timeout with an attempt burned, and the next worker files it. Drain first if
  you would rather not exercise that: `make librarian-status` shows what is in flight.

The first worker line after a deploy is the one worth reading:

```
stigmergy-librarian-boot: /home/app/knowledge is at origin/main@<sha>
filing into /home/app/knowledge against origin/main@<sha>
```

A refusal instead names which of three things was wrong: the fetch did not reach the remote (a
revoked/expired App installation), `HEAD` is not that commit, or the checkout is dirty.

### Rollback

```sh
fly releases                          # find the last-good release/image
fly deploy --image <that image ref>   # redeploy it directly
```

(Fly also has `fly apps rollback` on recent CLI versions — either is a single command.)

### Scheduled jobs — the four crons

| Workflow | When (UTC) | What |
|---|---|---|
| `index-rebuild.yml` | nightly ~04:17 | full staging index rebuild from `@main` |
| `retention-purge.yml` | nightly ~04:42 | `stigmergy-queue purge` (30-day terminal rows) |
| `gardener.yml` | daily ~05:07 | `stigmergy-gardener` corpus-health run |
| `repair-propose.yml` | daily ~06:07 | `stigmergy-repair propose` — the gardener's findings turned into proposals a steward decides |

The last one runs an hour behind the gardener so the findings it reads are that morning's. It
**proposes and cannot apply**: it holds no Slack token and no GitHub App credential, and every
proposal lands PENDING until a steward approves it one at a time, in the console's Repairs tab or
over MCP's `review_decide` ([repair.md](./repair.md)).

They run from the **knowledge repo's** own `.github/workflows/`; this repository ships them as
templates in [`deploy/workflows/`](../../deploy/workflows/README.md), outside `.github/` so that
GitHub does not register them here.

All four have `workflow_dispatch` for a manual run (`gh workflow run index-rebuild.yml`, etc.;
`retention-purge.yml` is the only one taking an input, `dry_run`) — and the admin console's Crons
tab drives the same dispatch/enable/disable with buttons when its GitHub PAT and repo are
configured ([admin-console.md](./admin-console.md)).
`retention-purge`, `gardener` and `repair-propose` each write a `job_runs` row; `index-rebuild`
writes none and its truth is `index_meta.built_at` instead. The Actions tab cannot tell you a
scheduled job stopped (a job skipped for an unset `vars.STIGMERGY_CRONS_ENABLED` is *green*). The
database can:

```sql
SELECT job, status, finished_at, stats FROM job_runs
ORDER BY started_at DESC LIMIT 20;
SELECT built_at FROM index_meta;                 -- index-rebuild's only trace
SELECT * FROM ingest_errors WHERE NOT resolved ORDER BY last_at DESC;
```

`stigmergy-digest` is deliberately NOT on a cron ([ADR 026](../decisions/026-the-purge.md) D6): run
`.venv/bin/stigmergy-digest --repo $STIGMERGY_REPO` (or `--dry-run`) by hand; its watermark means each
post covers exactly the window since the previous one.

### The incremental index webhook

`POST /webhook/github` lives on the `app` process group — the ONE path exempt from the bearer
middleware, by exact match, because it authenticates differently (HMAC over the raw body). Two Fly
secrets (above: `STIGMERGY_GITHUB_WEBHOOK_SECRET`, `STIGMERGY_GITHUB_REPO`; optional
`STIGMERGY_GITHUB_BRANCH`, default `main`, and `STIGMERGY_GITHUB_WEBHOOK_FILE_CAP`, default `50` — a
push touching more files than the cap is left to the nightly rebuild), plus one webhook in the
**knowledge** repo's GitHub Settings → Webhooks:

- **Payload URL**: `https://$FLY_APP.fly.dev/webhook/github`
- **Content type**: `application/json`
- **Secret**: the exact `STIGMERGY_GITHUB_WEBHOOK_SECRET` value
- **Events**: "Just the push event"

Until the secret is set, the endpoint is inert — every request fails the signature check with
the same generic `401`.

One residual now that the `app` machine auto-stops when idle (`fly.toml`'s `[http_service]`):
GitHub's webhook sender waits ~10 seconds and does not retry, so a delivery landing during a cold
start can be dropped. The nightly `index-rebuild` reconciles the index regardless, and a dropped
delivery is visible in GitHub's Recent Deliveries panel (redeliverable by hand) and as a gap in
`webhook-index-upsert` job runs. Verify it lands:

```sql
SELECT stats, finished_at FROM job_runs WHERE job = 'webhook-index-upsert'
ORDER BY started_at DESC LIMIT 5;
```

### The admin console

`/admin` on the `app` process group (ADR 029) — the daily loop (queue drain, crons, gardener,
repairs, digest, index, activity) in a browser instead of a terminal. Inert 404s until
`STIGMERGY_ADMIN_TOKEN_HASH` is set; everything it can do, each degraded mode and the rotation
drills are in [admin-console.md](./admin-console.md). It is management-only: nothing on it reads
the corpus.

## Wipe & re-seed

### What a wipe destroys — the queue is durable, the index is not

`stigmergy-index --rebuild` drops and recreates `pages_index` only. `capture_queue`, `audit_log`,
`job_runs` and `ingest_errors` survive it and cannot be rebuilt from git — a queued capture
exists nowhere else until the librarian files it.

> **`make db-down` destroys the local queue AND the local index**, and so does anything that
> runs `docker compose down -v` — which is **`make e2e`, `make e2e-write`, `make e2e-librarian`
> and `make e2e-librarian-container`**, every one, on the way in *and* the way out. The
> composition has no named volume by design (the index is a disposable cache; every e2e starts
> from empty state). Drain the queue before running an e2e. Staging's Postgres is the durable one;
> the index comes back with `make index-rebuild` (~1 min), the queue does not come back.

### The two databases

`make db-up` starts one Postgres (loopback, `localhost:54321`) with **two** databases:

| Database | Who uses it | What lives there |
|---|---|---|
| `stigmergy` | the running brain — MCP servers, `stigmergy-queue`, `stigmergy-index` | the derived index **and** the durable queue: real captures |
| `stigmergy_test` | `make test` and CI, nothing else | fixture rows, wiped constantly |

A running brain reads `$STIGMERGY_INDEX_DSN`; the suites read `$STIGMERGY_TEST_DSN` and never the
other. Pointing a fixture at `stigmergy` is refused by name (`tests.testdb.WrongDatabase`) with
no override flag — the suites truncate `capture_queue` at setup.

`stigmergy_test` is created by `scripts/postgres-init/01-test-database.sql`, which Postgres runs
once, on a fresh data directory. A container that predates the file never ran it; the suites
fail loudly rather than skip, and the fix is:

```sh
make db-down && make db-up      # WIPES the local Postgres, dogfood captures included
```

### Local wipe → re-seed

```sh
make db-up                                   # postgres+pgvector + minio (console: http://127.0.0.1:9001)
make index-rebuild                           # LOCAL index from $STIGMERGY_REPO with the real embedder
make index-rebuild EMBEDDER=fake             # ...keyless (the deterministic double — answers nothing usefully)
```

MinIO comes back empty too; staging's evidence plane is R2 and is untouched by anything local.

### Re-seeding corpus content

The corpus lives in the knowledge repo's three zones and is re-seeded by COMMITS there, never by
anything in this repo. Every zone has a closed set of writers:

| Zone | Written by |
|---|---|
| `wiki/notes,decisions,concepts/` | the librarian, filing a capture through the eight gates |
| `wiki/meetings/` + `sources/meetings/` | the meeting flow, from a `stigmergy-meeting drop` |
| `sources/slack/`, `sources/drive/` | the librarian's source attachment, from the 🧠 gesture and `stigmergy-drive drop` |
| `wiki/entities/` (+ `ops/entity-registry.json`) | `stigmergy.entities` **only** — a steward's own commit from the CLI, or a server-driven mint from MCP, Slack or the console ([ADR 030](../decisions/030-server-side-entity-minting.md)) |
| `views/` | `stigmergy.views` **only** — either `stigmergy-views regenerate` by hand, or the librarian's best-effort trigger right after a meeting files |

After any bulk re-seed: rebuild the index (next section) and run `make index-check`.

## Capture from Drive

### `stigmergy-drive drop` — the Drive door

One command, from YOUR terminal, with YOUR Google auth ([ADR 028](../decisions/028-drive-door.md)
— no Google credential exists server-side; the worker converts from the evidence blob and never
talks to Drive):

    stigmergy-drive drop '<share-URL-or-file-id>' [--submitted-by you@example.com] \
                       [--allow-split-stores]

**The door refuses a queue and a store on different deployments** (exit 3, before any fetch or
upload): a remote `STIGMERGY_INDEX_DSN` with a loopback evidence endpoint files a row whose bytes
the deployed worker can never read. That is the shape `set -a; source .env` produces, because
this repo's `.env` keeps the bucket under `R2_*` names while the code reads
`STIGMERGY_EVIDENCE_*`. Export the deployment's own evidence group, or pass `--allow-split-stores`.

Requirements, all local: `gog` installed and authenticated (`brew install steipete/tap/gogcli`,
`gog auth add` — check with `gog auth list`), and the same DSN/evidence environment every other
operator CLI uses (`STIGMERGY_INDEX_DSN` + the `STIGMERGY_EVIDENCE_*` group for staging; compose
defaults with none set). `--submitted-by` defaults to `$STIGMERGY_MEETING_OPERATOR_EMAIL`.

**What it does — and nothing more**: resolves the file, refuses what the door does not convert,
downloads (a native Google Doc/Slide/Sheet is exported to PDF by Drive itself), uploads the ORIGINAL
BYTES to evidence, and enqueues exactly ONE `kind="drive"` row whose material is a deterministic
manifest. No model, no conversion at the door. The worker extracts the text (pdftotext first; one
bounded vision OCR pass for a scanned PDF when the worker has one configured — `GEMINI_API_KEY`
for the bare Gemini model, or a provider-prefixed `VISION_MODEL` with its provider's key; the
prefixed form transcribes at most the first 40 pages and says in-line where it cut; OPTIONAL,
neither configured means scanned decks refuse honestly) and the librarian files ONE synthesis page plus the verbatim
`sources/drive/` part(s), atomically, anchored-or-asked-or-parked like every capture.

**The format policy**: pdf · txt/md/json · xlsx/xls/csv/tsv · docx · any native Google file. An
office binary (pptx/ppt/doc/odt/odp/ods/rtf) is refused naming its wake condition: the `office`
conversion path works wherever `GOTENBERG_URL` points at a Gotenberg container, and
`docker-compose.yml` runs none (the code's default `http://gotenberg:3000` resolves to nothing
there), so such a document fails conversion until you stand one up. Files over 25 MB are refused at
the door.

**Reading the result**: `stigmergy-queue show <id>` — a `failed` row naming the `conversion`
stage is the extraction refusing (scanned + no OCR, empty text, over the material cap, corrupt
file); the operator's log has the detail the wire omits. The filed source page carries `url:` → the
Drive link (the binary stays in Drive: door, never mirror) and its explicit chain `id:`.

**The agent budget for documents**: the deployed worker runs with
`STIGMERGY_LIBRARIAN_TIMEOUT_S=600` (fly.toml), because a figure-dense deck overlapping an entity
with existing pages exceeds the paste-sized 300s default. A `failed` row saying "the agent exceeded
its NNNs budget" on a drive capture means the pass ran long, not that the capture is bad — re-drop
it (identical bytes dedup only against FILED rows). The visibility timeout derives from this value.

## Index rebuild

### Local

```sh
make index-rebuild                                     # $STIGMERGY_REPO (default ../stigmergy-brain), real embedder
.venv/bin/stigmergy-index --rebuild --repo $STIGMERGY_REPO    # the same, bare (env: OPENAI_API_KEY, STIGMERGY_INDEX_DSN)
```

### Staging

```sh
make rebuild-staging                  # needs STAGING_DSN in .env (deliberately NOT STIGMERGY_INDEX_DSN)
gh workflow run index-rebuild.yml     # ...or trigger the nightly workflow manually
```

The running Fly machine picks up the new index on its next query — no restart, no redeploy (the
server holds no copy of the index). Check the `built_at` field on any `search_brain`/`ask` response
to confirm freshness. A filed page becomes searchable at the next rebuild or at the webhook's
incremental upsert, whichever lands first.

### The substrate lint

```sh
make index-check                                      # exit 1 on any ERROR finding
.venv/bin/stigmergy-index --check --repo $STIGMERGY_REPO     # the same, bare (--repo locates the registry)
```

Deterministic SQL over the LIVE `pages_index` (`src/stigmergy/index/check.py`):

- **ERROR** (exit 1): duplicate `page_id` · orphan continuation part · missing embedding /
  empty tsv (a page invisible to one arm).
- **WARN** (exit 0): dangling `superseded_by` · **anchored-but-unregistered entity** — an
  `entity:` value with no registry record still resolves for navigation but gets no aliases, no
  entity-first search, no TOLD boost. This is your early signal for registry gaps (see Recovery).

`scripts/e2e.sh` ends with this same check, run where the index just got built. Quick retrieval
probe, any time:

```sh
.venv/bin/stigmergy-search "what did we decide about Q3 pricing"
```

## Recovery

### A malformed `ops/entity-registry.json` HARD-FAILS every `ask`

The registry loader (`src/stigmergy/server/entity_aliases.py`) is deliberately asymmetric:

- **No registry at all → fail-open**: no aliases, no entity-first resolution, `ask` still answers
  from semantic search. Not an incident.
- **Malformed JSON (or a top level that is not `{"entities": {...}}`) → raises.** Every
  registry consumer — `ask`'s entity-first resolution, `list_entities`'s enrichment,
  `describe_entity`'s entity layer — hard-fails loudly rather than degrading silently.

**Symptom**: every `ask` (and `list_entities`/`describe_entity`) starts erroring at once, within
seconds of a push that touched `ops/entity-registry.json` — the push webhook caches that file into
the index, and the server reads the cache in preference to the copy baked at deploy time.

**Check** (the registry is plain JSON; one line settles it):

```sh
python3 -c 'import json,os; json.load(open(os.environ["STIGMERGY_REPO"]+"/ops/entity-registry.json")); print("ok")'
```

**Fix**: `git revert` the registry commit in the knowledge repo (or fix the JSON by hand and
commit) and **push** — the webhook caches the corrected file within seconds, no deploy needed. If
the webhook is not delivering, `make index-rebuild` from a checkout writes the same snapshot, and
clearing it (a rebuild from a repo with no registry) drops the server back to its baked
`--entity-registry` copy. `stigmergy-entities` is the only thing that writes this file; a hand edit
is the usual way it got malformed.

**Prevention**: `stigmergy-index --check --repo $STIGMERGY_REPO` warns on anchored-but-unregistered
entities — run it after registry changes; it raises on malformed registry JSON too. It lints the
copy the SERVER serves, so against a database carrying a snapshot the `--repo` file is not what it
reads (`--rebuild` is what makes a checkout's registry the served one). Its findings
name paths and page_ids of EVERY page, ACL-restricted ones included, so treat the output as scoped
material and don't paste it into shared trackers.

### A dead worker mid-item — lease redelivery

A dead worker costs one delivery, never a capture: the row returns to `queued` after the visibility
timeout (1290s default) with `attempts` incremented, and the next worker files it. Observe and force:

```sh
.venv/bin/stigmergy-queue list                              # depth per status + newest submissions
.venv/bin/stigmergy-queue show 7                            # one submission's trace and latencies
.venv/bin/stigmergy-queue reclaim --visibility-timeout 0    # release EVERY claimed row, right now
.venv/bin/stigmergy-queue reclaim --visibility-timeout 1290  # ...only ones past a 1290s lease
```

`--visibility-timeout` is mandatory on `reclaim` and the command refuses without it: it decides
how dead a worker must be before its work is taken away, and the CLI cannot see the lease that
worker actually holds — a shorter horizon requeues captures out from under processes still filing
them. Pass `0` when you know the worker is gone, or the worker's own lease otherwise. That lease is
DERIVED from the per-item budget rather than fixed: 1290s at the default
`STIGMERGY_LIBRARIAN_TIMEOUT_S`, **1890s on the deployed worker**, which runs at 600 (the extra
390s in both is the drive conversion's bounded worst case — the kernel's three vision clocks).
`stigmergy-librarian status --json` prints the resolved `visibility_timeout_s` for the environment
it is run in — read it there rather than assuming the default.

The admin console's Reclaim button states **the same derived lease**, resolving
`STIGMERGY_LIBRARIAN_TIMEOUT_S` through the worker's own arithmetic per request, so the Worker tab's
meter and the button agree with `status --json` (1890s on staging). Two conditions make that hold:
`fly.toml`'s `[env]` is app-wide, so the console process reads the variable the worker resolved; and
the worker command passes no `--visibility-timeout`, which would beat the derivation and which the
console cannot see. It does **not** hold in the local composition, where `docker-compose.yml` gives
the librarian its own environment block and no console runs beside it.

This is drill 2 of "Release gates & drills" below.

### Draining parked rows

`stigmergy-queue list` says, per parked row, who is being waited on and for how long: a
`needs_input` row is the submitter's; a `triage` row is yours.

```sh
stigmergy-queue requeue <id> --by <who> [--note "…"]        # back to the librarian
stigmergy-queue resolve <id> --by <who> --note "…" [--page <path>] [--commit <sha>]
stigmergy-queue reject  <id> --by <who> --reason "…"
```

All three refuse a row a worker currently holds and a row already terminal, and all three require
`--by` (attribution, recorded and never checked). The three text fields are NOT interchangeable:

- **`resolve --note` and `reject --reason` become the submitter's own report, verbatim.** They
  pass no secrets/PII gate on the way — no credentials, no personal data.
- **`requeue --note` is for the row's own history and is never shown to the submitter.**
- `resolve --page` / `--commit` are echoed to the submitter; leave both empty and their report has
  no pointer.

Use `resolve`, not `reject`, when you actually used the material.

A `triage` row that is an **identity question** mints through `stigmergy.entities` whichever of four
roads approves it ([ADR 030](../decisions/030-server-side-entity-minting.md)):

| Road | Right when | Needs |
|---|---|---|
| `stigmergy-entities`, below | you have a clone and no deployment has to be running, or you are scripting | your own clone + push identity, gitleaks |
| The admin console's Entities tab | you are already in the browser | the console enabled ([admin-console.md](./admin-console.md)) |
| Slack's doorbell card | the doorbell already DMed you | the deployed Slack app, nothing extra |
| MCP's `review_decide` | an agent session is already open | a caller token with steward status |

Whichever road you take, the ledger row records it (`review_decisions.extra->>'source'` is one of
`cli`/`admin`/`slack`/`mcp`), and the doorbell card already DMed for that item closes itself on the
next poll pass. What the road that arrives too late SAYS differs, though: MCP's `review_decide` and
the Slack card name who got there first, on which road and when; `stigmergy-entities` and the
console report the row's new state, which tells you the decision is gone without telling you whose
it was.

The ledger is also what closes a card, so this only holds for the identity decisions above: a
parked capture that is NOT an identity question, drained with `stigmergy-queue resolve`/`reject` or
from the console's Queue tab, writes no `review_decisions` row at all — its doorbell card is never
closed and simply ages out.

`stigmergy-entities` needs no server at all — it commits from YOUR OWN clone with YOUR OWN git
identity:

```sh
.venv/bin/stigmergy-entities list                       # parked rows waiting on an identity decision
.venv/bin/stigmergy-entities show 42                    # the material, the agent's reading, the exact next command
.venv/bin/stigmergy-entities approve 42 --id acme-corp --name "Acme Corp" --type organization \
    --aliases "Acme, ACME" --role "A logistics customer" --requeue
.venv/bin/stigmergy-entities reject 42 --reason "duplicate of an existing entity, different spelling"
```

`approve`/`create` require gitleaks (`brew install gitleaks`), refuse a drifted registry, and
never force-push. Registry/pages drift itself:

```sh
.venv/bin/stigmergy-entities regenerate --check         # drift check: exits non-zero, names the divergence
.venv/bin/stigmergy-entities regenerate                 # rewrite the registry from the pages (NOT committed)
```

**The other three roads mint from the server process instead of a steward's clone** — a throwaway
clone per request, pushed with the librarian App credential (`entities.remote.mint_via_clone`,
ADR 030 D3), never the operator's own identity. On the deployed `app` group that credential needs
no extra setup: `STIGMERGY_LIBRARIAN_REPO_URL` is a plain `fly.toml` `[env]` value, and the
librarian App triple set as Fly secrets for the `worker` group reaches `app` too, because Fly
secrets are app-wide. A server missing either — a local stdio MCP server, most often — refuses a
mint by naming exactly what is absent (`no knowledge-repo URL is configured for a server-driven
mint`, or `... needs the librarian GitHub App credential`) rather than degrading.

### A repair proposal stuck in `approved`

Every ordinary apply outcome writes itself down: it lands as `applied` with a commit, or as
`failed` with the reason. One residual cannot be written down from inside — the server process
dying between the `pending → approved` transition and the failure bookkeeping. The symptom is a row
that is `approved` with **both** `applied_commit` and `error` empty, and it is stuck: a steward
cannot decide it (it is no longer pending) and the proposer will not re-derive it (its key is
remembered while it is not `failed`).

```sql
SELECT id, decided_by, decided_at, target_paths
FROM repair_proposals
WHERE status='approved' AND applied_commit='' AND error='';
```

**Verify nothing landed before touching the row.** The apply may have pushed and died on the way to
recording it, in which case the corpus already carries the edit and marking the row `failed` would
be a lie an operator later acts on. In the knowledge repo:

```sh
git -C <knowledge repo> log --oneline -5 -- <the row's target_paths>
```

No commit naming that proposal id means nothing landed. Then mark it failed — the `WHERE` clause is
the guard, not decoration: it refuses a row that has moved on since you read it, so running this
twice, or against a proposal that landed while you were looking, cannot overwrite an applied commit.

```sql
UPDATE repair_proposals
SET status='failed', error='operator: process died mid-apply'
WHERE id=<id> AND status='approved' AND applied_commit='';
```

`0 rows` means the guard did its job — re-read the row before doing anything else. Once it is
`failed`, the next `stigmergy-repair propose` may derive the same repair again, because a failed
apply is not a dismissal.

### A view that did not catch up

**Usually you do not have to do anything.** The librarian worker converges `views/` to the corpus
on its own interval, whenever its queue is idle — an ordinary capture, a Slack or Drive drop, an
applied repair, an entity mint and a hand edit are all covered, and so is an entity that has never
had a view at all. `stigmergy-views` is for when you do not want to wait:

```sh
.venv/bin/stigmergy-views regenerate --entity acme-corp           # exactly this entity
.venv/bin/stigmergy-views regenerate --stale                      # every entity whose view no longer matches its members or its backlinks
.venv/bin/stigmergy-views regenerate --sweep                      # what the worker's periodic pass does, right now
.venv/bin/stigmergy-views regenerate --entity acme-corp --force   # bypass staleness; re-attempt a withheld synthesis
```

`--sweep` is the UNION of `--stale` and `--all`, and neither of those alone converges the zone:
`--stale` cannot create a view for an entity that never had one, and `--all` cannot remove one
whose members have all disappeared. Prefer `--sweep` when you want the repo correct;
[`views.md`](./views.md) has the table.

A withheld synthesis is not a bug: the skeleton (timeline, backlinks) still ships, and
`--force` is the operator-triggered retry — the periodic pass will not retry one on its own,
because neither staleness signal changed (`member_hash` over the members, `backlink_hash` over the
backlinks the page renders). Re-running against an unchanged corpus is a no-op.

**The two knobs on the periodic pass** (both on the librarian worker, both documented in full in
[`librarian.md`](./librarian.md)'s environment table):

| Var | Default | What it does |
|---|---|---|
| `STIGMERGY_LIBRARIAN_VIEW_SWEEP_INTERVAL_S` | `900` | how often the idle worker converges `views/`. `0` turns the pass off entirely; a negative value is refused at startup |
| `STIGMERGY_LIBRARIAN_VIEW_SWEEP_CEILING` | `10` | how many entities ONE pass may regenerate or remove — each is a model call. The surplus is picked up by the next pass |
| `STIGMERGY_VIEWS_MODEL` | the librarian's DEFAULT (`anthropic:claude-sonnet-5`) | the model a view's synthesis is WRITTEN with. It defaults to the librarian's compile-time default rather than to `CLEAN_MODEL`, because every unattended caller runs inside the worker, whose boot strips `$OPENAI_API_KEY` on purpose — a view agent inheriting the read path's model can only raise there. Note the edge: setting `STIGMERGY_LIBRARIAN_MODEL` moves the FILING agent and not this one — views and repair each hold their own knob, one model per artifact. One knob moves the worker and an operator's own `stigmergy-views regenerate` together |

The pass records itself in `job_runs` under the job name `views-sweep` (distinct from `views`, an
operator's own run, and `views-on-meeting`, the post-filing hook), and prints a `view sweep:` line
on the worker's stdout when it moved something. What a ceiling deferred is in that row's
`stats.skip_reasons`, spelled `run-ceiling-reached(N)` — the same wording the repair proposer uses.

### Postgres backup / restore

The queue needs one; the index does not, being a rebuildable cache. The procedure is drill 1 of
"Release gates & drills" below.

### The librarian branches from the REMOTE

Your local commits are invisible to it: every worktree starts from `origin/main`, fetched
fresh, and `ops/acl.json`, `ops/entity-registry.json` and the linter are read at that same
commit — **push it**. The `filing into <repo> against origin/main@<sha>` line the worker prints
first is the diagnosis: if it names a sha your `git log` does not know, `git pull --rebase`.

## Revocation

### Tester tokens — issue / rotate / revoke

**Issue** (the email must already be a key in `ops/identities.json` with the right audience
scope):

```sh
.venv/bin/stigmergy-issue-token ana@example.com
```

Prints the plaintext token **once** plus a `"<sha256hex>": "ana@example.com"` line. Add it to the
`STIGMERGY_TOKEN_STORE` Fly secret:

```sh
fly secrets set STIGMERGY_TOKEN_STORE="$(fly ssh console -C 'printenv STIGMERGY_TOKEN_STORE' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); d["<sha256hex>"]="ana@example.com"; print(json.dumps(d))')"
```

(or keep a local copy of the JSON, edit it, `fly secrets set STIGMERGY_TOKEN_STORE="$(cat
token-store.json)"` — either way the `fly secrets set` triggers a redeploy automatically.)

**Rotate**: issue a NEW token for the same email, add its hash, remove the OLD hash, set the
updated JSON. **Revoke**: remove the hash with no replacement. Once the new process is up, the
next call with that token gets the generic `401 {"error": "unauthorized"}`. The plaintext is never
stored anywhere; a lost token is a rotation, not a lookup.

### ⚠ NOTHING here takes effect without a process restart. Know which restart you get for free

`STIGMERGY_TOKEN_STORE` is parsed **once, at process startup**, and the running middleware holds
that dict. So a revoked token keeps working until the machines running the old process are gone.
`fly secrets set` triggers that redeploy on its own, which is the difference between the rows below:

| Change | Where it lives | What it takes |
|---|---|---|
| Revoke / rotate a **token** | `STIGMERGY_TOKEN_STORE` Fly secret | **one command** — `fly secrets set` triggers the redeploy that applies it. Effective when the new machines are healthy, not before |
| Change an identity's **audience scope** | `ops/identities.json` in the knowledge repo, **snapshotted into the image at deploy time** | a commit and a push in the knowledge repo, then `make deploy-staging` to re-bake and redeploy |
| Change a **steward's** authority, for the WORKER and any process holding a checkout | `ops/stewards.json` in the knowledge repo | a commit and a push — **no deploy**: it is re-read at a fresh base commit on every decision |
| Change a **steward's** authority, for the deployed `app` and `slack` groups | the same file, **snapshotted into the image at deploy time** (they hold no checkout) | a commit and a push, then `make deploy-staging` to re-bake and redeploy — the same row `ops/identities.json` occupies, for the same reason |

**On the deployed `app` and `slack` groups a steward's authority is a deploy-time snapshot**,
because those groups hold no checkout to re-read. Removing someone from `ops/stewards.json` and
pushing does not take their approve authority away there until the next `make deploy-staging`. If
that is not fast enough, revoke their token — the same one-command path as row 1.

**Editing `ops/identities.json` alone changes nothing about the running server**: the file it reads
per request is the baked `/app/identities.json`, not the one in your checkout. And **there is no way
to cut off a leaked token faster than a deploy** — if that is not fast enough,
`fly scale count app=0 -a $FLY_APP` takes the public surface down entirely.

### The librarian GitHub App + the filing model's provider key

Both live in Fly secrets, which are app-wide — the public server's environment carries them too,
the accepted residual of one app for three process groups. If either is suspected:

1. **App** — GitHub → the App's page → *Install App* → uninstall it from the knowledge repo.
   Every push then fails, in-flight items land `failed`, and **nothing is lost**: the captures
   stay in the queue and the evidence plane. Generate a new private key, `fly secrets set` it,
   reinstall, redeploy.
2. **The filing model's provider** — revoke the key in that provider's console and
   `fly secrets set` a new one under the variable that model reads (`anthropic:` models read
   `ANTHROPIC_API_KEY`, `openrouter:` ones `OPENROUTER_API_KEY`); the worker's items fail
   unauthenticated until the redeploy, then requeue. One key serving two seams — the same
   credential set as both the filing provider's and the embedder's `EMBED_API_KEY` — is two
   rotations, and `stigmergy-librarian-boot` says so at startup when it sees the same value
   survive under another name.
3. Confirm with `git log --format='%an %ae %s' -20` in the knowledge repo that nothing was
   authored by an identity you do not recognize.

**Key rotation without an incident**: generate a new private key on the App's page, replace the
`.pem` (locally: the `STIGMERGY_LIBRARIAN_PRIVATE_KEY_FILE` path in `.env`; staging: the Fly
secret), delete the old key in GitHub. App ID and installation ID are stable.

### Slack tokens

1. Slack App management page → *Socket Mode* → regenerate the App-Level Token, or *OAuth &
   Permissions* → *Reinstall App* for a fresh bot token (either invalidates the old one at once).
2. `fly secrets set SLACK_APP_TOKEN="xapp-..."` / `SLACK_BOT_TOKEN="xoxb-..."` — the set
   triggers the redeploy.
3. Nothing is lost: queued captures live in `capture_queue` and the evidence plane, not in the
   bot; Slack buffers events across the reconnect (expect a delay, not a hole).
4. Confirm with `fly logs -a $FLY_APP -i <slack machine id>` that the connection established.

### The webhook secret

`fly secrets set STIGMERGY_GITHUB_WEBHOOK_SECRET="$(openssl rand -hex 32)"`, then paste the same
value into the webhook's Secret field in the knowledge repo's GitHub settings. Between the two
steps the endpoint rejects pushes with the generic `401`; the nightly rebuild covers the gap.

## Release gates & drills

### `make gates` — the release gates

```sh
make gates                                    # = evals/run_gates.py; needs docker postgres + OPENAI_API_KEY (.env)
.venv/bin/python evals/run_gates.py --skip-adversarial   # golden bars only (adversarial already ran, e.g. in CI)
make adversarial                              # the armed adversarial categories alone, any time
```

Three instruments, three bars, one verdict (exit 0/1), printed as a `PASS`/`FAIL` table:

| Instrument | Size | Bar |
|---|---|---|
| adversarial suite | — | categories **1** (injection via content), **2** (ACL leakage/existence), **7** (forged frontmatter) all pass — collected by name, and the collection floor is itself a CI test (`tests/test_adversarial_gate.py`), so `-k` cannot fail open |
| retrieval golden (`evals/retrieval_golden.json`) | 16 questions | `final` **R@5 ≥ 0.80** |
| QA golden (`evals/qa_golden.json`) | 26 questions | **honesty ≥ 0.90** · **groundedness ≥ 0.84**; refutation REPORTED, never gated |

Both goldens run against the frozen reference corpus `evals/corpus/` (`--repo` defaults to it), so
a bar is a statement about that corpus and nothing else.

**The noise rule**: a real model over a real corpus is not deterministic, so an instrument whose bar
fails is re-run ONCE and the gate passes iff the re-run clears every bar of that instrument. The
re-run is **granted only when every failing bar sits within one question's weight of passing**,
computed from the report's own denominators. A bar missed by more than one case fails on the FIRST
attempt with no re-run, and a runner that exits non-zero is an infra failure that fails immediately.

Reports land in `evals/out/gates/` (`retrieval.json`, `qa.json`); the long score series is
`evals/history.ndjson`. This is the operator's release gate — it never runs in CI (both golden
halves are REAL measurements: real embedder, real model, real spend). The two instruments the
gate arms are also runnable alone (`make retrieval-golden`, `make qa-golden`).

There is a **third instrument the gate does not arm**: `make filing-golden`, which measures the
write path — ten golden captures through the real librarian, its gates and a real `git worktree`,
scored per facet. It is outside the release gate because it writes and costs a real agent pass per
capture, and it is the one to run when a change touches the librarian's agent, its brief or its
gates. It needs `gitleaks` on PATH and a Claude credential; `make filing-golden BACKEND=double` is
the keyless plumbing check. Full account: `evals/README.md`.

### Drill 1 — Postgres backup / restore of the durable schema

Proves the durable tables survive a round trip: the four `capture.schema` names it (`capture_queue`,
`audit_log`, `job_runs`, `ingest_errors`) plus every other table nothing can rebuild —
`review_decisions`, `slack_submissions`, `gardener_findings`, `admin_actions`, and
`steward_notifications`, which holds one row per (item, steward) already DMed and whose loss
re-rings the doorbell at every steward for every open item. Against the docker compose Postgres
(`make db-up` first):

```sh
# record the evidence you will compare against
psql "postgresql://stigmergy:stigmergy@localhost:54321/stigmergy" \
  -c "SELECT count(*) FROM capture_queue" -c "SELECT count(*) FROM audit_log"

# dump (custom format; run inside the container so client/server versions always match).
# `-T` is not optional: an allocated TTY mangles a binary -Fc stream on its way to the file, and
# `out/` is gitignored, so on a clean checkout it does not exist until you make it.
mkdir -p out
docker compose exec -T postgres pg_dump -U stigmergy -Fc stigmergy > out/stigmergy-backup.dump

# restore over the same database
docker compose exec -T postgres pg_restore -U stigmergy -d stigmergy --clean --if-exists \
  < out/stigmergy-backup.dump

# evidence: the same counts, and a queue trace that reads identically
psql "postgresql://stigmergy:stigmergy@localhost:54321/stigmergy" \
  -c "SELECT count(*) FROM capture_queue" -c "SELECT count(*) FROM audit_log"
.venv/bin/stigmergy-queue list
```

**Evidence to expect**: identical row counts before and after, `stigmergy-queue list` showing the
same submissions in the same states, and `pg_restore` exiting 0. The same `pg_dump` invocation
pointed at the Supabase DSN is the staging backup.

### Drill 2 — kill -9 the worker mid-item, watch lease redelivery

`stigmergy-queue` is the observation tool throughout.

```sh
make db-up
.venv/bin/stigmergy-queue list                  # pick/submit an item so the queue is non-empty
.venv/bin/stigmergy-librarian run &             # the loop claims the item (note the pid)
.venv/bin/stigmergy-queue list                  # the row is now `claimed`
kill -9 <that pid>                            # mid-item, no goodbye
.venv/bin/stigmergy-queue list                  # still `claimed` — the lease is honest about the hold
.venv/bin/stigmergy-queue reclaim --visibility-timeout 0   # force redelivery now — you killed it, so
                                                         # there is no lease left to respect (the
                                                         # worker's own horizon is 1290s, derived
                                                         # from the per-item budget)
.venv/bin/stigmergy-queue show <id>             # attempts incremented; the row is `queued` again
```

**Evidence to expect**: the row stranded in `claimed` after the kill; after `reclaim` (or the
real timeout), `queued` with `attempts` +1 and the reclaim recorded in `show <id>`'s trace; a
restarted worker then files it. This is the same recovery the Fly SIGKILL window leans on.

### Drill 3 — index wipe → rebuild → identical golden

```sh
make e2e          # DESTROYS the local queue (down -v) — drain first
```

`scripts/e2e.sh` does exactly this drill: empty volumes → build → run the golden questions
(fake embedder, deterministic) → **wipe volumes** → rebuild from scratch → diff the two
reports, then run the substrate check where the index just got built.

**Evidence to expect**: `E2E OK: wipe -> rebuild -> identical hit lists` with
`out/e2e/report-1.json` and `report-2.json` byte-identical (per-arm rankings included), and a
clean substrate-check report at the end. Any diff is a real nondeterminism bug, not embedding drift.

### Drill 4 — budget-ceiling trip

**The ceilings that exist** (know them before tripping one):

| Ceiling | Value | Where |
|---|---|---|
| overall, per identity | 30 requests/min (token bucket, starts full: 30th ok, 31st refused) | `src/stigmergy/server/ratelimit.py` |
| `ask`, per identity | an ADDITIONAL 10/min bucket (the synthesizer spends a model call per question — the expensive resource, whichever provider serves it) | same |
| one `ask`'s internal budget | 6 model requests / 8 tool calls per question | `src/stigmergy/answer/synthesize.py` |

**A daily spend ceiling does NOT exist — by ruling, not by omission**: a leaked token is bounded
per minute, not per day (10 asks/min sustained is still 14,400 asks/day). For a single-operator
deployment the accepted control is these buckets plus instant revocation (see Revocation). A daily
spend ledger is a WAKE condition — the first invoice that surprises.

**Trip the limiter visibly — the probe must be CONCURRENT.** The buckets refill at per-minute
rates, so sequential calls that each take seconds cannot trip them: one real `ask` runs an
evidence-gathering agent for 5–20 s, and a 10/min bucket refills faster than a sequential caller can
spend it. Fire the calls in parallel, over the HTTP transport ONLY: the limiter is wired where the
threat lives (`transport_http.build_http_app` constructs the `RateLimiter`; a leaked bearer token on
the public URL is the adversary it bounds). The stdio server is deliberately unthrottled.

**The everyday check is `search_brain`** — the same limiter and the same refusal shape with no
synthesizer spend. With a bearer token against the HTTP transport ([server.md](./server.md)), fire
**~35 `search_brain` calls concurrently**. The refusal reads:

```
rate limited: 30 requests/min exceeded — wait a moment and retry
```

**Expect one refusal from the burst, maybe two — that is the limiter working, not leaking.** A
token bucket refilling at 0.5/s admits most of a probe whose calls leak over a few seconds: the
ceiling bounds the RATE, never the count of refusals a single burst produces.

**The `ask` bucket specifically** — when the 10/min ceiling itself is what is being verified:
fire **11+ `ask` calls in parallel** (the same question is fine). The refusal reads:

```
rate limited: 10 ask requests/min exceeded — wait a moment and retry
```

A refusal, never a traceback — and the refusal leaves BOTH buckets untouched, so a refused call
never burns next minute's budget either.

**Evidence to expect**, from the audit trail (every call, both transports, one row):

```sql
SELECT ts, identity, tool, outcome, error_class FROM audit_log
WHERE error_class = 'RateLimitError'
ORDER BY ts DESC LIMIT 20;
```

`ok` rows for the served calls and `error`/`RateLimitError` rows for the refusals. A concurrent
burst mixing any tools trips the overall bucket at its 31st call the same way, with the
`30 requests/min` wording.

## Troubleshooting

**Every real client gets `421 Misdirected Request` / server logs `Invalid Host header`, even
with a valid token.** `$STIGMERGY_PUBLIC_HOST` (`fly.toml` `[env]`) doesn't match the hostname
clients connect to — the MCP SDK's DNS-rebinding protection allowlists localhost plus whatever
that variable names, rejecting everything else after bearer auth but before any tool runs (so no
audit row is written). The value must be a **bare hostname**: a scheme (`https://...`) makes every
request 421, indistinguishable from leaving it unset. Fix: set it to the app's real hostname
(comma-separated if it answers on more than one; a plain env var, not a secret), `fly deploy`,
confirm with one real client call. Not a reason to turn the check off.

**A generic `401 {"error": "unauthorized"}` on every call.** The token's hash is not in the
`STIGMERGY_TOKEN_STORE` **this process started with** — never issued, or removed and the redeploy
has landed. A malformed store is a different failure: the process refuses to start rather than
serving auth open. The mirror-image symptom is a *just-revoked token that still works* — the old
machines have not been replaced yet (see Revocation). Like the 421, these write no audit row.

**`ask`/`search_brain` failing `UndefinedColumn` right after a deploy.** Index schema skew —
the deploy shipped DDL the staging `pages_index` predates. `gh workflow run index-rebuild.yml`
(or `make rebuild-staging`) and re-ask. This is exactly why a deploy ends with one real `ask`
(see Deploy).

**The server refuses to start: empty index.** Fail-closed on purpose, both transports. Build
it: `make index-rebuild` locally, `make rebuild-staging` for staging.

**Every `ask` erroring at once after a registry commit.** Malformed
`ops/entity-registry.json` — see Recovery; the one-line `python3 -c` check settles it.

**The steward doorbell and ACL scope.** The doorbell does not consult the steward's own ACL
scope — `items_for_doorbell` is management-shaped and unscoped, so a doorbell line can name
material its recipient could not `read_page`. **Inert while every steward is unrestricted**;
revisit before a scoped steward ever exists.

**The doorbell rings for nothing / decisions fail closed.** Two shapes, and `job_runs` tells them
apart — the pass records the miss once per process lifetime under `job = 'steward-doorbell'` with
the reason in the stats blob (`doorbell-configuration` is a `stats->>'event'` value, never a `job`
name — filtering on it as one returns zero rows):

```sql
SELECT started_at, stats FROM job_runs
 WHERE job = 'steward-doorbell' AND stats->>'event' = 'doorbell-configuration'
 ORDER BY started_at DESC LIMIT 20;
```


- the map is missing or resolves to EMPTY. Commit and push it (`{"*": ["steward@example.com"]}` is
  the one in use); the worker picks it up on its next item, the `app`/`slack` groups at the next
  deploy;
- the deployment has **no source of stewards at all** — no checkout and no baked snapshot, so
  nothing resolves to a steward, no bell rings and every review decision fails closed.
  `make deploy-staging` bakes `ops/stewards.json` into the image and `fly.toml` passes
  `--stewards /app/stewards.json`.

**A capture attributed to the wrong identity over stdio.** The knowledge repo's own `.mcp.json`
declares two servers (`stigmergy` = `steward`, unrestricted; `stigmergy-ana` = `ana`, finance-scoped)
and a client session picks one on its own. Address the tool explicitly ("use the `stigmergy` MCP
server's `brain_submit`"); check with `stigmergy-queue list` (it prints `submitted_by`). There is no
re-attribution tool by design — resubmit under the right identity and let the duplicate be refused.
(The librarian's own agent never loads that file: it runs with an empty MCP server list and strict
MCP config, because a file in the repo can declare any command.)

**The librarian ignores your change / files against a sha you don't recognize.** It branches
from `origin/main`, not your working tree — the skill, the linter, `ops/acl.json` and the
registry are all read at that commit. Push, or `git pull --rebase` if your `main` diverged. The
`filing into … against origin/main@<sha>` startup line is the diagnosis.

**`make librarian-walk` (or the worker) refuses at startup.** `startup_checks` doing its job — each
refusal names its fix: a refused `sdk` backend value, an unpushed skill or linter, a half-configured
GitHub App (all three variables or none), missing `gitleaks` (`brew install gitleaks`), or a lease
shorter than one item's worst case. With `STIGMERGY_LIBRARIAN_BACKEND=pydantic` three more apply: a
model id with no provider prefix (pydantic-ai reads a bare `claude-sonnet-5` as an OpenAI model, so
spell it `anthropic:claude-sonnet-5`), a model with no configured price
(`STIGMERGY_LIBRARIAN_PRICING`), and a missing provider key (run through `make`, which loads
`.env`).

**The Postgres suites suddenly skip, or refuse with `WrongDatabase`.** See "The two databases"
under Wipe & re-seed — recreate the composition (`make db-down && make db-up`, destroys the
local queue) for the missing `stigmergy_test`; point `$STIGMERGY_TEST_DSN` at the test database,
never at `stigmergy`, for the refusal.

**`fly scale count worker=2` reports 2 but only one worker drains.** The second machine is
Fly's standby (`†` in `fly status`, state `stopped`) — see Deploy. Two draining workers is
`worker=3`; read STATE, not COUNT.

**Slack DMs impossible — "Sending messages to this app has been turned off".** The App Home
Messages Tab toggle ("Allow users to send Slash commands and messages from the messages tab")
is off. Presentation, not permission: no reinstall, no restart, nothing in the scope list hints
at it. Flip it in the Slack App's App Home settings.

**Slack events double-handled.** Two bots hold Socket Mode connections against one Slack app —
usually a local `make slack-run` beside the staging machine (the singleton advisory lock is per
DATABASE, so they don't exclude each other). Stop the local bot.

**The `slack` machine refuses to start and names a process that no longer exists.** The refusal is
literal — *"another `stigmergy-slack` process already holds the singleton lock"* — and you can
prove no such machine is running. **A CONNECTION POOLER outlives the machine that opened it.**
`pg_try_advisory_lock` is held for the life of the *session*, and against Supabase the session
belongs to Supavisor, not to your VM: destroy the machine and the pooler keeps its upstream
session — lock and all — until it reaps it. Find the holder and release it:

```sql
SELECT a.pid, a.state, now() - a.backend_start AS session_age
FROM pg_locks l JOIN pg_stat_activity a USING (pid)
WHERE l.locktype = 'advisory';
-- Only after confirming `fly status` shows no running slack machine:
SELECT pg_terminate_backend(<pid>);
```

Then `fly machine restart <slack machine>`. **Confirm the holder is an orphan before terminating**,
because the same query answers "the lock is doing its job" and "the lock is stale" identically —
`fly status -a <app> | grep slack` is what tells them apart. `session_age` measures the POOLER's
connection, which is reused across clients, so an age older than your deploy proves nothing.

**The 🧠 gesture never shows the hourglass/checkmark, but captures still land.** The app's token
lacks `reactions:write` (see [slack.md](./slack.md#what-the-slack-app-has-to-be-configured-with)
for the full scope list). Every reaction call is best-effort
(`stigmergy.slack.capture._react_or_log`), so a `missing_scope` failure is logged and swallowed,
never a lost capture. Add the scope and reinstall the app to fix the feedback.

**A gate or gardener run half-worked.** Read `job_runs`, not the terminal scrollback:

```sql
SELECT job, status, finished_at, stats FROM job_runs
WHERE job IN ('gardener', 'repair-propose', 'digest', 'digest-dry-run', 'capture-purge',
              'capture-purge-dry-run', 'webhook-index-upsert')
ORDER BY started_at DESC LIMIT 10;
```

A `gardener` row with `status='partial'` means the nine deterministic checks' findings are
complete and trustworthy and the model sweep failed (`stats->'sweep'->>'error'` names the class);
only `status='error'` means the run cannot be trusted. Gardener exit codes: 0 clean, 1 failed or
partial, 2 precondition (bad `--repo`, bad threshold, no database), 130 interrupted.

**Reading the audit trail** (every tool call, both transports, one `audit_log` row in the same
Postgres as the index):

```sql
-- everything a specific identity asked, most recent first
SELECT ts, tool, args, outcome, duration_ms
FROM audit_log
WHERE identity = 'ana@example.com'
ORDER BY ts DESC
LIMIT 50;

-- harvesting real `ask` questions into the golden set
SELECT DISTINCT args->>'question' AS question
FROM audit_log
WHERE tool = 'ask' AND outcome = 'ok'
ORDER BY question;

-- who's been active, and how much
SELECT identity, tool, count(*), avg(duration_ms)
FROM audit_log
GROUP BY 1, 2
ORDER BY 1, 2;
```

`args` is the full JSON for most tools, but `brain_submit`/`brain_reply` are audited by SIZE
and HASH only, never content. `outcome` is `ok` or `error`; `error_class` names the exception when
it isn't `ok`. `stigmergy-pilot-report` summarizes the same tables (latency percentiles,
answered-with-citation vs honest-refusal split); on a single-operator deployment its per-identity
counts are the operator's own credentials, never an adoption number.
