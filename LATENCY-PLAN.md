# Latency plan — where the seconds go, and the five changes worth making

Measured on the staging deployment on 2026-08-04: `audit_log.duration_ms` (179 tool calls),
`capture_queue` phase timestamps, and the code paths behind them. Scope: **user-perceived time
only**. Anything a human cannot feel next to an LLM round-trip is out of scope by design, and
listed at the end as explicitly rejected.

> **Status, 2026-08-06.** Items 3.1, 3.4 and 3.5.2 have landed; 3.2 was decided and not adopted;
> 3.5.1/3.5.3 and the *eager first search* (below, §7) are open. Every projection in this document
> that has since been measured is corrected in place rather than left standing — §3.1's expected
> win in particular turned out roughly half of what was first written here, and the instrument that
> would confirm it did not exist until this batch built it.

---

## 1. The measured map

| Flow | Measured today | Verdict |
|---|---|---|
| `ask` (MCP and Slack Q&A) | **p50 12.6 s · p90 20.4 s · max 25 s** (n=19) | The #1 pain |
| `search_brain` | p50 255 ms · p90 792 ms | Fine (embedding round-trip dominates) |
| `read_page` / `describe_entity` / `list_entities` | 10–50 ms | Irrelevant |
| `brain_submit` ack | 350 ms – 1.1 s (validation + R2 put + INSERT) | Fine |
| Slack 🧠 capture → visible ack | est. 1.5–4 s, **zero feedback until then** | Pain #3 (perceived) |
| Capture → `filed` report | ~5 s queue wait + **p50 111 s processing** + ≤10 s Slack poller | Pain #2 |
| Capture → searchable | seconds after commit (webhook upsert) | Fine |

### Where `ask`'s 12.6 s actually go

`ask` = a pydantic-ai agent (`ANSWER_MODEL=gpt-5.6-terra`, `ANSWER_REASONING_EFFORT=medium`,
≤6 sequential model requests, ≤8 tool calls, no streaming) + a deterministic verifier + **one
corrective retry when the first draft fails verification**.

The retry is the story. Re-measured 2026-08-06 over 27 staging asks (the 19-ask figures this
section first carried are superseded; the shape held, the magnitude moved):

| | n | p50 |
|---|---|---|
| Retried, answered | 9 | **16.7 s** |
| Retried, ended in a refusal | 1 | 17.1 s |
| Retried, ended `partial` | 1 | 17.8 s |
| **Not** retried, answered | 11 | **9.9 s** |
| **Not** retried, refused | 5 | 5.7 s |

**~41 % of asks fail first-pass verification** and pay a *second full agent run*. Compared
like with like — answered against answered — a retry costs **6.8 s**, not the 8.5 s a naive
split suggests: refusals are fast (5.7 s) and pool into the non-retried side, flattering it.

The retry prompt (`verify_answer.feedback()`) carried only the question + previous draft +
problem strings — no message history, no gathered evidence — so the retry re-searched and
re-read everything it already had. Nearly all retried asks end `verified` anyway: the knowledge
was there; the first draft's citations were just sloppy.

**And zero asks have ever been suppressed** (0/27). That matters because the two first-draft
failures are worth opposite amounts: an untraced FIGURE would be suppressed by the strict gate
without a successful retry, so the retry buys the answer; a single citation problem ships as
`partial` either way, so it buys a label and an accurate quote. Which one dominates was not
answerable from any column — `audit_log` recorded only the SHIPPED verdict — which is why this
batch added `first_verdict` to `audit_summary` (§6).

### Where the librarian's 111 s go

Per note: pre-agent (R2 GET + dedup SQL + gitleaks scan, ~1–3 s) → git prologue (fetch +
pinned-input reads + worktree add, ~1–3 s) → **one Claude Code headless session
(`claude-sonnet-5`, up to 30 turns, tools Read/Glob/Grep/Write/Edit) — the bulk, ~90–105 s** →
eight deterministic gates (2× gitleaks + whole-repo contract linter, ~3–8 s) → commit + fresh
GitHub App token mint + push (~2–5 s). The LLM session is ~85–90 % of the wall clock. On top of
that, pure sleep: worker claim poll (≤10 s) and Slack thread poller (≤10 s).

