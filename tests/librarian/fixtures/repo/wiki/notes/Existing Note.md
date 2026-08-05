---
type: note
title: "Existing Note"
status: developing
created: 2026-01-01
updated: 2026-01-01
tags: [note]
related: ["[[Acme Corp]]"]
sources: []
---

# Existing Note

## What the note says

This is a pre-existing page in the fixture knowledge repo, committed once before any
librarian test runs against it. It exists so a test can exercise the additive-edit, the
near-duplicate overlap, the deletion-veto and the body-rewrite-veto paths against a page
that genuinely already existed — the same shape a real capture would find when the graph
it reads already has material on the same subject.

## Why it is here

Anchoring, overlap callouts and additive edits are only meaningfully tested against a
page nobody just created in the same commit. A page invented fresh inside the very diff
being judged cannot prove "additive edit to an EXISTING page" the way this one can.

## Facts

- Anchored to [[Acme Corp]], so a capture about the same entity has something real to
  cross-link against.
- Carries no machine-owned frontmatter (no `submitted_by`, no `verification`), because it
  was never filed by the librarian — it is hand-authored fixture material, the same way a
  page a steward wrote by hand would be.
- The body is padded past the contract linter's thirty-line minimum on purpose.
- Every test that mutates this page (an overlap callout, a deletion, a rewrite) works on
  its own fresh clone of the fixture repo, so no test can leave it dirty for the next one.

## Connections

- [[Acme Corp]] — the entity this note is about
