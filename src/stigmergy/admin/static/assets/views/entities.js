// The Entities desk: identity decisions, each name checked against the registry this server
// serves BEFORE the form opens — and the registry itself, browsable, so "is this already one of
// ours?" is answered by looking rather than by minting a twin.

import { api } from "../api.js";
import { chartCard, hbars } from "../charts.js";
import { situation as situationCopy, verdict as verdictCopy } from "../copy.js";
import { getMeta } from "../state.js";
import {
  banner, card, chips, clickable, confirmForm, copyButton, debounce, el, emptyState, fmtAge, fmtWhen,
  icon, keyDot, kv, link, mono, pill, relTime, render, statusPill,
} from "../ui.js";
import { actorField, go, loading, materialPanel, mutate, timeline } from "./common.js";

const VERDICT_ICON = { registered: "check", collides: "x", similar: "alert", clear: "plus", unchecked: "help" };

// ── the list ──────────────────────────────────────────────────────────────────────────────────
export async function entitiesView(host) {
  await loading(host, async () => {
    const [data, registry] = await Promise.all([api.get("entities"), api.get("entities/registry").catch((ex) => ({ error: ex.message, entities: [], by_type: {}, count: 0, available: false }))]);
    const situations = data.situations;
    const summary = { collides: 0, registered: 0, similar: 0, clear: 0, unchecked: 0 };
    for (const row of situations) for (const c of row.checks || []) summary[c.verdict] = (summary[c.verdict] || 0) + 1;
    render(host,
      el("div", { class: "grid two-one" },
        el("section", { class: "card" },
          el("div", { class: "card-head" },
            el("div", { class: "card-title" },
              el("h2", {}, situations.length ? `${situations.length} identity decision(s) waiting` : "No identity decision is waiting"),
              el("div", { class: "sub" }, "each row parked because the librarian met a name the registry does not know — or a page type it does not file"))),
          situations.length ? el("div", { class: "row", style: { marginBottom: "12px" } },
            summary.clear ? pill(`${summary.clear} clear of the registry`, "accent") : null,
            summary.collides ? pill(`${summary.collides} would collide`, "fail") : null,
            summary.registered ? pill(`${summary.registered} now registered — requeue`, "git") : null,
            summary.similar ? pill(`${summary.similar} look similar`, "human") : null) : null,
          situations.length
            ? el("div", { class: "inbox-list" }, situations.map(situationRow))
            : emptyState("when the librarian meets a name it cannot place, it asks the submitter once and then parks the capture here", "nothing is parked right now")),
        registryCard(registry, { compact: true })),
      registry.available ? registryBrowser(registry) : null,
    );
  });
}

function situationRow(row) {
  const target = `entities/${row.id}`;
  const names = row.subjects && row.subjects.length ? row.subjects : [];
  const sit = situationCopy(row.situation);
  return clickable(el("div", { class: "inbox-row" },
    el("div", { class: "stripe k-human" }),
    el("div", {},
      el("div", { class: "row" }, pill(sit.label, "human", { small: true }), row.asked_at ? pill("submitter already asked", "code", { small: true }) : null),
      el("div", { class: "title" }, names.length
        ? el("span", {}, ...names.flatMap((n, i) => [i ? "  " : "", nameChip(n, (row.checks || []).find((c) => c.name === n))]))
        : el("span", {}, row.subject ? `page type: ${row.subject}` : "(nothing recorded)")),
      el("div", { class: "meta" }, el("span", {}, `sent by ${row.submitted_by}`),
        row.report && row.report.agent_rationale ? el("span", { class: "dim", title: row.report.agent_rationale }, String(row.report.agent_rationale).length > 140 ? `${String(row.report.agent_rationale).slice(0, 139).trimEnd()}…` : row.report.agent_rationale) : null)),
    el("div", { class: "side" }, el("span", {}, `waiting ${fmtAge(row.parked_age_ms)}`), el("span", {}, mono(`#${row.id}`, "nowrap")))),
  () => go(target));
}

function nameChip(name, check) {
  const v = check ? verdictCopy(check.verdict) : null;
  return el("span", { class: "entity-chip", title: v ? `${v.label} — ${v.explain}` : "" },
    v ? keyDot(v.tone, 7) : null, el("strong", {}, name),
    check && check.match ? el("span", { class: "type" }, `= ${check.match.name}`) : null);
}

