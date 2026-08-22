// Repairs: the LEDGER of what the repair pass did to the corpus, newest first (ADR 044). Nothing
// on this page decides anything and nothing on it waits — a repair is derived from a gardener
// finding, validated, gated and applied in ONE pass, and this page is where a person finds out
// afterwards.
//
// Which is why the diff is here, and why it is rendered UNRENDERED: the reason the ledger stores
// a diff at all is that nobody read those bytes before they were pushed. A page that summarised
// them would be showing a summary of prose a model wrote into somebody's corpus. The list is the
// scan, the row is the read.
//
// The three outcomes are three different readings, not three colours of one. `applied` has a
// commit and a diff. `failed` wrote nothing, and what there is to read is the sentence that
// refused it beside the ops it never got to write — FOUR kinds, four renderers, because a
// deletion that did not happen is still not an edit. `skipped` never became a repair at all and
// carries only its reason.

import { api } from "../api.js";
import { chartCard, partToWhole, runStrip } from "../charts.js";
import { REPAIR_OUTCOME_ORDER, repairKind, word } from "../copy.js";
import {
  banner, chips, confirmForm, el, emptyState, fmtWhen, icon, kv, link, mono, relTime, render,
  table, wordPill,
} from "../ui.js";
import { actorField, go, loading, mutate, runShape, runTable } from "./common.js";

