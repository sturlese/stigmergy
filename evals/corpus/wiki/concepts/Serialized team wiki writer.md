---
id: page_eval_writer
type: concept
title: Serialized team wiki writer
status: mature
created: 2026-08-01
updated: 2026-08-01
acl: null
entity: []
sources:
- sources/2026/08/10000000-0000-4000-8000-000000000001.md
---

# Serialized team wiki writer

One durable queue feeds one serialized Git writer. Captures, gardening, entity operations,
contradiction resolutions, and explicit deletion all build a candidate tree, run the same gates,
land at most one commit, and append one change record. No active operation waits for human
approval.
