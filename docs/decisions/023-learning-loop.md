# ADR 023 — the learning loop: staging, distillation, review, promotion, retention

Status: accepted, then reversed. **The learning loop does not exist in this codebase.**
`stigmergy.loop` and its `stigmergy-loop` CLI, the `conversations_staging` and `loop_candidates`
tables, the `candidate` review kind, the staging hooks in Slack and in the MCP tool closures, the
loop's own hint allowlist and its retention cron step were all deleted —
[ADR 027](./027-the-contraction.md) records that decision and why it was taken.

This record is kept because it is the design a rebuild would start from, and because a few of its
rulings outlived the subsystem they were written for. **Everything describing the loop below is in
the past tense on purpose: it describes code that is gone.** Two things it decided are still live
and are marked as such — the allowlist-per-hint-kind mechanism in `capture.schema`, and
`stigmergy.text.fence` as the one place the UNTRUSTED-DATA dialect is built.

## Context

The design promise was that the brain learns from its own use, with privacy engineered in and a
human gate that cannot be bypassed: a question that found no answer evaporated, a correction typed
in-thread evaporated, tacit knowledge shared in passing evaporated. The principle underneath it —
*nothing promotes without a human* — was only ever as real as an adversarial case proving no code
path violates it, and that case could not exist until the loop it guards existed. So the loop was
built: staging, distillation, review, promotion, retention.

## Decisions

**D1 — `UserRef` was a type, not a string.** A bare identifier reaching
`conversations_staging.user_ref` was a programming error, not a validation failure: `UserRef` could
only be constructed by hashing (`UserRef.from_plain(raw, key)`), and its own `__post_init__`
rejected any value that did not already match `h:[0-9a-f]{64}` — so `UserRef("steward@example.com")`
raised exactly like `UserRef("garbage")` did. The insert path additionally type-checked
(`isinstance(user_ref, UserRef)`) as a backstop for a future caller that tried to skip the
constructor. `expires_at` was derived the same way, but in SQL rather than in a Python type: the
INSERT statement computed `now() + make_interval(days => %(retention_days)s)` and took no
`expires_at` parameter at all, so there was no argument name a caller could pass to override it.

**D2 — staging and the capture queue shared no rows, no schema, no purpose, and the boundary was
stated in code, not merely implied by living in different tables.** `conversations_staging`
carried verbatim conversational content and physically expired; `capture_queue` survives until a
human disposes of it and is only ever NULL'd in place. The one bridge between them was promotion,
which crossed through `capture.queue.submit` — the loop never wrote to `capture_queue` directly
except through that one call, and never touched git at all (see D6).

