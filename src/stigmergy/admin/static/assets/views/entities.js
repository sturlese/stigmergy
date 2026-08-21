// The Entities desk: what the librarian proposed and a steward confirms, merges or declines —
// each already a page in the brain — plus the registry itself, browsable, and the one door for
// registering an entity nobody has captured about yet.

import { api } from "../api.js";
import { chartCard, hbars } from "../charts.js";
import { verdict as verdictCopy } from "../copy.js";
import { getMeta } from "../state.js";
import {
  card, chips, clickable, confirmForm, debounce, el, emptyState, icon, keyDot, kv, link, mono,
  pill, relTime, render,
} from "../ui.js";
import { actorField, go, loading, mutate } from "./common.js";

const VERDICT_ICON = { registered: "check", collides: "x", similar: "alert", clear: "plus", unchecked: "help" };

// ── the list ──────────────────────────────────────────────────────────────────────────────────
export async function entitiesView(host) {
  await loading(host, async () => {
    const [data, registry] = await Promise.all([api.get("entities"), api.get("entities/registry").catch((ex) => ({ error: ex.message, entities: [], by_type: {}, count: 0, available: false }))]);
    const proposals = data.proposals || [];
    const aliases = data.aliases || [];
    render(host,
      el("div", { class: "grid two-one" },
        el("section", { class: "card" },
          el("div", { class: "card-head" },
            el("div", { class: "card-title" },
              el("h2", {}, proposals.length ? `${proposals.length} proposed entit${proposals.length === 1 ? "y" : "ies"}` : "No proposed entity is waiting"),
              el("div", { class: "sub" }, "each one the librarian created unconfirmed while filing a capture — the page exists and search finds it; you confirm the identity, say which registered entity it really is, or decline it")),
            el("div", { class: "spacer" }),
            el("button", { class: "btn small", type: "button", onclick: () => createFlow() }, icon("plus", 14), "Register an entity")),
          proposals.length
            ? el("div", { class: "inbox-list" }, proposals.map(proposalRow))
            : emptyState("every capture files on its own — a proposal appears here only when the librarian met a name the registry did not know", "nothing is waiting right now"),
          aliases.length ? el("div", { style: { marginTop: "16px" } },
            el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, `${aliases.length} proposed spelling${aliases.length === 1 ? "" : "s"}`),
              el("div", { class: "sub" }, "a name the material used for a registered entity, which the registry did not list — it resolves already; confirming keeps it, declining drops it"))),
            el("div", { class: "inbox-list" }, aliases.map(aliasRow))) : null),
        registryCard(registry, { compact: true })),
      registry.available ? registryBrowser(registry) : null,
    );
  });
}

function proposalRow(item) {
  const check = item.check || null;
  const v = check ? verdictCopy(check.verdict) : null;
  return clickable(el("div", { class: "inbox-row" },
    el("div", { class: "stripe k-model" }),
    el("div", {},
      el("div", { class: "row" }, pill("proposed · unconfirmed", "model", { small: true }), pill(item.entity_type || "entity", "code", { small: true }),
        v && check.verdict !== "clear" && check.verdict !== "unchecked" ? pill(v.label, v.tone, { small: true }) : null),
      el("div", { class: "title" }, el("strong", {}, item.name || item.id), item.aliases && item.aliases.length ? el("span", { class: "muted" }, ` · also ${item.aliases.join(", ")}`) : null),
      el("div", { class: "meta" },
        el("span", {}, item.summary ? clipText(item.summary, 160) : "(no summary on the page yet)"),
        item.anchored_pages && item.anchored_pages.length ? el("span", {}, `filed against it: ${item.anchored_pages.map((p) => p.split("/").pop().replace(/\.md$/, "")).slice(0, 2).join(", ")}${item.anchored_total > 2 ? ` +${item.anchored_total - 2}` : ""}`) : null)),
    el("div", { class: "side" },
      el("div", { class: "row" }, ...proposalButtons(item)),
      el("span", {}, mono(item.id, "nowrap")))),
  () => go(`entities/${item.id}`));
}

