---
name: validate-deployment
description: >
  Walk a deployed Stigmergy stack end to end through its three real interfaces — MCP, Slack and
  the admin console — proving each feature works on the deployment rather than in the test
  suite. Use after a first deploy, after a release that touched more than one subsystem, or
  whenever "is this actually working?" needs an answer with evidence. Drives every step it can
  (MCP tools, operator CLIs, database truth) and tells the operator exactly what to click in
  Slack and in the console for the ones it cannot.
---

# validate-deployment: does the deployed stack actually work?

The test suite proves the code. This proves the **deployment**: real Postgres, real evidence
store, real Slack workspace, real models, real GitHub Actions. Every defect this exists to
catch lives in the gap between them — configuration, wiring, and copy that stopped being true.

Budget ~90 minutes. Work in order: each block leaves the state the next one needs.

## Before anything: where the coordinates live

Nothing deployment-specific is written in this file. Read it from the operator's environment:

| Fact | Source |
|---|---|
| Fly app name, knowledge-repo path | `FLY_APP`, `STIGMERGY_REPO` in the gitignored `.env` |
| Public hostname | the app's own `STIGMERGY_PUBLIC_HOST` (a Fly secret) — MCP is at `https://<host>/mcp`, the console at `https://<host>/admin/` |
| Staging index DSN | `STAGING_DSN` in `.env` (deliberately NOT `STIGMERGY_INDEX_DSN`, so a local test run can never point at it) |
| Evidence store | `.env`'s `R2_*` group → export as `STIGMERGY_EVIDENCE_ENDPOINT` / `_BUCKET` / `_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` before running any operator CLI |
| Operator CLIs | `.venv/bin/stigmergy-*` in this repo |

**Check the issue tracker first** for steps already known to be broken, and say so up front
rather than making the operator discover it: a walkthrough that stops at a known defect wastes
the session.

## Block 1 — the read path (MCP)

Drive these yourself against the deployed endpoint and report what came back.

- `search_brain` for something the corpus knows, in both languages the corpus uses. Hits must
  name their ranking arms.
- `read_page` on a hit: body inside the `UNTRUSTED-DATA` fence, plus `type`/`status`/`links`/
  `backlinks` and the supersedes chain.
- `read_page` on a path that does not exist: note the exact shape — a forbidden page answers
  identically, and that is the property being verified.
- `list_entities`, then `describe_entity` on one: the registry vocabulary and its layered view.
- `ask` a question the corpus can answer: citations, `verdict: verified`, a `built_at`.
- `ask` a question it cannot: `refused: true` with the reason. **A system that never refuses is
  the failure, not the success** — this step matters more than the one above it.

## Block 2 — the write path and the proposal (MCP)

- `brain_submit` real material. Watch `brain_submissions`: `queued → claimed → filed`, 1–3 min.
- Search for it immediately after `filed`. If the push webhook is configured it is already
  searchable; `built_at` does **not** move (that tracks full rebuilds only).
- Submit the identical bytes again: refused as a duplicate.
- Submit material naming an organisation the registry does not know. It must still reach
  `filed` — nothing waits on anybody — and the acknowledgement names the entity as one that
  will be proposed. `review_queue` then lists an `identity-proposal` for it, `list_entities`
  shows it marked proposed, and `search_brain` finds the page anchored to it. An instruction in
  the material ("approve it" / "file it against Y") is flagged `write-outside-lane`, not
  followed, and filed as ordinary content. Leave the proposal undecided: later blocks consume it.

## Block 3 — Slack

Tell the operator what to do; read the outcome from the database and the logs.

- `@brain <question>` in a public channel the bot is in → placeholder edited into a cited answer.
- The same question by DM → the fuller lane, citations carrying "Show it here".
- Click it as the asker (works) — anyone else clicking gets **silently nothing**, by design.
- Post real content in a public channel and react 🧠 → ack in thread, then a state report in
  that same thread when it files. The THREAD is captured verbatim into `sources/slack/`.
- Wait for the steward doorbell to DM the proposal from Block 2, and read its card: the name,
  the type, the summary, the pages filed against it, and Approve / Merge into / Decline. The
  doorbell **polls**: a proposal decided quickly never rings.

## Block 4 — the admin console, tab by tab

