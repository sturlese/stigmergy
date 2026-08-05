---
type: decision
title: "Two Ownership Zones"
status: mature
created: 2026-07-15
updated: 2026-07-15
tags: [architecture, substrate, governance]
entity: ["stigmergy"]
related: ["[[Git as the Canonical Store]]", "[[The Evidence Plane]]"]
sources: []
---

# Two Ownership Zones

## Context

The one open integration question between the source designs was whether authored
knowledge and machine-distilled knowledge share a repository. Authored pages are written by
humans-via-agents and by the librarian; ingested pages are the pipeline's verified output.
They have different writers and different trust guarantees.

## Options

- **One repo, two provenance zones** — `wiki/` (authored) and `sources/` (machine),
  unified under one wikilink graph, one lint, one clone, one index build.
- **Two separate repos from birth** — clean separation, but a split graph, two lints, and
  two index builds for no proven benefit.

## Decision

The `stigmergy` repo has two provenance zones with different owners and write rules:
`wiki/` is authored (the two lanes apply), and `sources/` is single-writer (the
pipeline bot; CI rejects any human PR that touches it). Machine rollups (`views/`), data
cards (`datasets/`), and generated digests (`meta/`) are also machine-owned zones.

## Why

Unifying wins because one authored `decision` can link the `ingested` page of the contract
that motivated it — one graph, one review surface, one index. The churn concern
(LLM-regenerated pages producing diff noise) is bounded by content-hash idempotency: only
pages whose source changed are rewritten, so steady state writes little.

## Consequences

This zoning builds on [[Git as the Canonical Store]] and is enforced mechanically by the
linter and CODEOWNERS. If ingest churn ever pollutes history in practice, `sources/`
splits into its own repo with zero downstream change — the index reads both and the graph
joins by page name. Raw artifacts stay out of both zones, in [[The Evidence Plane]].
