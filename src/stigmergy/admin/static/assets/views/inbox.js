// The inbox: everything waiting on a steward, one list across the three kinds — the identities
// and spellings the librarian proposed, and the nightly repairs. The same read the Slack doorbell
// rings from.

import { api } from "../api.js";
import { door, itemKind, repairKind, word } from "../copy.js";
import { banner, chips, clickable, el, emptyState, fmtWhen, keyDot, mono, pill, render, wordPill } from "../ui.js";
import { go, loading } from "./common.js";

const FILTERS = [
  { key: "all", label: "Everything", explain: "every item owing a steward a decision" },
  { key: "identity", label: "Proposed entities", kind: "identity-proposal", who: "model" },
  { key: "alias", label: "Proposed spellings", kind: "alias-proposal", who: "model" },
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
    const items = [...inbox.items];
    const counts = {
      all: items.length,
      identity: inbox.counts["identity-proposal"] || 0,
      alias: inbox.counts["alias-proposal"] || 0,
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
          : emptyState(state.filter === "all" ? "nothing is waiting on a steward" : "nothing of this kind is waiting",
            state.filter === "all" ? "every capture files on its own; a proposal appears here only when the librarian met a name the registry did not know" : "")));
  });
}

function clip(text, n) {
  const s = String(text);
  return s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s;
}

function matches(item, filter) {
  if (filter === "all") return true;
  const f = FILTERS.find((x) => x.key === filter);
  return f && item.kind === f.kind;
}

function inboxRow(item) {
  const kind = itemKind(item.kind);
  const target = item.kind === "identity-proposal" ? `entities/${item.id}`
    : item.kind === "alias-proposal" ? "entities" : `repairs/${item.id}`;
  let title, meta;
  if (item.kind === "identity-proposal") {
    title = el("span", {}, el("strong", {}, item.name || item.id), el("span", { class: "muted" }, ` · ${item.entity_type || "entity"}`));
    meta = [clip(item.summary || "(no summary on the page yet)", 180)];
    if (item.anchored_pages && item.anchored_pages.length) meta.push(`filed against it: ${item.anchored_pages.length}${item.anchored_total > item.anchored_pages.length ? "+" : ""} page(s)`);
    if (item.merge_candidates && item.merge_candidates.length) meta.push(`might be: ${item.merge_candidates.map((c) => c.name).join(", ")}`);
  } else if (item.kind === "alias-proposal") {
    title = el("span", {}, "«", el("strong", {}, item.alias), "» as a spelling of ", el("strong", {}, item.entity_name || item.entity_id));
    meta = ["the librarian met it in a capture and anchored the page to the registered entity"];
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
      el("div", { class: "row" }, pill(kind.label, kind.who, { small: true }),
        item.decision ? wordPill(item.decision.verdict, { small: true }) : null),
      el("div", { class: "title" }, title),
      el("div", { class: "meta" }, meta.map((m) => el("span", {}, m)))),
    el("div", { class: "side" },
      el("span", {}, item.created ? `since ${item.created}` : (item.created_at ? `since ${fmtWhen(item.created_at)}` : "")),
      el("span", {}, mono(item.id, "nowrap")),
      item.decision ? el("span", { class: "row" }, keyDot("human", 6), `${word(item.decision.verdict).label} by ${item.decision.actor} via ${door(item.decision.source)}`) : null)),
  () => go(target));
}