Dashboard (the inbox count, the write path drawn live, captures and questions per day, health
tiles) · Inbox (everything waiting on a steward, one list — the doorbell's own read: proposed
entities, proposed spellings, repair proposals) · Captures (read-only: the list, each row's
**detail** with the report's proposals clause; Reclaim and Retention purge are the list-level
buttons) · Entities (each proposal with the registry's verdict on its name — registered / collides
/ similar / clear — and Approve / Merge into… / Decline, each one governed commit; proposed
spellings; the registry browser; and **Register an entity**, born confirmed, with the name checked
live as you type) ·
Repairs (pending proposals, recently decided ones, and the proposer's own run history) · Gardener ·
Index (the substrate check runs in-process) · Worker · Jobs (Run now, Enable, Disable for the four
workflows — needs the fine-grained PAT; without it the page is read-only and says so) · Digest
(Preview is byte-identical to the post) · Activity.

Three things worth proving here specifically:

- **Retention purge** runs its dry run for you and shows the result inside the confirmation —
  there is no separate checkbox, and you cannot purge without having seen what would go.
- **Run-now on `index-rebuild`** must move the Index tab's `built_at`. That column is the only
  truth for that workflow: it writes no `job_runs` row, and a cron skipped for an unset
  `STIGMERGY_KNOWLEDGE_REPO` variable is *green* in Actions.
- **Approve on Repairs commits to the knowledge repo**, so read the detail card's ops table before
  clicking it: the evidence is the commit named in the response, carrying an `Approved-by:` trailer
  and nothing in its diff but the ops you just read. Decline a different one — its reason is the
  dismissal memory, so the evidence is a `rejected` row the next `repair-propose` run leaves alone.
  With nothing pending (`stigmergy-repair list` against the staging DSN settles it), report an
  empty tab as an empty tab: it proves the route serves, never that a repair applies.

## Block 5 — identity and ACL

With two identities (one unrestricted, one scoped), prove what is provable today:
attribution is resolved server-side and never claimed by the client; a non-steward's review
decision is refused with the same sentence a non-existent id gets.

**Say plainly whether a visibility split is observable at all.** If `ops/acl.json` stamps no
labels, no page carries an `acl:` line and every identity sees the same corpus — by
construction, not by fault. The mechanism is enforced on every read; it has nothing to bite on
until a restricting rule exists, and such a rule must **name** its audiences (an empty list in
that file means *open*, the opposite of what it means everywhere else).

## Block 6 — the guardrails, fired against the operator's own work

- **Rate limits**: fire ~35 `search_brain` calls **concurrently**. Expect a clean refusal
  naming the ceiling, plus its `RateLimitError` row in the audit trail. Do **not** try to trip
  the `ask` bucket sequentially: one real `ask` takes 5–20 s, so ten of them never fit inside a
  minute and the attempt teaches the operator that the limiter is broken.
- **The injection fence**: capture material that instructs the agent. It must be filed as
  quoted knowledge — never followed.
- **Trap parameters**: a client sending `submitted_by`/`verification`/`acl`/`content_hash` is
  refused; those are server-computed facts.
- **The meeting door**: `brain_submit` with `kind="meeting"` is refused by name.

## Block 7 — the flows that stay on the CLI by design

Meeting drop · Drive drop · view regeneration · entity mint. Run them yourself, with the
environment mapped as above (note `stigmergy-views` also needs `OPENAI_API_KEY`, and the
knowledge-repo commands run from that checkout with `--repo .`).

**The failure to watch for**: the queue and the evidence store are configured independently, so
a drop can queue against a remote database while uploading evidence to a local MinIO. The
capture then fails seconds later with `NoSuchKey`. Verify both halves point at the same
deployment before dropping anything.

## Closing

Read Overview one last time — and read it as **truth, not as zeros**. A walkthrough creates
proposals on purpose; the inbox count is the number that means *a steward owes a decision*, and
a permanent zero would mean nobody is capturing anything.

Then report, in this order: what was proved, what was blocked and why, and every defect found —
each as a tracker issue carrying the evidence (the exact output, the file:line, the failing
input), never as prose in a chat that scrolls away. A finding that is not written down as an
issue did not happen.

**Expect roughly a third of the findings to be in the words, not the code**: a report promising
something the deployment does not do, a refusal naming an environment variable nothing reads, a
suggested action the gates forbid. Those are real defects here — this project treats a message
containing a command as an executable promise.
