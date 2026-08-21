---
type: entity
title: "Acme Corp"
status: developing
entity_type: organization
aliases: ["Acme"]
created: 2026-01-01
updated: 2026-01-01
tags: [entity, organization]
related: []
sources: []
---

# Acme Corp

## What

Acme Corp is a fixture entity used by the librarian test suite to exercise entity
anchoring against a real, registry-resolvable page. It is not a real company; it exists
only so a filed page can declare a wikilink to Acme Corp and have that link resolve both
through the entity registry (`ops/entity-registry.json`) and to a real page already in
this repository — the same shape a governed entity page has in the production knowledge
repo, where entity birth is a steward's decision rather than the fast lane's.

## Why it is here

The anchoring gate requires every filed page to declare an entity it belongs to,
and the declaration is checked two ways: the entity registry has to know the name, and a
wikilink on the filed page has to resolve to a real page. A fixture entity keeps both
checks real instead of stubbing either one out.

## Facts

- Registered in the fixture entity registry under the id `acme-corp` — the slug of
  this page's title, which is the id `stigmergy-entities regenerate` derives.
- Carries no numeric claims of its own, so a capture that links to it cannot pick up a
  figure from this page by accident.
- The body is padded past the contract linter's thirty-line minimum on purpose, the same
  way the offline double pads its own drafts.
- Aliased to the short form "Acme" in the registry, so either spelling resolves to the
  same canonical entity.

## Connections

- Linked from fixture captures that anchor to a known, registered entity.
