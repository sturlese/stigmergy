// Repairs: the LEDGER of what has left the corpus, newest first. Nothing on this page decides
// anything and nothing on it waits — a person names pages, the worker removes them and rewrites
// every page that referred to them, and this page is where anyone finds out afterwards.
//
// Which is why the diff is here, and why it is rendered UNRENDERED: the reason the ledger stores
// a diff at all is that nobody read those bytes before they were pushed. A page that summarised
// them would be showing a summary of prose a model wrote into somebody's corpus. The list is the
// scan, the row is the read. The capture that asked for the removal carries the same reading per
// page — and is purged with the retention window, while this row is not.
//
// **Rows the elective repair loop wrote are still here.** That loop derived repairs from gardener
// findings and applied them unattended; it was removed, and its rows keep their own kinds
// (`repair.schema.RETIRED_KINDS`) and their own two outcomes. They render as what they were, with
// their ops listed generically: an old row still reads whole, and no renderer is maintained for a
// writer that is gone.

import { api } from "../api.js";
import { chartCard, partToWhole } from "../charts.js";
import { REPAIR_OUTCOME_ORDER, repairKind, word } from "../copy.js";
import {
  banner, chips, confirmForm, el, emptyState, fmtWhen, icon, kv, link, mono, relTime, render,
  table, wordPill,
} from "../ui.js";
import { actorField, go, loading, mutate } from "./common.js";

const KIND_DELETE = "delete";
const OP_DELETE_PAGE = "delete-page";
const STATUS_APPLIED = "applied";
const STATUS_FAILED = "failed";
const STATUS_SKIPPED = "skipped";

const state = { status: "" };

export async function repairsView(host) {
  await loading(host, async () => {
    const data = await api.get("repairs");
    const counts = data.counts || {};
    const shown = data.recent.filter((row) => !state.status || row.status === state.status);
    const here = {};
    for (const row of data.recent) here[row.status] = (here[row.status] || 0) + 1;
    render(host,
      chartCard({
        title: "The ledger by outcome",
        sub: "every row this deployment ever wrote here — removals, and the elective repairs that ran before that loop was removed",
        chart: partToWhole({ segments: outcomes(counts).map((key) => ({
          key, label: word(key).label, value: counts[key] || 0, color: word(key).who })) }),
        tableSpec: { headers: ["outcome", "rows"],
          rows: outcomes(counts).map((key) => ({ cells: [word(key).label, String(counts[key] || 0)] })) },
      }),
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" }, el("h2", {}, "The ledger"),
            el("div", { class: "sub" }, "what left the corpus, newest first — every row is already in the knowledge repo, and its diff is the reading it never got before it went in. Open a row for it.")),
          el("div", { class: "spacer" }),
          el("button", { class: "btn small", type: "button", onclick: () => deleteFlow() }, icon("x", 14), "Remove pages")),
        data.recent.length >= data.recent_limit
          ? banner("plain", `the newest ${data.recent_limit} rows — the ledger keeps every one ever written, and the chart above counts them all`)
          : null,
        chips([{ key: "", label: "everything", count: data.recent.length, on: !state.status },
          ...outcomes(here).map((key) => ({ key, label: word(key).label, count: here[key] || 0,
            on: state.status === key, who: word(key).who }))],
        (key) => { state.status = key; repairsView(host); }),
        table(["id", "outcome", "kind", "pages", "when", { text: "what happened", cls: "wrap" }],
          shown.map((row) => ({
            row,
            cells: [mono(`#${row.id}`, "nowrap"), wordPill(row.status), repairKind(row.kind).label,
              el("span", { class: "mono" }, row.target_paths.join(" · ") || "—"),
              relTime(row.created_at), outcomeCell(row)],
          })),
          { empty: state.status ? "no row with that outcome in this page of the ledger" : "the ledger is empty — nothing has been removed from this brain",
            emptyHint: "Remove pages queues a removal in your name; the worker performs it within a minute or so and the row lands here",
            onRow: (row) => go(`repairs/${row.id}`) })),
    );
  });
}

// The chart's and the chips' order is the vocabulary's, not the server's dict order, and a status
// this file has never met is appended rather than dropped — ugly, never invisible.
function outcomes(counted) {
  return [...REPAIR_OUTCOME_ORDER.filter((s) => s in counted),
    ...Object.keys(counted).filter((s) => !REPAIR_OUTCOME_ORDER.includes(s))];
}

