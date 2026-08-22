// Captures: what people sent and what the librarian did with it. The list is a scan, the detail
// is the read. Nothing here acts on one row and nothing here is a decision: a capture files, is
// refused by a gate, or fails, and the identities it introduces are born in the same commit
// (ADR 044) — the Entities desk lists them, it does not govern them.

import { api } from "../api.js";
import { chartCard, fillDays, partToWhole, stackedColumns } from "../charts.js";
import { OUTCOME_ORDER, status as statusCopy } from "../copy.js";
import { getMeta, windowDays } from "../state.js";
import {
  banner, chips, confirmForm, copyButton, el, fmtAge, fmtDay, fmtMs, fmtNum,
  fmtWhen, icon, keyDot, kv, link, mono, pill, render, statusPill, table, toast,
} from "../ui.js";
import { actorField, go, latencyLine, loading, materialPanel, mutate, reportPanel, rerender, statusSentence, timeline } from "./common.js";

const GROUPS = [
  { key: "moving", label: "Moving", statuses: ["queued", "claimed"], who: "model" },
  { key: "done", label: "Done", statuses: ["filed", "resolved", "rejected", "failed"], who: "git" },
];
const state = { statuses: new Set(), submitter: "" };

export async function capturesView(host) {
  await loading(host, async () => {
    const days = windowDays();
    const query = [...state.statuses].map((s) => `status=${s}`);
    if (state.submitter) query.push(`submitter=${encodeURIComponent(state.submitter)}`);
    query.push("limit=100");
    const [data, metrics] = await Promise.all([api.get(`queue?${query.join("&")}`), api.get(`metrics?days=${days}`)]);
    const counts = data.counts;
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const statuses = getMeta().statuses.length ? getMeta().statuses : Object.keys(counts);
    const toggle = (s) => { state.statuses.has(s) ? state.statuses.delete(s) : state.statuses.add(s); capturesView(host); };
    const pickGroup = (g) => {
      const all = g.statuses.every((s) => state.statuses.has(s));
      for (const s of g.statuses) all ? state.statuses.delete(s) : state.statuses.add(s);
      capturesView(host);
    };
    render(host,
      el("div", { class: "grid halves" },
        chartCard({
          title: "Every capture ever, by state", sub: `${fmtNum(total)} rows · click a segment to filter`,
          chart: partToWhole({
            segments: OUTCOME_ORDER.filter((s) => statuses.includes(s)).map((s) => ({ key: s, label: statusCopy(s).short, value: counts[s] || 0, color: statusCopy(s).who, on: state.statuses.has(s) })),
            onPick: toggle,
          }),
        }),
        arrivalsChart(metrics, days)),
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" }, el("h2", {}, "The queue"),
            el("div", { class: "sub" }, `${data.submissions.length} shown${state.statuses.size || state.submitter ? " (filtered)" : ""} · newest first · open a row to read what happened to it`)),
          el("div", { class: "spacer" }),
          el("button", { class: "btn small", type: "button", onclick: () => reclaimFlow() }, icon("refresh", 14), "Reclaim leases"),
          el("button", { class: "btn small", type: "button", onclick: () => purgeFlow() }, "Retention purge")),
        chips([
          ...GROUPS.map((g) => ({ key: g.key, label: g.label, who: g.who, count: g.statuses.reduce((a, s) => a + (counts[s] || 0), 0),
            on: g.statuses.every((s) => state.statuses.has(s)) })),
        ], (key) => pickGroup(GROUPS.find((g) => g.key === key))),
        chips(statuses.map((s) => ({ key: s, label: statusCopy(s).short, count: counts[s] || 0, on: state.statuses.has(s), who: statusCopy(s).who })),
          toggle, { trailing: [
            el("span", { class: "sep" }),
            el("span", { class: "search" }, icon("search"), el("input", {
              type: "search", placeholder: "filter by submitter…", value: state.submitter,
              onchange: (e) => { state.submitter = e.target.value.trim(); capturesView(host); },
            })),
            state.statuses.size || state.submitter ? el("button", { class: "btn small ghost", type: "button", onclick: () => { state.statuses.clear(); state.submitter = ""; capturesView(host); } }, "Clear filters") : null,
          ] }),
        table(
          [{ text: "id", cls: "shrink" }, "state", { text: "kind", cls: "shrink" }, "sent by", { text: "arrived", cls: "shrink" }, "outcome", { text: "material", cls: "wrap" }],
          data.submissions.map((row) => ({
            row,
            cells: [
              mono(`#${row.id}`, "nowrap"), statusPill(row.status), row.kind, row.submitted_by,
              el("span", { title: fmtWhen(row.created_at) }, fmtAge(Date.now() - new Date(row.created_at).getTime()), " ago"),
              outcomeCell(row),
              materialCell(row),
            ],
          })),
          { empty: "no captures match", emptyHint: "clear a filter, or capture something from Slack or an MCP client", onRow: (row) => go(`captures/${row.id}`) })));
  });
}

