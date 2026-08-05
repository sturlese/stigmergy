---
type: decision
title: "The Evidence Plane"
status: mature
created: 2026-07-15
updated: 2026-07-15
tags: [architecture, substrate, storage]
entity: ["stigmergy"]
related: ["[[Git as the Canonical Store]]", "[[Two Ownership Zones]]"]
sources: []
---

# The Evidence Plane

## Context

Source binaries and heavy text — PDFs, decks, meeting recordings, transcripts, exports,
OCR sidecars — are the proof behind distilled knowledge, but they do not belong in git.
Fifty people generate 5–50 GB/yr of raw artifacts, and retention/GDPR rules require real
deletion, which git history physically cannot provide.

## Options

- **Content-addressed object storage** — raw artifacts in a bucket (`key = sha256`) with
  versioning, object lock, per-prefix lifecycle rules, and prefix ACLs.
- **Everything in git** — every clone carries all history forever; the repo dies in months
  and deletion means rewriting everyone's clones.

## Decision

Raw artifacts never enter git. They live in an object store (Cloudflare R2 by default),
addressed by content. The bridge between planes is the **source page** in git: it carries
provenance (who, when, from where), the blob URI + hash, and the distillation. Git stores
the claim and the pointer; the bucket stores the proof.

## Why

Two physics force the split. Disk: git history is append-only and every clone carries it
all forever. Deletion: retention policies and right-to-be-forgotten require real deletion,
which is a catastrophe in git but a lifecycle rule in a bucket. Content addressing also
gives immutability, dedup for free, and stable citation URIs. Cost is negligible
(≈€1–2/month for 50 GB/yr).

## Consequences

The chain page → source page → blob answers "where does this come from?" with full audit,
completing the substrate defined by [[Git as the Canonical Store]] and
[[Two Ownership Zones]]. Full meeting/OCR extractions live as text sidecars next to their
blob and may be indexed as a low-authority raw layer, never as pages. Small curated images
that *are* knowledge may live in the repo under a size rule (<1 MB).
