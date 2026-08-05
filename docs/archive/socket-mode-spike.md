# Socket Mode spike — measured 2026-07-28

This measurement was owed **before** anything was built on top of Socket Mode, because the
deployment pins `max-instances=1` on an *assumption*: that a single Socket Mode instance is stable
enough to be the only listener, and that **duplicate event handling across a deploy's revision
overlap** is the risk worth designing against. Nothing in this project had tested either claim.

It was run late — after the Slack transport and the answer path were already built — because no
Slack workspace existed until then. That ordering is a deviation, and is recorded as one.

**Verdict: GO for the pilot.** No duplicate handling was observed, the singleton is enforced by
mechanism rather than by comment, and the failure mode the design feared did not appear. The
headline finding is that the *assumption under test was aimed at the wrong risk* — see §3.

## 1. Setup

- Workspace `stigmergy`, app `brain`, Socket Mode, 12 bot scopes, 4 event subscriptions.
- `stigmergy-slack` run locally against the docker Postgres, real OpenAI embedder,
  `gpt-5.6-terra` answering model, 41-page reference corpus in `pages_index`.
- Every claim below is read from `audit_log`, not from a screenshot. A screenshot misled this spike
  once (§3) and is not evidence of absence.

## 2. What was measured

### 2.1 Steady state — PASS

Two questions in a public channel, cold. End to end, from first tool call to the `ask` row:

| question | tools | wall clock | verdict |
|---|---|---|---|
| an answerable one | 2× `search_brain`, 3× `read_page`, 1× `ask` | **7 s** | `verified`, 5 citations, not suppressed |
| one with no answer in the corpus | — | ~5 s | refused honestly, not suppressed |

Identity resolved from the Slack profile email on every call; every row attributable.

### 2.2 The singleton, enforced by mechanism — PASS

A second `stigmergy-slack` started against the same database **refused to start**, exited non-zero,
and named both the risk and the operator fix:

```
stigmergy-slack: another stigmergy-slack process already holds the singleton lock — Socket Mode has no
leader election, so a second machine in the `slack` process group would double-handle every event
Slack delivers. Refusing to start; see the operator runbook
(`fly scale count slack=1`, never raised).
```

The first instance was unaffected. This is `pg_try_advisory_lock` in `slack.app`, added precisely
because the shipped defense had been a comment in `fly.toml`.

**Scope limit worth knowing before staging goes live**: the lock is held **per database**. A local
bot on the docker Postgres and a staging bot on Supabase hold *different* locks, so **running both
against the same Slack app WILL double-handle every event**. The mechanism protects one deployment
from itself, not one Slack app from two deployments. Operational rule: stop the local bot before
the staging one is live.

### 2.3 Hard kill and reconnect — PASS

`kill -9` mid-session, then restart. The process came back, re-acquired the lock, reconnected, and
resumed answering with no manual step. No duplicate handling of any earlier event.

### 2.4 Events delivered while the connection was down — **REDELIVERED, not lost**

The question this spike most needed to answer, and the one where the first reading was wrong.

Method: kill the bot, ask a question in the channel with it down, restart, then ask a *control*
question to prove the restarted bot was genuinely connected.

```
08:26:37 | control con el bot vivo        (sent AFTER the restart)
08:26:43 | control con el bot vivo        (its DM-fallback run)
08:26:44 | pregunta con el bot caido      (sent WHILE the bot was down)
08:26:49 | pregunta con el bot caido      (its DM-fallback run)
```

Slack **buffered the event and delivered it on reconnect**, roughly a minute later. It answered.

**The correction, recorded because the mistake is instructive**: a screenshot taken shortly after the
restart showed the down-time message with no reply, and an `audit_log` count taken 25 s after
restart still read pre-kill. Both were read as "events during downtime are silently lost", which
would have gone into the runbook as an operational fact and would have been false. The bot simply
had not finished connecting yet. **Absence of a reply is not evidence of loss until the control
proves the listener was live.**

Consequence for the pilot: **a deploy does not silently swallow questions.** Expect a delay, not a
hole.

### 2.5 Duplicate handling — NONE OBSERVED

Across the whole session — including the kill/restart and the refused second instance — every
question produced exactly the expected number of `ask` rows and no event was handled twice.

## 3. The finding that matters most: the assumption was aimed at the wrong risk

`max-instances=1` is pinned against **duplicate** handling. That risk is real but is now closed by
mechanism (§2.2), and it never fired in practice. What the measurement actually surfaced is that
Socket Mode's weak point is **delivery timing**, not duplication — and even that turned out
benign, because Slack buffers and redelivers.

So the assumption survives, but for a reason nobody wrote down: not because a single instance is
inherently stable, but because **Slack's own redelivery covers the gap a single instance leaves**.
If that behaviour ever changes, the design's safety margin changes with it — which is why it is
written here rather than left as folklore.

## 4. A cost observation, not a defect

Every channel question from an **unrestricted** identity costs **two** answering runs. The channel
is not listed in `ops/slack-channels.json`, so its scope is the empty set; an unrestricted asker is
therefore always strictly wider, the retrieval-set comparison always finds a difference, and the DM
fallback always fires — working exactly as specified.

A later fix took that second run off the asker's **rate-limit budget**; it does not take it off the
**bill**. For a pilot where several people ask through a channel, budget accordingly — or list the
channel with the audiences it should see, which collapses the comparison for anyone whose own scope
matches it.

## 5. What this spike did NOT measure

Stated plainly rather than implied by omission:

- **A ≥2-hour sustained window.** The session was tens of minutes. Long-run connection stability is
  untested.
- **A real Fly revision overlap.** The kill/restart above is a local approximation. The actual risk
  the `max-instances=1` pin names — two revisions briefly alive during a deploy — needs the staging
  deployment, and §2.2's per-database caveat means it must be measured there, not here.
- **Load.** One person asking. Nothing about concurrent askers or rate-limit behaviour under a
  cohort.

## 6. If Socket Mode ever fails this

The fallback is already permitted: the HTTP Events API on the existing `app` process group, with
Slack's signature verification and no third process group. Taking it is a scope change to be raised
first, not a silent substitution. One point in its favour, learned here: the Events API retries
failed deliveries explicitly, where Socket Mode's equivalent is Slack's buffering — a behaviour we
observe but do not control.
