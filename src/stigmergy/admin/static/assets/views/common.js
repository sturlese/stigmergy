// What every view shares: the loading wrapper, the mutation helper (toast the outcome, surface
// the warning), the actor field, the report renderer and the timeline. Nothing here reaches the
// network except through api.js.

import { api } from "../api.js";
import { ACTOR_HINT, KEY, status as statusCopy } from "../copy.js";
import { getMeta } from "../state.js";
import {
  banner, copyButton, el, fmtMs, fmtWhen, icon, keyDot, mono, render, skeletons, statusPill, toast,
} from "../ui.js";

export async function loading(host, fn) {
  host.setAttribute("aria-busy", "true");
  render(host, el("span", { class: "sr-only" }, "Loading…"), skeletons());
  try {
    await fn();
  } catch (ex) {
    render(host, banner("error",
      el("p", {}, ex.message),
      el("button", { class: "btn small", type: "button", onclick: () => loading(host, fn) }, icon("refresh", 14), "Try again")));
  } finally {
    host.removeAttribute("aria-busy");
  }
}

// THE mutation helper: one toast per outcome, the server's `warning` surfaced as a warning
// (never a second, contradictory toast), the result handed to `onSuccess` for the flows that
// need it (a commit sha, a count). Every button that changes state goes through here.
export async function mutate(path, body, successMessage, onSuccess) {
  try {
    const result = await api.post(path, body);
    const message = typeof successMessage === "function" ? successMessage(result) : successMessage;
    if (result && result.warning) toast(`${message} — ${result.warning}`, "warn");
    else toast(message, "good");
    if (onSuccess) onSuccess(result);
    return result || true;
  } catch (ex) {
    toast(ex.message, "error");
    return false;
  }
}

// A `job_runs` row as the run strip draws it: outcome as a KEY role, duration, a one-line detail.
// `detail` is the only part the views differ on; `partial` is a real `job_runs` status (the
// gardener writes it) and reads as a human-amber bar everywhere.
export function runShape(run, detail) {
  const started = run.started_at ? new Date(run.started_at).getTime() : null;
  const finished = run.finished_at ? new Date(run.finished_at).getTime() : null;
  const tone = run.status === "ok" ? "git" : run.status === "partial" ? "human" : "fail";
  return { status: tone, label: run.status, when: fmtWhen(run.started_at),
    duration_s: started && finished ? Math.round((finished - started) / 1000) : null,
    detail: (detail ? detail(run) : "") || run.error || "" };
}

// The table twin every run strip ships with.
export function runTable(runs) {
  return { headers: ["when", "outcome", { text: "duration", cls: "num" }, "detail"],
    rows: runs.map((r) => ({ cells: [r.when, r.label, r.duration_s === null || r.duration_s === undefined ? "—" : `${r.duration_s}s`, r.detail || "—"] })) };
}

export function actorField() {
  return {
    name: "actor", label: "Acting as", value: getMeta().actor_default, required: true,
    hint: ACTOR_HINT,
  };
}

export function go(hash) {
  window.location.hash = `#/${hash}`;
}

