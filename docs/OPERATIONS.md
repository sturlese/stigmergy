# Operations

## Deployed services

The Fly application uses one image and separate `app`, `worker`, and `slack` process groups. Every
host it answers at (a custom domain with Fly certificates, and the app's `fly.dev` host) is listed in
the `STIGMERGY_PUBLIC_HOST` secret, or MCP and the backoffice refuse the request. The bare host
redirects to the backoffice sign-in, unless the operator built its own landing page into the image
and set `STIGMERGY_LANDING=on` (see Deployment). Every response carries
`X-Robots-Tag: noindex, nofollow`.

- `app`: streamable HTTP MCP, bridge upload endpoints, GitHub index webhook, the backoffice, and
  the master console.
- `worker`: the only knowledge writer for every tenant: filing, gardening, nightly index rebuilds,
  layer erasures, whole-workspace deletions, and new tenants' repositories. It stops its own machine
  after `STIGMERGY_LIBRARIAN_IDLE_EXIT_S` seconds with nothing to do (600 in `fly.toml`; 0 keeps it
  running), but never while any active workspace has queued or retrying work, an open erasure, a
  repository to create, or an index never built. Whatever queues work for it (captures, deletions,
  entity changes, retries, gardener runs, erasures, new or resumed workspaces, rebuild requests)
  starts it again through the Fly Machines API with the app's `STIGMERGY_FLY_API_TOKEN`, a deploy
  token limited to this app (`fly tokens create deploy`); without it the writer is never woken, so
  leave the idle stop at 0. While it sleeps its heartbeat reads `sleeping`. Nothing wakes it on a
  schedule: a daily gardener or index rebuild whose time passed while it slept runs as soon as it
  wakes.
- `slack`: Socket Mode adapter for the tenant whose `slack_team_id` is the configured workspace.
  It runs only where it is enabled: deploy with `STIGMERGY_SLACK_MACHINES=1` (or run
  `fly scale count slack=1`) to start it; its secrets stay set while it is off.

Between operations the worker checks the garden schedule and, at most every five minutes, purges
expired upload staging objects and access requests; neither maintenance path depends on an idle
queue.

Postgres stores tenants, platform operators, access requests, and, per tenant, queue/index/audit
state and the roster: people, layers, personal tokens (SHA-256 only), backoffice sessions, and Slack
channel layers. The service connects as `stigmergy_app`, which owns the tables and cannot bypass
row-level security; on a host with a public data API such as Supabase, the API roles (`anon`,
`authenticated`) have no usage on the schema. R2 stores private evidence and compressed exact
patches. Each tenant's private repository in the GitHub organization named by `STIGMERGY_GITHUB_ORG`
stores its current Markdown, and nothing else.

## Supported commands

- `stigmergy-server`: deployed API or local stdio server (`--tenant` names the workspace).
- `stigmergy-librarian run`: long-running writer for every tenant.
- `stigmergy-librarian-boot` and `stigmergy-librarian-credential`: worker bootstrap.
- `stigmergy-slack`: Slack adapter.
- `stigmergy-index --rebuild --repo <checkout> --tenant <slug>`: one tenant's full derived-index
  reconciliation from a checkout of its repository; it refuses a checkout whose GitHub remote is
  another workspace's.
- `stigmergy-admin operator bootstrap|mint`: the first platform operator and a break-glass operator
  token. `stigmergy-admin tenant create --slug <slug> --name <name> --repo <owner/name>`: register a
  workspace; it starts `provisioning`, so the writer creates its repository, unless `--status active`
  registers one that already exists. `stigmergy-admin --tenant <slug> bootstrap|mint <email>`: a
  workspace's first administrator (`bootstrap` also takes `--name <name>`) and a break-glass
  administrator token. Creating and suspending workspaces happen in the master console; everyday
  people, layer, and token work happens in the backoffice.
- `stigmergy-bridge`: supported local MCP client.

Queue inspection/retry, gardener triggers, entity operations, changes, contradictions, and health
are backoffice capabilities rather than separate product CLIs.

Capture processing has one total budget from its first worker claim. Its absolute deadline is stored
with the queue item, so a configuration deploy cannot reset it. All filing work is bounded by that
remaining budget. Only explicit transient provider, network, database, Git timeout, lock, extraction
timeout, or evidence failures receive one automatic retry after ten seconds. A filing request that
timed out (`TimeoutError`) or reasoned until its output limit (`UnexpectedModelBehavior`, error
`OutputExhausted`) is retried the same way on a new budget, because it spent the first one. Deadline
exhaustion and deterministic failures become visible terminal failures. If a worker dies after
publication, one bounded recovery-only claim may reconcile the existing commit without invoking the
model. The original content-addressed
evidence remains available, and the backoffice retry resets the attempt count and budget without
creating a duplicate capture.

Queue schema changes use a clean-cut deployment, never runtime compatibility DDL. Stop every process
that can enqueue or claim work, verify that `capture_queue` has no `processing` rows, apply the
change as the database owner, deploy the new image, then restore the process counts. Roll back in
the same drained order with the previous image. Never run old and new workers concurrently across a
schema boundary.

The `POST /admin/api/knowledge/recompile` control, for workspace administrators only, submits the
same writer request as gardening with `mode: recompile`; it is not an index rebuild and has no separate installed CLI. It
recompiles only derived notes, concepts, entities, projects, and links from committed immutable sources in an
isolated worktree. A failed source, repair, or gate abandons the entire candidate. Successful
recompiles use the existing `garden` trigger in the unified change ledger, with
`operation: recompile` in the run report.

## Refiling a workspace

A contract change that reshapes existing pages (a new page role, a new librarian skill) reaches an
existing workspace only by filing its captures again. The workspace is refiled into a fresh one
and its people are handed over, so the old graph stays whole until the new one is accepted. Run
the commands on a machine with the service's database credentials (the app machine), naming
yourself as an enabled platform operator:

1. Create the target workspace in the master console. Its only person is its first administrator,
   who must be one of the source's people.
2. Suspend the source in the console. Readers of the source lose access until the handover, and
   no capture lands in it meanwhile. A source that owns the deployment's Slack app is refused.
3. Export the source and import into the target:

   ```bash
   python -m stigmergy.ops.replay_workspace --operator EMAIL export --workspace SOURCE --out source.json
   python -m stigmergy.ops.replay_workspace --operator EMAIL import --workspace TARGET --file source.json \
       --confirm SOURCE:TARGET [--order ids.json]
   ```

   Export leaves out every capture whose source a person deleted, and refuses while the source
   still has captures to file. The export file holds the source's people and capture metadata:
   keep it on the machine and delete it after the handover. Import copies the layers and people,
   never tokens, into a target that holds nothing else. It queues every exported capture once, in
   the order given (`--order` must list every exported id), under an id derived from the target
   and the original capture, with the original actor, layer, origin, and artifact references.
   Running it again queues nothing twice. Start the worker if it is asleep (`fly machine start`):
   it files the target's queue one capture at a time, OCR'ing images again, while other
   workspaces keep their turns. A retried capture may file after the one queued after it.
4. When the queue is empty, retry its failures from the backoffice, rebuild the target's index,
   and compare the two graphs.
5. Hand the people over:

   ```bash
   python -m stigmergy.ops.replay_workspace --operator EMAIL handover --from SOURCE --to TARGET \
       --confirm SOURCE:TARGET [--accept-unlanded N]
   ```

   Handover refuses while the target is filing or while any exported capture has not landed in
   it, unless `--accept-unlanded` names exactly how many have not. It moves the live tokens and
   sessions of every enabled person both workspaces share to the target, so their clients keep
   working unchanged, and leaves the source suspended.

To roll back, resume the source in the console and run the handover with the workspaces swapped,
passing `--accept-unlanded N` for the captures filed in the target itself (the refusal names N):
they never reached the source and stay in the target. Delete the source from the master console once
the target is accepted; evidence the target references survives the deletion.

Entity operations address entity pages by path: `wiki/entities/<Title>.md` in `public`,
`layers/<layer>/wiki/entities/<Title>.md` in a layer. A merge folds the listed pages of one layer
into the first one and requires an exact same-entity assertion copied from a cited
immutable source path that names every selected page completely and without same-label or
contained-name ambiguity. A rename takes the new title; a delete sweeps links to the page.

## Deployment

A new deployment, from empty accounts to a first answer, is set up as
[`SELF-HOSTING.md`](./SELF-HOSTING.md) describes.

```bash
make test
make lint
make deploy-staging PARITY_ARTIFACT=/absolute/path/to/parity-result.json
STIGMERGY_PARITY_GATE=off make deploy-staging   # a released version, without parity evidence
make rebuild-staging
```

Staging rebuild first refreshes the knowledge checkout at `STIGMERGY_REPO` (default `../knowledge`),
which must be one workspace's checkout, and indexes it as `STIGMERGY_TENANT` or the only active
workspace. The checkout must be clean, attached to a branch with a configured upstream, and able to
fast-forward to that upstream; otherwise the operation fails closed. The refresh yields the
checkout's canonical physical root and its verified commit SHA, and subsequent work is bound to that
exact revision. The rebuild passes `STAGING_DSN` only through the process environment, so the
database credential is neither included in command arguments nor written to command output.

`scripts/deploy_staging.sh` serializes staging deployments with a fail-closed lock, requires a
clean platform checkout and, unless the gate is off, a passing parity artifact, deploys the image,
and scales to one `app` and one `worker` machine and to as many `slack` machines as
`STIGMERGY_SLACK_MACHINES` (0 or 1, default 0). The image carries no roster: people, tokens, and
Slack channel layers live in Postgres. The parity gate is the release gate for a change to the
librarian contract; an operator deploying a released version to their own deployment turns it off
explicitly with `STIGMERGY_PARITY_GATE=off`, which the script reports, and any other value keeps it.

Deploy the cloud server before publishing a bridge that depends on its MCP contract. During rollback,
restore the compatible bridge first, then roll back the cloud server; this keeps local acquisition
from calling an unavailable or newer server contract.

The deployment's own names are Fly secrets, not `fly.toml` values: `STIGMERGY_PUBLIC_HOST` and
`STIGMERGY_GITHUB_ORG`. Legal pages (sub-processors, privacy, terms) belong to whoever operates the
deployment, so none ship with the platform. Keep them as Markdown files, one per page, outside the
checkout and deploy with `STIGMERGY_LEGAL_SOURCE=<directory>`: the script copies each `*.md` onto
every machine under `/srv/legal` and sets `STIGMERGY_LEGAL_DIR`; pass it on every deploy. The
server answers for every file whose name matches `^[a-z][a-z0-9-]{0,63}$`: `/legal/<name>` returns
its HTML (raw HTML escaped) and, with a landing page on, `/<name>` serves the landing, which can
fetch and render it.

The platform ships no landing page. An operator may build its own into the image: place `web/landing.html` and `web/src/landing/` (both ignored by git) in the build context before deploying; the console build adds them as a second entry. It is served at the
bare host only when the `STIGMERGY_LANDING` secret is `on` (or `1`, `true`) and the image holds it;
otherwise the bare host redirects to `/admin/`. A landing page may post to the platform's
access-request endpoint, which stores an email in `access_requests` for the master console.

A fresh database has no operator. Run `stigmergy-admin operator bootstrap <email> --name <name>`
once with direct database access (for example
`fly ssh console -C "stigmergy-admin operator bootstrap …"`); it prints one token, which opens the
master console, and refuses to run once an operator exists. Operator tokens expire after 90 days;
`stigmergy-admin operator mint <email>` issues a new one. Workspaces are created in the master
console: company, workspace name (the slug), and the first administrator's email and name; that
administrator's token is shown once, and the writer creates the private repository
`<org>/<slug>-brain` within seconds. `stigmergy-admin --tenant <slug> mint <email>` issues a new
token to an existing administrator of that workspace when every administrator token is lost.

### Tenants

The `STIGMERGY_GITHUB_ORG` secret names the organization that holds workspace repositories. The
librarian App is private to that organization and installed there for all repositories, with
contents and repository administration write and the `push` event; its one webhook posts to
`/webhook/github` with `STIGMERGY_GITHUB_WEBHOOK_SECRET`, and `STIGMERGY_LIBRARIAN_INSTALLATION_ID`
names the installation. The worker's checkouts live under `STIGMERGY_REPO`, one per tenant id,
cloned on first use. Suspending a workspace in the master console refuses its tokens, sessions, and
pushes and stops its work; its knowledge stays. A workspace's index can be rebuilt from its row in
the master console. A suspended workspace can be deleted whole from its row in the master console
(type its slug to confirm); the writer deletes its repository, rows, indexes, evidence, and
checkout, the console shows each step, and the slug is then free for a clean start. A workspace that
owns the Slack app cannot be deleted there. `docs/ERASE.md` lists what stays out of reach.

### Index schema changes

The index tables are shared by every tenant and derived from their repositories, so a release that
changes them drops and rebuilds them rather than migrating them:

1. Stop the worker and deploy the platform.
2. Drop `page_fragments` and `pages_index` as the database owner. In the same session run
   `SET app.scope = 'platform'` (row-level security hides every tenant's row otherwise) and set every
   tenant's `index_health.last_full_rebuild_at` to null.
3. Start the worker. It rebuilds each tenant's index, recreating the tables, on its next passes.

Until a tenant's rebuild finishes, its search and webhook indexing fail on the missing table and its
dirty marker stays set; run the window while the workspaces are idle.

### Layers

A layer is a roster group; its knowledge lives under `layers/<layer>/` in the knowledge repository.
Every identity needs a default layer (`public` or one of its layers) before it can capture without
naming one, and every Slack channel maps to exactly one layer. A layer that holds members or a
channel cannot be deleted, and neither can one the index cannot show to be empty: deletion needs an
index that has converged on the repository, no page in the layer, and no capture still being filed
there. Administrators give or remove access to a layer from its card in Access → Layers, or per
identity in People; every change is one audited identity update. People and agents alike sign in to
the backoffice with their token and read the wiki of their layers; only people can administer.

A layer's description is its routing rule. A capture that names no layer, from an identity that
may publish to two or more restricted layers, is routed by the classifier among those layers'
descriptions; when no single layer clearly fits it files in the identity's default layer. Change
routing by editing a description, never code. A description of up to 1,000 characters routes best
when it says what the layer is, what typically arrives there (kinds of documents, recurring
meetings, people, and organizations), and what does not belong there and where it goes instead. Review routed captures in Captures → Classified,
where the detail shows the probabilities; a misrouted capture is corrected by deleting it and
filing it again in the right layer. Slack captures always take their channel's layer, and the
backoffice always names one. Mapping a channel to a layer asserts that everyone in the channel may
read that layer: answers there are given at the channel's layer and the capture acknowledgement
names it.

### Erasing a layer

Access → Layers → the layer's eraser shows what the index, queue, and ledger hold for the layer and
queues its erasure once the name is typed; workspace administrators only (platform operators are
refused), recorded as `layer.erase`. The writer runs it before its next operation, under its lock,
with the heartbeat at `erasing`; until it ends, captures to the layer are refused (`layer_erasing`)
and its queued work is not filed. It mirrors the knowledge repository, removes `layers/<layer>/`
from every branch and tag with `git-filter-repo`, and force-pushes them atomically with a lease on
each ref, so nothing may stop the GitHub App from force-pushing any branch or tag. Then it resets
its checkout and drops the old objects, deletes the layer's rows and redacts others that name it,
remaps every stored commit, deletes the evidence objects nothing else references, rebuilds the
index, and prunes unused cached embeddings. The receipt on the layer card and the `layer.erased`
admin action hold the counts, the old and new heads, and what remains elsewhere: GitHub's cached
copies of the old commits and pull requests that reach them, until GitHub Support removes them (the
receipt lists the first changed commits the request needs); Supabase backups until their retention
ends; and clones made before the erasure.

Until the rewritten history is pushed, a failed attempt changes nothing; after three the request is
marked failed and the layer reopens, and asking again starts afresh. A request is marked failed only
when its push verifiably never landed. Once pushed it never fails: it retries with backoff, at most
15 minutes apart, until the rest is done; the layer stays closed, other layers keep filing, and no
other erasure of the workspace starts meanwhile. After an erasure, delete or clone again every other
checkout of the knowledge repository: a push from an old clone would bring the layer back. Delete the emptied layer with the ordinary
delete.

### Slack application

`deploy/slack-app-manifest.json` is the Slack configuration contract. Import it from the app's
**App Manifest** page, reinstall or reauthorize the app whenever its OAuth scopes change, and issue
an app-level Socket Mode token with `connections:write`.

The bot needs `files:read` to download user attachments and `groups:history` to capture mapped
private channels. `chat:write` also covers private delivery when the asker may see more than the
channel's layer. It does not need `im:write`, `files:write`, or `reactions:write`: users provide
attachments, and capture does not write reactions. Invite the bot to every mapped public or private
channel.

After installation, verify that the token's granted scopes match the manifest. Slack returns them
in the `x-oauth-scopes` response header; a checked-in manifest alone does not update an already
installed token.

After deployment, validate:

1. Fly release and all required machines are healthy.
2. `/health` and authenticated MCP initialization succeed.
3. The worker heartbeat advances and no processing lease is stale.
4. A text capture lands with one source, one commit, and one Changes entry.
5. A controlled multi-idea capture produces a cohesive, linked set of cold-readable pages with local
   citations; a rejected plan leaves only its immutable source, reports `plan_rejected`, and its
   summary says the filing plan was rejected so the submitter knows to send it again.
6. Search sees the landed result only for members of its layer, and one `describe_entity` call
   returns the fused entity page with the bounded visible pages that link it.
7. A layer-isolation probe, with an identity that is a member of a single layer, proves that other
   layers' pages, entity parts, backlinks, names, and source titles do not appear in its responses,
   and that a page shared with another layer shows only its own and `public`'s units.
8. A workspace administrator's recompile preserves immutable sources, records `operation: recompile` under the `garden`
   ledger trigger, and either advances the complete derived candidate or advances nothing.
9. An administrator signed in to the backoffice sees the capture, friendly diff, exact patch, entities, contradictions,
   gardener runs, and index-health state.
10. A full rebuild indexes repository HEAD, records row count/time, and clears the dirty marker.
11. Slack authentication and channel mapping are healthy; a controlled brain reaction lands through
   the same capture path with a supported attachment when a safe test channel is available.

Every staging deployment first validates the recorded real-model parity artifact:
`python evals/filing/parity.py --artifact <result.json> --repo-root <platform checkout>`. The command
is a fail-closed release gate: a non-zero result blocks rollout. Schema 9 binds three
implementations to the same corpus, immutable initial graph, and source-case and fixture hashes:
`hippocampus` as the baseline, the `stigmergy` candidate at the checkout's commit and librarian
skill hash, and `production` at a deployed commit the candidate descends from, with that commit's
skill hash. It requires recorded raw gates; each case's initial plan, optional bounded correction,
and replayed effective output; exactly three complete production-equivalent candidate repeats bound
to the configured librarian setting, and as many production repeats; and a verified
content-addressed blind-review bundle whose review finds no more material regressions in the
candidate than in production. The review bundle sits beside the artifact; keep both outside the
candidate checkout ([`evals/filing/README.md`](../evals/filing/README.md)).

The gate trusts the release operator and verifies evidence integrity within that operational boundary.
Give the reviewer only the blind packet and response schema, retain the response verbatim, and unblind
it mechanically. The hashes prove byte identity and binding, not reviewer identity against a malicious
operator with Fly credentials. That actor is out of scope for the local deployment flow: protecting
against it requires a protected CI environment to own both deployment authority and reviewer
attestation, rather than a signing key stored on the same release host.

## Aliases recorded at filing and by the garden

Each capture report lists the alias decisions under `aliases`: accepted, rejected, skipped with a
reason (`deadline`, `cap`, `budget`, or the classifier's failure class), or reverted when a gate
refused them. The capture detail in the backoffice shows the same list. An alias stays when its
source is deleted. There is no direct operation that removes one alias: a capture whose evidence
states the entity's names lets the librarian rewrite the list, and merging or renaming the entity
rewrites it too. The daily garden records the variants already linked in the corpus the same way
and reports how many entity pages it changed as `aliases_recorded`.

## Nightly reconciliation

The writer rebuilds each tenant's index daily at `STIGMERGY_INDEX_REBUILD_AT` (default `04:17`
UTC) under the writer lock, from that tenant's checkout fast-forwarded to its remote tip, and
records an `index-rebuild` job run. An index never built (a new tenant, a migrated one, or one the
console asked to rebuild) is rebuilt on the writer's next pass, at most every ten minutes while
that keeps failing. A daily rebuild missed while the writer slept runs when it wakes. The
backoffice warns when the last successful full rebuild is older than 26 hours and the index changed
since, or after 15 minutes while the index remains dirty.

A production full rebuild accepts only the exact, clean knowledge-repository root at its checked-out
HEAD.
It verifies HEAD again before publishing the rebuilt index so concurrent repository movement leaves
the previous index intact and the dirty marker visible.

The rebuild reads committed HEAD only and fails closed above 50 MiB for one page, 512 MiB across the
candidate corpus, 500,000 watched entries, or 250,000 eligible entries. It also reconciles recovered
committed source pages into the ACL-aware source projection, so `read_page` can serve readable
evidence after recovery without exposing it beyond its audience.