// ── the registry ──────────────────────────────────────────────────────────────────────────────
function registryCard(registry, opts = {}) {
  const types = Object.entries(registry.by_type || {}).sort((a, b) => b[1] - a[1]);
  return chartCard({
    title: "The registry this server serves",
    sub: registry.available
      ? `${registry.count} entities · ${registry.road === "snapshot" ? `the index's snapshot from ${registry.source || "an unrecorded sha"}, refreshed ${relTime(registry.refreshed_at)}` : "this server's own --entity-registry file"}`
      : (registry.error || "no registry is readable here — every name shows as Could not check, and the mint gate still runs at push time"),
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
    render(listHost, rows.length ? rows.map((e) => registryItem(e, q)) : emptyState("no registered entity matches", "a name nobody has registered is exactly what the mint form is for"));
  };
  const input = el("input", { type: "search", placeholder: "search names, aliases, ids…", value: browser.query,
    oninput: debounce((e) => { browser.query = e.target.value; draw(); }, 120) });
  draw();
  return card({ title: "Browse the registry", sub: "every registered entity with its aliases — the vocabulary captures anchor to. Searching here costs nothing; minting a twin costs a merge." },
    chips([{ key: "", label: "all types", count: registry.count, on: !browser.type },
      ...Object.entries(registry.by_type || {}).map(([t, n]) => ({ key: t, label: t, count: n, on: browser.type === t }))],
    (key) => { browser.type = key; draw(); }, { trailing: [el("span", { class: "sep" }), el("span", { class: "search" }, icon("search"), input)] }),
    listHost);
}

function registryItem(e, q) {
  return el("div", { class: `registry-item${q && e.name.toLowerCase().includes(q) ? " hit" : ""}` },
    el("div", { class: "rname" }, e.name, " ", el("span", { class: "muted" }, e.type)),
    el("div", { class: "rmeta" }, mono(e.id), e.aliases.length ? ` · also ${e.aliases.join(", ")}` : ""));
}

// ── the detail: one capture, its names, each checked ──────────────────────────────────────────
export async function entityDetailView(host, id) {
  await loading(host, async () => {
    const row = await api.get(`entities/${id}`);
    const report = row.report || {};
    const sit = situationCopy(row.situation);
    const names = (row.subjects || []).map((n) => String(n)).filter((n) => n.trim());
    const checks = row.checks || [];
    const about = row.registry_check || {};
    const parked = row.status === "triage";
    render(host,
      el("div", { class: "crumbs" }, link("entities", "Entities"), icon("chevron"), el("span", {}, `capture #${row.id}`)),
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" },
            el("h2", {}, sit.label),
            el("div", { class: "sub" }, sit.explain)),
          el("div", { class: "spacer" }),
          statusPill(row.status)),
        !parked ? banner("info", el("div", { class: "row" }, "this capture is no longer parked — it is ", statusPill(row.status), " — so nothing can be minted from it now. ", link(`captures/${row.id}`, "Open the capture"), " to read what happened.")) : null,
        kv([
          ["capture", el("span", { class: "row" }, link(`captures/${row.id}`, `#${row.id}`), el("span", { class: "muted" }, `· ${row.kind} · sent by ${row.submitted_by}`))],
          ["waiting", `${fmtAge(row.parked_age_ms)} on a steward`],
          ["the submitter was asked", row.asked_at ? `${fmtWhen(row.asked_at)}${row.reply ? " — and replied (below)" : " — no answer came; a steward takes it from here"}` : "never — it parked straight away"],
          ["checked against", about.available
            ? `${about.road === "snapshot" ? `the index's registry snapshot (${about.source || "unrecorded sha"}, refreshed ${relTime(about.refreshed_at)})` : "this server's own registry file"}`
            : (about.error ? `nothing — ${about.error}` : "nothing — no registry is readable here; the mint gate still runs")],
        ], { wide: true })),
      el("div", { class: "grid two-one" },
        el("div", {},
          names.length
            ? el("section", { class: "card" },
                el("div", { class: "card-head" }, el("div", { class: "card-title" },
                  el("h2", {}, names.length > 1 ? `${names.length} names to place — one decision each` : "The name to place"),
                  el("div", { class: "sub" }, names.length > 1
                    ? "each name is minted, aliased or declined on its own; the capture stays parked until every one has its answer"
                    : "minting creates the entity page and the registry entry in ONE commit the librarian App signs with your name"))),
                ...names.map((name) => nameCard(row, name, checks.find((c) => c.name === name), parked)))
            : el("section", { class: "card" },
                el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "No name to register"),
                  el("div", { class: "sub" }, "an unsupported-type park names a page TYPE, not an entity: the agent judged the material to be about one specific person (or another type the fast lane never files)"))),
                banner("warn", el("p", {}, "If the subject deserves an entity, mint it with the form — the name is yours to type. Otherwise requeue, resolve by hand or decline from the capture."),
                  el("div", { class: "row", style: { marginTop: "8px" } },
                    el("button", { class: "btn small primary", type: "button", disabled: !parked, onclick: () => entityApproveFlow(row, "") }, "Mint an entity for this capture"),
                    link(`captures/${row.id}`, el("span", { class: "btn small" }, "Open the capture"))))),
          el("section", { class: "card" },
            el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Other roads"), el("div", { class: "sub" }, "when none of the names should become a new entity"))),
            el("ul", { class: "roads" },
              el("li", {}, el("strong", {}, "Requeue"), " — after you registered or aliased the entity elsewhere (the CLI, the knowledge repo): the librarian re-reads the material against the new registry."),
              el("li", {}, el("strong", {}, "Resolve by hand"), " — you used the material yourself; your note becomes the submitter's report."),
              el("li", {}, el("strong", {}, "Decline"), " — it should not be in the brain; the reason reaches the submitter and the governance ledger.")),
            el("div", { class: "row", style: { marginTop: "10px" } }, link(`captures/${row.id}`, el("span", { class: "btn small" }, icon("arrow", 14), "Act on the capture"))))),
        el("div", {},
          el("section", { class: "card" },
            el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "What arrived"))),
            el("div", { class: "quote-label" }, keyDot("human"), `from ${row.submitted_by}`),
            materialPanel(row),
            row.reply ? el("div", { style: { marginTop: "12px" } }, el("div", { class: "quote-label" }, keyDot("human"), "the submitter's reply to the librarian's question"), el("div", { class: "material human" }, row.reply)) : null,
            report.agent_rationale ? el("div", { style: { marginTop: "12px" } }, el("div", { class: "quote-label" }, keyDot("model"), "the agent's reading"), el("div", { class: "material" }, report.agent_rationale)) : null),
          el("section", { class: "card" },
            el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "History"))),
            timeline(row.events)))),
    );
  });
}