const KIND_ENTITY_BODY = "entity-body";
const KIND_DELETE = "delete";
const KIND_ALIAS = "entity-alias";
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
    const runs = (data.history || []).map((run) => runShape(run, passDetail));
    render(host,
      el("div", { class: "grid halves" },
        chartCard({
          title: "Repairs by outcome",
          sub: "every repair this deployment ever ran, and what came of it",
          chart: partToWhole({ segments: outcomes(counts).map((key) => ({
            key, label: word(key).label, value: counts[key] || 0, color: word(key).who })) }),
          tableSpec: { headers: ["outcome", "repairs"],
            rows: outcomes(counts).map((key) => ({ cells: [word(key).label, String(counts[key] || 0)] })) },
        }),
        chartCard({
          title: runs.length ? `The last ${runs.length} repair pass(es)` : "Repair passes",
          sub: "each pass the worker ran while the queue was idle — height is duration, colour its outcome",
          chart: runStrip({ runs }), tableSpec: runTable(runs),
        })),
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" }, el("h2", {}, "The ledger"),
            el("div", { class: "sub" }, "what the repair pass did, newest first — an applied row is already in the knowledge repo, and its diff is the reading it never got before it went in. Open a row for it.")),
          el("div", { class: "spacer" }),
          el("button", { class: "btn small", type: "button", onclick: () => deleteFlow() }, icon("x", 14), "Remove pages")),
        data.recent.length >= data.recent_limit
          ? banner("plain", `the newest ${data.recent_limit} rows — the ledger keeps every repair ever run, and the chart above counts them all`)
          : null,
        chips([{ key: "", label: "everything", count: data.recent.length, on: !state.status },
          ...outcomes(here).map((key) => ({ key, label: word(key).label, count: here[key] || 0,
            on: state.status === key, who: word(key).who }))],
        (key) => { state.status = key; repairsView(host); }),
        table(["id", "outcome", "kind", "findings", "pages", "when", { text: "what happened", cls: "wrap" }],
          shown.map((row) => ({
            row,
            cells: [mono(`#${row.id}`, "nowrap"), wordPill(row.status), repairKind(row.kind).label,
              mono((row.finding_ids || []).map((f) => `#${f}`).join(", ") || "—"),
              el("span", { class: "mono" }, row.target_paths.join(" · ") || "—"),
              relTime(row.created_at), outcomeCell(row)],
          })),
          { empty: state.status ? "no repair with that outcome in this page of the ledger" : "the ledger is empty — no repair has run yet",
            emptyHint: "the worker runs a repair pass on its own interval whenever the queue is idle, over whatever findings the gardener has left it",
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

// The run strip's one-line detail: the four numbers a pass records, and every finding it saw is
// in exactly one of them.
function passDetail(run) {
  const stats = run.stats || {};
  const skipped = (stats.skipped_known || 0) + (stats.skipped_invalid || 0);
  return [stats.findings_seen !== undefined ? `${stats.findings_seen} findings seen` : "",
    stats.applied !== undefined ? `${stats.applied} applied` : "",
    stats.failed ? `${stats.failed} failed` : "", skipped ? `${skipped} skipped` : "",
    run.error || ""].filter(Boolean).join(" · ");
}

// One row's outcome in a table cell: the commit for an applied repair, and for the other two the
// sentence the ledger stores about them — never the diff, which no cell can hold.
function outcomeCell(row) {
  if (row.status === STATUS_APPLIED) {
    return el("span", { class: "row" }, mono(String(row.applied_commit || "").slice(0, 12) || "—"),
      el("span", { class: "sub" }, row.diff ? "diff inside" : "no diff recorded"));
  }
  return el("span", { class: "wrap" },
    (row.status === STATUS_FAILED ? row.error : row.reason) || "—");
}

function opsList(ops) {
  if (!ops || !ops.length) return emptyState("no ops — nothing would have changed");
  return el("ul", { class: "ops-list" }, ops.map((o) => el("li", {},
    el("span", { class: "op" }, o.op),
    el("span", {}, el("span", { class: "diff-add" }, "+ "), `link to ${o.link || "?"} on `, mono(o.path), o.note ? el("div", { class: "sub" }, o.note) : null))));
}

// The drafted body, whole and unrendered — plain text in a <pre>, never markdown turned into DOM:
// these bytes are what would have become the page, so these bytes are what there is to read.
function bodyDraft(ops) {
  return el("div", { class: "stack" },
    ...(ops || []).map((o) => el("div", {},
      el("div", { class: "quote-label" }, mono(o.path), o.role ? el("span", {}, ` · role: ${o.role}`) : null),
      el("pre", { class: "pre" }, o.body_markdown || "(the draft is empty)"))));
}

// The pages that would have gone, and the pages that would have changed because they went. Two
// lists rather than one table, because they are two different things: one removes a page and the
// other rewrites pages a reader may never have opened.
function deletionPlan(ops) {
  const removed = (ops || []).filter((o) => o.op === OP_DELETE_PAGE).map((o) => o.path);
  const scrubbed = (ops || []).filter((o) => o.op !== OP_DELETE_PAGE).map((o) => o.path);
  const list = (paths, cls) => el("ul", { class: "names" }, ...paths.map((p) => el("li", { class: cls }, p)));
  return el("div", {},
    el("div", { class: "sub" }, `${removed.length} page(s) would have STOPPED EXISTING`),
    list(removed, "diff-del"),
    el("div", { class: "sub", style: { marginTop: "12px" } }, `${scrubbed.length} page(s) would have been rewritten so they no longer referred to them — a model wrote these bodies, and none of them landed`),
    scrubbed.length
      // Whole and unrendered, exactly as `bodyDraft` shows a drafted entity body: what would have
      // landed in the repo is these bytes, so these bytes are what there is to read.
      ? el("div", { class: "stack" }, ...(ops || []).filter((o) => o.op !== OP_DELETE_PAGE).map((o) => el("div", {},
          el("div", { class: "quote-label" }, mono(o.path)),
          el("pre", { class: "pre" }, o.planned_after || "(no planned bytes — this repair could never have been applied)"))))
      : el("div", { class: "sub" }, "— nothing else referred to them"));
}

function mergePlan(ops) {
  const byOp = (name) => (ops || []).filter((o) => o.op === name).map((o) => o.path);
  return el("div", { class: "stack" },
    el("div", {}, el("div", { class: "sub" }, "survives, and gains the other's spellings"), el("ul", { class: "names" }, byOp("alias-survivor").map((p) => el("li", { class: "diff-add" }, p)))),
    el("div", {}, el("div", { class: "sub" }, "retired — marked superseded by the survivor (the page stays)"), el("ul", { class: "names" }, byOp("retire-absorbed").map((p) => el("li", { class: "diff-del" }, p)))),
    byOp("reanchor-page").length ? el("div", {}, el("div", { class: "sub" }, `${byOp("reanchor-page").length} page(s) re-anchored to the survivor`), el("ul", { class: "names mono" }, byOp("reanchor-page").map((p) => el("li", {}, p)))) : null,
    el("div", { class: "sub" }, "ops/entity-registry.json is regenerated by the generator, never hand-built"));
}

// One sentence per kind, above the ops of a repair that never landed: what the pass was going to
// write. Paired with `repairChange` below on the SAME dispatch, so the renderer and the sentence
// over it can never describe different kinds.
function changeSummary(kind) {
  if (kind === KIND_ENTITY_BODY) {
    return "this kind replaces the page's body BELOW its own title line. The frontmatter is preserved byte for byte apart from the updated date (and the role, when the page has none), and the title line itself does not change. Nothing was written: the draft below is what the page would have said.";
  }
  if (kind === KIND_DELETE) {
    return "the pages in the first list would have STOPPED EXISTING, and the pages below them would have been rewritten so they no longer referred to them: their related/sources entries dropped by code, their bodies written by a MODEL. Nothing landed — no page was removed and no body was replaced.";
  }
  if (kind === KIND_ALIAS) {
    return "two registry entries were judged one entity. The survivor's page would have gained the absorbed spellings, the absorbed page would have been marked superseded (it is never deleted), every page anchored to it moved to the survivor, and the registry regenerated. Nothing landed.";
  }
  return "every op is additive: a link added to that page's related list, and for overlap and contradiction a one-sentence callout below it. Nothing is rewritten or deleted here.";
}

// ONE dispatch, so the renderer and the sentence above it can never describe different kinds.
function repairChange(row) {
  if (row.kind === KIND_ENTITY_BODY) return bodyDraft(row.ops);
  if (row.kind === KIND_DELETE) return deletionPlan(row.ops);
  if (row.kind === KIND_ALIAS) return mergePlan(row.ops);
  return opsList(row.ops);
}

// The diff, unrendered, in the same `<pre>` the removal flow shows its own diffs in: what landed
// in the repo is these bytes. `_clean` keeps their newlines for exactly this reason — a diff
// flattened to one line is not a diff anybody can read — and the box scrolls on its own so a
// thousand-line commit does not bury the rest of the row.
function diffCard(row) {
  return el("section", { class: "card" },
    el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "The diff that landed"),
      el("div", { class: "sub" }, "the commit's own unified diff, as it was pushed — this is the reading nobody gave it first"))),
    row.diff
      ? el("pre", { class: "pre", style: { maxHeight: "560px", overflow: "auto" } }, row.diff)
      : emptyState("no diff was recorded for this repair",
          "a repair applied before ADR 044 landed has none — the column did not exist when it was pushed"));
}

