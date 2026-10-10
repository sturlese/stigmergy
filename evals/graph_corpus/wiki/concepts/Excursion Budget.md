---
id: page_3aa63d5f-99e8-48c9-8900-18a006751b88
type: concept
title: Excursion Budget
status: developing
created: '2026-01-22'
updated: '2026-07-07'
acl: null
sources:
- sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md
- sources/2026/03/7fea1cb0-76c8-5fee-bf88-e5d11f072207.md
- sources/2026/03/6b6348b1-877b-5da1-bc95-849a8a3c6ede.md
- sources/2026/03/62b65f0b-56b9-5b32-8640-0cf17bda9932.md
- sources/2026/04/067f41cc-472f-5ede-8b6c-dfeaa680e01e.md
- sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md
- sources/2026/05/764e9f2e-7356-53ba-8931-f52a87c758b2.md
- sources/2026/06/b7b68a5b-c333-5c46-8831-2006543cfc9b.md
- sources/2026/07/e64e610b-8873-5f99-b8ae-72f819367db6.md
---

# Excursion Budget

## Definition

An excursion budget is the total cumulative time a shipment may spend outside its temperature band before the product is considered at risk. (Source: `sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md`)

## How It Works

Rather than alerting on every threshold crossing, Tidewell would alert when a shipment has consumed 50 percent and 80 percent of its budget, and escalate when the budget is exhausted. [[Coldpath]] would show a budget gauge per shipment. (Source: `sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md`)

In the redesigned [[Coldpath]] home screen presented on 12 March 2026, every active shipment shows an excursion budget gauge that turns amber at 50 percent and red at 80 percent of budget. (Source: `sources/2026/03/6b6348b1-877b-5da1-bc95-849a8a3c6ede.md`)

## Why It Matters

Most threshold crossings are harmless, such as a door opening for four minutes at a supermarket dock. The memo argues that product safety depends on cumulative time out of range, not a single crossing, and expects excursion budgets to remove most of the noise [[Frostvale Distribution]] complains about while catching slow, dangerous excursions that single thresholds miss. The source does not report a design, owner, or delivery date. (Source: `sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md`)

## Examples

- Frozen goods: a common budget is 30 cumulative minutes above minus 15 degrees Celsius.
- Pharmaceuticals kept between 2 and 8 degrees: customers set the budget in their Good Distribution Practice procedures, often 60 cumulative minutes.
- [[Altamira Pharma Logistics]]: its Good Distribution Practice procedures set a budget of 60 cumulative minutes per shipment, and the pilot's alerts are based on it. (Source: `sources/2026/03/7fea1cb0-76c8-5fee-bf88-e5d11f072207.md`)
- [[Altamira Pharma Logistics]]: in its 90-day pilot, 21 of 23 detected excursions stayed within the 60-minute budget and 2 exhausted it. (Source: `sources/2026/05/764e9f2e-7356-53ba-8931-f52a87c758b2.md`)
- [[Halden Seafood]]: two salmon shipments spoiled in May 2026 under [[Brightline Telematics]] because a slow temperature rise never crossed its threshold for long enough to trigger an alert; [[Mireille Laurent]] said the rise would have consumed the excursion budget and raised an alert at 50 percent. (Source: `sources/2026/06/b7b68a5b-c333-5c46-8831-2006543cfc9b.md`)
- [[Halden Seafood]]: its pilot with [[Tidewell]] sets the budget at 20 cumulative minutes above 4 degrees Celsius for fresh salmon, the limit its quality team uses. (Source: `sources/2026/07/e64e610b-8873-5f99-b8ae-72f819367db6.md`)

## Connections

- [[Coldpath]] - dashboard that would show a budget gauge per shipment
- [[Tidewell]] - company proposing it
- [[Frostvale Distribution]] - customer whose alert noise motivated it
- [[Altamira Pharma Logistics]] - pilot it is recommended to precede
- [[Dwell-Time Awareness]] - related alert-noise reduction approach
- [[Excursion Budget Memo January 2026]] - memo proposing it
- [[Altamira Pharma Logistics Pilot Kickoff]] - pilot whose alerts use a 60-minute budget
- [[Coldpath Redesign Review March 2026]] - review where the gauge was presented
- [[Brightline Telematics]] - competitor that lacks an excursion budget
- [[Halberd Mutual Partnership Memorandum April 2026]] - the memorandum's discount requires excursion budget alerting
- [[Halberd Mutual]] - insurer whose discount requires it
- [[Dwell-Time Alerting Design April 2026]] - design where crossings within a dwell limit count against the budget without alerting
- [[Altamira Pilot Results May 2026]] - review reporting the pilot's excursions against the budget
- [[Halden Seafood]] - customer whose spoiled shipments illustrate a slow rise consuming the budget
- [[Halden Seafood Win-Back Call June 2026]] - note where the mechanism was explained
- [[Halden Seafood Pilot Start July 2026]] - note where the pilot's budget was set
- [[Tiago Ferreira]] - Halden quality assurance manager whose team uses the limit
