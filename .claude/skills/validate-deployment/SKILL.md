---
name: validate-deployment
description: >
  Verify a deployed Stigmergy release through MCP, Slack, the backoffice, the writer, the
  evidence store, and index reconciliation.
---

# Validate a Stigmergy deployment

Use this procedure after a deployment or a release spanning more than one subsystem. Read
deployment coordinates from the gitignored `.env` and Fly secrets; never print secret values.

## 1. Preflight

- Confirm every Fly process group is running the intended image and passes its health checks.
- Confirm Postgres, the private evidence bucket, the GitHub App, Slack credentials, and embedding
  credentials are configured without displaying them.
- Confirm the evidence endpoint is under EU jurisdiction (its host is
  `<account>.eu.r2.cloudflarestorage.com`; print only the host) and that the worker image imports
  `git_filter_repo`, which layer erasure needs.
- Confirm every active workspace's knowledge repository holds Markdown only (no workflows) and
  that the app connects as `stigmergy_app`, which cannot bypass row-level security.
- Inspect recent logs for restart loops, tracebacks, secret-bearing messages, and repeated jobs.

For a clean-cut test release, run the guarded reset before deployment validation. It must leave a
fresh target schema, an empty queue/index/object namespace, and the empty target knowledge scaffold.

## 2. Tenancy

- In the master console, every workspace shows its people, queue, index, gardener, and estimated
  spend; the writer page shows a current heartbeat and no unexplained failures.
- Create a throwaway workspace. Its repository appears in the organization with the scaffold, its
  first administrator's token signs in and sees only an empty wiki, and its index builds.
- With that token, search, read, list, and ask over MCP return nothing from any other workspace;
  a page path that exists elsewhere reads exactly like a missing one. Submit one capture and see it
  land only in the throwaway repository.
- An operator acting for one workspace sees only that workspace's screens; switching changes every
  screen. Suspend the throwaway workspace and confirm its token stops working on the next request.

## 3. Read path through MCP

After the first full rebuild, exercise all read tools with an authenticated identity:

- `search_brain` in both corpus languages and inspect the ranking arms (`fts`, `vec`, `entity`,
  `graph`), the one-line summaries, and the visible links of each hit;
- `read_page` on a visible result and on an unknown path, and on the `fragment` reference of a hit
  found inside a long page, which returns that part with its neighbours;
- `list_pages` by type, by a period (`since`/`until`), and by `links_to`, following `next` to the
  end, and `describe_entity` when an entity exists;
- `ask` for one supported and one unsupported answer, checking citations and honest refusal.

Repeat a restricted search/read with identities on both sides of a layer boundary, including a
temporary identity that is a member of a single layer. A page shared with another layer must show
only the reader's own and `public` units, and unknown, hidden,
and unauthorized pages and entity references must have indistinguishable external responses, including
`list_pages` totals and a `links_to` that names a hidden entity.

## 4. Unified capture and writer

Submit through the official local bridge:

- exact text;
- a local digital PDF;
- a local scanned PDF;
- a private Drive PDF using local Google OAuth when credentials are available.

As an identity that may publish to two or more layers, submit one capture without `audience`: the
receipt names `layer` and `layer_decision`, with `probabilities` when the classifier ran, and the
backoffice capture detail shows the same.

Observe `brain_submissions` reach `landed`. Each capture must retain exact original bytes, create one
neutral `sources/YYYY/MM/<capture-id>.md` path, and land one Git commit with one Changes record. A
retry with the same idempotency identity must not create another commit.

Check the `aliases` list of the first live capture reports after a release that touches the writer:
accepted variants are true other names, and no entry reports an unexpected failure class.

Verify a capture can create and later rewrite ordinary wiki knowledge without approval. Exercise an
explicit `brain_delete` against disposable knowledge and verify reference sweeping, current-search
removal, one commit, and one Changes entry.

## 5. Slack adapter

Only where the `slack` group runs (`STIGMERGY_SLACK_MACHINES=1`); otherwise confirm it has no
machine.

- Verify a mapped user can ask a question in a configured channel and receives a cited answer.
- React with the brain emoji to a thread containing multiple speakers and a supported attachment.
- Verify one normalized capture preserves order, speakers, timestamps, permalinks, and attachment
  boundaries in its neutral source page.
- Verify an unmapped channel, foreign workspace, unauthorized reactor, and invalid signature queue
  nothing and reveal no restricted context.

## 6. Backoffice

Sign in as an administrator and as a scoped member, and verify:

- sign-in sets an HttpOnly, Secure, SameSite=Strict session cookie; sign-out and token revocation
  end the session on the next request; a member meets every operational route as `not found`;
- the member's wiki catalog, pages, backlinks, graph, and search show only visible pages, and a
  hidden page reads exactly like a missing one;
- People, Layers, and Slack channels edit the roster; a new token is shown once and a change takes
  effect on the next MCP call and Slack event;

- paste, file upload, and public URL all create normalized captures;
- capture detail shows provenance, artifact/extraction state, retries, source, commit, and change;
- Changes presents a friendly per-path diff, collapses large source additions, and can reveal the
  hash-verified exact patch and parent/commit SHAs;
- Contradictions are derived from current Markdown and the resolution form queues an ordinary
  capture with rationale and optional evidence;
- Entities shows entity pages with aliases, and its rename/merge/delete controls queue atomic writer jobs;
- Gardener shows autonomous run summaries and creates no human task inbox;
- Index shows repository HEAD, indexed commit, dirty state, incremental event, full rebuild,
  and stale warnings and the filing context of recent captures, and its graph-measures panel shows a
  snapshot at the current indexed commit for
  the whole brain and each layer, with its constellation and drill-down lists;
- worker heartbeat and the last successful write are current; a heartbeat that reads `sleeping`
  is healthy, and a capture submitted then must wake the writer and land.

## 7. Autonomous maintenance and reconciliation

Trigger one gardener run from the backoffice. A clean corpus records a zero-change successful run;
a controlled repairable defect is fixed in at most one commit through the normal gates. A rejected
candidate must land nothing.

Request a rebuild of one workspace from the master console and require the writer to run it:
an `index-rebuild` job run succeeds, the Index screen records repository HEAD and clears the dirty
marker, every indexed page has a row in `page_fragments`, and each audience holding a page has its
own HNSW index whose predicate names that workspace and that audience alone. The writer rebuilds
every active workspace daily at `STIGMERGY_INDEX_REBUILD_AT` (default 04:17 UTC) and at once when a
workspace's index was never built.

## 8. Closeout evidence

Record the deployed image/version and each workspace's knowledge commit, the tenancy probe, Fly group health, representative MCP
responses, landed capture/change/commit IDs, Slack capture outcome, gardener run, index rebuild run,
and backoffice checks. Redact credentials, presigned URLs, private bytes, and restricted titles.

Do not call the release healthy if any required surface was skipped. State exactly what failed or
could not be exercised and keep the delivery open until it is fixed and rerun.
