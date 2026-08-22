// The gardener: corpus health on a nightly walk. Findings filter by severity and by check; the
// history strip shows each run's outcome; Run now dispatches the workflow (real model spend).

import { api } from "../api.js";
import { chartCard, hbars, runStrip, stackedColumns } from "../charts.js";
import { check as checkCopy, jobConsequence, severity as severityCopy } from "../copy.js";
import { getMeta, windowDays } from "../state.js";
import {
  banner, chips, el, fmtDay, fmtWhen, icon, kv, mono, pill, relTime, render, severityPill, table,
} from "../ui.js";
import { loading, runShape, runTable } from "./common.js";
import { dispatchFlow } from "./jobs.js";

const filter = { severity: "", check: "" };
const SEVERITY_TONE = { warn: "human", info: "code" };

export async function gardenerView(host) {
  await loading(host, async () => {
    const days = windowDays();
    const [data, metrics] = await Promise.all([api.get("gardener"), api.get(`metrics?days=${days}`)]);
    const severities = {};
    const byCheck = {};
    for (const f of data.findings) {
      severities[f.severity] = (severities[f.severity] || 0) + 1;
      byCheck[f.check] = (byCheck[f.check] || 0) + 1;
    }
    const filtered = data.findings.filter((f) =>
      (!filter.severity || f.severity === filter.severity) && (!filter.check || f.check === filter.check));
    const stats = (data.run && data.run.stats) || {};
    const passErrors = ["sweep", "empty_body", "duplicate_entity"].filter((k) => stats[k] && stats[k].error).map((k) => `${k.replaceAll("_", " ")}: ${stats[k].error}`);
    // the workflow row comes from the server's own table; no button when it is not listed there
    const gardenerWorkflow = (getMeta().workflows || []).find((w) => w.file === "gardener.yml");
    const history = metrics.job_history.gardener || [];
    const runs = history.map((r) => runShape(r, (run) => `${run.status}${(run.stats || {}).findings_total !== undefined ? ` · ${run.stats.findings_total} findings` : ""}`));
    const severityOrder = (getMeta().gardener_severities && getMeta().gardener_severities.length ? getMeta().gardener_severities : ["info", "warn"]).slice().reverse();
    render(host,
      el("div", { class: "grid two-one" },
        chartCard({
          title: `Findings per run, last ${days} days`, sub: "by severity — a rising line is a corpus drifting, a falling one somebody keeping up",
          chart: historyChart(history, severityOrder),
          tableSpec: { headers: ["run", "status", ...severityOrder], rows: history.map((r) => ({ cells: [fmtWhen(r.started_at), r.status, ...severityOrder.map((s) => String(((r.stats || {}).findings_by_severity || {})[s] || 0))] })) },
        }),
        el("section", { class: "card" },
          el("div", { class: "card-head" },
            el("div", { class: "card-title" }, el("h2", {}, "Latest completed run"),
              el("div", { class: "sub" }, data.run ? `#${data.run.id} · finished ${relTime(data.run.finished_at)}` : "no completed run yet")),
            el("div", { class: "spacer" }),
            gardenerWorkflow ? el("button", { class: "btn small primary", type: "button", disabled: !getMeta().github.configured,
              onclick: () => dispatchFlow(gardenerWorkflow, jobConsequence(gardenerWorkflow.file, gardenerWorkflow.title)) },
              icon("play", 14), "Run now") : null),
          !getMeta().github.configured ? el("div", { class: "sub" }, "Run now needs the GitHub token (Jobs page) — or run ", mono("stigmergy-gardener"), " from a terminal") : null,
          data.run ? kv([
            ["findings", el("div", { class: "row" }, ...Object.entries(severities).map(([s, n]) => el("span", { class: "row" }, severityPill(s), String(n))), !Object.keys(severities).length ? "none — a healthy corpus" : null)],
            ["pages walked", stats.pages !== undefined ? String(stats.pages) : (stats.sampled !== undefined ? `${stats.changed || 0} changed + ${stats.sampled} sampled` : null)],
            ["model spend", stats.cost_usd !== undefined ? `$${Number(stats.cost_usd).toFixed(3)}` : null],
          ]) : null,
          passErrors.length ? banner("warn", el("p", {}, el("strong", {}, "partial run"), " — the deterministic findings are complete and trustworthy; a model pass failed and produced zero findings this run:"), el("ul", { class: "names" }, passErrors.map((e) => el("li", {}, e)))) : null,
          el("div", { class: "hr" }),
          chartCard({ title: "Run history", sub: "height is duration, colour the outcome", chart: runStrip({ runs, height: 44 }), tableSpec: runTable(runs), cls: "tight" }))),
      el("div", { class: "grid halves" },
        chartCard({
          title: "Findings by check", sub: "what the latest run found most of — click a bar to filter the table",
          chart: hbars({ rows: Object.entries(byCheck).sort((a, b) => b[1] - a[1]).map(([c, n]) => ({ label: c, value: n, color: c.startsWith("model-") ? "model" : "code", sub: checkCopy(c) || "findings",
            onclick: () => { filter.check = filter.check === c ? "" : c; gardenerView(host); } })), labelWidth: 210 }),
          tableSpec: { headers: ["check", "findings", "meaning"], rows: Object.entries(byCheck).map(([c, n]) => ({ cells: [c, String(n), checkCopy(c)] })) },
        }),
        el("section", { class: "card" },
          el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "What the checks mean"), el("div", { class: "sub" }, "the deterministic checks first, then what the model passes judged; the gardener fixes nothing"))),
          table(["", "check", "looks for"],
            [...Object.keys(byCheck).filter((c) => !c.startsWith("model-")).sort().map((c) => ({ cells: [pill("code", "code", { small: true }), mono(c), checkCopy(c) || "—"] })),
             ...Object.keys(byCheck).filter((c) => c.startsWith("model-")).sort().map((c) => ({ cells: [pill("model", "model", { small: true }), mono(c), checkCopy(c) || "—"] }))],
            { dense: true, empty: "no findings to explain yet" }))),
      el("section", { class: "card" },
        el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, `${filtered.length} finding(s)`), el("div", { class: "sub" }, "each names a subject page or entity, what is off, and the suggested action"))),
        chips([{ key: "", label: "all severities", on: !filter.severity },
          ...severityOrder.map((s) => ({ key: s, label: severityCopy(s).label, count: severities[s] || 0, on: filter.severity === s, who: SEVERITY_TONE[s] }))],
        (key) => { filter.severity = key; gardenerView(host); }),
        filter.check ? el("div", { class: "row", style: { marginBottom: "10px" } }, el("span", { class: "sub" }, "check:"), pill(filter.check, "code"), el("button", { class: "btn small ghost", type: "button", onclick: () => { filter.check = ""; gardenerView(host); } }, "clear")) : null,
        table(["severity", "check", "subject", "what is off", "suggested action"],
          filtered.map((f) => ({
            cells: [severityPill(f.severity), el("span", { title: checkCopy(f.check) }, mono(f.check)), el("span", { class: "mono wrap" }, f.subject || "—"),
              el("span", { class: "wrap" }, f.detail), el("span", { class: "wrap" }, f.suggested_action || "—")],
          })), { empty: data.findings.length ? "nothing matches the filter" : "no findings — a healthy corpus" })),
    );
  });
}

function historyChart(history, severityOrder) {
  const rows = [...history].reverse().map((r) => {
    const by = (r.stats || {}).findings_by_severity || {};
    const values = Object.fromEntries(severityOrder.map((s) => [s, by[s] || 0]));
    if (!Object.values(values).some(Boolean) && (r.stats || {}).findings_total) values[severityOrder[severityOrder.length - 1]] = r.stats.findings_total;
    return { x: r.started_at, label: fmtDay((r.started_at || "").slice(0, 10)), values };
  });
  return stackedColumns({ series: severityOrder.map((s) => ({ key: s, label: severityCopy(s).label, color: SEVERITY_TONE[s] || "code" })), rows, height: 170 });
}