**D3 — the MCP capture hook lived at the MCP tool-closure seam (`mcp_server.py`), not inside
`BrainService.search`/`AnswerService.ask` themselves — a narrower reading of "the service layer"
than the obvious one, and a deliberate one.** Slack's `mention._run_ask` and
`mention._maybe_dm_fuller_answer` call `AnswerService.ask`/`BrainService.search` directly — the
exact same methods the MCP tools call — so hooking either method unconditionally would have doubled
the staging of every Slack exchange: once mislabeled `source=mcp` (wrong identity basis, no thread
continuity) and once through Slack's own explicit, richer hook. `stage_mcp_ask`/`stage_mcp_search`
(`server/service.py`) were therefore plain functions the `ask`/`search_brain` tool closures called
explicitly, right after their result existed — a seam stdio and HTTP shared verbatim (so "every MCP
transport inherits it" stayed true) and Slack never reached at all. This closed the double-staging
HAZARD; it did not by itself mean the DM-fuller-answer path was COVERED by staging somewhere else
— it was not, and D4 below states that positively as its own recorded exclusion.

**D4 — Slack staging was thread-scoped, not speaker-scoped.** `conversations_staging.user_ref` was
one HMAC per ROW, set once when the row was created by `handle_mention`'s first turn. A follow-up
turn (`handle_thread_message`) was appended to that same row regardless of which workspace member
actually typed it — the schema treated the thread as the unit of staging, matching the design
promise of "query + served page ids", extended to a conversation rather than one
exchange. `append_slack_followup` therefore never re-hashed an identity for a follow-up turn, and
— separately — never CREATED a row if none was already open for that thread: `handle_thread_message`
fires for every ordinary message in every thread in the workspace, the overwhelming majority
unrelated to `@brain`, and a find-or-create there would have started staging conversations the
system never participated in. `stage_slack_turn` (the mention path) kept find-or-create semantics,
because every mention genuinely is either the start of a conversation or a continuation of one.

Thread-scoping had an honest cost, recorded rather than hidden: if person A asked the
question that started the thread and person B later typed the correction in the SAME thread, B's
turn was appended to A's row under A's `user_ref` (D4 says why — the thread is the unit, not
the speaker). A purge of B's own `user_ref` therefore did NOT remove B's words — they lived inside
a row hashed to A. This folded into the same accepted trade the Known risks section already
named for candidate evidence surviving a purge: bounded (steward-eyes, physically expiring within
`STIGMERGY_LOOP_RETENTION_DAYS` regardless), and condition-owned to "the first real data-subject
request" rather than built around pre-emptively.

**The DM-fuller-answer surface (`mention._maybe_dm_fuller_answer`) was not staged AT ALL —
a recorded coverage exclusion, not an oversight.** D3 explains why the loop's OWN MCP hook had to
stay off it (double-staging); this is the separate, positive fact that nothing ELSE staged it
either. The comparison search calls it makes
(`BrainService.search`, twice, at two different scopes) and the fuller `ask()` it may trigger are
real service-layer calls with real content, and staging them would have meant a SECOND row (or a
second turn falsely appended to the channel thread's own row) for what the asker experiences as one
exchange arriving by DM — a shape the schema had no clean place for: the DM
has no `thread_ts` that package tracked, so even a reply TO that DM would have found no open row to
append to (the same silent, correct no-op every unrelated thread already got from
`append_slack_followup`). The exclusion was condition-owned — revisit it if a real correction of a
fuller DM answer ever starts mattering in practice — not date-owned.

**D5 — distillation's retry was one unified mechanism covering two different failure shapes, and
"garbage" was decided by application-level validation, never by the model's own schema
compliance alone.** One LLM call produced a `CandidateBatch` for the WHOLE bounded batch (one
prompt per batch, never per conversation). Every candidate was validated against the distiller's
own rules (a real `staging_id` from this batch, the excerpt cap, a non-empty claim) —
candidates that failed were separated from ones that passed, not fatal on their own. If validation
found ANY problem (a hard parse failure or a per-candidate one), the WHOLE batch was retried exactly
once, carrying the validation error as the retry's brief (message-as-brief, because the repair
is mechanical) — the retry's result REPLACED the first attempt entirely rather than being merged
with it, which is simpler and avoids a duplicate-insertion risk at the cost of occasionally
discarding an already-good candidate that a redo does not perfectly reproduce. Only when NOTHING
survived even after the retry did the batch become `DistillGarbage`: zero rows
inserted, the batch left unprocessed, the CLI exiting non-zero. An `AgentRunError` (the model call
itself failing — a timeout, an HTTP error, a usage-limit exhaustion) was a SEPARATE, orthogonal
failure the distiller caught nowhere: it propagated through `capture.ops.job_run` (recorded by
exception class name only — no staged content ever reached a log line) to the CLI, which reported
and exited non-zero the same way, for a different, honestly-distinguishable reason.

**D6 — promotion was decide-and-enqueue in ONE transaction, and the enqueue itself rode the
ordinary fast lane with no new write path.** `_decide_candidate` mirrored the review layer's own
transaction boundary exactly: lock the row (`FOR UPDATE`), authorize, check for an existing
decision, then — for `correction`/`tacit` — call
`capture.queue.submit(kind="raw", ...)` and finalize the candidate's status, all inside one
`conn.transaction()`. **The transaction was the actual guard, not the evidence store's dedup**: a
fault between the decision and the enqueue rolled BOTH back (the candidate stayed `pending`, no
queue row existed), so a subsequent `review_decide` call on the same still-pending candidate was an
ordinary fresh decision, never a "retry of a half-done promotion" — there was no half-done state
for it to retry. Content-addressed evidence dedup is a real but strictly smaller property one
level down, inside `capture.queue.submit` itself: IF the same material is ever submitted twice
(by coincidence, or by an operator resubmitting by hand), the blob store reuses the existing
object instead of storing it twice — it says nothing about, and is not what made true, the
decide-and-enqueue atomicity claim above. `submitted_by` on the
promoted capture was the APPROVING STEWARD's identity, stamped server-side — the human decision IS
the submission, the same rule `brain_submit` still enforces for itself. There was no other write
path from the loop to the knowledge repo: `stigmergy.loop` imported no git plumbing and held no path
under `wiki/`, which is what made "the loop cannot contaminate the corpus" a structural,
grep-provable property rather than a policy nobody checks.

**D7 — a candidate's authorization reused `_guard_governance_decision` unmodified, and the
missing self-approval branch was a consequence of having no submitter, not a new code path.** A
candidate was distilled, never filed by a person, so `_guard_governance_decision(service,
submitted_by="", ...)`'s self-approval clause (`if verdict == APPROVE and submitted_by and ...`)
never fired on a falsy `submitted_by` — there was no "you" who filed this to refuse a decider for
being. `NOT_YOURS_TO_DECIDE` still covers "does not exist" and "not a steward" identically, the
same no-existence-leak posture every other item kind has.

**D8 — every verdict on a candidate was terminal, diverging deliberately from the PR-shaped item
kind of the day.** That kind's `reject`/`request_changes` left `status='open'` because the
underlying GitHub PR was still open until a human closed it by hand — a second, different verdict
was legitimately still possible. A candidate had no external artifact like that: once a steward had
decided, there was nothing left "in flight" for a second decision to land on, and leaving a rejected
candidate re-eligible would have re-rung the doorbell for something already declined forever —
precisely the failure "first decision wins" exists to prevent. `duplicate-of:<id>` (free text in
`notes`, deliberately unenforced — automatic merging was forbidden) was the intended path for "I
already saw this, worded differently."

**D9 — `edit_approve`'s edit was audited by keeping both columns, never by overwriting.**
`loop_candidates.claim` was written once, at distillation, and never touched again; `edited_claim`
was written only by `edit_approve`, beside it. The diff IS the two columns read together —
"edit-then-approve, diff audited" was honored without turning `audit_log` into a transcript
(`review_decisions`/`audit_log` rows for a candidate stayed content-free: kind, id, verdict, never
claim text).

**D10 — the canon follow-up was a standing, unconditional clause, composed in two tiers because the
fact it stated was not knowable at decide time.** The loop did not try to detect "does this correct
something canonical" robustly — that detector is the gardener's. So `review_decide`'s own ack
(tier 1) stated the possibility in words, before any page existed; once the
librarian actually filed the promoted capture, `librarian.report.filed`'s own `loop_origin`/
`candidate_kind` parameters (read from the capture's own `hints.client`) composed the concrete,
pasteable follow-up instruction (tier 2) — unconditionally, on every
loop-originated `correction`/`tacit` filing, worded as a hedge rather than an assertion, because
nothing in the fast lane knew whether it was true. Both tiers died with the loop, and the canon
lane they pointed at was removed separately ([ADR 026](./026-the-purge.md) D1).

**D11 — the fence-dialect class fix migrated four modules, and `librarian/agent.py` stays
excepted on purpose.** The four modules that carried a weaker, hand-rolled `UNTRUSTED-DATA`
dialect were moved onto `stigmergy.text.fence` — the hardened one, with in-band `UNTRUSTED-DATA`
occurrences neutralized — closing a real gap that had been found and then deferred once already.
Those four modules have since been deleted with the corpus-distillation pipeline
([ADR 026](./026-the-purge.md) D4), but the ruling behind the migration is live and load-bearing:
**`stigmergy.text` is the ONE place the fence is built**, and a test enforces it.

`librarian/agent.py`'s own neutralized form places the word joiner differently
(`UNTRUSTED⁠-DATA` vs. `stigmergy.text`'s `UNTRUSTED-DATA⁠`); consolidating it changes the bytes
reaching a LIVE agent's prompt, which is a behavior change and not a consolidation — so it stays a
named, deliberate exception rather than a piece of drift. That is still true today.

**D12 — a staging failure degraded to a lost datum, never to a lost primary-surface response.**
All four staging call sites (`slack.mention._stage_exchange`,
`slack.replies.handle_thread_message`'s follow-up hook, and both `mcp_server.py` tool closures'
`stage_mcp_search`/`stage_mcp_ask` calls) ran try/except around the staging call itself, logging
loudly with a correlation ref and then continuing to serve the real response regardless. Before
this decision, three of the four ran BEFORE the answer/edit that follows them (mention's own call
sites sit just ahead of `_edit_or_fallback`; replies' sits ahead of the ask-back reply logic),
so an unguarded DB blip there took the primary response down with it — for mention specifically, a
placeholder ("please wait…") that would then never resolve. The other two (the MCP closures) ran
AFTER a successful result but BEFORE the return, so the same blip converted a successful
`search_brain`/`ask` into an error response, at the generic `except Exception` handler each
closure already carries for genuinely unanticipated failures.

The asymmetry this decision leaned on generalises past the loop and is worth keeping:
a telemetry store exists to feed something downstream, and a missing row in it is invisible to
every user and bounded by design — whereas the read/ask/reply path IS the product. Silently
swallowing the exception (rather than logging it) was rejected for the same reason: a table that
never receives rows because of a real, recurring bug must still be observable to an operator, just
never at the cost of the surface it rides alongside.

## Consequences

- `conversations_staging` was, by design, the most sensitive store this platform ever had — the
  mitigations were structural (HMAC refs, hard expiry, physical delete, gate default-off,
  steward-eyes-only surfaces), not procedural, and the known risk that candidate excerpts survive a
  purge by design was an accepted trade, condition-owned to "the first real data-subject request."
  It no longer exists, and neither does the risk.
- The gap between "a candidate exists" and "the librarian actually files the resulting page" was
  the SAME gap every fast-lane capture still has (anchor-or-ask-or-triage can park a
  promoted capture exactly like an ordinary one) — the loop's promise ended at "enqueued
  through the normal write path," never at "guaranteed filed."
- **Still live:** `capture.schema`'s allowlist-per-hint-kind mechanism. The loop's own group
  (`origin`, `candidate_kind`, `area`, `entities`) went with it, and a later door added one of its
  own ([ADR 028](./028-drive-door.md) D7) with nothing else touched; the pattern — a new
  hint-bearing caller joins the SAME allowlist mechanism with its own small, string-valued group
  rather than inventing a parallel one — is exactly what made removing a whole group a deletion
  rather than a migration.