### What the Slack user actually sees

- **Q&A**: one `_thinking…_` placeholder, then nothing until the final `chat.update` 12–25 s
  later. No intermediate signal. (Issue #32 — duplicate `show_it_here` block_ids — made the
  final update fail and stranded the placeholder forever; the fix is in flight on this branch.)
- **🧠 capture**: nothing at all until the "queued" thread ack, which sits behind **6+N
  sequential Slack Web API calls** (N = thread participants, each an *uncached* `users.info`,
  serial) + 2 R2 round-trips + the queue INSERT. Then the filed report arrives ~2 min later via
  the 10 s poller.

---

## 2. Exonerated — measured, not guessed

- **git**: the read path (`search_brain`/`read_page`/`ask`) touches git **zero** times — it is
  Postgres-only. On the write path git is one `fetch` per item + a push with ~5 s worst-case
  retry patience: single-digit seconds of a 111 s pipeline.
- **Cold starts**: none. `fly.toml` has `auto_stop_machines = false`,
  `min_machines_running = 1`; worker and slack groups are always-on.
- **DB**: Supabase (eu-central-1) from Fly fra — a few ms per query; search does 4 sequential
  queries + 1 OpenAI embedding, all inside 255 ms p50.
- **The eight gates**: all deterministic code, no LLM; seconds, not tens of seconds.
- **Embeddings / index / webhook**: searchable-within-seconds already works; `built_at` and the
  incremental upsert are healthy.

---

## 3. The plan, ranked by perceived impact per unit of risk

### 3.1 Ask — eliminate the verification-retry tax *(biggest real-time win, zero product risk)*

Three moves, same target — the 47 % first-pass failure rate and the price paid when it fires:

1. **Diagnose offline.** `evals/run_qa.py` already drives the real loop over 26 golden
   questions. Add per-question `retried` / `citation_problems` / wall-time to its `--report`
   and look at what actually breaks first drafts (expected: quotes copied from search snippets
   or titles instead of the fenced body; unicode punctuation drift).
2. **Fix the first pass.** Tighten `ANSWER_SYS` quoting discipline (short quotes, copied
   character-for-character from fenced `read_page` bodies only — never from snippets or
   titles), and/or extend `check_citations`' normalization (unicode NFC, curly-quote/dash
   folding) — the repo's own doctrine allows this: *"findings feed the matcher — they never
   loosen the gate."* The verifier and strict gate stay untouched.
3. **Make the retry cheap when it still fires.** Pass the first run's `message_history` into
   the corrective `agent.run()` so the model redrafts from evidence already in context instead
   of re-searching from scratch: ~8–10 s → ~2–3 s.

**LANDED 2026-08-06** — and the expectation this section first carried (*p50 12.6 s → ~7 s*) was
too optimistic by about half. The honest arithmetic on the re-measured numbers: 0.41 × (6.8 s −
~2.5 s) ≈ **1.8 s off the average ask**, not 5 s. Moves 1 and 3 landed; move 2 landed partially —
the `ANSWER_SYS` quoting discipline in full, the matcher normalization as NFC + typographic quotes
+ ellipsis only. **Dashes were deliberately excluded**: `tests/answer/test_verify.py` pins "an
ASCII hyphen is not the page's em dash" as an adversarial twin, so folding that class would retire
a standing defense rather than widen a table — and the measurement below found no retry caused by
punctuation drift, so there was no evidence to spend that defense on.

**The guard held**: honesty 1.00 / groundedness 0.93 / refutation 1.00, identical before and
after, same single miss (`globex-meeting-budget`).

