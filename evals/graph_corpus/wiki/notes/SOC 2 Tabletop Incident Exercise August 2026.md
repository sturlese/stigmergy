---
id: page_7ef49640-e911-4c5e-89ca-27bafa187405
type: note
title: SOC 2 Tabletop Incident Exercise August 2026
status: developing
created: '2026-08-04'
updated: '2026-08-04'
acl: null
sources:
- sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md
---

# SOC 2 Tabletop Incident Exercise August 2026

Tabletop exercise on 4 August 2026, required by [[Tidewell]]'s SOC 2 Type II auditors. It was facilitated by [[Dmitri Volkov]], with [[Grace Liu]], the lead auditor from [[Carrow & Finch]], observing. Participants were [[Rafael Duarte]], [[Priya Raman]], [[Aurélie Fontaine]] and [[Hannah Brandt]]. (Source: `sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md`)

## Scenario

The scenario was ransomware encrypting Tidewell's databases in the [[Cumulus Cloud]] eu-west region on a Friday night. (Source: `sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md`)

## What Worked

The Frankfurt failover region took over alerting within 12 minutes in the simulation, and the [[Tidewell Sentinel]] gateways kept buffering readings. (Source: `sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md`)

## Gaps Found

- Nobody knew who informs customers on a weekend; [[Aurélie Fontaine]] now owns weekend customer communication.
- Backups of the analytics store built by [[Amara Nwosu]] were in the same region as the primary; they move to Frankfurt.

(Source: `sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md`)

## Outcome

[[Grace Liu]] accepted the exercise as evidence for the incident response control. (Source: `sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md`)

## Entities

- [[Tidewell]] - company that ran the exercise
- [[Dmitri Volkov]] - facilitated the exercise
- [[Grace Liu]] - lead auditor from Carrow & Finch, observing
- [[Carrow & Finch]] - audit firm
- [[Rafael Duarte]] - participant
- [[Priya Raman]] - participant
- [[Aurélie Fontaine]] - participant, now owns weekend customer communication
- [[Hannah Brandt]] - participant
- [[Cumulus Cloud]] - hosting provider whose eu-west region was the scenario
- [[Tidewell Sentinel]] - gateways that kept buffering readings
- [[Amara Nwosu]] - built the analytics store whose backups move to Frankfurt

## Connections

- [[SOC 2 Type II Readiness Review May 2026]] - review where the auditors asked for a tabletop exercise before August
- [[Cumulus Cloud Outage February 2026]] - earlier incident whose postmortem the auditors accepted as evidence
- [[Carrow & Finch]] - audit firm whose lead auditor accepted the exercise