function clipText(text, n) {
  const s = String(text);
  return s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s;
}

function proposalButtons(item, opts = {}) {
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  return [
    el("button", { class: "btn small primary", type: "button", onclick: stop(() => approveFlow(item)) }, icon("check", 14), "Approve"),
    el("button", { class: "btn small", type: "button", onclick: stop(() => mergeFlow(item)) }, icon("branch", 14), "Merge into…"),
    el("button", { class: "btn small danger", type: "button", onclick: stop(() => declineFlow(item, opts)) }, icon("x", 14), "Decline"),
  ];
}

function aliasRow(item) {
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  return el("div", { class: "inbox-row" },
    el("div", { class: "stripe k-model" }),
    el("div", {},
      el("div", { class: "row" }, pill("proposed spelling", "model", { small: true })),
      el("div", { class: "title" }, "«", el("strong", {}, item.alias), "» for ", el("strong", {}, item.entity_name || item.entity_id)),
      el("div", { class: "meta" }, el("span", {}, mono(item.entity_id)))),
    el("div", { class: "side" },
      el("div", { class: "row" },
        el("button", { class: "btn small primary", type: "button", onclick: stop(() => aliasDecide(item, "approve")) }, icon("check", 14), "Approve"),
        el("button", { class: "btn small danger", type: "button", onclick: stop(() => aliasDecide(item, "decline")) }, icon("x", 14), "Decline"))));
}

// ── the registry ──────────────────────────────────────────────────────────────────────────────
function registryCard(registry, opts = {}) {
  const types = Object.entries(registry.by_type || {}).sort((a, b) => b[1] - a[1]);
  return chartCard({
    title: "The registry this server serves",
    sub: registry.available
      ? `${registry.count} entities · ${registry.road === "snapshot" ? `the index's snapshot from ${registry.source || "an unrecorded sha"}, refreshed ${relTime(registry.refreshed_at)}` : "this server's own --entity-registry file"}`
      : (registry.error || "no registry is readable here — every name shows as Could not check, and the birth gate still runs at push time"),
    chart: types.length ? hbars({ rows: types.map(([type, n]) => ({ label: type, value: n })), color: "accent", labelWidth: 110 }) : el("div", { class: "chart-empty" }, "no entities registered yet"),
    tableSpec: { headers: ["type", "entities"], rows: types.map(([t, n]) => ({ cells: [t, String(n)] })) },
    cls: opts.compact ? "tight" : "",
  });
}

const browser = { query: "", type: "" };

function registryBrowser(registry) {
  const listHost = el("div", { class: "registry-list" });
  const draw = () => {
    const q = browser.query.trim().toLowerCase();
    const rows = registry.entities.filter((e) => (!browser.type || e.type === browser.type)
      && (!q || e.name.toLowerCase().includes(q) || e.id.includes(q) || e.aliases.some((a) => a.toLowerCase().includes(q))));
    render(listHost, rows.length ? rows.map((e) => registryItem(e, q)) : emptyState("no registered entity matches", "a name nobody has registered is what Register an entity is for"));
  };
  const input = el("input", { type: "search", placeholder: "search names, aliases, ids…", value: browser.query,
    oninput: debounce((e) => { browser.query = e.target.value; draw(); }, 120) });
  draw();
  return card({ title: "Browse the registry", sub: "every registered entity with its aliases — the vocabulary captures anchor to. A proposed one is marked until a steward confirms it." },
    chips([{ key: "", label: "all types", count: registry.count, on: !browser.type },
      ...Object.entries(registry.by_type || {}).map(([t, n]) => ({ key: t, label: t, count: n, on: browser.type === t }))],
    (key) => { browser.type = key; draw(); }, { trailing: [el("span", { class: "sep" }), el("span", { class: "search" }, icon("search"), input)] }),
    listHost);
}

