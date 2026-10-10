---
id: page_a8697ae0-4916-4527-97ad-43c925e33565
type: note
title: Nordhafen Tiefkühl Pilot Kickoff
status: developing
created: '2026-07-02'
updated: '2026-07-02'
acl: null
sources:
- sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md
---

# Nordhafen Tiefkühl Pilot Kickoff

Kickoff on 2 July 2026 for the pilot between [[Tidewell]] and [[Nordhafen Tiefkühl]], a frozen-food distributor based in Bremen with 150 trucks. Nordhafen was represented by [[Klaus Ehrlich]], Head of Fleet, and [[Maren Vogt]], IT lead; Tidewell by [[Sabine Krüger]] and [[Lukas Becker]]. (Source: `sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md`)

## Scope

The pilot covers 30 trucks on the Bremen–Hamburg routes from 1 September 2026 for 60 days, with a pilot fee of 9,000 euros. (Source: `sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md`)

## Integration

[[Maren Vogt]] requires that Tidewell alerts appear inside [[Stratos ERP]], the enterprise system Nordhafen uses for dispatch and shipment records. [[Sabine Krüger]] confirmed that the [[Frostline API]] can push excursion events to Stratos, and that Tidewell is talking to [[Stratos Software]] about a certified connector. (Source: `sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md`)

## Data and Reporting

[[Klaus Ehrlich]] asked for German-language reports and for the data to stay in the Frankfurt region of [[Cumulus Cloud]]. Both were confirmed. (Source: `sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md`)

## Installation

[[Lukas Becker]] will install the gateways at the Bremen depot in the last week of August. (Source: `sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md`)

## Entities

- [[Tidewell]] - supplier running the pilot
- [[Nordhafen Tiefkühl]] - customer piloting
- [[Klaus Ehrlich]] - Nordhafen Head of Fleet
- [[Maren Vogt]] - Nordhafen IT lead
- [[Sabine Krüger]] - Tidewell sales lead at the kickoff
- [[Lukas Becker]] - Tidewell field engineer who will install the gateways
- [[Stratos ERP]] - Nordhafen's enterprise system for dispatch and shipment records
- [[Stratos Software]] - vendor of Stratos ERP
- [[Frostline API]] - Tidewell API that can push excursion events to Stratos
- [[Cumulus Cloud]] - hosting provider whose Frankfurt region will hold the data

## Connections

- [[Hamburg Office Opening Report June 2026]] - report where Nordhafen was the most advanced German prospect wanting a September pilot
- [[Nordhafen Tiefkühl]] - the pilot this kickoff starts
- [[Frostline API]] - integration channel for alerts into Stratos
- [[Cumulus Cloud]] - data residency in its Frankfurt region
