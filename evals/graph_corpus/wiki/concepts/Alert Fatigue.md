---
id: page_4480333e-8af2-4371-959a-7074a6a47826
type: concept
title: Alert Fatigue
status: developing
created: '2026-04-30'
updated: '2026-04-30'
acl: null
sources:
- sources/2026/04/76cb1b34-48f6-50d9-9cc0-199d77e64b7a.md
---

# Alert Fatigue

## Definition

Alert fatigue is the state in which people receive so many alerts that they stop reacting to any of them. (Source: `sources/2026/04/76cb1b34-48f6-50d9-9cc0-199d77e64b7a.md`)

## How It Works

In cold-chain operations, alert fatigue is dangerous because the one alert that matters, such as a compressor failing on a motorway, arrives in the same stream as dozens of harmless door openings. (Source: `sources/2026/04/76cb1b34-48f6-50d9-9cc0-199d77e64b7a.md`)

The acknowledged-alert ratio — the share of alerts that a dispatcher opens and acts on — measures it. Tidewell measured 22 percent at one large frozen-food customer in December 2025; a healthy operation should be above 70 percent. (Source: `sources/2026/04/76cb1b34-48f6-50d9-9cc0-199d77e64b7a.md`)

## Reducing It

Four levers reduce alert fatigue:

- [[Excursion Budget|excursion budgets]], which alert on cumulative risk instead of every threshold crossing;
- [[Dwell-Time Awareness|dwell-time limits]], which understand planned stops;
- grouping alerts by trip, so one journey produces one row;
- quiet hours for sites where loading is scheduled.

(Source: `sources/2026/04/76cb1b34-48f6-50d9-9cc0-199d77e64b7a.md`)

## Why It Matters

The article argues that alerting quality, not sensor accuracy, is now the main reason cold-chain monitoring fails in practice. (Source: `sources/2026/04/76cb1b34-48f6-50d9-9cc0-199d77e64b7a.md`)

## Connections
- [[Excursion Budget]] - lever that alerts on cumulative risk instead of every threshold crossing
- [[Dwell-Time Awareness]] - dwell-time limits that understand planned stops
- [[Coldpath]] - dashboard where trip grouping and quiet hours appear
- [[Frostvale Distribution]] - customer whose December 2025 acknowledged-alert ratio was 22 percent
- [[Alert Fatigue in Cold-Chain Operations]] - the article that explains it
