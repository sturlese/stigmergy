// Repairs: the gardener's findings, one approvable edit at a time (ADR 039). The list is a SCAN
// and the detail is the read: nothing here renders the ops as prose, because the applier's own
// callout wording lives in `librarian.page` and a second copy of it here would show a reader a
// change that is not quite the one they are authorizing. The ops list IS the stored ops.
//
// FOUR kinds, four renderers. An `entity-body` op carries a page's whole drafted body, and the
// person reading that draft IS the check for that kind — squeezed into a table cell it is
// unreadable, and dropped from the table it is invisible. A `delete` proposal carries two
// different op shapes, and the one thing that must be legible before Approve is which pages STOP
// EXISTING. An `entity-alias` merge names which identity absorbs which.

import { api } from "../api.js";
import { chartCard, partToWhole, runStrip } from "../charts.js";
import { repairKind, word } from "../copy.js";
import { windowDays } from "../state.js";
import {
  banner, chips, confirmForm, el, emptyState, fmtWhen, icon, kv, link, mono, pill, relTime,
  render, table, wordPill,
} from "../ui.js";
import { actorField, go, loading, mutate, runShape, runTable } from "./common.js";

const KIND_ENTITY_BODY = "entity-body";
const KIND_DELETE = "delete";
const KIND_ALIAS = "entity-alias";
const OP_DELETE_PAGE = "delete-page";

const state = { kind: "" };

export async function repairsView(host) {
  await loading(host, async () => {
    const days = windowDays();
    const [data, metrics] = await Promise.all([api.get("repairs"), api.get(`metrics?days=${days}`)]);
    const pending = data.pending.filter((p) => !state.kind || (p.kind || "edits") === state.kind);
    const kinds = {};
    for (const p of data.pending) kinds[p.kind || "edits"] = (kinds[p.kind || "edits"] || 0) + 1;
    const byStatus = data.counts || {};
    const runs = (metrics.job_history["repair-propose"] || []).map((r) => runShape(r, proposerDetail));
    render(host,
      el("div", { class: "grid halves" },
        chartCard({
          title: "Proposals by outcome", sub: "every proposal ever drafted, and what was decided about it",
          chart: partToWhole({ segments: [
            { key: "pending", label: "waiting", value: byStatus.pending || 0, color: "human" },
            { key: "applied", label: "applied", value: byStatus.applied || 0, color: "git" },
            { key: "rejected", label: "declined", value: byStatus.rejected || 0, color: "code" },
            { key: "failed", label: "failed at a gate", value: byStatus.failed || 0, color: "fail" },
            { key: "approved", label: "approved, applying", value: byStatus.approved || 0, color: "model" },
          ] }),
          tableSpec: { headers: ["outcome", "proposals"], rows: Object.entries(byStatus).map(([k, n]) => ({ cells: [word(k).label, String(n)] })) },
        }),
        chartCard({
          title: `Proposer runs, last ${days} days`, sub: "each nightly run — height is duration, colour its outcome",
          chart: runStrip({ runs }), tableSpec: runTable(runs),
        })),
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" }, el("h2", {}, `${data.pending.length} proposal(s) waiting on you`),
            el("div", { class: "sub" }, "each one is approved or declined on its own — an approve applies exactly its edits as one commit through the librarian's own gates")),
          el("div", { class: "spacer" }),
          el("button", { class: "btn small", type: "button", onclick: () => deleteFlow() }, icon("x", 14), "Remove pages")),
        data.pending_truncated ? banner("warn", `showing the oldest ${data.pending_limit} pending proposals — more are waiting than this page carries`) : null,
        chips([{ key: "", label: "all kinds", count: data.pending.length, on: !state.kind },
          ...Object.entries(kinds).map(([k, n]) => ({ key: k, label: repairKind(k).label, count: n, on: state.kind === k, who: "model" }))],
        (key) => { state.kind = key; repairsView(host); }),
        table(["id", "kind", "what", "pages", "proposed"],
          pending.map((row) => ({
            row,
            cells: [mono(`#${row.id}`, "nowrap"), pill(repairKind(row.kind).label, "model", { small: true }), el("span", { class: "wrap" }, row.rationale),
              el("span", { class: "mono" }, row.target_paths.join(" · ")), relTime(row.created_at)],
          })),
          { empty: "nothing pending — the last proposer run found no repair worth asking about", onRow: (row) => go(`repairs/${row.id}`) })),
      el("section", { class: "card" },
        el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Recently decided"),
          el("div", { class: "sub" }, "a declined proposal is why the nightly run stops suggesting that repair; a failed one is an apply a gate refused, left visible with its reason rather than quietly retried"))),
        table(["id", "outcome", "kind", "decided by", "when", "pages", "what happened"],
          data.recent.map((row) => ({
            row,
            cells: [mono(`#${row.id}`, "nowrap"), wordPill(row.status), repairKind(row.kind).label, row.decided_by || "—", fmtWhen(row.decided_at),
              el("span", { class: "mono" }, row.target_paths.join(" · ")),
              el("span", { class: "wrap" }, row.error || row.notes || (row.applied_commit ? mono(row.applied_commit.slice(0, 12)) : "—"))],
          })),
          { empty: "nothing decided yet", onRow: (row) => go(`repairs/${row.id}`) })),
    );
  });
}