function registryItem(e, q) {
  return el("div", { class: `registry-item${q && e.name.toLowerCase().includes(q) ? " hit" : ""}` },
    el("div", { class: "rname" }, e.name, " ", el("span", { class: "muted" }, e.type), e.proposed ? el("span", {}, " ", pill("proposed", "model", { small: true })) : null),
    el("div", { class: "rmeta" }, mono(e.id), e.aliases.length ? ` · also ${e.aliases.join(", ")}` : "",
      e.proposed_aliases && e.proposed_aliases.length ? ` · proposed: ${e.proposed_aliases.join(", ")}` : ""));
}

// ── the detail: one proposal, what the librarian wrote, what it might be ──────────────────────
export async function entityDetailView(host, id) {
  await loading(host, async () => {
    const item = await api.get(`entities/${id}`);
    const check = item.check || { verdict: "unchecked", similar: [], match: null };
    const v = verdictCopy(check.verdict);
    const candidates = item.merge_candidates || [];
    render(host,
      el("div", { class: "crumbs" }, link("entities", "Entities"), icon("chevron"), el("span", {}, item.name || item.id)),
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" },
            el("h2", {}, item.name || item.id, " ", el("span", { class: "sub" }, item.entity_type || "entity")),
            el("div", { class: "sub" }, "proposed by the librarian while filing — the page is in the brain and search finds it; nothing waits on anybody but you")),
          el("div", { class: "spacer" }),
          el("div", { class: "row" }, ...proposalButtons(item, { stay: true }))),
        kv([
          ["page", item.page ? el("span", { class: "row" }, mono(item.page)) : el("span", { class: "muted" }, "not indexed yet — the next rebuild or push refresh will show it")],
          ["aliases", item.aliases && item.aliases.length ? item.aliases.join(", ") : el("span", { class: "muted" }, "none")],
          ["filed against it", item.anchored_pages && item.anchored_pages.length
            ? el("ul", { class: "names" }, item.anchored_pages.map((p) => el("li", {}, mono(p))))
            : el("span", { class: "muted" }, "nothing indexed yet")],
          ["proposed", item.created || "—"],
          ["ledger", item.decision ? `${item.decision.verdict} by ${item.decision.actor} via ${item.decision.source}` : "no decision yet"],
        ], { wide: true })),
      el("div", { class: "grid two-one" },
        el("section", { class: "card" },
          el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "What the librarian wrote"), el("div", { class: "sub" }, "the page's own What / Who paragraph — a steward decides on this"))),
          el("div", { class: "material" }, item.summary || "(the page carries no summary yet)")),
        el("section", { class: "card" },
          el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Is it really something else?"), el("div", { class: "sub" }, "the birth gate's own check of the proposed name against the rest of the registry"))),
          el("div", { class: `verdict tone-${v.tone}` }, icon(VERDICT_ICON[check.verdict] || "info", 16),
            el("div", {}, el("div", { class: "v-title" }, v.label, check.match ? el("span", { class: "muted" }, ` — ${check.match.name} (${check.match.type})`) : ""),
              el("div", {}, v.explain))),
          candidates.length ? el("div", { style: { marginTop: "10px" } },
            el("div", { class: "quote-label" }, keyDot("code"), "shares a word with"),
            el("div", { class: "row" }, ...candidates.map((c) => el("span", { class: "entity-chip" }, el("strong", {}, c.name), el("span", { class: "type" }, c.id))))) : null,
          el("div", { class: "row", style: { marginTop: "10px" } },
            el("button", { class: "btn small", type: "button", onclick: () => mergeFlow(item) }, icon("branch", 14), "Merge into…")))),
    );
  });
}

