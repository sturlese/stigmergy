---
id: page_cc92de1c-dcc9-4364-a16a-8786b2322576
type: concept
title: Dwell-Time Awareness
status: developing
created: '2026-01-15'
updated: '2026-06-10'
acl: null
sources:
- sources/2026/01/5649a227-256b-5fc1-9ddd-551c0b8b3de4.md
- sources/2026/01/3e586328-f506-5919-af51-2773aa286315.md
- sources/2026/03/6b6348b1-877b-5da1-bc95-849a8a3c6ede.md
- sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md
- sources/2026/06/c53e64d7-7552-5fe9-a458-e2f48f6820b8.md
---

# Dwell-Time Awareness

## Definition

Dwell-time awareness is an alerting capability that accounts for planned stops, so that a temperature alert caused by a door opening during a scheduled loading-dock stop is not raised as an anomaly. (Source: `sources/2026/01/5649a227-256b-5fc1-9ddd-551c0b8b3de4.md`)

## How It Works

The source does not describe an implementation. As requested by [[Gunnar Lindqvist]], alerts would "understand planned stops at loading docks," distinguishing a door opening during a planned delivery from an unexpected one. (Source: `sources/2026/01/5649a227-256b-5fc1-9ddd-551c0b8b3de4.md`)

In a design document of 23 April 2026, [[Priya Raman]] specified the implementation: every site in [[Coldpath]] gets a dwell limit, the time a vehicle may stay at that site with doors open, for example 25 minutes at a supermarket dock. While a vehicle is within its dwell limit at a known site, temperature crossings count against the [[Excursion Budget|excursion budget]] but raise no alert. An alert fires only when the dwell limit is exceeded, when the door stays open away from a known site, or when the excursion budget reaches 80 percent. The [[Tidewell Sentinel]] gateway already reports door sensor state and GPS position, so no hardware change is needed. (Source: `sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md`)

## Why It Matters

At [[Frostvale Distribution]], most December temperature alerts resolved on their own within ten minutes, usually because a door was opened during a planned delivery, and dispatchers acknowledged only 22 percent of alerts. Dwell-time awareness is one of two changes Gunnar requested to cut alert noise, the other being a single view per trip instead of a flat list of alerts. The source does not report a design, owner, or delivery date. (Source: `sources/2026/01/5649a227-256b-5fc1-9ddd-551c0b8b3de4.md`)

The April 2026 design expects alert volume at Frostvale to fall by about 45 percent without missing any excursion that consumes the budget, and rolls out to all customers on 15 May 2026. (Source: `sources/2026/04/ce9fdf9b-593d-5421-ac1a-3685218be795.md`)

The first full month of results at [[Frostvale Distribution]], reported on 10 June 2026, showed alerts falling from about 1,900 to 930 a month, a 51 percent reduction, better than the 45 percent the design predicted, and the acknowledged-alert ratio rising from 22 percent to 64 percent, with no budget-exhausting excursion left unalerted. (Source: `sources/2026/06/c53e64d7-7552-5fe9-a458-e2f48f6820b8.md`)

## Connections

- [[Coldpath]] - dashboard where such alerts would appear and where dwell limits are configured
- [[Frostvale Distribution]] - customer that requested it
- [[Gunnar Lindqvist]] - requested it
- [[Frostvale Quarterly Business Review January 2026]] - meeting where it was requested
- [[Excursion Budget]] - alternative alert-noise reduction approach proposed for Tidewell
- [[Coldpath Redesign Review March 2026]] - review where quiet hours per site, a related way to suppress planned-loading alerts, were presented
- [[Dwell-Time Alerting Design April 2026]] - design document specifying the dwell-limit implementation
- [[Priya Raman]] - author of the design
- [[Tidewell Sentinel]] - gateway that reports door state and GPS position
- [[Frostvale Renewal Negotiation April 2026]] - renewal where Tidewell committed to ship it by 15 May 2026
- [[Frostvale Dwell-Time Results June 2026]] - review reporting its first-month results