function proposerDetail(r) {
  const stats = r.stats || {};
  return [stats.proposals !== undefined ? `${stats.proposals} proposed` : "",
    stats.skipped_known !== undefined ? `${stats.skipped_known} already decided` : "", r.error || ""].filter(Boolean).join(" · ");
}

function opsList(ops) {
  if (!ops || !ops.length) return emptyState("no ops — nothing would change");
  return el("ul", { class: "ops-list" }, ops.map((o) => el("li", {},
    el("span", { class: "op" }, o.op),
    el("span", {}, el("span", { class: "diff-add" }, "+ "), `link to ${o.link || "?"} on `, mono(o.path), o.note ? el("div", { class: "sub" }, o.note) : null))));
}

// The drafted body, whole and unrendered — plain text in a <pre>, never markdown turned into DOM:
// what lands in the repo is these bytes, so these bytes are what you should be judging.
function bodyDraft(ops) {
  return el("div", { class: "stack" },
    ...(ops || []).map((o) => el("div", {},
      el("div", { class: "quote-label" }, mono(o.path), o.role ? el("span", {}, ` · role: ${o.role}`) : null),
      el("pre", { class: "pre" }, o.body_markdown || "(the draft is empty)"))));
}

// The pages that go, and the pages that change because they go. Two lists rather than one table,
// because they are two different things to agree to: one is irreversible from this console, and
// the other is a rewrite of pages you may not have opened.
function deletionPlan(ops) {
  const removed = (ops || []).filter((o) => o.op === OP_DELETE_PAGE).map((o) => o.path);
  const scrubbed = (ops || []).filter((o) => o.op !== OP_DELETE_PAGE).map((o) => o.path);
  const list = (paths, cls) => el("ul", { class: "names" }, ...paths.map((p) => el("li", { class: cls }, p)));
  return el("div", {},
    el("div", { class: "sub" }, `${removed.length} page(s) STOP EXISTING`),
    list(removed, "diff-del"),
    el("div", { class: "sub", style: { marginTop: "12px" } }, `${scrubbed.length} page(s) rewritten so they no longer refer to them — a model wrote these bodies, and this is the only place anybody reads them before they land`),
    scrubbed.length
      // Whole and unrendered, exactly as `bodyDraft` shows a drafted entity body: what lands in
      // the repo is these bytes, so these bytes are what you should be judging.
      ? el("div", { class: "stack" }, ...(ops || []).filter((o) => o.op !== OP_DELETE_PAGE).map((o) => el("div", {},
          el("div", { class: "quote-label" }, mono(o.path)),
          el("pre", { class: "pre" }, o.planned_after || "(no planned bytes — this proposal cannot be applied)"))))
      : el("div", { class: "sub" }, "— nothing else refers to them"));
}

