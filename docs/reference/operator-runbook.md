# Operator runbook

Everything below is for the operator running the system. The live world this runbook covers:
**two zones** in the knowledge repo (`wiki/` · `sources/`), the librarian's
**9 gates**, **8 MCP tools** (`search_brain`/`read_page`/`list_entities`/`describe_entity`,
`ask`, `brain_submit`/`brain_submissions`/`brain_delete`),
**one Fly app** with three process groups (`app` · `slack` · `worker`), the **night shift** the
worker runs on its own idle branch (the gardener and the retention purge), the optional `/admin`
console on the `app` group, and the golden evals under `evals/` with the release gates
(`make gates`) over them.

**Nothing here runs in GitHub Actions.** Every unattended pass runs inside the deployment, on the
librarian worker's idle branch —
so there is no second place to configure, no schedule that can be silently skipped, and no
credential for another service to keep. The one exception is the index REBUILD, which needs the
embedding key the worker deliberately does not have; it stays a command an operator runs.

Organized by OPERATION: Deploy · Wipe & re-seed · Capturing a meeting or a document ·
Index rebuild · Removing pages · Recovery · Revocation · Release gates & drills · Troubleshooting.

There is no read site: navigation happens through `read_page` and the entity tools.

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
   fly secrets set STIGMERGY_TOKEN_STORE='{"<sha256hex>": "ana@example.com"}'
   # the evidence plane (R2 — server AND worker read it)
   fly secrets set STIGMERGY_EVIDENCE_ENDPOINT="https://<account-id>.r2.cloudflarestorage.com"
   fly secrets set STIGMERGY_EVIDENCE_BUCKET="stigmergy-evidence-staging"
   fly secrets set STIGMERGY_EVIDENCE_ACCESS_KEY_ID="..."
   fly secrets set STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY="..."
   # the librarian GitHub App — the `worker` group PUSHES with it, and the `app` group's index
   # webhook READS with it (Contents API, no clone). It is the only git credential in the
   # deployment, and the `app` group has no write path of its own to spend it on.
   fly secrets set STIGMERGY_LIBRARIAN_APP_ID="123456"
   fly secrets set STIGMERGY_LIBRARIAN_INSTALLATION_ID="87654321"
   fly secrets set STIGMERGY_LIBRARIAN_PRIVATE_KEY="$(cat ~/.config/stigmergy/librarian.private-key.pem)"
   # Only if your App is not named `stigmergy-librarian` — its slug is what the bot commits as.
   fly secrets set STIGMERGY_LIBRARIAN_APP_LOGIN="my-librarian"
   # The FILING model's own provider key — whichever STIGMERGY_LIBRARIAN_MODEL names
   # (`anthropic:` -> ANTHROPIC_API_KEY, `openrouter:` -> OPENROUTER_API_KEY,
   # `google-gla:` -> GEMINI_API_KEY). A missing one is refused at startup, by name.
   fly secrets set ANTHROPIC_API_KEY="sk-ant-..."
   # the Slack transport (`slack` group) — or `make slack-secrets` to stage all three from .env
   fly secrets set SLACK_APP_TOKEN="xapp-..."
   fly secrets set SLACK_BOT_TOKEN="xoxb-..."
   fly secrets set SLACK_TEAM_ID="T..."
   # the incremental index webhook (`app` group) — the KNOWLEDGE repo, the one being pushed to
   fly secrets set STIGMERGY_GITHUB_WEBHOOK_SECRET="$(openssl rand -hex 32)"
   fly secrets set STIGMERGY_GITHUB_REPO="<owner>/stigmergy-brain"
   # the admin console (`app` group) — OPTIONAL; unset = the console does not exist.
   # Hash from `stigmergy-admin-token`. That hash is the console's ENTIRE credential surface: it
   # holds no token for any other service, because it drives none (the passes it used
   # to dispatch through a GitHub PAT now run in the worker).
   fly secrets set STIGMERGY_ADMIN_TOKEN_HASH="<from stigmergy-admin-token>"
   ```

   **Every secret here is read once, at process startup** — `STIGMERGY_TOKEN_STORE` included, which
   is why revoking a token is not instantaneous (see Revocation). A plain `fly secrets set`
   triggers the deploy that applies it; `make slack-secrets` uses `--stage` instead, so
   `make slack-secrets && make deploy-staging` is one rollout, not two.
3. **Supabase Postgres** must already have the index built at least once
   (`.venv/bin/stigmergy-index --rebuild --repo $STIGMERGY_REPO` against `STIGMERGY_INDEX_DSN`) — the
   server refuses to serve an empty index.
4. **The night shift needs no setup at all.** The gardener and the retention purge run inside the
   `worker` process group, on its idle branch, and read the same environment the worker already
   has. Two optional variables move them; both default to a sensible UTC time and both take the
   word `off`:

   | Fly secret / env | Default | What it does |
   |---|---|---|
   | `STIGMERGY_LIBRARIAN_GARDEN_AT` | `05:07` | when the daily gardener pass runs, UTC `HH:MM`, or `off` |
   | `STIGMERGY_LIBRARIAN_RETENTION_AT` | `04:42` | when the daily retention purge runs, UTC `HH:MM`, or `off` |
   | `STIGMERGY_RETENTION_DAYS` | `30` | how long a terminal capture keeps its payload |

   The gardener pass needs no model and no provider key of its own: every check it runs is
   deterministic. It used to, and the setting for it was **must be set** on a deployment precisely
   because its default resolved through a provider whose key the worker boot strips — a trap that
   went with the model passes.

   A pass never starts while a capture is waiting in the queue, and yields between units, so
   maintenance cannot delay a filing. "Did it run" is answered from `job_runs` — the same rows the
   worker itself reads to decide whether today's pass is still due, which is what makes a restart
   at 05:08 not garden twice.

   **Nothing is skipped silently.** The failure mode this replaced was a cron guarded by a
   repository variable: an unset variable made every job green-and-skipped, so "the crons stopped
   running" looked identical to "the crons are fine". A pass that does not run now leaves its last
   `job_runs` row where it was, and the console's Jobs page shows how long ago that was.

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

Bakes **three** files out of `$STIGMERGY_REPO/ops/` into the `deploy/` directory, which the
Dockerfile then `COPY`s to `/app/`:

| Baked from | To | Missing in the knowledge repo |
|---|---|---|
| `ops/identities.json` | `/app/identities.json` | **the script exits 2** — this is the one required file |
| `ops/entity-registry.json` | `/app/entity-registry.json` | `{"entities": {}}` — `ask` searches without entity-first resolution. The baked copy is the FALLBACK: a server whose index carries a registry snapshot (the webhook refreshes it on every push that touches the file) answers from that instead |
| `ops/slack-channels.json` | `/app/slack-channels.json` | `{}` — every audience falls back to the safe empty default |

All three are always written, so the unconditional `COPY` can never fail on a missing source.

**`deploy/` is TRACKED, not gitignored.** The `COPY`s are unconditional, so a fresh clone has to
build, so the three files are committed as EMPTY defaults (`{}` · `{"entities": {}}` · `{}`).
What the script bakes over them is a whole deployment's identity roster, one `git add -A` from being
published, so it restores the committed defaults on the way out through an EXIT trap that fires on
**every** path out, a failed `fly deploy` included. `tests/test_deploy_defaults.py` holds both
halves: that `fly deploy` saw the real files, and that nothing but the defaults outlived the script.
If you ever find real data under `deploy/`, restore the empty defaults before committing.

The script touches **only those three names**, never the `deploy/` directory itself, because
`deploy/` may hold tracked files it does not bake. It used to clear the directory outright, and
since the EXIT trap knew how to rebuild the baked JSON files and nothing else, one
`make deploy-staging` deleted every other tracked file under `deploy/` from the working tree; a
routine `git add -A` afterwards would have committed their removal. The delete set and the restore
set are now derived from one list in the script, so they cannot drift apart again.

Then it runs `fly deploy` (one image, all three process groups) and pins both singleton groups:
`fly scale count slack=1 --yes` — Socket Mode has no leader election and `fly deploy` creates two
machines by default for a NEW process group — and `fly scale count worker=1 --yes`, because the
worker's default second machine is a standby one `fly machine start` away from a second paid poller
that nothing refuses. The trade is deliberate: with no standby, a worker host failure stalls queue
draining until an operator redeploys or starts a machine. The knowledge repo's `ops/` stays the
single source of truth; the script takes a deploy-time snapshot, which is why a scope change needs a
redeploy (see Revocation).

**A deploy is not complete without two verifications**:

1. **A release carrying an index schema change rebuilds the index right after the deploy.**
   Nothing rebuilds it for you — the rebuild needs an embedding key the worker does not have, so
   it is always a command somebody runs. Until the index catches up, every `ask`/`search_brain`
   fails `UndefinedColumn`. After any deploy whose diff touches `index/store.py`'s DDL or the
   columns `index/corpus.py` parses: `make rebuild-staging`, before calling the deploy done.
2. **The deploy check ends with ONE real `ask`, end to end.** `fly status` showing every
   process group healthy proves nothing about the read path a schema-skewed index breaks
   silently.

### Configuration: what changing something takes

Four different mechanisms carry configuration into a running process, and they answer "what do I
do to change this" differently enough that guessing costs a redeploy or a silent no-op.

| Kind | Example | What changing it takes |
|---|---|---|
| Baked into the image at deploy time | `ops/identities.json` — the copy `fly.toml` points the `app` and `slack` groups at (`--identities`); the fallback copy of `ops/entity-registry.json` is baked the same way, but only answers where the index has no snapshot | a commit and a push in the knowledge repo, then `make deploy-staging` to re-bake `deploy/` and redeploy |
| Cached in the index, refreshed by the push webhook | `ops/entity-registry.json`, `ops/identities.json`, `ops/slack-channels.json` — `ops_file_snapshot` rows every process group reads through its database connection. The console's Index panel shows each file's freshness and source sha | a commit and a push — no deploy; the webhook writes each pushed file within seconds (fetched at the branch ref, so replays are inert), and the nightly index rebuild reconciles per file — it never clears the two access files over an absent checkout copy |
| A Fly secret, read once at process startup | `STIGMERGY_TOKEN_STORE`, `OPENAI_API_KEY`, `STIGMERGY_INDEX_DSN`, the four `STIGMERGY_EVIDENCE_ENDPOINT`/`_BUCKET`/`_ACCESS_KEY_ID`/`_SECRET_ACCESS_KEY`, the librarian App triple, `ANTHROPIC_API_KEY`, `STIGMERGY_ADMIN_TOKEN_HASH`, the three `SLACK_*` tokens, `STIGMERGY_GITHUB_WEBHOOK_SECRET` | `fly secrets set …` — triggers the redeploy that applies it; effective once the new machines are healthy, not before |
| A non-secret env var in `fly.toml`'s `[env]`, app-wide | `STIGMERGY_LIBRARIAN_TIMEOUT_S` — the worker's per-item agent budget, and what its lease (`config.resolved_visibility_timeout_s`) is derived from | edit `fly.toml`, then `make deploy-staging`/`fly deploy` — every process group's machines restart on the new value together. The admin console re-derives the DEPENDENT lease fresh on every request rather than caching a class default, so its meter and Reclaim horizon can never disagree with the worker's own once the new machines are up — see "A dead worker mid-item" below |
| Committed to the knowledge repo, read at a base commit, wherever a checkout exists | `ops/entity-registry.json` and the contract linter — the WORKER's own reads, at each item's own base commit, distinct from the `app`/`slack` groups' baked copies above | a commit and a push — no deploy; picked up at the very next item the worker claims |

`ops/identities.json` is the one access file that is still a deploy-time snapshot, which is why
changing an audience scope is a redeploy (see Revocation). `ops/entity-registry.json` used to work
that way too and no longer does — a commit that pushed a new entity left `describe_entity` serving
it with no name and no aliases until the next deploy, so the registry moved into the index where the
webhook can refresh it for every group at once.

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
  worst case on the deployed worker is 1320s (two agent attempts at the deployed
  `STIGMERGY_LIBRARIAN_TIMEOUT_S=600`, plus 120s for the gates, the commit and the push). A deploy
  or `fly machine stop` SIGTERMs (the worker stops claiming and exits at the next terminal
  state), but an item still running at 300s is SIGKILLed — the row returns after the derived
  1500s visibility timeout with an attempt burned, and the next worker files it. Drain first if
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

### The night shift — what runs unattended

| Pass | When (UTC) | Where | What |
|---|---|---|---|
| gardener | daily, `STIGMERGY_LIBRARIAN_GARDEN_AT` (default 05:07) | the worker's idle branch | `stigmergy-gardener`'s corpus-health run, findings persisted |
| retention purge | daily, `STIGMERGY_LIBRARIAN_RETENTION_AT` (default 04:42) | the worker's idle branch | payload and hints of terminal rows past the window |
| index rebuild | **never automatically** | an operator's terminal | the full rebuild — see below |

All of it runs inside the `worker` process group. **No pass starts while a capture is waiting**,
and each yields between units, so maintenance can never put itself between a capture and its
filing.

The two DAILY passes decide "is today's run still owed" by reading their own last `job_runs` row,
not an in-process timer — which is why a redeploy at 05:08 does not garden a second time, and why
a worker that was down all night does not run a 05:07 pass at 23:00 (it would report on a corpus
nobody was reading it against).

**The index rebuild is the one pass that cannot move into the worker.** It needs an embedding key,
and the worker's environment deliberately has none: `librarian.bootstrap` strips `OPENAI_API_KEY`
and `EMBED_API_KEY` before exec'ing the worker, so the write path cannot reach the read path's
credential. Rebuild by hand, with the key exported:

```sh
.venv/bin/stigmergy-index --rebuild --repo $STIGMERGY_REPO      # or: make rebuild-staging
```

Between rebuilds the index is kept current by the push webhook (below), and the admin console's
Index page lints the LIVE index on demand — duplicate page ids, orphan continuation parts,
dangling supersessions, unregistered anchors — which is where drift shows up.

**"Did it run" is a database question, and only a database question.** There is no Actions tab to
read and no schedule to check:

```sql
SELECT job, status, finished_at, stats FROM job_runs
ORDER BY started_at DESC LIMIT 20;
SELECT built_at FROM index_meta;                 -- the rebuild's only trace; it writes no job row
SELECT * FROM ingest_errors WHERE NOT resolved ORDER BY last_at DESC;
```

The console's Jobs page is the same rows with the times spelled out, and its Repairs page is the
ledger of what has left the corpus ([repair.md](./repair.md)).

### The incremental index webhook

`POST /webhook/github` lives on the `app` process group — the ONE path exempt from the bearer
middleware, by exact match, because it authenticates differently (HMAC over the raw body). Two Fly
secrets (above: `STIGMERGY_GITHUB_WEBHOOK_SECRET`, `STIGMERGY_GITHUB_REPO`; optional
`STIGMERGY_GITHUB_BRANCH`, default `main`, and `STIGMERGY_GITHUB_WEBHOOK_FILE_CAP`, default `50` — a
push touching more files than the cap is left to the next rebuild), plus one webhook in the
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

`/admin` on the `app` process group — the daily loop (queue drain, the night shift,
gardener, repairs, index, activity) in a browser instead of a terminal. Inert 404s until
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
| `wiki/notes,concepts/` | the librarian, filing a capture of any kind through the nine gates — one page, or as many as its material establishes |
| `sources/slack/`, `sources/documents/`, `sources/meetings/`, `sources/notes/` | the librarian's source archive — every capture files its material verbatim, and the door and the kind choose the folder |
| `wiki/entities/` (+ `ops/entity-registry.json`) | one writer, and no second: `librarian.identity` CREATES an entity page inside the capture's own commit, `approved_by:` naming whoever captured, and appends new facts, connections and spellings to a registered entity's page. A removal's sweep may later scrub a dead link off such a page; nothing but the librarian creates one |

After any bulk re-seed: rebuild the index (next section) and run `make index-check`.

## Capturing a meeting or a document

Both enter over `brain_submit`, from whatever client already holds the text. There is no operator
command for either and no Google credential anywhere in the deployment: the CLIENT is the extractor
— a Claude session with the Drive connector reads the document and submits it, a person with the
file open sends what it says, a script sends what it already has.

| What | The call |
|---|---|
| a transcript | `brain_submit(kind="meeting", material=<the transcript>, hints={"title": …, "meeting_date": "YYYY-MM-DD", "attendees": "a, b"})` — `title` and `meeting_date` are required |
| a document | `brain_submit(kind="document", material=<the document's text>, hints={"title": …, "source_url": "https://…"})` — `title` is required, `source_url` is optional and must be an http(s) URL |

Material is capped at 1 MB for both (256 KB for `raw` and `page`), in UTF-8 bytes. A submission
missing a required hint, or over its cap, is refused at the enqueue seam with no queue row and no
evidence blob written.

**Both ride the same pipe every capture rides.** The material is archived verbatim — a transcript
under `sources/meetings/`, a document under `sources/documents/` — and the agent then files the
`wiki/` pages that material establishes: one for a document, as many as it took decisions for a
transcript, each anchored on its own (to a registered entity, to one this capture introduces, or
company-wide with a reason) and all of them in ONE commit with the archive and any identity they
needed. The `source_url` hint lands as `url:` on the archived page, as the submitter's own claim of
where the text came from. `title` and `meeting_date` are required for a transcript because a
transcript's own text usually states neither; they reach the filing agent as hints.

**Reading the result**: `stigmergy-queue show <id>`, or `brain_submissions` from the client that
submitted it. A `failed` row names the stage the filing died at — there is no `conversion` stage,
because nothing is fetched or converted server-side.

**The agent budget for documents**: the deployed worker runs with
`STIGMERGY_LIBRARIAN_TIMEOUT_S=600` (fly.toml), because a figure-dense document overlapping an
entity with existing pages exceeds the paste-sized 300s default. A `failed` row saying "the agent
exceeded its NNNs budget" means the pass ran long, not that the capture is bad — submit it again
(identical bytes dedup only against FILED rows). The visibility timeout derives from this value.

## Index rebuild

### Local

```sh
make index-rebuild                                     # $STIGMERGY_REPO (default ../stigmergy-brain), real embedder
.venv/bin/stigmergy-index --rebuild --repo $STIGMERGY_REPO    # the same, bare (env: OPENAI_API_KEY, STIGMERGY_INDEX_DSN)
```

### Staging

```sh
make rebuild-staging                  # needs STAGING_DSN in .env (deliberately NOT STIGMERGY_INDEX_DSN)
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

**When the entity pages and `ops/entity-registry.json` disagree, there is no command to run.** The
registry is DERIVED from the pages, and the librarian worker rewrites it inside every commit that
touches the identity zone, so neither side is ever hand-edited — and a disagreement means one of
them was. Two surfaces tell you:

- `stigmergy-index --check` warns on the SYMPTOM that reaches readers, an `entity:` value with no
  registry record (above);
- the worker refuses the next capture that would introduce an identity, whole and unrepairably,
  naming up to three divergences — rather than regenerating a registry that commit was not meant to
  rewrite ([librarian.md](./librarian.md#writing-an-identity-what-a-filing-does-to-the-registry)).
  Ordinary captures keep filing meanwhile; nothing is lost.

The fix is a commit in the KNOWLEDGE repo putting the two sides back in step — correct the page, or
delete the hand-written registry entry — after which the next identity-bearing filing rewrites the
file itself.

## Removing pages

**A removal is decided by the person who asks for it, and performed by the librarian** — there is
nothing to approve afterwards, and nothing but the worker writes to the knowledge repo.
Two doors, and the same seam behind
both:

- **MCP**: `brain_delete(paths=["wiki/notes/Old Memo.md"], why="what makes it stale")`. It requires
  an **UNRESTRICTED identity** — one with no audience restriction in `ops/identities.json` — and
  nothing else. That is the one question the server can answer at the door: a removal touches the
  pages it names AND every page that refers to them, a set nobody knows until the corpus is read,
  and only a caller who can see the whole corpus can be entitled to all of it. A scoped caller gets
  the door's one anonymous refusal — *"there is nothing for you to remove at those paths"*, the same
  sentence whether or not the paths exist — which is also why no refusal can reveal a referrer.
- **The console**, Repairs → **Remove pages**. Its token is the whole authorization there, which
  makes it the most consequential button on that console.

**What comes back from either door is a queue acknowledgement, not a commit.** Both write one
`capture_queue` row of kind `delete` — the reason as its material, the pages in its hints, your name
as its submitter — and the worker takes it from there: the pages go; every page that referred to one
has its `related:`/`sources:` entries dropped by code and its BODY rewritten by a model, so a
sentence that cited a removed page still reads and a callout that only existed because of one is
gone; the nine gates judge the result; the knowledge repo's own linter is run over the whole tree to
prove no dead link survives; and one App-authored commit lands with your name in an `Approved-by:`
trailer.

**Read the row back, and read the diff on it.** Nobody read that prose before it landed — that is
the trade, stated rather than softened, — so the report on the capture carries a unified diff
per rewritten page alongside the paths that stopped existing. **`brain_submissions` is where that
reading happens**: it renders each diff ACL-scoped per path and fenced, and NAMES any path it
withholds, so a page you may not read still shows as changed. The console's Captures page carries
the same row and the librarian's sentences about it; `stigmergy-queue show <id>` gives the row's
trace and status but not the report.

```sh
.venv/bin/stigmergy-queue list --status queued --status claimed   # is the removal still waiting?
.venv/bin/stigmergy-queue show <id>                               # its trace, attempts and latency
```

Refused **at the door**, with nothing queued: a scoped caller, an entity page (an identity is merged
away by removing what made it one, never deleted at this door), a path outside the corpus, more than ten pages in one
request, an empty reason. The length bounds on `paths` and `why` are checked inside
`BrainService.delete_pages`, within the audited seam, so a refusal is an audited call rather than a
silent one.

Refused **by the worker**, as a `rejected` capture whose report says why and whose `reason_code` is
`unremovable`: a page that is not there, a page whose frontmatter is not a shape this can read
(CRLF, a BOM, an unterminated `---`), a plan over `$STIGMERGY_REPAIR_MAX_PLAN_BYTES`, a reference in
a frontmatter field the sweep does not rewrite, a body the writer could not reconcile in one retry,
a gate's veto, and a dead link the sweep would have left behind. Every one of those lands nothing at
all. A reason that matches a likely secret or a personal-data pattern is refused there too, under
`secret`/`pii`, by the same scan every capture's material passes — the reason becomes a commit
message, which is the one place no gate looks.

**The undo is `git revert` in the knowledge repo**, by an operator with a checkout. When the sweep
rewrote a `sources/` page, that revert is an operator commit in a machine-owned zone —
so it needs adding to that repo's reviewed authorship baseline, or its CI goes red on every later
push.

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
`--entity-registry` copy. The librarian worker is the only thing that writes this file, and it
regenerates it from the entity pages; a hand edit is the usual way it got malformed.

**Prevention**: `stigmergy-index --check --repo $STIGMERGY_REPO` warns on anchored-but-unregistered
entities — run it after registry changes; it raises on malformed registry JSON too. It lints the
copy the SERVER serves, so against a database carrying a snapshot the `--repo` file is not what it
reads (`--rebuild` is what makes a checkout's registry the served one). Its findings
name paths and page_ids of EVERY page, ACL-restricted ones included, so treat the output as scoped
material and don't paste it into shared trackers.

### A dead worker mid-item — lease redelivery

A dead worker costs one delivery, never a capture: the row returns to `queued` after the visibility
timeout (900s default) with `attempts` incremented, and the next worker files it. Observe and force:

```sh
.venv/bin/stigmergy-queue list                              # depth per status + newest submissions
.venv/bin/stigmergy-queue show 7                            # one submission's trace and latencies
.venv/bin/stigmergy-queue reclaim --visibility-timeout 0    # release EVERY claimed row, right now
.venv/bin/stigmergy-queue reclaim --visibility-timeout 900   # ...only ones past a 900s lease
```

`--visibility-timeout` is mandatory on `reclaim` and the command refuses without it: it decides
how dead a worker must be before its work is taken away, and the CLI cannot see the lease that
worker actually holds — a shorter horizon requeues captures out from under processes still filing
them. Pass `0` when you know the worker is gone, or the worker's own lease otherwise. That lease is
DERIVED from the per-item budget rather than fixed: 900s at the default
`STIGMERGY_LIBRARIAN_TIMEOUT_S` (two agent attempts, 120s of gates, 180s of headroom), **1500s on
the deployed worker**, which runs at 600.
`stigmergy-librarian status --json` prints the resolved `visibility_timeout_s` for the environment
it is run in — read it there rather than assuming the default.

The admin console's Reclaim button states **the same derived lease**, resolving
`STIGMERGY_LIBRARIAN_TIMEOUT_S` through the worker's own arithmetic per request, so the Worker tab's
meter and the button agree with `status --json` (1500s on staging). Two conditions make that hold:
`fly.toml`'s `[env]` is app-wide, so the console process reads the variable the worker resolved; and
the worker command passes no `--visibility-timeout`, which would beat the derivation and which the
console cannot see. It does **not** hold in the local composition, where `docker-compose.yml` gives
the librarian its own environment block and no console runs beside it.

This is drill 2 of "Release gates & drills" below.

### A removal that did not land

A removal the worker could not perform is a `rejected` CAPTURE, never a `repairs` row: the ledger
records what happened to the corpus, and a refusal changed nothing.

```sql
SELECT id, created_at, submitted_by, report->>'summary'
FROM capture_queue WHERE kind='delete' AND status='rejected' ORDER BY id DESC LIMIT 20;
```

**Read the summary before doing anything.** Every sentence that road raises is written to be
published — it names the gate and its codes, or the validator's own refusal, in repo-relative paths
— and it is the whole of what the person who asked will ever be told. Nothing is retried and
nothing is put back: they read it and decide whether to ask again.

What to do with one depends on what the sentence says:

- **A page it named is not there** — somebody removed it already, or the path was typed wrong.
- **A gate refused the diff, or the sweep left a dead link** — the corpus is untouched. Ask again;
  if it happens twice, the surviving reference is spelled in a shape the sweep does not read
  (`repair.deletion`'s link scanner mirrors the frozen contract linter, and both are files).
- **The plan is over `$STIGMERGY_REPAIR_MAX_PLAN_BYTES`** — the pages named are referred to by too
  much of the corpus to remove in one decision. Remove fewer at a time.

**Nothing should ever be deleted from `repairs`.** Every row in it is a record of something that
actually happened in the corpus, and the rows of retired kinds are the only record left of a repair
loop that no longer exists.

### A `views/` directory in the knowledge repo

**Inert, and nothing needs doing about it.** The per-entity rollup used to be a stored page under
`views/`, kept fresh by a convergence sweep on the worker's idle branch. Both are gone: "what do we
know about X" is answered at read time by `describe_entity` and `ask`, per reader and ACL-scoped,
which a single shared file could never be.

If your knowledge repo predates the change it still has that directory. Those files are no longer
indexed — absent from `search`, `read_page` and `ask` — and no command writes there any more.
**Nothing in this system deletes them.** Removing them is your decision to make in your own repo:

```bash
git rm -r views/ && git commit -m "chore: drop the retired views zone"
```

There is no sweep to check on, no `views-sweep` row in `job_runs`, and no interval, ceiling or
model knob to tune. A deployment that still sets the three variables that used to configure the
pass is setting nothing — nothing reads them, so they are safe to leave and better to drop; the
names are in this repo's git history if you need to grep your own secrets for them.

### Postgres backup / restore

The queue needs one; the index does not, being a rebuildable cache. The procedure is drill 1 of
"Release gates & drills" below.

### The librarian branches from the REMOTE

Your local commits are invisible to it: every worktree starts from `origin/main`, fetched
fresh, and `ops/entity-registry.json` and the linter are read at that same
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

**There is no role file to revoke from.** No map grants anybody authority over the write path:
what a person may do is decided by their token (does it resolve to an identity at all) and by that
identity's audience scope in `ops/identities.json` (unrestricted or not — which is what
`brain_delete` asks). Both rows above are the whole surface.

**Editing `ops/identities.json` alone changes nothing about the running server**: the file it reads
per request is the baked `/app/identities.json`, not the one in your checkout. And **there is no way
to cut off a leaked token faster than a deploy** — if that is not fast enough,
`fly scale count app=0 -a $FLY_APP` takes the public surface down entirely.

### The librarian GitHub App + the filing model's provider key

Both live in Fly secrets, which are app-wide. The public server's environment carries the filing
model's key as the accepted residual of one app for three process groups; it carries the App
deliberately, because the index webhook reads the knowledge repo's pushed files with it. What it
cannot do with either is write: no code in that process commits or pushes.
If either is suspected:

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
steps the endpoint rejects pushes with the generic `401`; the next rebuild covers the gap.

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
write path — 14 golden captures through the real librarian, its gates and a real `git worktree`,
scored per facet (`status`, `reason`, `type`, `folder`, `anchor`, `edits`, `proposals`, `decisions`,
plus the two cost facets `attempts` and `bounces`). `proposals` keeps its name and scores the
identity a filing INTRODUCED for a name the registry does not know — it replaced the two facets the
retired ask-back loop had. It is
outside the release gate because it writes and costs a real agent pass per
capture, and it is the one to run when a change touches the librarian's agent, its brief or its
gates. It needs `gitleaks` on PATH and a Claude credential; `make filing-golden BACKEND=double` is
the keyless plumbing check. Scores are comparable per FACET and not per run, since the denominators
moved with the redesign. Full account: `evals/README.md`.

### Drill 1 — Postgres backup / restore of the durable schema

Proves the durable tables survive a round trip: the four `capture.schema` names it (`capture_queue`,
`audit_log`, `job_runs`, `ingest_errors`) plus every other table nothing can rebuild —
`repairs`, the permanent record of every page that has left the corpus (and of the retired repair
loop that ran before it), `slack_submissions`, `gardener_findings` and `admin_actions`. Against the
docker compose Postgres (`make db-up` first):

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
                                                         # worker's own horizon is 900s, derived
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
the deploy shipped DDL the staging `pages_index` predates. `make rebuild-staging` and re-ask. This is exactly why a deploy ends with one real `ask`
(see Deploy).

**The server refuses to start: empty index.** Fail-closed on purpose, both transports. Build
it: `make index-rebuild` locally, `make rebuild-staging` for staging.

**Every `ask` erroring at once after a registry commit.** Malformed
`ops/entity-registry.json` — see Recovery; the one-line `python3 -c` check settles it.

**`brain_delete` refuses with "there is nothing for you to remove at those paths".** The caller's
identity is audience-RESTRICTED in `ops/identities.json`, and a removal needs an unrestricted one
(see Removing pages). The sentence is deliberately anonymous — it is the same one for a caller who
may not act and for a path that does not exist — so it never confirms a page or a referrer. The fix
is to run the removal under an unrestricted identity, or from the console; widening somebody's
scope is a knowledge-repo commit plus a redeploy (see Revocation).

**A capture attributed to the wrong identity over stdio.** The knowledge repo's own `.mcp.json`
declares two servers — `stigmergy` and `stigmergy-ana` — each pinned to its own `--identity` (an
unrestricted one and a scoped one), and a client session picks one on its own. Address the tool
explicitly ("use the `stigmergy` MCP server's `brain_submit`"); check with `stigmergy-queue list`
(it prints `submitted_by`). There is no
re-attribution tool by design — resubmit under the right identity and let the duplicate be refused.
(The librarian's own agent never loads that file: it runs with an empty MCP server list and strict
MCP config, because a file in the repo can declare any command.)

**The librarian ignores your change / files against a sha you don't recognize.** It branches
from `origin/main`, not your working tree — the skill, the linter and the
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
WHERE job IN ('gardener', 'capture-purge',
              'capture-purge-dry-run', 'webhook-index-upsert')
ORDER BY started_at DESC LIMIT 10;
```

A `gardener` row is `status='ok'` or `status='error'` — every check is deterministic, so a run
completes or it does not. A `status='partial'` row is a HISTORICAL one, from when the gardener had
model passes that could fail while the deterministic findings committed anyway; its findings are
as trustworthy as they ever were, because they were the deterministic ones. Gardener exit codes: 0
clean (findings are data, not errors), 1 the run failed, 2 precondition (bad `--repo`, bad
threshold, no database), 130 interrupted.

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

`args` is the full JSON for most tools, but `brain_submit` is audited by SIZE
and HASH only, never content. `outcome` is `ok` or `error`; `error_class` names the exception when
it isn't `ok`. The console's Activity page summarizes the same tables (latency percentiles,
answered-with-citation vs honest-refusal split); on a single-operator deployment its per-identity
counts are the operator's own credentials, never an adoption number.
