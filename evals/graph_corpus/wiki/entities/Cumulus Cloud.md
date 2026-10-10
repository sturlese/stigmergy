---
id: page_41d4bc4a-cba9-49c7-a99b-ee77775a0b8c
type: entity
title: Cumulus Cloud
status: developing
created: '2026-02-03'
updated: '2026-08-04'
acl: null
sources:
- sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md
- sources/2026/04/19f0d701-6384-5034-bf11-afa208b071d8.md
- sources/2026/05/006a32e9-a5eb-5b14-bbfb-1a5a8b15373f.md
- sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md
- sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md
entity_type: organization
aliases: []
---

# Cumulus Cloud

## What / Who

Cumulus Cloud is the hosting provider for [[Tidewell]]. (Source: `sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md`)

## Facts

- 2026-02-01: its eu-west region was unavailable for 3 hours and 40 minutes, during which Tidewell's ingestion service and alerting pipeline, which ran only in that region, sent no alerts.

(Source: `sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md`)

- 2026-04-16: has a Frankfurt region; the second region planned after the February outage will be Frankfurt, covering both failover and data residency for [[Tidewell]]'s German customers.

(Source: `sources/2026/04/19f0d701-6384-5034-bf11-afa208b071d8.md`)

- 2026-05-13: named as one of two critical suppliers for which [[Tidewell]]'s SOC 2 readiness review found vendor risk assessments missing.

(Source: `sources/2026/05/006a32e9-a5eb-5b14-bbfb-1a5a8b15373f.md`)

- 2026-07-02: its Frankfurt region was confirmed as the location for the data of the [[Nordhafen Tiefkühl]] pilot, after [[Klaus Ehrlich]] asked for the data to stay in the Frankfurt region.

(Source: `sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md`)

- 2026-08-04: its eu-west region was the setting of [[Tidewell]]'s SOC 2 tabletop incident exercise, in which ransomware encrypted Tidewell's databases there; the Frankfurt failover region took over alerting within 12 minutes in the simulation.

(Source: `sources/2026/08/64c1d91a-92fc-55a1-8f95-cf07cb0e9a72.md`)

## Connections

- [[Tidewell]] - customer whose services run on it
- [[Cumulus Cloud Outage February 2026]] - outage of its eu-west region
- [[Hamburg Office Plan April 2026]] - meeting where its Frankfurt region was confirmed
- [[SOC 2 Type II Readiness Review May 2026]] - review where it was named a critical supplier missing a vendor risk assessment
- [[Nordhafen Tiefkühl]] - customer whose pilot data will stay in its Frankfurt region
- [[Nordhafen Tiefkühl Pilot Kickoff]] - kickoff where the data residency was confirmed
- [[Klaus Ehrlich]] - Nordhafen Head of Fleet who required the Frankfurt region
- [[SOC 2 Tabletop Incident Exercise August 2026]] - exercise whose scenario was set in its eu-west region