function mergePlan(ops) {
  const byOp = (name) => (ops || []).filter((o) => o.op === name).map((o) => o.path);
  return el("div", { class: "stack" },
    el("div", {}, el("div", { class: "sub" }, "survives, and gains the other's spellings"), el("ul", { class: "names" }, byOp("alias-survivor").map((p) => el("li", { class: "diff-add" }, p)))),
    el("div", {}, el("div", { class: "sub" }, "retired — marked superseded by the survivor (the page stays)"), el("ul", { class: "names" }, byOp("retire-absorbed").map((p) => el("li", { class: "diff-del" }, p)))),
    byOp("reanchor-page").length ? el("div", {}, el("div", { class: "sub" }, `${byOp("reanchor-page").length} page(s) re-anchored to the survivor`), el("ul", { class: "names mono" }, byOp("reanchor-page").map((p) => el("li", {}, p)))) : null,
    el("div", { class: "sub" }, "ops/entity-registry.json is regenerated by the generator, never hand-built"));
}

function changeSummary(kind) {
  if (kind === KIND_ENTITY_BODY) {
    return "this replaces the page's body BELOW its own title line. Its frontmatter is preserved byte for byte apart from the updated date (and the role, when the page has none), and the title line itself does not change. Read the draft: it is what the page will say.";
  }
  if (kind === KIND_DELETE) {
    return "the pages in the first list STOP EXISTING, and the pages below them are rewritten so they no longer refer to them: their related/sources entries are dropped by code, and their bodies are written by a MODEL — a sentence that cited a removed page still reads, and a callout that only existed because of one is gone. Those bodies are shown in full because approving this is the only reading they get before they land. Undoing it means a revert in the knowledge repo.";
  }
  if (kind === KIND_ALIAS) {
    return "two registry entries were one entity. The survivor's page gains the absorbed spellings as aliases, the absorbed page is marked superseded (it is never deleted), every page anchored to it moves to the survivor, and the registry is regenerated.";
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

export async function repairDetailView(host, id) {
  await loading(host, async () => {
    const row = await api.get(`repairs/${id}`);
    const pending = row.status === "pending";
    const kind = repairKind(row.kind);
    render(host,
      el("div", { class: "crumbs" }, link("repairs", "Repairs"), icon("chevron"), el("span", {}, `proposal #${row.id}`)),
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" }, el("h2", {}, `Proposal #${row.id} — ${kind.label}`), el("div", { class: "sub" }, kind.explain)),
          el("div", { class: "spacer" }), wordPill(row.status),
          pending ? el("div", { class: "row" },
            el("button", { class: "btn small", type: "button", onclick: () => repairRejectFlow(row) }, icon("x", 14), "Decline"),
            el("button", { class: "btn small primary", type: "button", onclick: () => repairApproveFlow(row) }, icon("check", 14), row.kind === KIND_DELETE ? "Approve & remove" : "Approve & apply")) : null),
        el("p", { class: "lede" }, row.rationale || "(no rationale recorded)"),
        kv([
          ["pages it would touch", el("ul", { class: "names mono" }, row.target_paths.map((p) => el("li", {}, p)))],
          ["from findings", (row.finding_ids || []).map((f) => `#${f}`).join(", ") || "(none)"],
          ["proposed", `${fmtWhen(row.created_at)} by ${row.model_id || "a person"}`],
          ["decided", row.decided_at ? `${fmtWhen(row.decided_at)} by ${row.decided_by || "?"}` : null],
          ["reason given", row.notes || null],
          ["commit", row.applied_commit ? mono(row.applied_commit) : null],
          ["why it failed", row.error ? el("span", { class: "diff-del" }, row.error) : null],
        ], { wide: true })),
      el("section", { class: "card" },
        el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "What it would change"), el("div", { class: "sub" }, changeSummary(row.kind)))),
        repairChange(row)),
      pending
        ? banner("info", "approving pushes ONE commit to the knowledge repo, authored by the librarian App with your name in an Approved-by trailer (attribution, not a second authorization check — the token is this console's). The edits are re-validated and re-gated against the repo as it stands, so the apply can still be refused.")
        : decidedBanner(row),
    );
  });
}