// One row's outcome in a table cell: the commit for a row that landed, and for the retired loop's
// other two outcomes the sentence the ledger stores — never the diff, which no cell can hold.
function outcomeCell(row) {
  if (row.status === STATUS_APPLIED) {
    return el("span", { class: "row" }, mono(String(row.applied_commit || "").slice(0, 12) || "—"),
      el("span", { class: "sub" }, row.diff ? "diff inside" : "no diff recorded"));
  }
  return el("span", { class: "wrap" },
    (row.status === STATUS_FAILED ? row.error : row.reason) || "—");
}

// A retired kind's ops, listed by name and path with whatever else the row stored — the generic
// reading, because the writers that gave those ops their shapes are gone.
function opsList(ops) {
  if (!ops || !ops.length) return emptyState("no ops — nothing would have changed");
  return el("ul", { class: "ops-list" }, ops.map((o) => el("li", {},
    el("span", { class: "op" }, o.op || "op"),
    el("span", {}, mono(o.path || "—"),
      ...Object.entries(o).filter(([k, v]) => !["op", "path"].includes(k) && v)
        .map(([k, v]) => el("div", { class: "sub" }, `${k}: ${v}`))))));
}

// The pages that went, and the pages that changed because they went. Two lists rather than one
// table, because they are two different things: one removes a page and the other rewrites pages a
// reader may never have opened.
function deletionPlan(ops, landed) {
  const removed = (ops || []).filter((o) => o.op === OP_DELETE_PAGE).map((o) => o.path);
  const scrubbed = (ops || []).filter((o) => o.op !== OP_DELETE_PAGE).map((o) => o.path);
  const list = (paths, cls) => el("ul", { class: "names" }, ...paths.map((p) => el("li", { class: cls }, p)));
  return el("div", {},
    el("div", { class: "sub" }, `${removed.length} page(s) ${landed ? "STOPPED EXISTING" : "would have STOPPED EXISTING"}`),
    list(removed, "diff-del"),
    el("div", { class: "sub", style: { marginTop: "12px" } }, `${scrubbed.length} page(s) ${landed ? "were" : "would have been"} rewritten so they no longer referred to them — a model wrote these bodies`),
    scrubbed.length
      // Whole and unrendered: what landed in the repo is these bytes, so these bytes are what
      // there is to read.
      ? el("div", { class: "stack" }, ...(ops || []).filter((o) => o.op !== OP_DELETE_PAGE).map((o) => el("div", {},
          el("div", { class: "quote-label" }, mono(o.path)),
          el("pre", { class: "pre" }, o.planned_after || "(no planned bytes recorded)"))))
      : el("div", { class: "sub" }, "— nothing else referred to them"));
}

// One sentence per kind, above the ops. Paired with `repairChange` below on the SAME dispatch, so
// the renderer and the sentence over it can never describe different kinds.
function changeSummary(kind, landed) {
  if (kind === KIND_DELETE) {
    return landed
      ? "the pages in the first list stopped existing, and the pages below them were rewritten so they no longer referred to them: their related/sources entries dropped by code, their bodies written by a MODEL. A revert in the knowledge repo is the only undo."
      : "the pages in the first list would have STOPPED EXISTING, and the pages below them would have been rewritten so they no longer referred to them. Nothing landed — no page was removed and no body was replaced.";
  }
  return "this row was written by the elective repair loop, which has been removed. Its ops are listed as they were stored; nothing here can be derived or applied again.";
}

// ONE dispatch, so the renderer and the sentence above it can never describe different kinds.
function repairChange(row) {
  if (row.kind === KIND_DELETE) return deletionPlan(row.ops, row.status === STATUS_APPLIED);
  return opsList(row.ops);
}

// The diff, unrendered, in the same `<pre>` the capture shows its own diffs in: what landed in the
// repo is these bytes. `_clean` keeps their newlines for exactly this reason — a diff flattened to
// one line is not a diff anybody can read — and the box scrolls on its own so a thousand-line
// commit does not bury the rest of the row.
function diffCard(row) {
  return el("section", { class: "card" },
    el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "The diff that landed"),
      el("div", { class: "sub" }, "the commit's own unified diff, as it was pushed — this is the reading nobody gave it first"))),
    row.diff
      ? el("pre", { class: "pre", style: { maxHeight: "560px", overflow: "auto" } }, row.diff)
      : emptyState("no diff was recorded for this row",
          "a row written before the diff column existed has none — it was pushed before the column was"));
}

