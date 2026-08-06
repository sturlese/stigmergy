---
type: note
title: "Café Zürich Renewal"
status: developing
created: 2026-01-02
updated: 2026-01-02
tags: [note]
related: ["[[Acme Corp]]"]
sources: []
---

# Café Zürich Renewal

## What the note says

This page exists for its FILENAME, not its prose. `git diff --name-status` C-quotes any path
containing a non-ASCII byte under the default `core.quotePath=true`, so this page's path arrives
as `"wiki/notes/Caf\303\251 Z\303\274rich Renewal.md"` unless every diff invocation disables
that. A quoted path matches none of the `wiki/...` prefix tests, which had two consequences
and neither of them was visible in a green suite.

## Why it is here

First, a capture that touched a page with an accented name was refused as a SYSTEM FAULT — the
zone gate saw a path outside the lane, so a perfectly ordinary page titled "Café" could never be
filed or additively edited. Accented names are ordinary in any corpus with a European customer in
it, so that is the common case rather than an edge case.

Second, and worse because it fails open rather than closed: `gate_contract` filters the linter's
findings to the paths this capture touched, and the linter reports UNQUOTED paths. The quoted path
never matched, so contract errors on exactly these files were silently dropped. A page with a dead
wikilink or a broken template would have been filed with the gate that exists to catch it saying
nothing at all.

## Facts

- The filename carries two accented characters, in two different words, so a partial fix that
  handled only the first byte would still fail here.
- It carries a space as well as the accents: git appends a TAB after a path containing a space in
  the `--- a/` and `+++ b/` diff headers, which is a SECOND path-parsing trap on the same page and
  was silently disabling the body-rewrite gate.
- Anchored to [[Acme Corp]], so a capture about the same entity has something real to cross-link
  against and the overlap and additive-edit paths can be driven through this page too.
- Hand-authored fixture material, carrying no machine-owned frontmatter, the same shape a page
  a steward wrote by hand, outside the librarian, would have.

## Connections

- [[Acme Corp]] — the entity this note is about
