---
id: page_8b9218a4-d134-487f-861c-efd2809695ac
type: note
title: Sentinel Gateway v3 Hardware Plan May 2026
status: developing
created: '2026-05-27'
updated: '2026-05-27'
acl: null
sources:
- sources/2026/05/ecbc4006-43b5-542a-80a7-44fb9f384b8c.md
---

# Sentinel Gateway v3 Hardware Plan May 2026

On 27 May 2026, [[Tidewell]] held a hardware planning meeting for the [[Tidewell Sentinel]] v3 gateway. Present were [[Rafael Duarte]] (CTO), [[Tomasz Zielinski]] (Head of Operations) and [[Lotte de Vries]] of [[Veldt Sensors]]. (Source: `sources/2026/05/ecbc4006-43b5-542a-80a7-44fb9f384b8c.md`)

## Changes from v2

The v3 gateway will replace the current v2 gateway, with three changes:

- LTE-M connectivity with Wi-Fi fallback, for cold rooms in stores, needed for the [[Casa Verde Markets]] rollout;
- a local buffer of 7 days instead of 72 hours, following the review after the [[Cumulus Cloud]] outage;
- two probe inputs per compartment, so drift detection can compare paired probes in firmware.

(Source: `sources/2026/05/ecbc4006-43b5-542a-80a7-44fb9f384b8c.md`)

## Probes

[[Veldt Sensors]] will supply the probes for v3 with an individual calibration certificate for each probe, which pharmaceutical customers require. [[Lotte de Vries]] confirmed that the resin problem behind batch VS-2207 cannot recur with the new supplier of the probe housing. (Source: `sources/2026/05/ecbc4006-43b5-542a-80a7-44fb9f384b8c.md`)

## Cost and Timeline

Unit cost falls from 97 euros for v2 to 84 euros for v3. Pilot units are due in August, with production from October 2026. Existing v2 gateways stay supported until 2028. (Source: `sources/2026/05/ecbc4006-43b5-542a-80a7-44fb9f384b8c.md`)

## Entities

- [[Tidewell]] - company planning the gateway
- [[Tidewell Sentinel]] - gateway being planned
- [[Rafael Duarte]] - CTO at the meeting
- [[Tomasz Zielinski]] - Head of Operations at the meeting
- [[Lotte de Vries]] - Veldt Sensors account manager at the meeting
- [[Veldt Sensors]] - probe supplier for v3
- [[Casa Verde Markets]] - customer whose rollout needs the new connectivity

## Connections

- [[Cumulus Cloud Outage February 2026]] - outage whose review led to the longer buffer
- [[Probe Drift]] - failure mode the paired probe inputs target
- [[Veldt Sensors Call February 2026]] - call where the VS-2207 resin problem was traced
- [[Casa Verde Spain Expansion Meeting March 2026]] - meeting where the in-store cold room rollout was discussed
