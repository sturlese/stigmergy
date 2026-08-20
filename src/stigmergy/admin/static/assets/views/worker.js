// The worker: `stigmergy-librarian status` live — depth, the item in flight with its lease, and
// the measured capture→filed pace. Read-only; draining and Fly scaling stay in the terminal.

import { api } from "../api.js";
import { chartCard, fillDays, histogram, meter, stackedColumns } from "../charts.js";
import { windowDays } from "../state.js";
import { banner, el, fmtDay, fmtMs, fmtWhen, link, mono, pill, render, table, tile } from "../ui.js";
import { loading } from "./common.js";

export async function workerView(host) {
  await loading(host, async () => {
    const days = windowDays();
    const [data, overview, metrics] = await Promise.all([api.get("worker"), api.get("overview"), api.get(`metrics?days=${days}`)]);
    const lat = data.latency;
    const throughput = fillDays(metrics.captures_by_day, days, { filed: 0, rejected: 0, failed: 0 }).map((r) => ({ x: r.day, label: fmtDay(r.day), values: { filed: r.filed || 0, rejected: r.rejected || 0, failed: r.failed || 0 } }));
    render(host,
      el("div", { class: "grid tiles" },
        tile("Queued", String(data.counts.queued || 0), "waiting for the librarian", { who: "code" }),
        tile("In flight", String(data.in_flight.length), data.in_flight.length ? "claimed — a worker holds the lease" : "idle", { who: "model" }),
        tile("Lease", `${data.visibility_timeout_s}s`, "derived from the agent budget + gates + headroom; Reclaim uses this number too", { who: "code" }),
        tile("Deliveries before failing", String(data.max_attempts), "a poison item burns one per claim", { who: "code" }),
        tile("Capture → filed p50", lat.enough_data ? fmtMs(lat.p50_ms) : "—", lat.enough_data ? `p95 ${fmtMs(lat.p95_ms)} · ${lat.samples} filings` : `${lat.samples} of ${lat.min_samples} samples needed`, { who: "git" })),
      data.in_flight.length
        ? el("div", { class: "grid halves" }, ...data.in_flight.map((row) => inFlightCard(row, data)))
        : el("section", { class: "card" }, el("div", { class: "empty" }, el("div", { class: "empty-title" }, "nothing in flight"), el("div", { class: "empty-hint" }, "the worker is idle; the next capture to arrive is claimed within its poll interval"))),
      el("div", { class: "grid halves" },
        chartCard({
          title: `What the librarian finished, last ${days} days`, sub: "by the day the capture arrived",
          chart: stackedColumns({ series: [{ key: "filed", label: "filed", color: "git" }, { key: "rejected", label: "refused", color: "code" }, { key: "failed", label: "failed", color: "fail" }], rows: throughput, height: 160 }),
          tableSpec: { headers: ["day", "filed", "refused", "failed"], rows: throughput.filter((r) => Object.values(r.values).some(Boolean)).map((r) => ({ cells: [r.label, String(r.values.filed), String(r.values.rejected), String(r.values.failed)] })) },
        }),
        chartCard({
          title: "Capture → filed, distribution", sub: "seconds from arrival to the commit, most recent filings",
          chart: histogram({ samples: metrics.filed_latency_ms, color: "git" }),
          tableSpec: { headers: ["sample", "seconds"], rows: metrics.filed_latency_ms.slice(0, 60).map((ms, i) => ({ cells: [`#${i + 1}`, (ms / 1000).toFixed(1)] })) },
        })),
      el("section", { class: "card" },
        el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Unresolved ingest errors"), el("div", { class: "sub" }, "captures that burned every delivery — the librarian could not finish them"))),
        table(["capture", "stage", "error", "attempts", "last seen"], overview.ingest_errors.rows.map((r) => ({
          cells: [link(`captures/${r.source_doc_id}`, `#${r.source_doc_id}`), r.stage, el("span", { class: "wrap" }, r.error), String(r.attempts), fmtWhen(r.last_at)],
        })), { dense: true, empty: "none — every capture the librarian claimed, it finished" })),
      banner("info", "draining, walking and Fly scaling stay in the terminal on purpose — this page is the ", mono("stigmergy-librarian status"), " read, live."),
    );
  });
}

function inFlightCard(row, data) {
  const ratio = Math.min(1, (row.claimed_age_ms || 0) / (data.visibility_timeout_s * 1000));
  return el("section", { class: "card" },
    el("div", { class: "card-head" },
      el("div", { class: "card-title" }, el("h2", {}, link(`captures/${row.id}`, `Capture #${row.id}`), " ", el("span", { class: "sub" }, `${row.kind} · sent by ${row.submitted_by}`))),
      el("div", { class: "spacer" }),
      pill(row.lease_expired ? "lease expired" : "within lease", row.lease_expired ? "fail" : "git")),
    el("div", { class: "sub" }, `delivery ${row.attempts} of ${data.max_attempts} · held ${fmtMs(row.claimed_age_ms)} of a ${data.visibility_timeout_s}s lease`),
    el("div", { style: { margin: "8px 0" } }, meter({ value: row.claimed_age_ms || 0, max: data.visibility_timeout_s * 1000, tone: row.lease_expired ? "fail" : "model", label: "lease" })),
    el("div", { class: "quote-label" }, "what this means"),
    el("div", { class: "sub" }, row.verdict));
}
