---
id: page_eval_git
type: concept
title: Git canonical knowledge store
status: evergreen
created: 2026-08-01
updated: 2026-08-01
acl: null
entity: []
sources:
- sources/2026/08/10000000-0000-4000-8000-000000000001.md
---

# Git canonical knowledge store

Current team knowledge lives in Markdown in the private Git repository. Git provides the current
tree, atomic commits, exact history, ordinary review tooling, and a reversible record of every
knowledge mutation. Postgres holds operational state and a rebuildable search index, not a second
editable copy of the wiki.