// ── the three decisions, each saying what it will do ──────────────────────────────────────────
async function approveFlow(item) {
  const answer = await confirmForm({
    title: `Approve ${item.name || item.id}`,
    consequence: `confirms the identity: ONE commit to the knowledge repo (authored by the librarian App, Decided-by you) stamps your name on ${mono(item.page || `wiki/entities/${item.name}.md`).textContent} and the registry stops calling it proposed. The page and everything filed against it stay exactly as they are.`,
    fields: [actorField()],
    confirmLabel: "Approve",
  });
  if (answer && await mutate("entities/decide", { ...answer.values, item_kind: "identity-proposal", item_id: item.id, verdict: "approve" },
    (r) => `approved ${item.name || item.id} — commit ${String(r.commit || "").slice(0, 12) || "?"}`)) go("entities");
}

async function mergeFlow(item) {
  const candidates = item.merge_candidates || [];
  const fields = [actorField()];
  if (candidates.length) {
    fields.push({ name: "into_pick", label: "It is really this registered entity", kind: "select",
      options: candidates.map((c) => c.id), hint: `the registered entities sharing a word with the proposal: ${candidates.map((c) => `${c.name} (${c.id})`).join(", ")}` });
  }
  fields.push({ name: "into_typed", label: candidates.length ? "Or type its registry id" : "The registered entity's id", hint: "as the registry browser shows it, e.g. acme-corp", required: !candidates.length, live: liveIdCheck });
  const answer = await confirmForm({
    title: `Merge ${item.name || item.id} into a registered entity`,
    consequence: "folds the proposal into an entity that already exists: its name and every spelling it carried become that entity's aliases, its page is removed, and every page filed against it is re-anchored to the survivor — ONE commit, Decided-by you. A merge cannot be undone from here.",
    fields,
    confirmLabel: "Merge",
  });
  if (!answer) return;
  const into = (answer.values.into_typed || "").trim() || answer.values.into_pick || "";
  if (!into) return;
  if (await mutate("entities/decide", { actor: answer.values.actor, item_kind: "identity-proposal", item_id: item.id, verdict: "merge", into },
    (r) => `merged ${item.name || item.id} into ${into} — commit ${String(r.commit || "").slice(0, 12) || "?"}${r.reanchored && r.reanchored.length ? `; ${r.reanchored.length} page(s) re-anchored` : ""}`)) go("entities");
}

async function liveIdCheck(value, setNote) {
  const id = value.trim();
  if (!id) { setNote(null); return; }
  try {
    const registry = await api.get("entities/registry");
    const hit = (registry.entities || []).find((e) => e.id === id);
    setNote(el("div", { class: `verdict tone-${hit ? (hit.proposed ? "fail" : "git") : "fail"}` }, icon(hit && !hit.proposed ? "check" : "x", 15),
      el("span", {}, hit ? (hit.proposed ? `${hit.name} is itself a proposal — confirm it first, or merge both into the entity they are` : `${hit.name} (${hit.type}) — a registered entity`) : `no registered entity has the id ${id}`)));
  } catch (ex) {
    setNote(el("div", { class: "banner plain" }, `could not check the registry: ${ex.message}`));
  }
}

async function declineFlow(item, opts = {}) {
  const answer = await confirmForm({
    title: `Decline ${item.name || item.id}`,
    consequence: "removes the proposed page from the knowledge repo; every page filed against it loses that anchor (the pages themselves stay). The ledger remembers the decline, so the librarian never proposes this identity again — ONE commit, Decided-by you.",
    fields: [actorField(), { name: "notes", label: "Why (optional)", kind: "textarea", hint: "for the ledger — a steward reading the decision later" }],
    confirmLabel: "Decline", danger: true,
  });
  if (answer && await mutate("entities/decide", { ...answer.values, item_kind: "identity-proposal", item_id: item.id, verdict: "decline" },
    (r) => `declined ${item.name || item.id} — commit ${String(r.commit || "").slice(0, 12) || "?"}`)) go("entities");
}

