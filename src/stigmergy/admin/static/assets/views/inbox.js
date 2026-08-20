// The inbox: every item parking on a human, oldest first, one list across the three kinds. The
// same read the Slack doorbell rings from.

import { api } from "../api.js";
import { door, itemKind, repairKind, situation as situationCopy, status as statusCopy, word } from "../copy.js";
import { banner, chips, clickable, el, emptyState, fmtAge, fmtWhen, keyDot, mono, pill, render, statusPill, wordPill } from "../ui.js";
import { go, loading } from "./common.js";

const FILTERS = [
  { key: "all", label: "Everything", explain: "every item owing a person a decision, oldest first" },
  { key: "entity", label: "Identity decisions", kind: "entity-proposal", who: "human" },
  { key: "parked", label: "Parked captures", kind: "parked-capture", who: "human" },
  { key: "submitter", label: "— of those, on their submitter", who: "human",
    explain: "parked captures whose one question is still waiting for the submitter's answer; a steward can answer too, through the MCP tool the capture names" },
  { key: "repair", label: "Repair proposals", kind: "repair-proposal", who: "model" },
];

function explainFilter(f) {
  return f.explain || (f.kind ? itemKind(f.kind).explain : "");
}
const state = { filter: "all" };

export async function inboxView(host, filterKey) {
  if (filterKey) state.filter = filterKey;
  const raw = window.location.hash.replace(/^#\/?/, "");
  const sub = raw.split("/")[1];
  if (sub && FILTERS.some((f) => f.key === sub)) state.filter = sub;
  await loading(host, async () => {
    const inbox = await api.get("inbox");
    const items = [...inbox.items].sort((a, b) => (b.parked_age_ms || 0) - (a.parked_age_ms || 0) || (a.created_at || "").localeCompare(b.created_at || ""));
    const counts = {
      all: items.length,
      entity: inbox.counts["entity-proposal"] || 0,
      parked: inbox.counts["parked-capture"] || 0,
      submitter: inbox.waiting_on_submitter || 0,
      repair: inbox.counts["repair-proposal"] || 0,
    };
    const visible = items.filter((i) => matches(i, state.filter));
    const current = FILTERS.find((f) => f.key === state.filter) || FILTERS[0];
    render(host,
      el("section", { class: "card" },
        chips(FILTERS.map((f) => ({ key: f.key, label: f.label, count: counts[f.key], on: state.filter === f.key, who: f.who, title: explainFilter(f) })),
          (key) => { state.filter = key; window.location.hash = `#/inbox${key === "all" ? "" : `/${key}`}`; }),
        el("div", { class: "sub", style: { marginBottom: "10px" } }, explainFilter(current)),
        inbox.truncated ? banner("warn", `showing the first ${inbox.limit} items — more are waiting than this list can carry`) : null,
        visible.length
          ? el("div", { class: "inbox-list" }, visible.map(inboxRow))
          : emptyState(state.filter === "all" ? "nothing is waiting on a person" : "nothing of this kind is waiting",
            state.filter === "all" ? "a permanent zero would mean nobody is capturing anything — check Captures" : "")));
  });
}

function clip(text, n) {
  const s = String(text);
  return s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s;
}

function matches(item, filter) {
  if (filter === "all") return true;
  if (filter === "submitter") return item.kind === "parked-capture" && item.status === "needs_input";
  const f = FILTERS.find((x) => x.key === filter);
  return f && item.kind === f.kind;
}

function inboxRow(item) {
  const kind = itemKind(item.kind);
  const target = item.kind === "entity-proposal" ? `entities/${item.id}`
    : item.kind === "parked-capture" ? `captures/${item.id}` : `repairs/${item.id}`;
  let title, meta;
  if (item.kind === "entity-proposal") {
    const names = item.subjects && item.subjects.length ? item.subjects : [item.subject || "(nothing recorded)"];
    title = el("span", {}, names.length > 1 ? `${names.length} names to place: ` : "Who or what is ", ...names.flatMap((n, i) => [i ? ", " : "", el("strong", {}, `«${n}»`)]), names.length > 1 ? "" : "?");
    meta = [situationCopy(item.situation).label, `sent by ${item.submitted_by}`];
  } else if (item.kind === "parked-capture") {
    title = el("span", { title: item.summary || "" }, clip(item.summary || "(no summary recorded)", 180));
    meta = [statusCopy(item.status).label, `sent by ${item.submitted_by}`];
  } else {
    title = el("span", { title: item.rationale || "" }, clip(item.rationale || "(no rationale recorded)", 180));
    const kinds = (item.ops_preview && item.ops_preview.kinds) || [];
    meta = [`${(item.ops_preview && item.ops_preview.count) || 0} op(s) · ${kinds.join(", ") || repairKind("").label}`,
      ...(item.target_paths || []).slice(0, 2).map((p) => p)];
    if (item.merge) meta.push(`merge: ${item.merge.absorbed || "?"} → ${item.merge.survivor || "?"}`);
  }
  return clickable(el("div", { class: "inbox-row" },
    el("div", { class: `stripe k-${kind.who}` }),
    el("div", {},
      el("div", { class: "row" }, pill(kind.label, kind.who, { small: true }), item.kind === "parked-capture" ? statusPill(item.status, { small: true, short: true }) : null,
        item.decision ? wordPill(item.decision.verdict, { small: true }) : null),
      el("div", { class: "title" }, title),
      el("div", { class: "meta" }, meta.map((m) => el("span", {}, m)))),
    el("div", { class: "side" },
      el("span", {}, item.parked_age_ms ? `waiting ${fmtAge(item.parked_age_ms)}` : (item.created_at ? `since ${fmtWhen(item.created_at)}` : "")),
      el("span", {}, mono(`#${item.id}`, "nowrap")),
      item.decision ? el("span", { class: "row" }, keyDot("human", 6), `${word(item.decision.verdict).label} by ${item.decision.actor} via ${door(item.decision.source)}`) : null)),
  () => go(target));
}