**What could NOT be demonstrated, stated plainly.** The latency win is projected, not measured.
The frozen `evals/corpus` retries at **8 %** where staging retries at 41 %, so the eval population
barely exercises the change; per-question wall time also swings ±5-7 s run to run, which is larger
than the effect. The single question that retried in both runs got *slower* (13.2 s → 17.7 s),
which at n=1 proves nothing in either direction. **Deterministic properties belong in a test, not
a stopwatch** — that the retry now carries the first run's history is pinned in
`tests/answer/test_service_ask.py` and mutation-killed. Confirming the wall-clock win is a staging
job, not a corpus job: after deploy, `retried=true` p50 should fall from 16.7 s toward ~11 s.

One defect was introduced and caught by the audit rather than by the suite: the fold was applied
BEFORE the derender, and `…` → `...` lengthens a string past `_SPAN`, so a 200-character struck
span containing an ellipsis stopped being dropped and a quote of RETRACTED text verified. The fold
now runs last, so every length bound measures the bytes it measured before this layer existed.

### 3.2 Ask — model/effort choice: measured, and the answer is about cost, not speed

Three cells of the sweep were run on 2026-08-04 (local stack, frozen `evals/corpus`, all 26
golden questions per cell, wall-clocked per question from outside the runner):

| Cell | Honesty | Groundedness | Refutation | Median /question | Mean /question |
|---|---|---|---|---|---|
| `gpt-5.6-terra` × medium *(prod today)* | 1.00 | 0.93 | 1.00 | 5.8 s | 6.9 s |
| **`gpt-5.6-terra` × low** | 1.00 | 0.93 | 1.00 | **5.2 s** | **6.0 s** |
| `gpt-5.6-terra` × none | 1.00 | 0.93 | 1.00 | 6.2 s | 6.2 s |
| `gpt-5.6-luna` × medium | 1.00 | 0.93 | 1.00 | 7.3 s | 8.1 s |
| `gpt-5.6-luna` × low | 1.00 | 0.93 | 1.00 | 7.2 s | 8.3 s |

(Same single miss in every cell — `globex-meeting-budget`, a yardstick miss with verdict
`verified`. One run per cell, n=26; serving-load variance exists. The 5.6 family's effort
ladder is none/low/medium/high/xhigh/max; the three cells below prod's `medium` are measured
above.)

**Findings:**

- **The effort curve is U-shaped, and `low` is the sweet spot for terra.** medium 5.8 →
  low **5.2** → none 6.2 (median). At `none` — and on the small tier generally — the model
  compensates for less thinking with *more loop turns and longer output*, which costs more
  than the reasoning it saved: `ask`'s latency is dominated by the number of sequential
  requests, not per-token speed.
- **Luna is not a speed lever.** ~25 % *slower* per question than terra in this agent loop,
  at both efforts (7.2–7.3 s). **Luna IS a cost lever**: identical bars at **10× lower price**
  ($0.20/$1.20 vs $2/$12 per MTok).
- Behavioral nuance at reduced effort (seen at luna×low and terra×low, gone again at
  terra×none — run-to-run wobble): a `refute` case passes by *refusing* rather than by
  *correcting with a citation*. Both count as pass; the correction is the better product
  behavior. Watch it on adoption.

