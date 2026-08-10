---
type: note
title: "Northwind Freight Onboarding"
note_type: synthesis
question: ""
status: developing
created: 2026-01-12
updated: 2026-01-12
tags: [note, onboarding]
entity: ["northwind-freight"]
related: ["[[Northwind Freight]]"]
sources: []
---

# Northwind Freight Onboarding

How the [[Northwind Freight]] account was brought onto the routing system: which depots joined
first, what the integration touched, and what was left for a later pass.

## How it was sequenced

The account came on in phases rather than all at once, on the argument that a depot which joins
early and works is a better reference for the next one than any amount of planning. The first
depots were connected to the routing system and the rest were left for a later phase, with the
sequence deliberately left open rather than published as a schedule.

- Dispatch schedules were migrated first, because they are read by every other integration and
  everything downstream had to be re-pointed at them anyway.
- Depot contact details and operating hours were migrated with the schedules, since a schedule
  without them cannot be acted on.
- Slotting rules were left on their existing footing pending the review recorded in
  [[Warehouse Slotting Policy]].

## What it deliberately did not cover

- The remaining depots. They are a later phase and nothing here describes them.
- Anything about how a depot is laid out internally. That was already understood to be a
  question of its own, and it was answered separately rather than inside the onboarding.
- Training beyond the dispatch handover. Depot staff were walked through the schedule screens
  and nothing else, on the assumption that the rest is learned by using it.

## Open questions

- **Weekend cover.** Cover staff move between depots at weekends and nobody has written down how
  they are briefed. It came up repeatedly during the onboarding and was left unanswered; this
  note does not answer it either. Each depot briefs its own cover informally, which works while
  the same people cover the same depots.

## Connections

- [[Northwind Freight]] — the entity this note is about
- [[Warehouse Slotting Policy]] — the decision the slotting question was deferred to