export function rerender() {
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

// Who performed a trace event, in the key's terms: the librarian is the model's name here.
export function actorRole(actor) {
  return !actor || actor === "librarian" ? "model" : "human";
}

const EVENT_LABEL = {
  asked: "asked the submitter a question", replied: "the submitter replied",
  requeued: "sent back to the librarian", resolved: "resolved by hand", rejected: "declined",
};

// A capture's own history (`trace`), newest last, with the key's dot for who acted.
export function timeline(events, opts = {}) {
  if (!events || !events.length) {
    return el("div", { class: "empty" }, el("div", { class: "empty-title" }, opts.empty || "no human has touched this row"));
  }
  return el("ul", { class: "timeline" }, events.map((e) => {
    const role = actorRole(e.actor);
    return el("li", {},
      el("div", { class: `tl-dot k-${role}` }),
      el("div", { class: "what" },
        el("div", { class: "head" },
          el("strong", {}, EVENT_LABEL[e.event] || e.event),
          e.actor ? el("span", { class: "muted" }, `by ${e.actor}`) : null,
          el("span", { class: "when" }, fmtWhen(e.at))),
        e.note ? el("div", { class: "note" }, e.note) : null));
  }));
}

// The librarian's account of a capture (`report`), as a person reads it: the headline sentence
// first, then the facts that have a home — page, commit, anchor, links, rationale, findings.
export function reportPanel(row) {
  const report = row.report || {};
  const parts = [];
  if (report.summary) parts.push(el("p", { class: "lede" }, report.summary));
  const facts = [];
  if (report.page_path) facts.push(["page", el("span", { class: "row" }, mono(report.page_path), copyButton(report.page_path, ""))]);
  if (report.commit) facts.push(["commit", el("span", { class: "row" }, mono(String(report.commit).slice(0, 12)), copyButton(report.commit, ""))]);
  if (report.anchored_to) facts.push(["anchored to", mono(report.anchored_to)]);
  if (report.anchor_reason) facts.push(["why that anchor", report.anchor_reason]);
  if (Array.isArray(report.links_created) && report.links_created.length) {
    facts.push(["links created", el("ul", { class: "names" }, report.links_created.map((l) => el("li", {}, mono(String(l)))))]);
  }
  if (Array.isArray(report.pages_edited) && report.pages_edited.length) {
    facts.push(["pages edited", el("ul", { class: "names" }, report.pages_edited.map((l) => el("li", {}, mono(String(l)))))]);
  }
  if (Array.isArray(report.overlaps_flagged) && report.overlaps_flagged.length) {
    facts.push(["overlaps flagged", el("ul", { class: "names" }, report.overlaps_flagged.map((l) => el("li", {}, String(typeof l === "object" ? (l.path || JSON.stringify(l)) : l))))]);
  }
  if (Array.isArray(report.entities_born) && report.entities_born.length) {
    facts.push(["identities introduced", el("ul", { class: "names" }, report.entities_born.map((e) => el("li", {}, `${e.name || e.id} (${e.type || "entity"}) — `, mono(String(e.id)),
      e.confirmed_by ? ` · confirmed by ${e.confirmed_by}` : "")))]);
  }
  if (Array.isArray(report.aliases_added) && report.aliases_added.length) {
    facts.push(["spellings taught", el("ul", { class: "names" }, report.aliases_added.map((a) => el("li", {}, `«${a.alias}» for `, mono(String(a.entity)))))]);
  }
  if (Array.isArray(report.entities_updated) && report.entities_updated.length) {
    facts.push(["entity pages grown", el("ul", { class: "names" }, report.entities_updated.map((u) => el("li", {}, mono(String(u.entity)),
      ` · ${u.facts || 0} fact(s), ${u.connections || 0} connection(s)`)))]);
  }
  if (report.reason_code) facts.push(["refusal code", mono(report.reason_code)]);
  if (report.judged_type) facts.push(["judged type", mono(report.judged_type)]);
  if (Array.isArray(report.findings) && report.findings.length) {
    facts.push(["findings", el("ul", { class: "names" }, report.findings.map((f) => el("li", {}, String(typeof f === "object" ? (f.detail || f.message || JSON.stringify(f)) : f))))]);
  }
  if (facts.length) parts.push(el("dl", { class: "kv wide" }, facts.map(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])));
  if (report.agent_rationale) {
    parts.push(el("div", {},
      el("div", { class: "quote-label" }, keyDot("model"), "the agent's reading"),
      el("div", { class: "material" }, report.agent_rationale)));
  }
  if (!parts.length) return el("div", { class: "empty" }, el("div", { class: "empty-title" }, "the librarian has not reported on this capture yet"));
  return el("div", { class: "stack" }, ...parts);
}

// One sentence a row's state means for the person reading it.
export function statusSentence(row) {
  const s = statusCopy(row.status);
  const who = KEY[s.who];
  return el("div", { class: "row" }, statusPill(row.status), el("span", { class: "sub" }, `${s.explain}${who ? ` · ${who.label} decides here` : ""}`));
}

export function latencyLine(row) {
  const bits = [];
  if (row.queue_wait_ms !== null && row.queue_wait_ms !== undefined) bits.push(`waited ${fmtMs(row.queue_wait_ms)} for a worker`);
  if (row.total_latency_ms !== null && row.total_latency_ms !== undefined) bits.push(`${fmtMs(row.total_latency_ms)} from arrival to the end`);
  return bits.join(" · ");
}

export function materialPanel(row, opts = {}) {
  if (row.payload_purged) return el("em", { class: "muted" }, "payload purged by retention — the evidence blob is unaffected");
  if (row.withheld_reason) return el("em", { class: "muted" }, row.withheld_reason);
  if (!row.excerpt) return el("em", { class: "muted" }, "(no excerpt)");
  return el("div", { class: "material" }, opts.clip ? String(row.excerpt).slice(0, opts.clip) : row.excerpt);
}