function nameCard(row, name, check, parked) {
  const v = check ? verdictCopy(check.verdict) : verdictCopy("unchecked");
  const verdictKey = check ? check.verdict : "unchecked";
  const match = check && check.match;
  const similar = (check && check.similar) || [];
  // `mintable` is the server's answer (`entities.situations.is_mintable_name`): the librarian's
  // placeholder for a park that named nothing is listed, explained, and never offered a button.
  const mintable = check ? check.mintable !== false : true;
  const actions = [];
  if (!mintable) {
    actions.push(pill("not a name — the librarian's placeholder", "code", { small: true }));
  } else if (verdictKey === "registered") {
    actions.push(el("button", { class: "btn small primary", type: "button", disabled: !parked, onclick: () => requeueHint(row, match) }, icon("refresh", 14), "Requeue — it resolves now"));
  } else if (verdictKey === "collides") {
    actions.push(el("button", { class: "btn small", type: "button", onclick: () => aliasHint(name, match) }, icon("branch", 14), "How to alias it"));
    // the form opens with the Name EMPTY: the console has just computed that this exact string
    // will be refused, so it must not be the value most stewards submit unchanged
    actions.push(el("button", { class: "btn small", type: "button", disabled: !parked, onclick: () => entityApproveFlow(row, "", { blank: true, collidesWith: match, colliding: name }) }, "Mint under another name"));
  } else {
    actions.push(el("button", { class: "btn small primary", type: "button", disabled: !parked, onclick: () => entityApproveFlow(row, name) }, icon("plus", 14), `Mint «${name}»`));
  }
  return el("div", { class: `name-card v-${verdictKey}` },
    el("div", { class: "row between" },
      el("div", { class: "name" }, name),
      el("div", { class: "row" }, ...actions)),
    !mintable ? el("div", { class: "verdict tone-code" }, icon("info", 16), el("div", {}, el("div", { class: "v-title" }, "Nothing was named"), el("div", {}, "this is the librarian's word for a park whose material named no entity it could place. Read the material: if it is about something, mint that under its real name; otherwise requeue, resolve by hand or decline the capture."))) : null,
    el("div", { class: `verdict tone-${v.tone}` }, icon(VERDICT_ICON[verdictKey] || "info", 16),
      el("div", {},
        el("div", { class: "v-title" }, v.label, match ? el("span", { class: "muted" }, ` — ${match.name} (${match.type})`) : ""),
        el("div", {}, v.explain),
        match ? el("div", { style: { marginTop: "6px" } }, entityChip(match)) : null,
        similar.length ? el("div", { class: "row", style: { marginTop: "6px" } }, ...similar.map((s) => entityChip(s, s.why))) : null)));
}

