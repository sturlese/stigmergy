---
name: validate-deployment
description: >
  Verify a deployed Stigmergy release through MCP, Slack, the master backoffice, the writer, the
  evidence store, and index reconciliation.
---

# Validate a Stigmergy deployment

Use this procedure after a deployment or a release spanning more than one subsystem. Read
deployment coordinates from the gitignored `.env` and Fly secrets; never print secret values.

## 1. Preflight

- Confirm every Fly process group is running the intended image and passes its health checks.
- Confirm Postgres, the private evidence bucket, the GitHub App, Slack credentials, and embedding
  credentials are configured without displaying them.
- Confirm the deployed knowledge-repository commit contains the target control files and pins the
  released platform commit in both workflows.
- Inspect recent logs for restart loops, tracebacks, secret-bearing messages, and repeated jobs.

For a clean-cut test release, run the guarded reset before deployment validation. It must leave a
fresh target schema, an empty queue/index/object namespace, and the empty target knowledge scaffold.

## 2. Read path through MCP

After the first full rebuild, exercise all read tools with an authenticated identity:

- `search_brain` in both corpus languages and inspect lexical/vector ranking arms;
- `read_page` on a visible result and on an unknown path;
- `list_entities` and `describe_entity` when an entity exists;
- `ask` for one supported and one unsupported answer, checking citations and honest refusal.

Repeat a restricted search/read with identities on both sides of an ACL boundary. Unknown, hidden,
and unauthorized pages and entity IDs must have indistinguishable external responses.

## 3. Unified capture and writer

Submit through the official local bridge:

- exact text;
- a local digital PDF;
- a local scanned PDF;
- a private Drive PDF using local Google OAuth when credentials are available.

Observe `brain_submissions` reach `landed`. Each capture must retain exact original bytes, create one
neutral `sources/YYYY/MM/<capture-id>.md` path, and land one Git commit with one Changes record. A
retry with the same idempotency identity must not create another commit.

Verify a capture can create and later rewrite ordinary wiki knowledge without approval. Exercise an
explicit `brain_delete` against disposable knowledge and verify reference sweeping, current-search
removal, one commit, and one Changes entry.

## 4. Slack adapter

- Verify a mapped user can ask a question in a configured channel and receives a cited answer.
- React with the brain emoji to a thread containing multiple speakers and a supported attachment.
- Verify one normalized capture preserves order, speakers, timestamps, permalinks, and attachment
  boundaries in its neutral source page.
- Verify an unmapped channel, foreign workspace, unauthorized reactor, and invalid signature queue
  nothing and reveal no restricted context.

## 5. Master backoffice

Authenticate with the master token and verify:

- paste, file upload, and public URL all create normalized captures;
- capture detail shows provenance, artifact/extraction state, retries, source, commit, and change;
- Changes presents a friendly per-path diff, collapses large source additions, and can reveal the
  hash-verified exact patch and parent/commit SHAs;
- Contradictions are derived from current Markdown and the resolution form queues an ordinary
  capture with rationale and optional evidence;
- Entities shows scoped claims/provenance and its merge/delete controls queue atomic writer jobs;
- Gardener shows autonomous run summaries and creates no human task inbox;
- Index health shows repository HEAD, indexed commit, dirty state, incremental event, full rebuild,
  and stale warnings;
- worker heartbeat and the last successful write are current.

## 6. Autonomous maintenance and reconciliation

Trigger one gardener run from the backoffice. A clean corpus records a zero-change successful run;
a controlled repairable defect is fixed in at most one commit through the normal gates. A rejected
candidate must land nothing.

Run the knowledge repository's `index rebuild` workflow manually and require a green run that
actually executes `stigmergy-index --rebuild --repo .`. Confirm it records repository HEAD and
clears the dirty marker. Confirm the nightly trigger is `17 4 * * *`, actions and platform are
pinned, and a deliberately missing required configuration fails visibly rather than succeeding as
a no-op.

## 7. Closeout evidence

Record the deployed image/version and knowledge commit, Fly group health, representative MCP
responses, landed capture/change/commit IDs, Slack capture outcome, gardener run, index rebuild run,
and backoffice checks. Redact credentials, presigned URLs, private bytes, and restricted titles.

Do not call the release healthy if any required surface was skipped. State exactly what failed or
could not be exercised and keep the delivery open until it is fixed and rerun.