// What the outcome MEANS for the person reading it. Three sentences and not one, because the
// three states have nothing in common: one is in git now, one wrote nothing and is never retried,
// one never became a repair at all.
function outcomeBanner(row) {
  if (row.status === STATUS_APPLIED) {
    return banner("plain", el("span", {}, "applied — commit ", mono(String(row.applied_commit || "").slice(0, 12)),
      " is in the knowledge repo. A revert there is the only undo, and it is permanent: this exact repair is never derived again."));
  }
  if (row.status === STATUS_FAILED) {
    return banner("plain", "a gate or its own validator refused this repair and NOTHING was written. It is not retried either — the finding behind it stops being answered until the corpus moves, which is why the reason above has to be one an operator can act on.");
  }
  return banner("plain", "nothing was derived here — the reason above is the whole of it. A skipped row carries no content key, so nothing remembers it and a later pass is free to try again.");
}

export async function repairDetailView(host, id) {
  await loading(host, async () => {
    const row = await api.get(`repairs/${id}`);
    const kind = repairKind(row.kind);
    const applied = row.status === STATUS_APPLIED;
    // `finding_subjects` drops the findings that named nothing, so it is NOT positional against
    // `finding_ids` — the two are shown as two facts, never zipped into a pairing that lies.
    const subjects = [...new Set((row.finding_subjects || []).flat())];
    render(host,
      el("div", { class: "crumbs" }, link("repairs", "Repairs"), icon("chevron"), el("span", {}, `repair #${row.id}`)),
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" }, el("h2", {}, `Repair #${row.id} — ${kind.label}`), el("div", { class: "sub" }, kind.explain)),
          el("div", { class: "spacer" }), wordPill(row.status)),
        el("p", { class: "lede" }, row.rationale || "(no rationale recorded)"),
        kv([
          [applied ? "pages it changed" : "pages it named",
            row.target_paths.length ? el("ul", { class: "names mono" }, row.target_paths.map((p) => el("li", {}, p))) : null],
          ["findings it answered", (row.finding_ids || []).map((f) => `#${f}`).join(", ") || "(none)"],
          ["pages those findings named", subjects.length ? el("ul", { class: "names mono" }, subjects.map((p) => el("li", {}, p))) : null],
          ["ran", `${fmtWhen(row.created_at)}${row.model_id ? ` · ${row.model_id}` : ""}`],
          ["commit", row.applied_commit ? mono(row.applied_commit) : null],
          ["why it failed", row.error ? el("span", { class: "diff-del" }, row.error) : null],
          ["why it was skipped", row.status === STATUS_SKIPPED ? (row.reason || "(no reason recorded)") : null],
        ], { wide: true }),
        outcomeBanner(row)),
      applied ? diffCard(row) : null,
      row.status === STATUS_FAILED
        ? el("section", { class: "card" },
            el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "What it would have changed"),
              el("div", { class: "sub" }, changeSummary(row.kind)))),
            repairChange(row))
        : null,
    );
  });
}

