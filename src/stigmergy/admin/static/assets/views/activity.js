// Activity: who is using the brain, how, and how well it answers — audit aggregates, the answer
// shape, latency per tool, the questions people asked, rate-limit refusals, the console's own log.

import { api } from "../api.js";
import { chartCard, fillDays, hbars, partToWhole, stackedColumns } from "../charts.js";
import { door, itemKind } from "../copy.js";
import { windowDays } from "../state.js";
import { banner, el, fmtDay, fmtMs, fmtNum, fmtPct, fmtWhen, mono, render, table, tile, wordPill } from "../ui.js";
import { loading } from "./common.js";

const TOOL_SLOT = ["s1", "s2", "s3", "s4", "s5", "s6"];

export async function activityView(host) {
  await loading(host, async () => {
    const days = windowDays();
    const [data, metrics] = await Promise.all([api.get("activity"), api.get(`metrics?days=${days}`)]);
    const shape = data.report.answer_shape;
    const filed = data.report.capture_to_filed_latency;
    const searchable = data.report.capture_to_searchable_latency;
    const latencySub = (s) => s.enough_data ? `p50 ${fmtMs(s.p50_ms)} · p95 ${fmtMs(s.p95_ms)} · ${s.samples} samples` : `${s.samples} sample(s) — ${s.min_samples} needed before p50/p95 mean anything`;
    const tools = metrics.calls_by_tool.map((t) => t.tool);
    const top = tools.slice(0, 6);
    const series = top.map((tool, i) => ({ key: tool, label: tool, color: TOOL_SLOT[i] }));
    if (tools.length > 6) series.push({ key: "other", label: "other", color: "other" });
    const byDay = {};
    for (const r of metrics.calls_by_day) {
      const key = top.includes(r.tool) ? r.tool : "other";
      byDay[r.day] = byDay[r.day] || { day: r.day };
      byDay[r.day][key] = (byDay[r.day][key] || 0) + r.calls;
    }
    const rows = fillDays(Object.values(byDay), days, {}).map((r) => ({ x: r.day, label: fmtDay(r.day), values: r }));
    const calls = metrics.calls_by_tool.reduce((a, t) => a + t.calls, 0);
    render(host,
      el("div", { class: "grid tiles" },
        tile("Calls", fmtNum(calls), `in ${days} days · ${metrics.calls_by_identity.length} identities`, { who: "code" }),
        tile("Questions asked", String(shape.total), "successful ask calls with a recorded shape, all time", { who: "human" }),
        tile("Answered with a citation", fmtPct(shape.answered_with_citation_pct), `${shape.answered_with_citation} of ${shape.total}`, { who: "git" }),
        tile("Honest refusals", fmtPct(shape.refused_pct), "a system that never refuses is the failure, not the success", { who: "code" }),
        tile("Capture → searchable", searchable.enough_data ? fmtMs(searchable.p50_ms) : "—", latencySub(searchable), { who: "git" }),
        tile("Capture → filed", filed.enough_data ? fmtMs(filed.p50_ms) : "—", latencySub(filed), { who: "git" })),
      el("div", { class: "grid halves" },
        chartCard({
          title: `Calls per day, last ${days} days`, sub: "by tool — reads, writes and the review lane",
          chart: stackedColumns({ series, rows, height: 190 }),
          tableSpec: { headers: ["day", ...series.map((s) => s.label)], rows: rows.filter((r) => series.some((s) => r.values[s.key])).map((r) => ({ cells: [r.label, ...series.map((s) => String(r.values[s.key] || 0))] })) },
        }),
        chartCard({
          title: "Answer shape, all time", sub: "from the verifier's verdict — never the question or the answer text",
          chart: partToWhole({ segments: [
            { key: "cited", label: "answered with a citation", value: shape.answered_with_citation, color: "git" },
            { key: "uncited", label: "answered without one", value: shape.answered_no_citation, color: "human" },
            { key: "refused", label: "honest refusal", value: shape.refused, color: "code" },
          ] }),
        })),
      el("div", { class: "grid halves" },
        chartCard({
          title: "Who asks", sub: `calls per identity in ${days} days`,
          chart: hbars({ rows: metrics.calls_by_identity.slice(0, 12).map((r) => ({ label: r.identity, value: r.calls, sub: `${r.asks} asks · ${r.submits} captures` })), labelWidth: 200 }),
          tableSpec: { headers: ["identity", "calls", "asks", "captures", "rate-limited", "last"], rows: metrics.calls_by_identity.map((r) => ({ cells: [r.identity, String(r.calls), String(r.asks), String(r.submits), String(r.rate_limited), fmtWhen(r.last_at)] })) },
        }),
        el("section", { class: "card" },
          el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Per tool"), el("div", { class: "sub" }, `calls, errors and latency in ${days} days`))),
          table(["tool", { text: "calls", cls: "num" }, { text: "errors", cls: "num" }, { text: "p50", cls: "num" }, { text: "p95", cls: "num" }, "last"],
            metrics.calls_by_tool.map((t) => ({ cells: [mono(t.tool), fmtNum(t.calls), String(t.errors), fmtMs(t.p50_ms), fmtMs(t.p95_ms), fmtWhen(t.last_at)] })),
            { dense: true, empty: "no audit rows in this window", emptyHint: "widen the window, or the brain has not been asked anything yet" }))),
      el("div", { class: "grid halves" },
        el("section", { class: "card" },
          el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Questions people asked"), el("div", { class: "sub" }, "the golden-set quarry — user content behind this console's one credential; no answers, no pages"))),
          data.ask_questions.length
            ? el("ul", { class: "names" }, data.ask_questions.map((q) => el("li", {}, q)))
            : el("div", { class: "empty" }, el("div", { class: "empty-title" }, "no successful ask calls recorded yet"), el("div", { class: "empty-hint" }, "the first question answered over MCP or Slack shows up here"))),
        el("div", {},
          el("section", { class: "card" },
            el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Rate-limit refusals"), el("div", { class: "sub" }, "the budgets protect spend behind a public url"))),
            table(["when", "identity", "tool"], data.rate_limited.map((r) => ({ cells: [fmtWhen(r.ts), r.identity, mono(r.tool)] })), { dense: true, empty: "no rate-limit trips recorded", emptyHint: "a trip would mean somebody hit a per-identity budget — nobody has" })),
          el("section", { class: "card" },
            el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Governance decisions"), el("div", { class: "sub" }, "the latest verdict per item, whichever door took it"))),
            table(["when", "item", "verdict", "by", "door"], metrics.decisions.slice(0, 15).map((d) => ({
              cells: [fmtWhen(d.created_at), `${itemKind(d.kind).label} #${d.id}`, wordPill(d.verdict), d.actor, door(d.source)],
            })), { dense: true, empty: "no decisions recorded yet", emptyHint: "the first approve or decline on an identity or a repair lands here" })))),
      el("section", { class: "card" },
        el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Console actions"), el("div", { class: "sub" }, "this console's own ledger — every attempted mutation, succeeded or not"))),
        table(["when", "actor", "action", "arguments", "outcome"], (data.admin_actions || []).map((r) => ({
          cells: [fmtWhen(r.ts), r.actor, mono(r.action), argsCell(r.args), el("span", { class: "row" }, wordPill(r.outcome), r.error_class ? mono(r.error_class) : null)],
        })), { dense: true, empty: "no console actions yet", emptyHint: "every requeue, mint, approve, dispatch or post from this console lands here, succeeded or not" })),
      banner("info", "every number here comes from a column something else already wrote — audit rows and job rows; no new measurement channel."),
    );
  });
}

// The action's arguments, as short chips — ids and names, never a JSON dump: a decline reason
// or a resolve note is in there verbatim, and a table cell is not where a steward reads it.
function argsCell(args) {
  const entries = Object.entries(args || {}).filter(([, v]) => typeof v !== "object");
  if (!entries.length) return el("span", { class: "muted" }, "—");
  return el("span", { class: "row" }, ...entries.map(([k, v]) => el("span", { class: "entity-chip", title: `${k}: ${String(v)}` },
    el("span", { class: "type" }, k), String(v).length > 40 ? `${String(v).slice(0, 39)}…` : String(v))));
}
