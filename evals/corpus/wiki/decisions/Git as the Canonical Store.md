---
type: decision
title: "Git as the Canonical Store"
status: canonical
owner: steward
created: 2026-07-15
updated: 2026-07-15
tags: [architecture, substrate]
entity: ["stigmergy"]
related: ["[[Two Ownership Zones]]", "[[The Evidence Plane]]"]
sources: []
---

# Git as the Canonical Store

## Context

The company brain needs a canonical store for all knowledge. The write path — review,
stewardship, attribution, audit — is the differentiator, not the read path. Whatever holds
the knowledge must make that write path cheap and portable, at a volume of roughly 3–8k
new pages per year (tens of MB after three years).

## Options

- **A GitHub repository of markdown pages** — PRs, CODEOWNERS, blame, CI, branch
  protection, webhooks, and the API/`gh` CLI for agents, all out of the box.
- **Notion / Confluence as the store** — loses diffs, PRs, CODEOWNERS, CI lint, and local
  clones for agents; binds the brain to opaque retrieval and rate limits.
- **A custom database as the store** — rebuilds versioning, review, audit, and permissions
  badly, to gain what a derived index already provides.

## Decision

All company knowledge lives as plain markdown pages with YAML frontmatter and
`[[wikilinks]]` in a GitHub repository (`stigmergy`), plus enclave repos for restricted
domains. GitHub specifically — not "git in the abstract" — because the design leans on
its PR, CODEOWNERS, Actions, and GitHub-App machinery.

## Why

The code analogy is exact: nobody stores source code in a search engine because grep
scales poorly; code lives in git and is indexed on top. The write side (lanes, PRs,
stewardship, audit, portability) is where git is irreplaceable; the read side gets a
derived index layered over it. The store is auditable and portable — the whole brain is a
folder of text files with zero lock-in.

## Consequences

Knowledge splits into [[Two Ownership Zones]] inside the one repo, with different write
rules per zone. Raw binaries never enter git; they live in [[The Evidence Plane]]. If a
sovereignty requirement ever appears, GitLab self-hosted is a legitimate substitute in the
same role. Revisit only if the volume assumptions (tens of MB) prove wrong by an order of
magnitude.