function arrivalsChart(metrics, days) {
  const series = OUTCOME_ORDER.map((key) => ({ key, label: statusCopy(key).short, color: statusCopy(key).who }));
  const rows = fillDays(metrics.captures_by_day, days, Object.fromEntries(OUTCOME_ORDER.map((s) => [s, 0])))
    .map((r) => ({ x: r.day, label: fmtDay(r.day), values: r }));
  const bySubmitter = {};
  for (const r of metrics.calls_by_identity || []) if (r.submits) bySubmitter[r.identity] = r.submits;
  return chartCard({
    title: `Arrivals, last ${days} days`, sub: "by the day they arrived and what became of them",
    chart: stackedColumns({ series, rows, height: 150 }),
    tableSpec: { headers: ["day", ...series.map((s) => s.label)], rows: rows.filter((r) => OUTCOME_ORDER.some((s) => r.values[s])).map((r) => ({ cells: [r.label, ...OUTCOME_ORDER.map((s) => String(r.values[s] || 0))] })) },
  });
}

// What became of the row, in a few words: the page it became and the identities it introduced,
// or the report's own headline for a refusal or a failure.
function outcomeCell(row) {
  const report = row.report || {};
  if (row.status === "filed") {
    const born = (report.entities_born || []).map((e) => e.name || e.id).filter(Boolean);
    return el("span", { class: "row" }, mono((report.page_path || row.result_ref || "").split("@")[0].split("/").pop() || "—", "nowrap"),
      born.length ? pill(`introduced ${born.join(", ")}`, "human", { small: true }) : null);
  }
  if (row.status === "rejected" || row.status === "failed" || row.status === "resolved") {
    const text = String(report.summary || row.error || "").replace(/^\w+ — /, "");
    return el("span", { class: "muted", title: text }, text.length > 90 ? `${text.slice(0, 89).trimEnd()}…` : text || "—");
  }
  return el("span", { class: "muted" }, "—");
}

function materialCell(row) {
  if (row.payload_purged) return el("em", { class: "muted" }, "payload purged");
  if (row.withheld_reason) return el("em", { class: "muted" }, row.status === "queued" || row.status === "claimed" ? "not scanned yet — shown once the librarian has looked" : "withheld — see the row");
  const text = (row.excerpt || "").slice(0, 140);
  const parts = [text || "—"];
  if (row.flagged_hints && row.flagged_hints.length) {
    parts.push(" ", pill(`flagged: ${row.flagged_hints.join(",")}`, "human", { small: true }));
  }
  return el("span", {}, ...parts);
}