// What a decided row means depends on HOW it was decided: a decline is remembered by the proposer
// forever, a failed apply is not, an applied one lives in git now.
function decidedBanner(row) {
  if (row.status === "rejected") {
    return banner("plain", `declined by ${row.decided_by || "somebody"}, and the proposer remembers: this exact repair will not be proposed again. To revisit it, the finding has to change in the knowledge repo, or somebody makes the edit by hand.`);
  }
  if (row.status === "failed") {
    return banner("plain", "a gate refused this apply and nothing was written. The proposer does not remember failures, so the next nightly run can derive this repair again.");
  }
  if (row.status === "applied") {
    return banner("plain", el("span", {}, "applied — commit ", mono(String(row.applied_commit || "").slice(0, 12)), ". Undoing it means a revert in the knowledge repo."));
  }
  return banner("plain", "approved and being applied — if this row stays here, read the runbook's section on a repair proposal stuck in approved.");
}

// The one action on this page that is not a verdict on somebody else's proposal: a PERSON removing
// pages (ADR 043). It waits on nobody — the judgment is the operator's and it lands in this call —
// so the confirm has to carry the whole consequence, and the result has to carry the diff, because
// nobody read the rewritten prose before it landed.
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

async function repairApproveFlow(row) {
  // The last sentence anybody reads before a page stops existing, so it says REMOVE rather than
  // "apply N edit(s)" — the generic wording would be the one place in the console that describes
  // a deletion as an edit.
  const removing = row.kind === KIND_DELETE ? (row.ops || []).filter((o) => o.op === OP_DELETE_PAGE).length : 0;
  const answer = await confirmForm({
    title: removing ? `Approve #${row.id} — remove ${removing} page(s)` : `Approve #${row.id} — apply ${row.ops.length} edit(s)`,
    consequence: removing
      ? "removes those pages from the knowledge repo and rewrites every page that links to them, in ONE commit. It is re-computed and re-gated against the repo as it stands right now, so it can still be refused — but if it lands, nothing here can undo it."
      : "applies exactly these edits and pushes ONE commit to the knowledge repo. It is re-validated and re-gated against the repo as it stands right now, so it can still be refused — but if it lands, nothing here can undo it.",
    note: banner("info",
      el("div", {}, removing ? "the pages this would remove or rewrite:" : "the pages this would edit:"),
      el("ul", { class: "names" }, row.target_paths.map((p) => el("li", { class: "mono" }, p)))),
    fields: [actorField()],
    confirmLabel: removing ? "Approve & remove" : "Approve & apply",
  });
  if (!answer) return;
  if (await mutate(`repairs/${row.id}/approve`, answer.values, (r) => `applied #${row.id} — commit ${String(r.commit || "").slice(0, 12) || "?"}`)) go("repairs");
}

async function repairRejectFlow(row) {
  const answer = await confirmForm({
    title: `Decline #${row.id}`,
    consequence: "records the decision and, because a declined proposal is the proposer's memory of having asked, stops this exact repair being suggested again. Nothing is written to the knowledge repo.",
    fields: [actorField(), { name: "reason", label: "Reason", kind: "textarea", required: true, hint: "the whole of what a later reader will know about why this was declined" }],
    confirmLabel: "Decline", danger: true,
  });
  if (answer && await mutate(`repairs/${row.id}/reject`, answer.values, `declined #${row.id}`)) go("repairs");
}