function entityChip(e, why) {
  return el("span", { class: "entity-chip", title: why || "" },
    el("strong", {}, e.name), el("span", { class: "type" }, e.type),
    e.aliases && e.aliases.length ? el("span", { class: "aliases" }, `aka ${e.aliases.join(", ")}`) : null,
    why ? el("span", { class: "aliases" }, `· ${why}`) : null);
}

async function requeueHint(row, match) {
  const answer = await confirmForm({
    title: `Requeue capture #${row.id}`,
    consequence: `«${match ? match.name : "the entity"}» now resolves in the registry this server serves, so the librarian will anchor the capture on its next pass — nothing to mint. Deliveries unchanged, claimable immediately.`,
    fields: [actorField(), { name: "note", label: "Note", kind: "textarea", value: match ? `${match.name} is registered now — re-file` : "", hint: "for the row's own history" }],
    confirmLabel: "Requeue",
  });
  if (answer && await mutate(`queue/${row.id}/requeue`, answer.values, `requeued #${row.id}`)) go("entities");
}

function aliasHint(name, match) {
  const page = `wiki/entities/${match.name}.md`;
  const steps = [
    el("span", {}, "Open ", mono(page), " in the knowledge repo and add «", name, "» to its ", mono("aliases:"), " list."),
    el("span", {}, "Run ", mono("stigmergy-entities regenerate"), " so ", mono("ops/entity-registry.json"), " follows the page, then commit and push both."),
    el("span", {}, "Come back and Requeue the capture: the librarian resolves the new spelling on its next pass."),
  ];
  return confirmForm({
    title: `Make «${name}» an alias of ${match.name}`,
    consequence: "the console cannot edit an entity page — aliases are governed content in the knowledge repo, changed by a steward's own commit. These are the three steps; nothing happens when you close this.",
    note: el("div", {},
      el("ol", { class: "roads" }, steps.map((s) => el("li", {}, s))),
      el("div", { class: "row" }, copyButton(`aliases: [${[...(match.aliases || []), name].map((a) => JSON.stringify(a)).join(", ")}]`, "Copy the aliases line"),
        copyButton("stigmergy-entities regenerate", "Copy the command"))),
    fields: [], confirmLabel: "Close", cancelLabel: null,
  });
}

