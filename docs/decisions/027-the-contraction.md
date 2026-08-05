# ADR 027 — The contraction: the loop is deleted, two meeting-flow checks step down

Date: 2026-08-02 · Status: accepted

## Decision

1. **The learning loop is DELETED, not parked** — `stigmergy.loop` whole (staging, distillation,
   candidates, promotion, retention, `stigmergy-loop`), its tests, and its tentacles in ~18 live
   files: the hint seam (`LOOP_HINT_KEYS` + the forgery guard), the promotion path in
   `server.review`, the `candidate` review kind and its doorbell filling, the digest's "what the
   loop learned" section, answer staging in `slack.mention`/`replies`, the MCP ask/search staging
   hooks, `STIGMERGY_CAPTURE_ENABLED` and its HMAC key in settings, the loop-retention cron step,
   `report.filed`'s follow-up note, and the staging schema ensured at every server boot.
2. **The date-bearing-body-link veto steps down to a gardener finding**
   (`gardener.checks.check_date_bearing_body_links`, slug `date-bearing-body-link`) — it is a
   style convention (date-bearing page names belong in frontmatter, not body prose), and the thing
   that made it a veto was the ingest-time verification tier ADR 026 removed.
3. **`meeting-links-mismatch` is folded out** — code writes the meeting page from the same
   decision list it links, so the mismatch class is structurally impossible and the check was
   double-entry bookkeeping.
4. **The PII gate STAYS** — ruled the other way, on evidence: the case for dropping it assumed
   email/phone patterns that do not exist (the real four are PEM/IBAN/DNI/Luhn-valid cards, zero
   false positives ever), and git's permanence puts committed PII on the irreversible side of the
   line that governs both surfaces — gates veto the irreversible; the gardener flags conventions.

## Why

This amends ADR 026 D5's "park, not delete", two days later, with a reason that decision could
not have had: an incident of exactly the class a dormant organ manufactures. CI had been silently
red on `main` because a test pinned a full-history truth the live system had already moved past —
and the loop was 2.7k test lines pinning contracts every live refactor moves, a drag an unrelated
rename had already paid once. ADR 026's "the lessons live in code and tests" was verified lesson
by lesson: lease fencing lives in `queue`/`worker`, Slack redelivery in `slack.store`, none of it
in `stigmergy.loop`. What dies is the staging→distill→promote mechanics, and ADR 023 preserves that
design in full.

## Consequences

- The wake condition becomes a **rebuild condition**: if real users ever earn the loop, it is
  re-designed from ADR 023 against the then-current contract — not woken. The loop-contamination
  adversarial category dies with it and is re-designed with it.
- The digest is two sections. The review inbox is two kinds. `ALLOWED_HINT_KEYS` loses its loop
  group. The nightly retention cron is the capture-queue purge alone.
- The deployed app drops `conversations_staging`/`loop_candidates` and loses the
  `STIGMERGY_CAPTURE_ENABLED`/`STIGMERGY_LOOP_HMAC_KEY` secrets.