**Decision (2026-08-04): stay on `gpt-5.6-terra` × `medium`.** The measured gain at
`low` (~0.6 s median, ~10 %) was judged too small to be worth adopting; `none` and luna are
slower and rejected for speed outright. The grid stays here as the evidence: if `ask` latency
ever needs the extra margin after 3.1 lands, `low` is the pre-validated cell to re-confirm
(one repeat run, per `run_gates`' own noise rule) — and luna remains a pure **cost** option
(same bars, 10× cheaper). The *big* `ask` speed comes from 3.1 (the retry tax) and loop shape,
not from any model/effort cell. Tooling gap if a cell is ever adopted: `run_gates.py`
hardcodes terra and `run_qa.py` builds `Settings()` directly so `ANSWER_REASONING_EFFORT` is
ignored — both need a passthrough (this sweep used a scratch wrapper).

**Cross-provider card (hold until after 3.1).** The one model experiment still worth building
for `ask` is a Claude cell — Haiku 4.5 ($1/$5) as the speed tier, Sonnet as the quality
control. Rationale: the retry *rate* is a property of the model's citation fidelity, not of
the harness — a model that nails verbatim quotes first-pass beats a per-token-faster one
end-to-end. It is not a free experiment: `build_synthesizer` accepts only `openai|fake`, so
this needs a small `ANSWER_LLM=anthropic` branch (pydantic-ai supports it; the key is already
a Fly secret for the librarian) plus the `run_qa` passthrough above. Build it only if `ask`
still feels slow after 3.1 lands and is re-measured — and let the same bars decide. Gemini
Flash: same door, same rule, lower expectation.

### 3.3 Slack Q&A — a placeholder that shows progress *(perceived latency; lands after #32)*

Same wall time *feels* half as long when it moves:

- Post the placeholder as the **first** Web API call (today `users.info` + a channel-file read
  run before it).
- Stage the placeholder through 2–3 edits driven from the agent's own tool-call hooks
  (`SynthesisContext.record` already sees every search/read): `_searching the brain…_` →
  `_reading 3 pages…_` → `_drafting a cited answer…_`. Throttle to ≥1.5 s between edits, max 3
  edits, so Slack rate limits are never in play.

**Expected**: the 6–12 s wait reads as work-in-progress instead of a hang. Prerequisite:
issue #32 fix (in flight) so placeholder edits are reliable.

### 3.4 Slack 🧠 capture — instant acknowledgment *(kills the "did it even work?" seconds)*

- **React immediately** (`reactions.add` ⏳ or 👀) as the very first action after the event
  arrives — visible in ~200 ms, costs one cheap API call, needs no identity resolution.
- Then do the real pipeline and post the "queued" thread ack as today (the durable receipt),
  upgrading the reaction to ✅.
- Trim the ack's serial fat while there: resolve thread participants through the existing
  5-min `UsersInfoCache` (today they bypass it, serially), and drop the second uncached
  `users.info` for the reactor's display name (the first, cached lookup already has the
  profile).

**Expected**: first feedback 1.5–4 s → **instant**; the ack itself lands ~1–2 s sooner.

**LANDED 2026-08-06.** Two consequences worth knowing, both now documented in
`docs/reference/slack.md` rather than discovered later: the marker fires after
`is_configured_workspace` but BEFORE the channel and identity checks — that is what makes it
instant — so a private channel, an unrecognized reactor and a transient identity failure now get a
brief hourglass where they previously produced no channel-visible artifact at all. And the done
mark means **queued**, never *filed*: it is not revoked when a filing is later rejected, and it
does not claim the thread ack posted (that send is best-effort by design).

### 3.5 Capture → filed — instrument the turns, cut the sleeps, then trim the turns

The 111 s is a Claude Code session whose shape nobody can see today:

1. **Persist `num_turns` + `total_cost_usd` per filing** — already parsed off the SDK's
   `ResultMessage` (`agent.py`) and then dropped; write them into the item's `report`/queue row
   and surface in the admin Worker tab. One evening of work; makes the next step evidence-based.
2. **Cut pure sleep**: worker `poll_interval_s` 10 → 3 s, Slack poller 10 → 5 s. Saves ~6–12 s
   median turnaround for free (compose already runs the worker at 0.5 s). **LANDED 2026-08-06** —
   the only deterministic saving in this document, and the largest. `fly.toml` passes no
   `--poll-interval`, so staging picks the new default up on deploy with no config change. The
   doorbell keeps its own 10 s: `identity.py`'s rate-limit arithmetic is written against it.
