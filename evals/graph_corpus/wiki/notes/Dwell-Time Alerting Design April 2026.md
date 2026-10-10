---
id: page_5a57e59c-721c-4577-8605-bfd1dd448492
type: note
title: Dwell-Time Alerting Design April 2026
status: developing
created: '2026-04-23'
updated: '2026-04-23'
acl: null
sources:
- sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md
---

# Dwell-Time Alerting Design April 2026

[[Priya Raman]], staff engineer responsible for the alerting pipeline at [[Tidewell]], wrote an engineering design document on 23 April 2026 for dwell-time alerting. (Source: `sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md`)

## Problem

Most alerts at [[Frostvale Distribution]] come from planned stops. When a truck is parked at a loading dock with its doors open, temperature rises and threshold alerts fire, although nothing is wrong. (Source: `sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md`)

## Design

Every site in [[Coldpath]] gets a dwell limit: the time a vehicle may stay at that site with doors open, for example 25 minutes at a supermarket dock. While a vehicle is within its dwell limit at a known site, temperature crossings count against the [[Excursion Budget|excursion budget]] but raise no alert. An alert fires only when the dwell limit is exceeded, when the door stays open away from a known site, or when the excursion budget reaches 80 percent. (Source: `sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md`)

The [[Tidewell Sentinel]] gateway already reports door sensor state and GPS position, so no hardware change is needed. (Source: `sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md`)

## Expected Result

Alert volume at Frostvale is expected to fall by about 45 percent without missing any excursion that consumes the budget. The source does not report a baseline, measurement procedure, or evaluation of the estimate. (Source: `sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md`)

## Rollout

The design rolls out to all customers on 15 May 2026, starting with Frostvale as promised in its renewal. (Source: `sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md`)

## Entities

- [[Priya Raman]] - author, staff engineer for the alerting pipeline
- [[Tidewell]] - company where the design was written
- [[Frostvale Distribution]] - customer whose alert noise motivates the design and first rollout
- [[Coldpath]] - dashboard where each site gets a dwell limit
- [[Tidewell Sentinel]] - gateway that already reports door state and GPS position

## Connections

- [[Dwell-Time Awareness]] - the capability this design implements
- [[Excursion Budget]] - crossings within a dwell limit count against it without alerting
- [[Frostvale Renewal Negotiation April 2026]] - renewal where Tidewell committed to ship dwell-time alerting by 15 May 2026
- [[Frostvale Quarterly Business Review January 2026]] - meeting where Gunnar Lindqvist requested dwell-time awareness
- [[Coldpath Redesign Review March 2026]] - review where quiet hours per site, a related planned-loading suppression, were presented
