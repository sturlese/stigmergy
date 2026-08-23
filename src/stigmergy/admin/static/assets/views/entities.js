// The Entities desk: the vocabulary the brain has grown, and the one door for a name nobody has
// captured about yet. Nothing here is waiting on anybody — a capture that meets a name the
// registry does not know writes the entity page in the same commit that files it, confirmed by
// whoever captured. What is left to do from a console is READ the vocabulary, and
// commission the one entity no capture has introduced.

import { api } from "../api.js";
import { chartCard, hbars } from "../charts.js";
import { verdict as verdictCopy } from "../copy.js";
import { getMeta } from "../state.js";
import {
  card, chips, confirmForm, debounce, el, emptyState, icon, mono, relTime, render,
} from "../ui.js";
import { actorField, go, loading, mutate } from "./common.js";

const VERDICT_ICON = { registered: "check", collides: "x", similar: "alert", clear: "plus", unchecked: "help" };

export async function entitiesView(host) {
  await loading(host, async () => {
    const registry = await api.get("entities/registry")
      .catch((ex) => ({ error: ex.message, entities: [], by_type: {}, count: 0, available: false }));
    render(host,
      registryCard(registry),
      registry.available ? registryBrowser(registry) : null,
    );
  });
}

// ── the registry ──────────────────────────────────────────────────────────────────────────────
function registryCard(registry) {
  const types = Object.entries(registry.by_type || {}).sort((a, b) => b[1] - a[1]);
  return chartCard({
    title: "The registry this server serves",
    sub: registry.available
      ? `${registry.count} entities · ${registry.road === "snapshot" ? `the index's snapshot from ${registry.source || "an unrecorded sha"}, refreshed ${relTime(registry.refreshed_at)}` : "this server's own --entity-registry file"}`
      : (registry.error || "no registry is readable here — every name shows as Could not check, and the birth gate still runs at push time"),
    chart: types.length ? hbars({ rows: types.map(([type, n]) => ({ label: type, value: n })), color: "accent", labelWidth: 110 }) : el("div", { class: "chart-empty" }, "no entities registered yet"),
    tableSpec: { headers: ["type", "entities"], rows: types.map(([t, n]) => ({ cells: [t, String(n)] })) },
    actions: [el("button", { class: "btn small", type: "button", onclick: () => createFlow() }, icon("plus", 14), "Register an entity")],
  });
}

const browser = { query: "", type: "" };

function registryBrowser(registry) {
  const listHost = el("div", { class: "registry-list" });
  const draw = () => {
    const q = browser.query.trim().toLowerCase();
    const rows = registry.entities.filter((e) => (!browser.type || e.type === browser.type)
      && (!q || e.name.toLowerCase().includes(q) || e.id.includes(q) || e.aliases.some((a) => a.toLowerCase().includes(q))));
    render(listHost, rows.length ? rows.map((e) => registryItem(e, q)) : emptyState("no registered entity matches", "a name nobody has captured about is what Register an entity is for"));
  };
  const input = el("input", { type: "search", placeholder: "search names, aliases, ids…", value: browser.query,
    oninput: debounce((e) => { browser.query = e.target.value; draw(); }, 120) });
  draw();
  return card({ title: "Browse the registry", sub: "every registered entity with the spellings it answers to — the vocabulary captures anchor to. Each one is here because a capture introduced it, or because somebody registered it from this page." },
    chips([{ key: "", label: "all types", count: registry.count, on: !browser.type },
      ...Object.entries(registry.by_type || {}).map(([t, n]) => ({ key: t, label: t, count: n, on: browser.type === t }))],
    (key) => { browser.type = key; draw(); }, { trailing: [el("span", { class: "sep" }), el("span", { class: "search" }, icon("search"), input)] }),
    listHost);
}

function registryItem(e, q) {
  return el("div", { class: `registry-item${q && e.name.toLowerCase().includes(q) ? " hit" : ""}` },
    el("div", { class: "rname" }, e.name, " ", el("span", { class: "muted" }, e.type)),
    el("div", { class: "rmeta" }, mono(e.id), e.aliases.length ? ` · also ${e.aliases.join(", ")}` : "",
      e.approved_by ? ` · introduced by ${e.approved_by}` : ""));
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
    : check.verdict === "registered" ? `${what} already resolves to${detail} — there is nothing to register`
    : check.verdict === "similar" ? `${what} looks similar to ${similar} — make sure it is not the same thing`
    : check.verdict === "clear" ? `${what} — nothing like it is registered`
    : `${what} — could not be checked here`;
  return el("div", { class: `verdict tone-${v.tone}` }, icon(VERDICT_ICON[check.verdict] || "info", 15), el("span", {}, text));
}
