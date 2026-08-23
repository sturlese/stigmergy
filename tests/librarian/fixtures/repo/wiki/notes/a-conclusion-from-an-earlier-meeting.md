---
type: note
title: "A conclusion from an earlier meeting"
status: developing
created: 2026-01-01
updated: 2026-01-01
tags: [note]
related: ["[[Acme Corp]]"]
sources: []
---

# A conclusion from an earlier meeting

Something concluded about [[Acme Corp]] in an earlier conversation, unrelated to whatever
any test that links to this page's stem is filing.

## What this page is for

It exists so a wikilink to its filename STEM resolves. The fixture repo's own dead-link
check walks every page under `wiki/` by filename stem, so a test that wants to prove some
OTHER rule refuses a capture needs its links to actually resolve first — otherwise the
contract linter's `dead_links` check refuses the capture for an unrelated reason and the
check under test never gets a chance to fire.

## Why it reads the way it does

- It carries no numeric claims of its own, so a test that reads it directly cannot pick up
  a figure from this page by accident.
- No capture in the suite adds or modifies it, so the contract linter's per-capture
  `touched` filter never surfaces a finding about this page itself.
- The body is padded past the contract linter's line minimum on purpose, the same way the
  offline double pads its own drafts.

## Connections

- Linked from the fixtures that need a resolvable cross-page link, on purpose, so the rule
  under test is the one that does the refusing.
