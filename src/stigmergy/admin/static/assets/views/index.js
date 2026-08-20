// The index: freshness, pages per zone, the ops files this stack serves, the substrate check, and
// the incremental webhook's recent upserts.

import { api } from "../api.js";
import { chartCard, fillDays, hbars, stackedColumns } from "../charts.js";
import { jobConsequence } from "../copy.js";
import { getMeta, windowDays } from "../state.js";
import { agoFrom, banner, el, fmtAge, fmtDay, fmtWhen, icon, mono, pill, render, severityPill, skeletons, table, tile, wordPill } from "../ui.js";
import { loading } from "./common.js";
import { dispatchFlow } from "./jobs.js";

const OPS_FILE = {
  "ops/entity-registry.json": { label: "Entity registry", what: "the vocabulary captures anchor to — who can be named" },
  "ops/identities.json": { label: "Identity roster", what: "who can READ what — each identity's audiences" },
  "ops/slack-channels.json": { label: "Slack channel map", what: "which channel sees which audience's answers" },
};

export async function indexView(host) {
  await loading(host, async () => {
    const days = windowDays();
    const [data, metrics] = await Promise.all([api.get("index"), api.get(`metrics?days=${days}`)]);
    const meta = data.meta;
    const checkHost = el("div", {});
    const zones = Object.entries(data.zones);
    const pages = zones.reduce((a, [, n]) => a + n, 0);
    // the workflow row comes from the server's own table; no button when it is not listed there
    const indexRebuildWorkflow = (getMeta().workflows || []).find((w) => w.file === "index-rebuild.yml");
    const webhook = metrics.job_history["webhook-index-upsert"] || [];
    const perDay = fillDays(Object.values(webhook.reduce((acc, r) => {
      const day = (r.started_at || "").slice(0, 10);
      acc[day] = acc[day] || { day, upserts: 0, errors: 0 };
      if (r.status === "ok") acc[day].upserts += 1; else acc[day].errors += 1;
      return acc;
    }, {})), days, { upserts: 0, errors: 0 }).map((r) => ({ x: r.day, label: fmtDay(r.day), values: r }));
    render(host,
      el("div", { class: "grid tiles" },
        tile("Index built", meta && meta.built_at ? `${fmtAge(agoFrom(meta.built_at))} ago` : "never",
          meta ? `${fmtWhen(meta.built_at)} · ${meta.host || "embedding host unrecorded"}` : "no index yet — nothing can be answered until one is built: Rebuild now, or make rebuild-staging from a terminal",
          { tone: meta && agoFrom(meta.built_at) > 2 * 86400000 ? "warn" : "" }),
        tile("Pages indexed", String(pages), zones.map(([z, n]) => `${z} ${n}`).join(" · ") || "no pages yet"),
        tile("Embedding model", meta ? meta.model : "—", meta ? `${meta.dim} dimensions · FTS ${meta.fts_config}` : null),
        ...Object.entries(data.ops_files || {}).map(([relpath, snap]) => {
          const copy = OPS_FILE[relpath] || { label: relpath, what: "" };
          return tile(copy.label, snap ? `${fmtAge(agoFrom(snap.refreshed_at))} ago` : "no snapshot",
            snap ? `from ${snap.source} · ${copy.what}` : `readers fall back to their own baked file · ${copy.what}`,
            { tone: snap ? "" : "warn" });
        })),
      el("div", { class: "grid halves" },
        chartCard({
          title: "Pages per zone", sub: "sources are the machine's, wiki is the team's, views are derived rollups",
          chart: hbars({ rows: zones.map(([z, n]) => ({ label: `${z}/`, value: n, color: z === "wiki" ? "human" : z === "views" ? "model" : "code" })), labelWidth: 90 }),
          tableSpec: { headers: ["zone", "pages"], rows: zones.map(([z, n]) => ({ cells: [z, String(n)] })) },
        }),
        chartCard({
          title: `Incremental upserts, last ${days} days`, sub: "the push webhook keeps the index current between nightly rebuilds",
          chart: stackedColumns({ series: [{ key: "upserts", label: "upserts", color: "git" }, { key: "errors", label: "errors", color: "fail" }], rows: perDay, height: 150 }),
          tableSpec: { headers: ["day", "upserts", "errors"], rows: perDay.filter((r) => r.values.upserts || r.values.errors).map((r) => ({ cells: [r.label, String(r.values.upserts), String(r.values.errors)] })) },
        })),
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" }, el("h2", {}, "Substrate check"),
            el("div", { class: "sub" }, "lints the LIVE index in-process: duplicate page ids, orphan continuation parts, arm-invisible pages, dangling supersessions, unregistered anchors — against the registry copy this server serves. Run it after registry changes.")),
          el("div", { class: "spacer" }),
          el("button", { class: "btn small", type: "button", onclick: () => checkFlow(checkHost) }, icon("shield", 14), "Run the check"),
          indexRebuildWorkflow ? el("button", { class: "btn small primary", type: "button", disabled: !getMeta().github.configured,
            onclick: () => dispatchFlow(indexRebuildWorkflow, jobConsequence(indexRebuildWorkflow.file, indexRebuildWorkflow.title)) }, icon("play", 14), "Rebuild now") : null),
        !getMeta().github.configured ? el("div", { class: "sub" }, "Rebuild now needs the GitHub token (Jobs page) — or ", mono("make rebuild-staging"), " from a terminal") : null,
        checkHost),
      el("section", { class: "card" },
        el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Recent webhook deliveries"), el("div", { class: "sub" }, "each push to the knowledge repo upserts the pages it changed"))),
        table(["when", "outcome", "what", "error"], data.webhook.map((r) => ({
          cells: [fmtWhen(r.started_at), wordPill(r.status), statsLine(r.stats), r.error ? el("span", { class: "diff-del" }, r.error) : "—"],
        })), { dense: true, empty: "no webhook deliveries recorded — pushes to the knowledge repo land here" })),
    );
  });
}

function statsLine(stats) {
  if (!stats || typeof stats !== "object") return "—";
  return el("span", { class: "row" }, ...Object.entries(stats).filter(([, v]) => typeof v !== "object").slice(0, 5).map(([k, v]) => el("span", { class: "entity-chip" }, el("span", { class: "type" }, k.replaceAll("_", " ")), String(v))));
}

async function checkFlow(checkHost) {
  render(checkHost, skeletons(1));
  try {
    const result = await api.post("index/check");
    render(checkHost,
      el("div", { style: { marginTop: "12px" } },
        el("div", { class: "row", style: { marginBottom: "10px" } },
          pill(result.errors ? `${result.errors} error(s)` : "no errors", result.errors ? "fail" : "git"),
          pill(`${result.warnings} warning(s)`, result.warnings ? "human" : "neutral")),
        table(["severity", "check", "detail"],
          result.findings.map((f) => ({ cells: [severityPill(f.severity), mono(f.check), el("span", { class: "wrap" }, f.detail)] })),
          { empty: "a clean substrate — no findings at all" })));
  } catch (ex) {
    render(checkHost, banner("error", ex.message));
  }
}
