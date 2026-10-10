---
id: page_7c56c37d-4f9a-440c-9e39-f3772a207ae7
type: note
title: Excursion Budget Memo January 2026
status: developing
created: '2026-01-22'
updated: '2026-01-22'
acl: null
sources:
- sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md
---

# Excursion Budget Memo January 2026

[[Priya Raman]], staff engineer responsible for the alerting pipeline at [[Tidewell]], wrote an internal memo on 22 January 2026 proposing that Tidewell replace per-crossing threshold alerts with excursion budgets. (Source: `sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md`)

## Proposal

Tidewell currently raises an alert every time a probe crosses a temperature threshold. The memo proposes an [[Excursion Budget|excursion budget]]: the total minutes a shipment may spend out of range before the product is at risk. Instead of alerting on every crossing, Tidewell would alert when a shipment has consumed 50 percent and 80 percent of its budget, and escalate when the budget is exhausted. [[Coldpath]] would show a budget gauge per shipment. (Source: `sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md`)

## Rationale

Most threshold crossings are harmless: a door opens for four minutes at a supermarket dock and the air warms, then recovers. The memo argues that what matters for product safety is the cumulative time a shipment spends outside its temperature band, not a single crossing. Priya expects the change to remove most of the noise [[Frostvale Distribution]] complains about while catching the slow, dangerous excursions that single thresholds miss. (Source: `sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md`)

## Timing

Priya recommends building excursion budgets before the [[Altamira Pharma Logistics]] pilot, because pharmaceutical customers think in excursion budgets already. (Source: `sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md`)

## Entities

- [[Priya Raman]] - author, staff engineer for the alerting pipeline
- [[Tidewell]] - company where the memo was written
- [[Frostvale Distribution]] - customer whose December alert volume motivated the proposal
- [[Coldpath]] - dashboard that would show a budget gauge
- [[Altamira Pharma Logistics]] - pilot the memo recommends building before

## Connections

- [[Excursion Budget]] - the mechanism the memo proposes
- [[Dwell-Time Awareness]] - related alert-noise reduction requested by Frostvale
- [[Frostvale Quarterly Business Review January 2026]] - meeting where the alert noise was reported
