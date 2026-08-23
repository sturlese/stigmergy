---
type: note
title: "Warehouse Slotting Policy"
status: developing
created: 2026-01-20
updated: 2026-01-20
tags: [note, operations]
entity: ["northwind-freight"]
related: ["[[Northwind Freight]]", "[[Northwind Freight Onboarding]]"]
sources: []
---

# Warehouse Slotting Policy

## Context

[[Northwind Freight]] runs depots whose picking layouts had drifted apart, each having been
arranged by whoever opened it. The onboarding work recorded in
[[Northwind Freight Onboarding]] deferred the question rather than answering it, and this page
is where it was answered.

## Options

- **Leave each depot to arrange its own layout** — cheapest, and it keeps working while nobody
  moves between depots. It stops working the moment cover staff do.
- **One slotting rule across every depot** — a single layout everyone learns once, at the cost
  of re-arranging depots that were already fine.

## Decision

Depots follow one slotting rule, and a depot that departs from it records why on its own page.

## Why

The cost being paid was not layout quality; it was that a cover shift arrived at a depot
arranged on a convention it had never seen. One rule makes that cost disappear, and the escape
hatch — record the departure — keeps a genuinely different depot from having to lie about
itself.

## Consequences

- Depots re-arrange on their own schedule rather than all at once.
- A departure from the rule is a page, not an informal arrangement, which is what makes it
  reviewable later.
- Anything that briefs a person against "the layout" — cover arrangements, training, a depot
  visit — now has a single layout to brief against, which it did not before.