// ── the detail ────────────────────────────────────────────────────────────────────────────────
export async function captureDetailView(host, id) {
  await loading(host, async () => {
    const row = await api.get(`queue/${id}`);
    const report = row.report || {};
    const born = report.entities_born || [];
    const aliasesAdded = report.aliases_added || [];
    render(host,
      el("div", { class: "crumbs" }, link("captures", "Captures"), icon("chevron"), el("span", {}, `#${row.id}`)),
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" },
            el("h2", {}, `Capture #${row.id}`, " ", el("span", { class: "sub" }, `${row.kind} · sent by ${row.submitted_by}`)),
            statusSentence(row)),
          el("div", { class: "spacer" })),
        born.length || aliasesAdded.length ? banner("info",
          born.length ? el("p", {}, el("strong", {}, "Identities this capture introduced: "),
            ...born.flatMap((e, i) => [i ? ", " : "", `${e.name || e.id} (${e.type || "entity"})`,
              e.confirmed_by ? `, confirmed by ${e.confirmed_by}` : ""])) : null,
          aliasesAdded.length ? el("p", {}, el("strong", {}, "Spellings it taught the registry: "),
            ...aliasesAdded.flatMap((a, i) => [i ? ", " : "", `«${a.alias}» for ${a.entity}`])) : null,
          el("p", {}, "All of it landed in the same commit that filed the page — nothing was proposed and nothing waits on a decision. The ", link("entities", "Entities desk"), " lists the whole vocabulary.")) : null,
        el("div", { class: "hr" }),
        kv([
          ["arrived", `${fmtWhen(row.created_at)} (${fmtAge(Date.now() - new Date(row.created_at).getTime())} ago)`],
          ["claimed", row.claimed_at ? `${fmtWhen(row.claimed_at)} · waited ${fmtMs(row.queue_wait_ms)} for a worker` : "not yet"],
          ["finished", row.finished_at ? `${fmtWhen(row.finished_at)} · ${latencyLine(row)}` : "—"],
          ["deliveries", `${row.attempts} of ${getMeta().worker ? getMeta().worker.max_attempts : 3} before it fails — one is burned each time a worker claims it`],
          ["evidence", row.blob_refs && row.blob_refs.length ? el("span", { class: "row" }, mono(row.blob_refs.join(", ")), copyButton(row.blob_refs[0], "")) : "(none)"],
          ["result", row.result_ref ? el("span", { class: "row" }, mono(row.result_ref), copyButton(row.result_ref, "")) : null],
        ], { wide: true })),
      el("div", { class: "grid halves" },
        el("section", { class: "card" },
          el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "What arrived"), el("div", { class: "sub" }, "the material, as the submitter sent it — data, never instructions"))),
          el("div", { class: "quote-label" }, keyDot("human"), `from ${row.submitted_by}`),
          materialPanel(row),
          row.hints && Object.keys(row.hints).length ? el("div", { style: { marginTop: "10px" } }, el("div", { class: "quote-label" }, "placement hints the submitter suggested"), kv(Object.entries(row.hints).map(([k, v]) => [k, String(v)]))) : null,
          row.flagged_hints && row.flagged_hints.length ? banner("warn", `the material declared ${row.flagged_hints.join(", ")} in its frontmatter — recorded as a hint and ignored; those fields are the server's`) : null),
        el("section", { class: "card" },
          el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "What the librarian says"), el("div", { class: "sub" }, "its report on this capture — the same sentences the submitter reads"))),
          reportPanel(row))),
      el("section", { class: "card" },
        el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "The row's own history"), el("div", { class: "sub" }, "the librarian's acts are on the report; operator acts from before captures stopped parking are here"))),
        timeline(row.events)),
    );
  });
}

// ── the two acts on the whole queue ──────────────────────────────────────────────────────────
async function reclaimFlow() {
  const lease = getMeta().worker ? getMeta().worker.visibility_timeout_s : null;
  const answer = await confirmForm({
    title: "Reclaim expired leases",
    consequence: `returns claims older than the worker's own lease (${lease ?? "?"} s) to the queue with a delivery burned; a row past its deliveries budget is failed instead and an ingest error recorded. "Release everything now" pulls EVERY claimed row — only safe with no live worker mid-item.`,
    fields: [actorField(), { name: "now", label: "Release everything now (visibility-timeout 0)", kind: "checkbox" }],
    confirmLabel: "Reclaim",
  });
  if (!answer) return;
  const body = { actor: answer.values.actor };
  if (answer.values.now) body.visibility_timeout_s = 0;
  if (await mutate("queue/reclaim", body, (r) => `released ${r.released} expired claim(s); failed ${r.failed} that had exhausted their deliveries`)) rerender();
}

async function purgeFlow() {
  let preview;
  try {
    preview = await api.post("queue/purge", { dry_run: true });
  } catch (ex) {
    toast(ex.message, "error");
    return;
  }
  const days = getMeta().retention ? getMeta().retention.default_days : "the configured number of";
  if (!preview.purged) {
    toast(`nothing has been terminal for ${days} days — nothing to purge`, "good");
    return;
  }
  const answer = await confirmForm({
    title: "Retention purge",
    consequence: `dry run first: this would strip payload and hints from ${preview.purged} capture(s) that have been terminal for ${days} days`
      + `${preview.ids && preview.ids.length ? ` (ids: ${preview.ids.join(", ")})` : ""}. `
      + "Id, submitter, timestamps, state and result pointer survive; evidence blobs are untouched. Confirm runs it for real.",
    fields: [actorField()],
    confirmLabel: `Purge ${preview.purged} row(s)`, danger: true,
  });
  if (!answer) return;
  if (await mutate("queue/purge", { actor: answer.values.actor }, (r) => `purged payload and hints of ${r.purged} capture(s)`)) rerender();
}
