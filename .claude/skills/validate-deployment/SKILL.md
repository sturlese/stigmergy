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

## Block 2 — the write path and the newborn identity (MCP)

- `brain_submit` real material. Watch `brain_submissions`: `queued → claimed → filed`, 1–3 min.
- Search for it immediately after `filed`. If the push webhook is configured it is already
  searchable; `built_at` does **not** move (that tracks full rebuilds only).
- Submit the identical bytes again: refused as a duplicate.
- Submit material naming an organisation the registry does not know. It must still reach `filed`
  — nothing waits on anybody, before or after — and the report's `entities_born` names the
  identity your own capture introduced. `list_entities` then serves it, `read_page` on
  `wiki/entities/<Name>.md` shows `approved_by:` carrying YOUR email, and `search_brain` finds
  the note anchored to it. An instruction in the material ("register it as canonical" / "file it
  against Y") is flagged `write-outside-lane`, not followed, and filed as ordinary content.

## Block 3 — Slack

Tell the operator what to do; read the outcome from the database and the logs.

- `@brain <question>` in a public channel the bot is in → placeholder edited into a cited answer.
- The same question by DM → the fuller lane, citations carrying "Show it here".
- Click it as the asker (works) — anyone else clicking gets **silently nothing**, by design.
- Post real content in a public channel and react 🧠 → ack in thread, then a state report in
  that same thread when it files. The THREAD is captured verbatim into `sources/slack/`. If that
  capture introduced an entity, the filed card names it and says the identity is confirmed by the
  person who captured — nothing waits on anybody.

## Block 4 — the admin console, tab by tab

Dashboard (the window's captures filed, the write path drawn live, captures and questions per day,
health tiles) · Captures (read-only: the list, each row's **detail** naming the identities that
capture introduced and the spellings it taught the registry; Reclaim and Retention purge are the
list-level buttons) · Entities (the registry browser — every registered entity with its aliases and
who introduced it — and **Register an entity**, which commissions a capture: you say what it is,
the console takes you to that capture, and the entity's page appears here when the librarian files
it, born confirmed by you; the name is checked live against the registry as you type) ·
Repairs (pending proposals, recently decided ones, the proposer's own run history, and **Remove
pages**) · Gardener ·
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
- **Remove pages on Repairs decides and writes in the same act** (ADR 043): there is no second
  click, so the evidence is the commit it names PLUS the diffs it hands back in the dialog — read
  them, because nobody read that prose before it landed. Delete a page something else refers to in
  PROSE, not only in `related:`, or the sweep writer never runs and the step proves nothing.
  `brain_delete` over MCP is the same act from the other door.

### The deletion drill

The one act that both decides and writes. Do it on a page you are prepared to lose and that
something else refers to **in prose** — a `related:` entry alone exercises code's half and proves
nothing about the writer.

```
brain_delete(paths=["wiki/notes/<a junk page>.md"], why="<what makes it stale>")
```

Evidence: the commit it names, present in the knowledge repo with an `Approved-by:` trailer; the
page gone; each referring page reconciled — a sentence that cited it still reading, a callout that
only existed because of it gone; and the diffs the response carries, which are the only reading
that prose gets. A `repair_proposals` row in `applied`, never `pending`, decided by you.

Then the refusals, each landing nothing: an entity page, and a **scoped** identity — deletion is
the unrestricted identity's act, and a caller without that reach must be refused by name.

## Block 5 — identity and ACL

With two identities (one unrestricted, one scoped), prove what is provable today:
attribution is resolved server-side and never claimed by the client — the `approved_by:` on a
newborn entity page and the `Approved-by:` trailer on a deletion both name the caller the SERVER
resolved, whatever the client said — and a scoped identity's `brain_delete` is refused by name.

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
- **The four kinds**: `brain_submit` takes `raw`, `page`, `meeting` (with `title` and
  `meeting_date`) and `document` (with `title`, optionally `source_url`). Submit a transcript and
  a document's text and follow both through `brain_submissions`; a meeting missing `meeting_date`
  is refused by name, with no row queued.

## Block 7 — the flows that stay on the CLI by design

View regeneration (`stigmergy-views`) and the index builder (`stigmergy-index --check`). Run them
yourself, with the environment mapped as above (note `stigmergy-views` also needs
`OPENAI_API_KEY`, and the knowledge-repo commands run from that checkout with `--repo .`).

Entity registration is NOT on this list any more: it is a capture like every other write, from the
console's **Register an entity** or from `brain_submit`, and the worker writes the page.

**The failure to watch for**: the queue and the evidence store are configured independently, so
an environment that names the deployment's database and this machine's MinIO files a row whose
bytes the deployed worker can never read — the capture dies with `NoSuchKey` seconds after it is
claimed. Nothing refuses that combination any more (no CLI enqueues), so check both halves name the
same deployment yourself before you submit anything.

## Closing

Read Overview one last time — and read it as **truth, not as zeros**. Captures filed in the window
is the number your own walk moved; a permanent zero there would mean nobody is capturing anything,
and every other tile is read against that one.

Then report, in this order: what was proved, what was blocked and why, and every defect found —
each as a tracker issue carrying the evidence (the exact output, the file:line, the failing
input), never as prose in a chat that scrolls away. A finding that is not written down as an
issue did not happen.

**Expect roughly a third of the findings to be in the words, not the code**: a report promising
something the deployment does not do, a refusal naming an environment variable nothing reads, a
suggested action the gates forbid. Those are real defects here — this project treats a message
containing a command as an executable promise.