// What the outcome MEANS for the person reading it. Three sentences and not one, because the
// three states have nothing in common: one is in git now, one wrote nothing, one never became
// anything at all.
function outcomeBanner(row) {
  if (row.status === STATUS_APPLIED) {
    return banner("plain", el("span", {}, "applied — commit ", mono(String(row.applied_commit || "").slice(0, 12)),
      " is in the knowledge repo. A revert there is the only undo, and it is permanent."));
  }
  if (row.status === STATUS_FAILED) {
    return banner("plain", "a gate or its own validator refused this and NOTHING was written. It belongs to the elective repair loop, which has been removed, so nothing will attempt it again.");
  }
  return banner("plain", "nothing was derived here — the reason above is the whole of it. It belongs to the elective repair loop, which has been removed.");
}

export async function repairDetailView(host, id) {
  await loading(host, async () => {
    const row = await api.get(`repairs/${id}`);
    const kind = repairKind(row.kind);
    const applied = row.status === STATUS_APPLIED;
    // `finding_subjects` drops the findings that named nothing, so it is NOT positional against
    // `finding_ids` — the two are shown as two facts, never zipped into a pairing that lies.
    // Both are empty on a removal: nobody derived it from a finding, a person asked for it.
    const subjects = [...new Set((row.finding_subjects || []).flat())];
    render(host,
      el("div", { class: "crumbs" }, link("repairs", "Repairs"), icon("chevron"), el("span", {}, `row #${row.id}`)),
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" }, el("h2", {}, `#${row.id} — ${kind.label}`), el("div", { class: "sub" }, kind.explain)),
          el("div", { class: "spacer" }), wordPill(row.status)),
        el("p", { class: "lede" }, row.rationale || "(no reason recorded)"),
        kv([
          [applied ? "pages it changed" : "pages it named",
            row.target_paths.length ? el("ul", { class: "names mono" }, row.target_paths.map((p) => el("li", {}, p))) : null],
          ["findings it answered", (row.finding_ids || []).length ? (row.finding_ids).map((f) => `#${f}`).join(", ") : null],
          ["pages those findings named", subjects.length ? el("ul", { class: "names mono" }, subjects.map((p) => el("li", {}, p))) : null],
          ["ran", `${fmtWhen(row.created_at)}${row.model_id ? ` · ${row.model_id}` : ""}`],
          ["commit", row.applied_commit ? mono(row.applied_commit) : null],
          ["why it failed", row.error ? el("span", { class: "diff-del" }, row.error) : null],
          ["why it was skipped", row.status === STATUS_SKIPPED ? (row.reason || "(no reason recorded)") : null],
        ], { wide: true }),
        outcomeBanner(row)),
      applied ? diffCard(row) : null,
      el("section", { class: "card" },
        el("div", { class: "card-head" }, el("div", { class: "card-title" },
          el("h2", {}, applied ? "What it changed" : "What it would have changed"),
          el("div", { class: "sub" }, changeSummary(row.kind, applied)))),
        repairChange(row)),
    );
  });
}

// The ONE act on this page, and the only thing here a person decides: removing pages.
// It waits on nobody — the judgment is the operator's — but it does not land in this call either
//: the librarian worker is the one writer the corpus has, so what this button does is
// QUEUE the removal in the operator's name. The confirm still has to carry the whole consequence,
// because nothing else will ask.
async function deleteFlow() {
  const answer = await confirmForm({
    title: "Remove pages from the brain",
    consequence: "queues the removal of these pages and the rewriting of every page that refers to them — their related/sources entries by code, their bodies by a model — as ONE commit the librarian pushes within a minute or so. There is no second click: this console's token is the authorization. Once it lands, only a revert in the knowledge repo undoes it.",
    note: banner("warn", "nobody reads the rewritten prose before it lands. The diffs land on the capture — open it from Captures to read them, and revert in the knowledge repo if a page came out wrong."),
    fields: [
      actorField(),
      { name: "paths", label: "Pages", kind: "textarea", required: true,
        hint: "one repo-relative path per line, e.g. wiki/notes/Old Memo.md — never an entity page" },
      { name: "why", label: "Why", kind: "textarea", required: true,
        hint: "what makes them stale: the commit carries it, and it is all a later reader will have" },
    ],
    confirmLabel: "Remove", danger: true,
  });
  if (!answer) return;
  const paths = String(answer.values.paths || "").split("\n").map((p) => p.trim()).filter(Boolean);
  const body = { actor: answer.values.actor, why: answer.values.why, paths };
  const result = await mutate("pages/delete", body,
    (r) => `queued as capture #${r.id ?? "?"} — the librarian performs it`);
  if (!result) return;
  go(`captures/${result.id}`);
}
