---
id: page_9922a1a3-2cdd-4231-bf7a-0b043eafab8a
type: note
title: Cumulus Cloud Outage February 2026
status: developing
created: '2026-02-03'
updated: '2026-02-03'
acl: null
sources:
- sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md
---

# Cumulus Cloud Outage February 2026

On 1 February 2026 the eu-west region of [[Cumulus Cloud]], [[Tidewell]]'s hosting provider, was unavailable for 3 hours and 40 minutes. (Source: `sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md`)

## Impact

Tidewell's ingestion service and alerting pipeline both ran only in that region, so no alerts were sent during the outage. 14 customers were affected, including [[Casa Verde Markets]] and [[Frostvale Distribution]]. No readings were lost, because every [[Tidewell Sentinel]] gateway buffers up to 72 hours of readings locally and uploaded them when the region came back. Two Casa Verde trucks had temperature excursions during the outage that were only reported afterwards; the product was inspected and kept. (Source: `sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md`)

## Response

The local buffer in the Sentinel gateway worked as designed. Customer success, led by [[Aurélie Fontaine]], called every affected customer within two hours. (Source: `sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md`)

## What Went Wrong

A single hosting region for the alerting pipeline, and no public status page. (Source: `sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md`)

## Actions

- Run the alerting pipeline in two Cumulus Cloud regions with automatic failover. Owner: [[Priya Raman]]. Due: end of March 2026.
- Publish a status page. Owner: [[Dmitri Volkov]]. Due: end of February 2026.
- Review whether the 72-hour gateway buffer is long enough. Owner: [[Rafael Duarte]].

(Source: `sources/2026/02/059b5629-3b54-5ba3-821e-08fcb2c7146f.md`)

## Entities

- [[Tidewell]] - operator of the affected ingestion and alerting services
- [[Cumulus Cloud]] - hosting provider whose eu-west region failed
- [[Dmitri Volkov]] - wrote the postmortem and owns the status page action
- [[Priya Raman]] - owns the multi-region failover action
- [[Rafael Duarte]] - owns the gateway buffer review action
- [[Aurélie Fontaine]] - led customer success calls to affected customers
- [[Casa Verde Markets]] - affected customer
- [[Frostvale Distribution]] - affected customer
- [[Tidewell Sentinel]] - gateway whose local buffer prevented data loss

## Connections

- [[Tidewell]] - company whose services were affected
- [[Cumulus Cloud]] - provider whose region was unavailable
- [[Tidewell Sentinel]] - gateway buffer that prevented data loss
- [[Frostvale Distribution]] - affected customer
- [[Casa Verde Markets]] - affected customer
