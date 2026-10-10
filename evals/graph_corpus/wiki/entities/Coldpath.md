---
id: page_abd0d376-12bc-4a3e-b057-ed28b1238c7d
type: entity
title: Coldpath
status: developing
created: '2026-01-08'
updated: '2026-07-21'
acl: null
sources:
- sources/2026/01/60e59b80-2e07-52cb-9a03-a4b3d40e34fd.md
- sources/2026/01/5649a227-256b-5fc1-9ddd-551c0b8b3de4.md
- sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md
- sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md
- sources/2026/03/6b6348b1-877b-5da1-bc95-849a8a3c6ede.md
- sources/2026/04/af1eefa5-0082-5c2c-92bc-a108b57dcce0.md
- sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md
- sources/2026/06/c53e64d7-7552-5fe9-a458-e2f48f6820b8.md
- sources/2026/07/e64e610b-8873-5f99-b8ae-72f819367db6.md
- sources/2026/07/dab77dff-5866-5868-8074-3fa9683636ba.md
entity_type: product
aliases: []
---

# Coldpath

## What / Who

Coldpath is [[Tidewell]]'s web dashboard, used by dispatchers to view readings streamed from the [[Tidewell Sentinel]] gateway. (Source: `sources/2026/01/60e59b80-2e07-52cb-9a03-a4b3d40e34fd.md`)

## Facts

- 2026-01-08: dispatchers at [[Frostvale Distribution]] complained they cannot find the alerts that matter in Coldpath; [[Joon-ho Park]] will redesign it in the first half of 2026.

(Source: `sources/2026/01/60e59b80-2e07-52cb-9a03-a4b3d40e34fd.md`)

- 2026-01-15: in December, dispatchers at [[Frostvale Distribution]] received 1,900 temperature alerts through it; 70 percent resolved on their own within ten minutes and only 22 percent were acknowledged, and [[Gunnar Lindqvist]] said his team has started ignoring its notifications.
- 2026-01-15: [[Gunnar Lindqvist]] requested alerts with dwell-time awareness and a single view per trip instead of a flat list of alerts.

(Source: `sources/2026/01/5649a227-256b-5fc1-9ddd-551c0b8b3de4.md`)

- 2026-01-22: an internal memo proposed showing a budget gauge per shipment in it, as part of replacing per-crossing threshold alerts with excursion budgets.

(Source: `sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md`)

- 2026-02-10: will flag pairs of probes in the same compartment that disagree by more than 0.8 degrees for a day, as part of drift detection added to the [[Tidewell Sentinel]] firmware.

(Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

- 2026-03-12: at a design review, [[Joon-ho Park]] presented a redesigned home screen with an [[Excursion Budget|excursion budget]] gauge for every active shipment (amber at 50 percent, red at 80 percent), an alert inbox grouped by trip, and quiet hours per site during which planned loading does not raise alerts; the redesign ships on 20 April 2026.
- 2026-03-12: in a usability test with six dispatchers from [[Frostvale Distribution]] and [[Casa Verde Markets]], the time to find the one shipment that needed action fell from 4 minutes 10 seconds to 1 minute 50 seconds, and five of six dispatchers preferred the trip grouping.
- 2026-03-12: [[Rafael Duarte]] confirmed the excursion budget logic from [[Priya Raman]]'s memo will be live by the ship date, and a weekly email summary for customers who do not open Coldpath daily was added to the backlog at [[Aurélie Fontaine]]'s request.

(Source: `sources/2026/03/6b6348b1-877b-5da1-bc95-849a8a3c6ede.md`)

- 2026-04-09: [[Gunnar Lindqvist]] said the redesign demo convinced his dispatchers more than any slide; [[Aurélie Fontaine]] will run training sessions for [[Frostvale Distribution]]'s three dispatch centres after it ships on 20 April.

(Source: `sources/2026/04/af1eefa5-0082-5c2c-92bc-a108b57dcce0.md`)

- 2026-04-23: a design document by [[Priya Raman]] gives every site in it a dwell limit, the time a vehicle may stay with doors open, and suppresses alerts while a vehicle is within that limit at a known site.

(Source: `sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md`)

- 2026-06-10: in the first full month after dwell-time alerting went live on 15 May 2026, alerts at [[Frostvale Distribution]] fell from about 1,900 to 930 a month and the acknowledged-alert ratio rose from 22 percent to 64 percent; [[Gunnar Lindqvist]] said his dispatchers now trust it again.

(Source: `sources/2026/06/c53e64d7-7552-5fe9-a458-e2f48f6820b8.md`)

- 2026-07-07: [[Aurélie Fontaine]] trained [[Halden Seafood]]'s four dispatchers on its trip view at the start of the pilot.

(Source: `sources/2026/07/e64e610b-8873-5f99-b8ae-72f819367db6.md`)

- 2026-07-21: [[Tidewell]] configures the sites in it for phase 1 of the [[Casa Verde Markets]] cold-room rollout, where [[Refritec Instalações]] installs the probes and mounts the gateways.

(Source: `sources/2026/07/dab77dff-5866-5868-8074-3fa9683636ba.md`)

## Connections

- [[Tidewell]] - maker
- [[Tidewell Sentinel]] - gateway that streams readings to it
- [[Joon-ho Park]] - will redesign it
- [[Frostvale Distribution]] - customer whose dispatchers use it
- [[Tidewell 2026 Kickoff]] - meeting where the redesign was announced
- [[Gunnar Lindqvist]] - Frostvale VP Logistics who reported the alert noise
- [[Dwell-Time Awareness]] - requested alerting capability
- [[Frostvale Quarterly Business Review January 2026]] - meeting where the alert noise was reported
- [[Excursion Budget]] - proposed budget gauge per shipment
- [[Excursion Budget Memo January 2026]] - memo proposing the gauge
- [[Veldt Sensors Call February 2026]] - call where the probe-pair flagging was decided
- [[Probe Drift]] - failure mode the flagging targets
- [[Coldpath Redesign Review March 2026]] - review where the redesigned home screen was presented
- [[Casa Verde Markets]] - customer whose dispatchers took part in the usability test
- [[Aurélie Fontaine]] - requested the weekly email summary
- [[Priya Raman]] - author of the excursion budget memo
- [[Frostvale Renewal Negotiation April 2026]] - meeting where the demo's effect was reported
- [[Dwell-Time Alerting Design April 2026]] - design specifying dwell limits per site
- [[Frostvale Dwell-Time Results June 2026]] - review reporting its first month of dwell-time alerting results
- [[Halden Seafood]] - customer whose dispatchers were trained on its trip view
- [[Halden Seafood Pilot Start July 2026]] - note recording the training
- [[Refritec Instalações]] - contractor installing the Casa Verde rollout
- [[Nuno Carvalho]] - Refritec installer at the phase 1 status meeting
- [[Casa Verde Cold-Room Rollout Phase 1 July 2026]] - meeting where its role in the rollout was recorded
