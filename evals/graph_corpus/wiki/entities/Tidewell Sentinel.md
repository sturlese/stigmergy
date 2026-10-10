---
id: page_1fe16e5d-164c-45f6-bc10-bde6e0e9d7d9
type: entity
title: Tidewell Sentinel
status: developing
created: '2026-01-08'
updated: '2026-08-04'
acl: null
sources:
- sources/2026/01/60e59b80-2e07-52cb-9a03-a4b3d40e34fd.md
- sources/2026/01/5649a227-256b-5fc1-9ddd-551c0b8b3de4.md
- sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md
- sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md
- sources/2026/02/a72c62aa-a32d-5e01-8437-bdc6a7a85b1a.md
- sources/2026/03/50f70755-f5c4-5148-a444-1e4187125b02.md
- sources/2026/05/ecbc4006-43b5-542a-80a7-44fb9f384b8c.md
- sources/2026/06/f0965a0c-1c57-529c-a4f0-679d12f235f1.md
- sources/2026/07/dab77dff-5866-5868-8074-3fa9683636ba.md
- sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md
entity_type: product
aliases: []
---

# Tidewell Sentinel

## What / Who

The Tidewell Sentinel is [[Tidewell]]'s gateway product. It sits in each truck, reads temperature probes and door sensors, and streams readings to [[Coldpath]]. (Source: `sources/2026/01/60e59b80-2e07-52cb-9a03-a4b3d40e34fd.md`)

## Facts

- 2026-01-08: described as the in-truck gateway that reads temperature probes and door sensors and streams readings to the Coldpath dashboard.

(Source: `sources/2026/01/60e59b80-2e07-52cb-9a03-a4b3d40e34fd.md`)

- 2026-01-15: all 420 of [[Frostvale Distribution]]'s refrigerated trucks are fitted with it.

(Source: `sources/2026/01/5649a227-256b-5fc1-9ddd-551c0b8b3de4.md`)

- 2026-02-01: its local buffer of up to 72 hours of readings prevented data loss during the [[Cumulus Cloud]] eu-west outage; [[Rafael Duarte]] will review whether 72 hours is long enough.

(Source: `sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md`)

- 2026-02-10: [[Rafael Duarte]]'s team will add drift detection to its firmware: when two probes in the same compartment disagree by more than 0.8 degrees for a day, [[Coldpath]] flags the pair.

(Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

- 2026-02-26: priced at 9 euros per month per gateway under the per-asset pricing agreed at the pricing workshop.

(Source: `sources/2026/02/a72c62aa-a32d-5e01-8437-bdc6a7a85b1a.md`)

- 2026-03-19: confirmed to support fixed installation with mains power, so it can monitor cold rooms inside stores as well as trucks.

(Source: `sources/2026/03/50f70755-f5c4-5148-a444-1e4187125b02.md`)

- 2026-05-27: a v3 gateway was planned to replace v2, adding LTE-M connectivity with Wi-Fi fallback for in-store cold rooms, a local buffer of 7 days instead of 72 hours, and two probe inputs per compartment so drift detection can compare paired probes in firmware; unit cost falls from 97 euros for v2 to 84 euros for v3, with pilot units in August and production from October 2026, and v2 supported until 2028.

(Source: `sources/2026/05/ecbc4006-43b5-542a-80a7-44fb9f384b8c.md`)

- 2026-07-21: the v2 gateway has no cellular fallback for fixed installations, which blocks 9 stores in the [[Casa Verde Markets]] cold-room rollout where Wi-Fi does not reach the cold rooms; [[Tomasz Zielinski]] proposed using the v3 pilot units there because v3 has LTE-M.

(Source: `sources/2026/07/dab77dff-5866-5868-8074-3fa9683636ba.md`)

- 2026-08-04: in [[Tidewell]]'s SOC 2 tabletop incident exercise, the gateways kept buffering readings while the Frankfurt failover region took over alerting.

(Source: `sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md`)

## Connections

- [[Tidewell]] - maker
- [[Coldpath]] - dashboard it streams readings to
- [[Tidewell 2026 Kickoff]] - meeting where the product was described
- [[Frostvale Distribution]] - customer whose fleet is fitted with it
- [[Frostvale Quarterly Business Review January 2026]] - meeting where the fleet was described
- [[Cumulus Cloud Outage February 2026]] - outage during which its buffer prevented data loss
- [[Cumulus Cloud]] - provider whose region failed
- [[Veldt Sensors Call February 2026]] - call where the firmware drift detection was decided
- [[Probe Drift]] - failure mode the drift detection targets
- [[Rafael Duarte]] - CTO whose team will add the drift detection
- [[Pricing Workshop February 2026]] - workshop where its per-asset price was set
- [[Per-Asset Pricing]] - model that prices it per gateway
- [[Casa Verde Markets]] - customer considering it for in-store cold rooms
- [[Casa Verde Spain Expansion Meeting March 2026]] - meeting where the fixed installation was confirmed
- [[Sentinel Gateway v3 Hardware Plan May 2026]] - meeting where the v3 changes, cost and timeline were set
- [[Veldt Sensors]] - supplier of the v3 probes
- [[First-Half 2026 Review June 2026]] - review naming the v3 October ship a second-half priority
- [[Refritec Instalações]] - contractor mounting the gateways in the Casa Verde rollout
- [[Nuno Carvalho]] - Refritec installer at the phase 1 status meeting
- [[Casa Verde Cold-Room Rollout Phase 1 July 2026]] - meeting where the v2 limitation was raised
- [[SOC 2 Tabletop Incident Exercise August 2026]] - exercise where the gateways kept buffering readings
