---
type: decision
title: "A decision from a previous meeting"
status: developing
created: 2026-01-01
updated: 2026-01-01
tags: [decision, meeting]
related: ["[[Acme Corp]]"]
sources: []
---

# A decision from a previous meeting

Decided about [[Acme Corp]] in an earlier meeting, unrelated to the one any test that
links to this page's stem is filing.

## What was decided

This page exists only so a wikilink to its filename STEM (`a-decision-from-a-previous-
meeting`) resolves — the fixture repo's own dead-link check walks every page under
`wiki/` by filename stem, exactly like the librarian's own
`_meeting_page_decision_links` reads a meeting page's own body. A test that wants to
prove the meeting flow refuses a stale cross-meeting decision link needs that link to
actually resolve; otherwise the contract linter's `dead_links` check refuses the capture
for an unrelated reason before the check under test ever gets a chance to.

## Why it is here

`meeting-foreign-decision-link` (`stigmergy.librarian.double`) points a meeting page's
"## Decisions" section at this exact stem, simulating an agent that links a decision page
filed by a genuinely different, earlier meeting rather than one this capture created.
Without a real page at this stem, that link would be dead on arrival, and the dead-link
check — not `_cross_check_meeting_outcome` — would be doing the refusing.

## Facts

- Filed under `wiki/decisions/`, the same folder any decision page from any meeting
  lives in.
- Carries no numeric claims of its own, so a test that reads it directly cannot pick up a
  figure from this page by accident.
- Never touched (added or modified) by any meeting capture in the suite, so the contract
  linter's per-capture `touched` filter never surfaces a finding about this page itself.
- The body is padded past the contract linter's line minimum on purpose, the same way the
  offline double pads its own drafts.

## Connections

- Linked from the `meeting-foreign-decision-link` sabotage fixture, on purpose, to prove
  the meeting flow's own decision-link cross-check — not the dead-link rule — is what
  refuses a stale cross-meeting link.
