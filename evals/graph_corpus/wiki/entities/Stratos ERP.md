---
id: page_f81b002b-9d79-4e1d-923d-907800780192
type: entity
title: Stratos ERP
status: developing
created: '2026-07-02'
updated: '2026-07-14'
acl: null
sources:
- sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md
- sources/2026/07/e17d5404-01e2-535c-8c1a-a03bf0d45a03.md
entity_type: product
aliases: []
---

# Stratos ERP

## What / Who

Stratos ERP is the enterprise system [[Nordhafen Tiefkühl]] uses for dispatch and shipment records. It is made by [[Stratos Software]]. (Source: `sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md`)

It is used by food distributors in Germany and the Netherlands, and [[Casa Verde Markets]] runs it in its Spanish warehouses. (Source: `sources/2026/07/e17d5404-01e2-535c-8c1a-a03bf0d45a03.md`)

## Facts

- 2026-07-02: [[Maren Vogt]] required that Tidewell alerts appear inside it; [[Sabine Krüger]] confirmed the [[Frostline API]] can push excursion events to it, and that [[Tidewell]] is talking to [[Stratos Software]] about a certified connector.

(Source: `sources/2026/07/c54967da-3d89-5c8d-b202-c1056f8f92e4.md`)

- 2026-07-14: under the agreement between [[Tidewell]] and [[Stratos Software]], a certified connector built on the [[Frostline API]] will attach excursion events and Good Distribution Practice reports to the matching shipment record in it.

(Source: `sources/2026/07/e17d5404-01e2-535c-8c1a-a03bf0d45a03.md`)

## Connections

- [[Nordhafen Tiefkühl]] - organization that uses it
- [[Stratos Software]] - maker
- [[Frostline API]] - API that can push excursion events to it
- [[Tidewell]] - supplier seeking a certified connector
- [[Nordhafen Tiefkühl Pilot Kickoff]] - kickoff where the integration was discussed
- [[Casa Verde Markets]] - customer that runs it in its Spanish warehouses
- [[Stratos Software Partnership Call July 2026]] - call where the connector agreement was reached