// The ONE act on this page, and the only thing here a person decides: removing pages (ADR 043).
// It waits on nobody — the judgment is the operator's and it lands in this call — so the confirm
// has to carry the whole consequence, and the result has to carry the diffs, because nobody read
// the rewritten prose before it landed.
async function deleteFlow() {
  const answer = await confirmForm({
    title: "Remove pages from the brain",
    consequence: "removes these pages and rewrites every page that refers to them — their related/sources entries by code, their bodies by a model — as ONE commit, right now. There is no proposal and no second click: this console's token is the authorization. If it lands, only a revert in the knowledge repo undoes it.",
    note: banner("warn", "nobody reads the rewritten prose before it lands. The diffs come back here — read them, and revert in the knowledge repo if a page came out wrong."),
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
    (r) => `removed ${(r.deleted || []).length} page(s) — commit ${String(r.commit || "").slice(0, 12) || "?"}`);
  if (!result) return;
  await showDiffs(result);
  go("repairs");
}

// The reading, moved after the push (ADR 043 D5). A `confirmForm` with no fields is the plainest
// dialog this console has, and the diffs go in it UNRENDERED — what landed in the repo is these
// bytes, so these bytes are what the person who pressed Remove should be looking at.
function showDiffs(result) {
  const rewritten = Object.entries(result.rewritten || {});
  return confirmForm({
    title: `Removed ${(result.deleted || []).length} page(s) — commit ${String(result.commit || "").slice(0, 12)}`,
    consequence: "this has already landed in the knowledge repo. Read what the model wrote into the pages that referred to the removed ones; a revert there is the undo.",
    note: rewritten.length
      ? el("div", { class: "stack" }, ...rewritten.map(([path, diff]) => el("div", {},
          el("div", { class: "quote-label" }, mono(path)),
          el("pre", { class: "pre" }, diff))))
      : banner("plain", "nothing referred to the removed page(s), so no page was rewritten."),
    fields: [], confirmLabel: "Done", cancelLabel: "Close", wide: true,
  });
}