3. **Trim turns with the evidence from (1)**: if sessions burn turns Glob/Grep-ing facts the
   prompt could state (registry, target zones, naming), tighten the librarian SKILL.md
   accordingly. The skill lives in the knowledge repo — iterating is cheap.
4. **Explicitly deferred**: swapping the librarian to a faster model tier. Unlike `ask`, there
   is **no quality instrument** for filing judgment (the gates catch form, not judgment), so a
   model change has no safety net today. If 2× here ever matters, the shape is decided:
   - **Candidate**: `claude-haiku-4-5` (the provider is fixed — the filing engine is built on
     Claude Code: skills, confinement hooks, worktree). Up-tier (opus) has nothing to offer;
     down-tier risk is specific: a gate veto burns the corrective attempt (a whole second
     session), and judgment errors (placement, anchoring) land ungated — so a cheaper model
     that vetoes more can be *slower* than today (the luna lesson, with consequences).
   - **Instrument first**: a golden-filing eval on the existing `e2e-librarian` harness with
     `STIGMERGY_LIBRARIAN_BACKEND=sdk` — ~10–15 frozen captures, code-checkable asserts over the
     filing `report` (`page_path` zone, type, `anchored_to`, supersession) plus gate veto-rate
     and turns/wall-clock per item. No LLM judge needed.
   - **Lowest-risk first cell**: the meeting distiller (one structured `Write`-only call, no
     exploration) — note it shares `STIGMERGY_LIBRARIAN_MODEL` with the filing agent, so a
     per-flow model would need that setting split in two.

**Expected**: ~2 min → **~60–90 s** honest target (sleep cuts are deterministic; turn trims
depend on what (1) shows). Note: the ack copy already sets expectations, which is why this
ranks below the ask work.

---

## 4. Explicitly rejected — real optimizations a human would never feel

Each of these is measurable and each is noise next to an LLM round-trip; they are listed so
they don't get re-proposed piecemeal:

- Batching/reducing git subprocesses (~30–40 per filing ≈ 1–2 s total).
- Caching `identities.json` / entity-registry disk reads (µs–ms each, correctness-sensitive).
- Embedding model swap or query-embedding cache (saves ~100–200 ms under a 5–12 s ask).
- DB co-location / connection pooling changes (queries are already ms).
- Token-streaming answers into Slack via rapid `chat.update` (rate-limit risk; staged edits in
  3.3 buy the same feeling for three API calls).
- GitHub App token caching, HNSW tuning, `fetch_pages` column trimming, audit-INSERT async.
- Model swaps for **views** (`gpt-5.4`) and the **gardener** (`gpt-5.4-mini`): both are
  background stages — the gardener is one tool-less call per nightly cron, views regenerate
  per entity off the operator CLI / post-meeting — so speed there is imperceptible by
  definition, the absolute spend is cents, and neither stage has a quality instrument to gate
  a change. Wrong side of both P1 (speed-with-quality) and P2 (price).
- Anything that weakens the verifier, the strict gate, or the eight gates. The plan speeds up
  *reaching* the guarantees, never bypassing them.

---

## 5. Defects noticed on the way (worth their own issues)

1. **DM double-fire**: `@brain <question>` typed *inside* the bot DM triggers both
   `app_mention` and `message.im` listeners → two placeholders, two full asks (double spend,
   duplicate answers). Mentions have no event-level dedup (capture does).
2. **MCP `ask` has no wall-clock timeout** (Slack caps at 90 s). A wedged tool call holds the
   request open indefinitely; the embedder's own httpx timeout is 120 s.
3. Stranded-placeholder on `invalid_blocks` — already being fixed as issue #32 on this branch.

---

## 6. The scoreboard — how every change gets verified

- **Quality**: `evals/run_qa.py` bars (honesty ≥ 0.90 · groundedness ≥ 0.84 · R@5 ≥ 0.80),
  extended with per-question wall-time and retry-rate columns; grid runs recorded in
  `evals/history.ndjson` as usual.
