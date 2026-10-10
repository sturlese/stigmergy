---
id: page_6fcdc5df-f67d-46a4-99c3-0e1b60f0614f
type: note
title: SOC 2 Type II Readiness Review May 2026
status: developing
created: '2026-05-13'
updated: '2026-05-13'
acl: null
sources:
- sources/2026/05/006a32e9-a5eb-5b14-bbfb-1a5a8b15373f.md
---

# SOC 2 Type II Readiness Review May 2026

Readiness review on 13 May 2026 for [[Tidewell]]'s SOC 2 Type II audit. Present were [[Dmitri Volkov]] (security and reliability lead), [[Rafael Duarte]], and two auditors from [[Carrow & Finch]], the audit firm Tidewell engaged. (Source: `sources/2026/05/006a32e9-a5eb-5b14-bbfb-1a5a8b15373f.md`)

## Observation Window

The SOC 2 Type II observation window runs from 1 May to 31 August 2026, so the report can be issued in late September. (Source: `sources/2026/05/006a32e9-a5eb-5b14-bbfb-1a5a8b15373f.md`)

## Gaps Found

- Access reviews for production systems are not documented; [[Dmitri Volkov]] will run them every quarter.
- Vendor risk assessments are missing for the two critical suppliers, [[Cumulus Cloud]] and [[Veldt Sensors]].
- Incident response has never been tested. The auditors accepted the blameless postmortem of the [[Cumulus Cloud Outage February 2026]] as evidence of the process, but asked for a tabletop exercise before August.

(Source: `sources/2026/05/006a32e9-a5eb-5b14-bbfb-1a5a8b15373f.md`)

## Why It Matters

[[Altamira Pharma Logistics]] will not sign a production contract without the SOC 2 report, and German distributors contacted by the Hamburg team ask for it as well. (Source: `sources/2026/05/006a32e9-a5eb-5b14-bbfb-1a5a8b15373f.md`)

## Failover

[[Rafael Duarte]] confirmed that the multi-region failover promised after the February outage is live in Frankfurt since 28 April. (Source: `sources/2026/05/006a32e9-a5eb-5b14-bbfb-1a5a8b15373f.md`)

## Entities

- [[Tidewell]] - company undergoing the audit
- [[Dmitri Volkov]] - security and reliability lead, owns the quarterly access reviews
- [[Rafael Duarte]] - CTO, confirmed the failover
- [[Carrow & Finch]] - audit firm Tidewell engaged
- [[Cumulus Cloud]] - critical supplier missing a vendor risk assessment
- [[Veldt Sensors]] - critical supplier missing a vendor risk assessment
- [[Altamira Pharma Logistics]] - customer that requires the report
- [[Cumulus Cloud Outage February 2026]] - postmortem accepted as evidence of incident response

## Connections

- [[Tidewell 2026 Kickoff]] - meeting where the SOC 2 audit was set as a 2026 priority
- [[Cumulus Cloud Outage February 2026]] - outage whose postmortem the auditors accepted
- [[Hamburg Office Plan April 2026]] - meeting where the German market and data residency were discussed
- [[Cumulus Cloud]] - hosting provider whose Frankfurt region now runs the failover
- [[Veldt Sensors]] - probe supplier named as a critical supplier
- [[Altamira Pharma Logistics]] - customer requiring the report
