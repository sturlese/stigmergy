---
id: page_76515780-9755-41e0-9f4f-6df2ebeb6dfc
type: note
title: Veldt Sensors Call February 2026
status: developing
created: '2026-02-10'
updated: '2026-02-10'
acl: null
sources:
- sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md
---

# Veldt Sensors Call February 2026

On 10 February 2026, [[Tidewell]] and [[Veldt Sensors]], the supplier of Tidewell's temperature probes, held a call about probe drift in production batch VS-2207. Veldt was represented by [[Lotte de Vries]], account manager; Tidewell by [[Tomasz Zielinski]] and [[Rafael Duarte]]. (Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

## Probe Drift in Batch VS-2207

Tomasz reported that probes from batch VS-2207 read about 1.4 degrees Celsius too warm after roughly nine months in service. Field engineers found the drift when comparing probes against reference thermometers at [[Frostvale Distribution]]. About 600 probes from the batch are installed across Tidewell customers, 180 of them in Frostvale trucks. (Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

Lotte confirmed that Veldt had traced the drift to a faulty resin used in that batch. (Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

## Replacement

Veldt will replace every affected probe free of charge and cover the field engineers' travel. Replacement at Frostvale will be finished by 30 March. (Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

## Decisions

- Tidewell will run a calibration audit every quarter, comparing a sample of probes against reference thermometers.
- Rafael's team will add drift detection to the [[Tidewell Sentinel]] gateway firmware: when two probes in the same compartment disagree by more than 0.8 degrees for a day, [[Coldpath]] flags the pair.
- Tomasz will keep a register of probe batches per customer so that a future defect can be traced in hours instead of weeks.

(Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

## Entities

- [[Tidewell]] - company whose probes drifted
- [[Veldt Sensors]] - supplier of the probes
- [[Lotte de Vries]] - Veldt account manager on the call
- [[Tomasz Zielinski]] - Tidewell Head of Operations, reported the drift
- [[Rafael Duarte]] - Tidewell CTO, owns the firmware drift detection
- [[Frostvale Distribution]] - customer with 180 affected probes
- [[Tidewell Sentinel]] - gateway whose firmware will gain drift detection
- [[Coldpath]] - dashboard that will flag disagreeing probe pairs

## Connections

- [[Probe Drift]] - the failure mode discussed
- [[Calibration Audit]] - quarterly practice Tidewell decided to adopt
- [[Tidewell]] - company that called the meeting
- [[Frostvale Distribution]] - customer where the drift was found and replacement is due
