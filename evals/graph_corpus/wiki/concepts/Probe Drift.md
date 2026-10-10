---
id: page_734d0117-3b5d-49b0-a05f-bf06f2177c13
type: concept
title: Probe Drift
status: developing
created: '2026-02-10'
updated: '2026-02-10'
acl: null
sources:
- sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md
---

# Probe Drift

## Definition

Probe drift is a slow measurement error that develops in a temperature probe over time in service, so that its readings deviate from the true temperature without an obvious failure. (Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

## How It Works

The source reports one traced cause: a faulty resin used in production batch VS-2207 of [[Veldt Sensors]] probes, which made them read about 1.4 degrees Celsius too warm after roughly nine months in service. Drift is found by comparing probes against reference thermometers. (Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

## Why It Matters

Drift is silent: a probe keeps reporting readings, so a fleet can carry a systematic temperature error for months. At [[Frostvale Distribution]] field engineers found it only by comparing probes against reference thermometers. The source does not report how widespread the resin fault is beyond batch VS-2207, nor a general drift rate. (Source: `sources/2026/02/789e0314-ea63-54a8-b7c2-dfa80f6d595f.md`)

## Examples

- Batch VS-2207: about 600 probes read about 1.4 degrees Celsius too warm after roughly nine months; 180 of them were in Frostvale trucks.

## Connections

- [[Veldt Sensors]] - supplier whose batch VS-2207 drifted
- [[Tidewell]] - operator whose fleet was affected
- [[Frostvale Distribution]] - customer where the drift was found
- [[Tidewell Sentinel]] - gateway that will gain drift detection
- [[Coldpath]] - dashboard that will flag disagreeing probe pairs
- [[Calibration Audit]] - practice used to detect drift
- [[Veldt Sensors Call February 2026]] - call where it was reported
