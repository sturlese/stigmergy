---
id: page_eval_index
type: note
title: Nightly index reconciliation
status: mature
created: 2026-08-01
updated: 2026-08-01
acl: null
entity: []
sources:
- sources/2026/08/10000000-0000-4000-8000-000000000001.md
---

# Nightly index reconciliation

GitHub webhooks update the hybrid lexical and vector index quickly. A full rebuild runs every day
at 04:17 UTC and can also be dispatched manually. A successful rebuild records repository HEAD,
row count, and completion time, then clears the dirty marker only after indexing that HEAD.