- **Latency truth**: staging `audit_log` percentiles (the Activity tab already renders them);
  the queries used for this document:

  ```sql
  select tool, count(*),
         percentile_cont(0.5) within group (order by duration_ms) p50,
         percentile_cont(0.9) within group (order by duration_ms) p90
  from audit_log where outcome='ok' group by tool;

  select kind, percentile_cont(0.5) within group
         (order by extract(epoch from (finished_at-claimed_at))) processing_p50
  from capture_queue where status='filed' group by kind;
  ```

- **Retry rate**: `audit_log.result->'retried'` over recent asks — 11/27 on 2026-08-06;
  target < 1/10. Compare retried against non-retried **on answered asks only** — refusals are
  fast and flatter whichever bucket they land in:

  ```sql
  select (result->>'retried')::bool retried, (result->>'refused')::bool refused, count(*),
         percentile_cont(0.5) within group (order by duration_ms) p50
  from audit_log where tool='ask' and outcome='ok' group by 1,2 order by 1 desc;
  ```

- **What the retry was FOR**: `audit_log.result->'first_verdict'` (added 2026-08-06) — counts
  only, same reduction as `verdict`. This is the column that decides whether the retry path
  deserves more work: a first draft with `unverified_figures > 0` would have been SUPPRESSED
  without the retry, while one with a single `citation_problems` ships as `partial` regardless.
  The shipped `verdict` cannot answer it — a retried ask that ended clean and one that never
  needed a retry are identical in it.

  ```sql
  select result->'first_verdict'->>'verdict' first,
         (result->'first_verdict'->>'unverified_figures')::int figs,
         (result->'first_verdict'->>'citation_problems')::int cites, count(*)
  from audit_log where tool='ask' and outcome='ok' and (result->>'retried')::bool
  group by 1,2,3 order by 4 desc;
  ```

- **NOT the golden corpus.** `evals/corpus` retries at ~8 % against staging's ~41 %, so it cannot
  measure retry-rate work; per-question wall time there also varies ±5-7 s between runs. Use it
  for the quality bars, which it measures well, and staging for latency.
- **Feel**: TESTING-GUIDE.md walkthrough, Block 3 (steps 18–22), after each wave.

---

## 7. Open — the untested lever, and what would size it

`ask`'s non-retried answered p50 is 9.9 s, of which the TOOLS are ~0.3 s: `search_brain` p50
263 ms (dominated by one OpenAI embedding round-trip), `read_page` 12 ms, `describe_entity`
19 ms. **Better than 96 % of the wall clock is sequential model turns**, and the agent's first
turn almost always exists only to say "search for this".

**Eager first search** — run `search(question)` server-side before the agent's first request and
hand it the listing in the initial prompt — would delete one full model turn on **100 %** of asks,
where §3.1 only reaches the 41 % that retry. Plausibly 2-3 s. It is not free: the model may prefer
`describe_entity` for an entity question (a prefetched search then buys nothing), and anchoring it
on a search it did not choose is a quality risk the golden bars would have to clear.

Size it before building it — two facts, one instrumented `run_qa` pass:

1. **How many model requests does an ask actually make?** The limit is 6
   (`ANSWER_REQUEST_LIMIT`) and nobody has counted the real distribution. Removing one turn of
   three is a third of the wall clock; one of five is a fifth.
2. **How often is the first `search()` literally the question?** `ctx.searched` records every
   query verbatim, in first-tried order, so this is a direct read.

Also open, and unrelated to speed: `answer_limits()` bounds requests and tool calls but not
TOKENS, and carrying message history made context the larger term; `evals/history.ndjson` carries
three committed git conflict markers that `eval_history.read_history` skips silently; and
`_STRIKE`'s whole-span drop is bounded at `_SPAN`, so a retraction longer than that stays quotable
as current — a known residual, corrected in the prose that used to promise otherwise.
