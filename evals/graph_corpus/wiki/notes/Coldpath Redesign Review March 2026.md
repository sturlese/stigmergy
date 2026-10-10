---
id: page_32003f93-e938-46b4-9c98-d3a9b98ea78f
type: note
title: Coldpath Redesign Review March 2026
status: developing
created: '2026-03-12'
updated: '2026-03-12'
acl: null
sources:
- sources/2026/03/6b6348b1-877b-5da1-bc95-849a8a3c6ede.md
---

# Coldpath Redesign Review March 2026

On 12 March 2026, [[Tidewell]] held a design review of the [[Coldpath]] redesign. Present were [[Joon-ho Park]] (product designer), [[Rafael Duarte]] and [[Aurélie Fontaine]]. (Source: `sources/2026/03/6b6348b1-877b-5da1-bc95-849a8a3c6ede.md`)

## Redesign

Joon-ho presented the redesigned Coldpath home screen with three changes:

- an [[Excursion Budget|excursion budget]] gauge for every active shipment, turning amber at 50 percent and red at 80 percent;
- an alert inbox grouped by trip, so a dispatcher sees one row per truck journey instead of dozens of separate alerts;
- quiet hours per site, during which planned loading does not raise alerts.

(Source: `sources/2026/03/6b6348b1-877b-5da1-bc95-849a8a3c6ede.md`)

## Usability Test

Joon-ho ran a usability test with six dispatchers, three from [[Frostvale Distribution]] and three from [[Casa Verde Markets]]. The time to find the one shipment that needed action dropped from 4 minutes 10 seconds on the old screen to 1 minute 50 seconds on the new one. Five of six dispatchers preferred the trip grouping. (Source: `sources/2026/03/6b6348b1-877b-5da1-bc95-849a8a3c6ede.md`)

## Decisions

- The redesign ships on 20 April 2026.
- [[Rafael Duarte]] confirmed that the excursion budget logic from [[Priya Raman]]'s memo will be live by then.
- [[Aurélie Fontaine]] asked for a weekly email summary for customers who do not open Coldpath every day; Joon-ho added it to the backlog.

(Source: `sources/2026/03/6b6348b1-877b-5da1-bc95-849a8a3c6ede.md`)

## Entities

- [[Tidewell]] - company whose dashboard was reviewed
- [[Coldpath]] - dashboard redesigned
- [[Joon-ho Park]] - product designer who presented the redesign and ran the usability test
- [[Rafael Duarte]] - confirmed the excursion budget logic will be live by the ship date
- [[Aurélie Fontaine]] - requested the weekly email summary
- [[Frostvale Distribution]] - three of its dispatchers took part in the usability test
- [[Casa Verde Markets]] - three of its dispatchers took part in the usability test
- [[Priya Raman]] - author of the excursion budget memo
- [[Excursion Budget]] - mechanism behind the gauge

## Connections

- [[Coldpath]] - the dashboard this review covers
- [[Excursion Budget]] - gauge shown in the redesign
- [[Excursion Budget Memo January 2026]] - memo whose logic Rafael confirmed would be live
- [[Dwell-Time Awareness]] - related alert-noise reduction; quiet hours suppress planned-loading alerts
- [[Frostvale Quarterly Business Review January 2026]] - meeting where the alert noise and a single view per trip were first requested