// `entity_id` is deliberately NOT a field here, the same call `slack.render`'s entity-mint modal
// makes (ADR 030 D5, "one less field to mistype") — it defaults server-side to the slug of `name`.
//
// `Name` prefills from `mint_name_prefill` — the decision `entities.situations` takes once on the
// parked row, which `admin.service._situation` only sanitizes and sends, identically on the list
// and detail routes — and NEVER from the joined `subject` display string
// (`entities.situations.subject_of`): a park naming two unresolved entities joins them into
// "Jack, Acme Capital", which is neither name, and one submission here mints ONE entity with ONE
// commit nothing can cancel afterwards. This flow does not count names: an empty prefill with
// names still to place IS the several-names case, so the field stays empty and `subjects`, the
// per-name list, is listed for the steward to pick from. A steward who clicked "Mint «X»" on one
// name's card has made that pick by hand, and `chosen` carries exactly the string they clicked —
// a human choice, not a count. The Slack mint modal obeys the same decided value, so neither door
// can disagree about WHEN a default is safe. `opts.blank` opens the form with the Name EMPTY —
// the "mint under another name" road, where the one name the row carries is the one the console
// has just computed the gate will refuse.
async function entityApproveFlow(row, chosen = "", opts = {}) {
  const names = (row.subjects || []).map((n) => String(n)).filter((n) => n.trim());
  const proposed = opts.blank ? "" : (chosen || String(row.mint_name_prefill || ""));
  const meta = getMeta();
  // one sequence per field: a slow answer to an older keystroke never overwrites a newer verdict
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
    title: `Mint a new entity from capture #${row.id}`,
    consequence: "mints a real entity: pushes ONE commit to the knowledge repo (authored by the librarian App, Approved-by you) and regenerates the registry. Cancelling after this point cannot undo it. The gate re-checks collisions against the repo as it stands.",
    wide: true,
    note: opts.blank && opts.collidesWith
      ? banner("warn", el("div", {}, `«${opts.colliding}» would be confused with ${opts.collidesWith.name} (${opts.collidesWith.type}) — type a name that cannot be: the registered entity's spellings are what the gate compares against.`))
      : !proposed && names.length
      ? banner("warn",
          // No count in this sentence: the several-names decision was taken on the raw row, and
          // this list is what survived sanitizing — a name made entirely of control characters
          // counts towards "no default is safe" and then has nothing left to show. Naming a
          // number here would contradict the bullets on exactly the park that motivated the rule.
          el("div", {}, "this capture names several entities the registry does not recognize — these are the ones it can show:"),
          el("ul", { class: "names" }, names.map((name) => el("li", {}, name))),
          el("div", {}, "they are minted one at a time — type the single name you are approving now; the others stay unresolved on this capture until each gets its own decision."))
      : null,
    fields: [
      actorField(),
      { name: "name", label: "Name", value: proposed, required: true, hint: "the entity's page title, filename and wikilink target — checked live against the registry", live: liveCheck("name") },
      { name: "entity_type", label: "Type", kind: "select", options: meta.entity_types, required: true },
      { name: "aliases", label: "Aliases (optional, comma-separated)", hint: "other spellings captures use for it — each one is checked too, because an alias that collides is refused like a name", live: liveCheck("aliases") },
      { name: "role", label: "Role (optional)", hint: "one line on what this entity is" },
      { name: "requeue", label: "Requeue this capture once the push lands, so it re-files anchored to the new entity", kind: "checkbox", value: true },
    ],
    confirmLabel: "Approve & mint",
  });
  if (!answer) return;
  const minted = await mutate(`entities/${row.id}/approve`, answer.values,
    (r) => `minted ${r.name} (${r.entity_id}) — commit ${String(r.commit || "").slice(0, 12) || "?"}`
      + (r.requeued ? `; capture #${row.id} requeued` : "; NOT requeued — still parked"));
  if (minted) go("entities");
}

function liveVerdict(check, label) {
  const v = verdictCopy(check.verdict);
  const what = label === "aliases" ? `alias «${check.name}»` : `«${check.name}»`;
  const detail = check.match ? ` ${check.match.name} (${check.match.type}${check.match.aliases.length ? `, aka ${check.match.aliases.join(", ")}` : ""})` : "";
  const similar = (check.similar || []).map((s) => s.name).join(", ");
  const text = check.mintable === false ? `${what} is the librarian's placeholder for a park that named nothing — not an identity; the gate refuses it`
    : check.verdict === "collides" ? `${what} would collide with${detail} — the mint will be refused`
    : check.verdict === "registered" ? `${what} already resolves to${detail} — there is nothing to mint`
    : check.verdict === "similar" ? `${what} looks similar to ${similar} — make sure it is not the same thing`
    : check.verdict === "clear" ? `${what} — nothing like it is registered`
    : `${what} — could not be checked here`;
  const tone = check.mintable === false ? "fail" : v.tone;
  return el("div", { class: `verdict tone-${tone}` }, icon(check.mintable === false ? "x" : VERDICT_ICON[check.verdict] || "info", 15), el("span", {}, text));
}