async function aliasDecide(item, verdict) {
  const approve = verdict === "approve";
  const answer = await confirmForm({
    title: `${approve ? "Approve" : "Decline"} «${item.alias}» for ${item.entity_name || item.entity_id}`,
    consequence: approve
      ? "confirms the spelling: it moves onto the entity's own aliases list in ONE commit, Decided-by you, and keeps resolving."
      : "drops the spelling from the entity's page in ONE commit, Decided-by you; captures using it will be judged again by the librarian next time.",
    fields: [actorField()],
    confirmLabel: approve ? "Approve" : "Decline", danger: !approve,
  });
  if (answer && await mutate("entities/decide", { ...answer.values, item_kind: "alias-proposal", item_id: item.id, verdict },
    (r) => `${approve ? "approved" : "declined"} «${item.alias}» — commit ${String(r.commit || "").slice(0, 12) || "?"}`)) go("entities");
}

// ── registering an entity nobody captured about yet ───────────────────────────────────────────
async function createFlow() {
  const meta = getMeta();
  const sequence = { name: 0, aliases: 0 };
  const liveCheck = (label) => async (value, setNote) => {
    const candidates = label === "aliases" ? value.split(",").map((s) => s.trim()).filter(Boolean) : [value.trim()];
    const ticket = ++sequence[label];
    if (!candidates.length || !candidates[0]) { setNote(null); return; }
    try {
      const result = await api.post("entities/resolve", { names: candidates });
      if (ticket !== sequence[label]) return;
      setNote(el("div", { class: "stack" }, result.checks.map((c) => liveVerdict(c, label))));
    } catch (ex) {
      if (ticket !== sequence[label]) return;
      setNote(el("div", { class: "banner plain" }, `could not check the registry: ${ex.message}`));
    }
  };
  const answer = await confirmForm({
    title: "Register an entity",
    consequence: "commissions the entity: what you write below is queued as a capture, the librarian writes the page from it and from what the brain already holds, anchors the note to it, and the entity is born confirmed by you — one commit, a few minutes from now. Cancelling after this point cannot undo it; the gates re-check collisions against the repo as it stands.",
    wide: true,
    fields: [
      actorField(),
      { name: "name", label: "Name", required: true, hint: "the entity's page title, filename and wikilink target — checked live against the registry", live: liveCheck("name") },
      { name: "entity_type", label: "Type", kind: "select", options: meta.entity_types, required: true },
      { name: "aliases", label: "Aliases (optional, comma-separated)", hint: "other spellings captures use for it — each one is checked too, because an alias that collides is refused like a name", live: liveCheck("aliases") },
      { name: "about", label: "What is it?", kind: "textarea", required: true, hint: "in your own words, everything you know: what it is, what it does, who is behind it, how it relates to what the brain already holds. The librarian writes the page from this — a page with nothing said about the entity is not written at all" },
    ],
    confirmLabel: "Register",
  });
  if (!answer) return;
  const ack = await mutate("entities/create", answer.values,
    (r) => `commissioned as capture #${r.id} — the librarian is writing ${r.name}'s page; it appears here when the capture files`);
  if (ack && ack.id) go(`captures/${ack.id}`);
}

function liveVerdict(check, label) {
  const v = verdictCopy(check.verdict);
  const what = label === "aliases" ? `alias «${check.name}»` : `«${check.name}»`;
  const detail = check.match ? ` ${check.match.name} (${check.match.type}${check.match.aliases.length ? `, aka ${check.match.aliases.join(", ")}` : ""})` : "";
  const similar = (check.similar || []).map((s) => s.name).join(", ");
  const text = check.verdict === "collides" ? `${what} would collide with${detail} — the gate will refuse it`
    : check.verdict === "registered" ? `${what} already resolves to${detail} — there is nothing to create`
    : check.verdict === "similar" ? `${what} looks similar to ${similar} — make sure it is not the same thing`
    : check.verdict === "clear" ? `${what} — nothing like it is registered`
    : `${what} — could not be checked here`;
  return el("div", { class: `verdict tone-${v.tone}` }, icon(VERDICT_ICON[check.verdict] || "info", 15), el("span", {}, text));
}
