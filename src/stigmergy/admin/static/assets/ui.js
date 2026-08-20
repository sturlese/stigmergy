// DOM construction helpers. THE house rule of this frontend: every node is built with
// createElement/createElementNS and filled through textContent — there is no HTML-string path
// anywhere, which is what makes untrusted queue/finding text inert by construction. A test greps
// these files for the banned sinks. Styles are applied through the CSSOM (`node.style`), never a
// `style="…"` attribute: the console ships under `style-src 'self'`, which refuses the attribute.

import { KEY, severity as severityCopy, status as statusCopy, word as wordCopy } from "./copy.js";

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style") {
      if (typeof value === "string") node.style.cssText = value;
      else Object.assign(node.style, value);
    } else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (key === "value") node.value = value;
    else if (key === "checked") node.checked = Boolean(value);
    else if (key === "disabled") node.disabled = Boolean(value);
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

// `Element.replaceChildren` STRINGIFIES a null argument into the literal text "null" — every
// view mounts through this instead, so conditional children (`cond ? node : null`) stay inert.
export function render(host, ...children) {
  host.replaceChildren(...children.flat(Infinity)
    .filter((child) => child !== null && child !== undefined && child !== false)
    .map((child) => (child instanceof Node ? child : document.createTextNode(String(child)))));
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export const SVG_NS = "http://www.w3.org/2000/svg";

// SVG sibling of `el`: attributes only (SVG has no `value`/`checked`), `class` via setAttribute.
export function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "style") Object.assign(node.style, value);
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

// ── icons — feather-style 24×24 strokes, DOM-built (no markup strings) ────────────────────────
export function icon(name, size = 17) {
  const paths = ICONS[name] || ICONS.dot;
  const node = svg("svg", {
    viewBox: "0 0 24 24", width: size, height: size, fill: "none", stroke: "currentColor",
    "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true",
    class: "icon",
  });
  for (const d of paths) node.append(svg("path", { d }));
  return node;
}

const ICONS = {
  dot: ["M12 11a1 1 0 1 0 0 2 1 1 0 0 0 0-2"],
  dashboard: ["M3 3h8v8H3z", "M13 3h8v5h-8z", "M13 10h8v11h-8z", "M3 13h8v8H3z"],
  inbox: ["M22 12h-6l-2 3h-4l-2-3H2", "M5.5 5.1L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.7 4H7.3a2 2 0 0 0-1.8 1.1z"],
  captures: ["M4 6h16", "M4 12h16", "M4 18h10"],
  entities: ["M20 12l-8 8-9-9V4h7z", "M7.5 7.5h.01"],
  repairs: ["M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-7.9 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-7.9z"],
  gardener: ["M12 22c6-3 8-8 8-13V5l-8-3-8 3v4c0 5 2 10 8 13z"],
  index: ["M12 2c-5 0-8 1.5-8 3.5S7 9 12 9s8-1.5 8-3.5S17 2 12 2z", "M4 5.5v13c0 2 3 3.5 8 3.5s8-1.5 8-3.5v-13",
          "M4 12c0 2 3 3.5 8 3.5s8-1.5 8-3.5"],
  worker: ["M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8", "M12 2v3", "M12 19v3", "M4.2 4.2l2.2 2.2",
           "M17.6 17.6l2.2 2.2", "M2 12h3", "M19 12h3", "M4.2 19.8l2.2-2.2", "M17.6 6.4l2.2-2.2"],
  jobs: ["M12 2v4", "M12 18v4", "M4.9 4.9l2.9 2.9", "M16.2 16.2l2.9 2.9", "M2 12h4", "M18 12h4",
         "M4.9 19.1l2.9-2.9", "M16.2 7.8l2.9-2.9"],
  digest: ["M4 4h16v12H8l-4 4z"],
  activity: ["M22 12h-4l-3 8L9 4l-3 8H2"],
  check: ["M20 6L9 17l-5-5"],
  x: ["M18 6L6 18", "M6 6l12 12"],
  alert: ["M12 3l10 18H2z", "M12 10v4", "M12 18h.01"],
  info: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18", "M12 11v5", "M12 8h.01"],
  help: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18", "M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .9-1 1.7", "M12 17h.01"],
  play: ["M6 4l14 8-14 8z"],
  copy: ["M9 9h11v11H9z", "M5 15H4V4h11v1"],
  refresh: ["M21 12a9 9 0 1 1-2.6-6.4", "M21 3v6h-6"],
  logout: ["M9 21H4V3h5", "M16 17l5-5-5-5", "M21 12H9"],
  external: ["M15 3h6v6", "M10 14L21 3", "M21 14v7H3V3h7"],
  arrow: ["M5 12h14", "M13 6l6 6-6 6"],
  back: ["M19 12H5", "M11 18l-6-6 6-6"],
  search: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14", "M20 20l-4-4"],
  git: ["M6 3v12", "M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6", "M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6", "M18 9a9 9 0 0 1-9 9"],
  person: ["M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2", "M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8"],
  clock: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18", "M12 7v5l3 2"],
  tag: ["M20 12l-8 8-9-9V4h7z", "M7.5 7.5h.01"],
  table: ["M3 5h18v14H3z", "M3 10h18", "M3 15h18", "M9 5v14"],
  chart: ["M4 20V10", "M10 20V4", "M16 20v-7", "M22 20H2"],
  plus: ["M12 5v14", "M5 12h14"],
  chevron: ["M9 6l6 6-6 6"],
  sparkle: ["M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"],
  shield: ["M12 22c6-3 8-8 8-13V5l-8-3-8 3v4c0 5 2 10 8 13z", "M9 12l2 2 4-4"],
  branch: ["M6 3v12", "M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6", "M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6", "M18 9a9 9 0 0 1-9 9"],
};

// ── formatting — the CLI's own renderings, ported ─────────────────────────────────────────────
export function fmtAge(ms) {
  if (ms === null || ms === undefined) return "—";
  const minutes = Math.floor(Math.max(0, ms) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 24) return rest ? `${hours}h ${rest} min` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const h = hours % 24;
  return h ? `${days}d ${h}h` : `${days}d`;
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.round((ms % 60000) / 1000);
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

export function fmtWhen(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function fmtDay(isoDay) {
  const date = new Date(`${isoDay}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return isoDay;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

export function fmtNum(n) {
  if (n === null || n === undefined) return "—";
  const value = Number(n);
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (Math.abs(value) >= 10_000) return `${(value / 1000).toFixed(1)}K`;
  return value.toLocaleString();
}

export function fmtPct(n, digits = 0) {
  return n === null || n === undefined ? "—" : `${Number(n).toFixed(digits)}%`;
}

export function agoFrom(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : Date.now() - t;
}

export function relTime(iso) {
  const ms = agoFrom(iso);
  if (ms === null) return "—";
  return ms < 0 ? `in ${fmtAge(-ms)}` : `${fmtAge(ms)} ago`;
}

// The returned function carries `.cancel()`: a debounced call scheduled by a field in a modal
// must not fire after the modal closed.
export function debounce(fn, wait = 250) {
  let timer = null;
  const run = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  run.cancel = () => clearTimeout(timer);
  return run;
}

// ── the key — who decides ─────────────────────────────────────────────────────────────────────
// Decorative: text always rides beside it, so it carries no accessible name of its own.
export function keyDot(who, size = 8) {
  const role = KEY[who] ? who : who === "accent" ? "accent" : "code";
  return el("span", { class: `kdot k-${role}`, style: { width: `${size}px`, height: `${size}px` }, "aria-hidden": "true" });
}

export function keyLegend() {
  return el("div", { class: "keylegend" },
    el("div", { class: "eyebrow" }, "colour is who decides"),
    ...Object.entries(KEY).map(([who, k]) => el("div", { class: "keyrow", title: k.explain },
      keyDot(who), el("span", {}, k.label))));
}

// ── pills — the word always rides with the colour (status is never colour alone) ─────────────
const TONE_BY_WHO = { human: "human", model: "model", code: "code", git: "git", fail: "fail" };

export function pill(text, tone, opts = {}) {
  const cls = tone || "neutral";
  const node = el("span", { class: `pill ${cls}${opts.small ? " small" : ""}` },
    el("span", { class: "dot" }), String(text).replaceAll("_", " "));
  if (opts.title) node.title = opts.title;
  return node;
}

// A capture status as its human label, coloured by who is acting, with the system word and the
// one-line meaning on hover. `short` renders the compact form for dense tables.
export function statusPill(word, opts = {}) {
  const s = statusCopy(word);
  return pill(opts.short ? s.short : s.label, TONE_BY_WHO[s.who] || "neutral",
    { title: `${word} — ${s.explain}`, small: opts.small });
}

// Every other closed word — a ledger verdict, a proposal or job outcome, a GitHub workflow
// state — as its human label, coloured by who decided it, the raw word on hover (`copy.WORD`).
export function wordPill(raw, opts = {}) {
  const w = wordCopy(raw);
  return pill(w.label, TONE_BY_WHO[w.who] || "neutral", { title: String(raw), small: opts.small });
}

// Two severity vocabularies share this pill: the substrate check's error/warn and the
// gardener's info/warn/sla (sla = the urgent one — it is what triggers the Slack notice). The
// human label, the raw word and its meaning on hover — the same deal every status gets.
export function severityPill(raw) {
  const tone = raw === "error" || raw === "sla" ? "fail" : raw === "warn" ? "human" : "neutral";
  const sev = severityCopy(raw);
  return pill(sev.label, tone, { title: `${raw} — ${sev.explain}` });
}

// ── composites ────────────────────────────────────────────────────────────────────────────────
export function eyebrow(text, ...rest) {
  return el("div", { class: "eyebrow" }, text, ...rest);
}

export function card(opts, ...children) {
  const { title, sub, actions = [], eyebrowText, cls = "", id } = opts || {};
  const head = title || actions.length || eyebrowText
    ? el("div", { class: "card-head" },
        el("div", { class: "card-title" },
          eyebrowText ? eyebrow(eyebrowText) : null,
          title ? el("h2", {}, title) : null,
          sub ? el("div", { class: "sub" }, sub) : null),
        el("div", { class: "spacer" }),
        ...actions)
    : null;
  return el("section", { class: `card ${cls}`.trim(), id }, head, ...children);
}

export function tile(label, value, sub, opts = {}) {
  const tone = opts.tone ? ` tone-${opts.tone}` : "";
  const node = el("div", { class: `tile${opts.hero ? " hero" : ""}${tone}${opts.onclick ? " clickable" : ""}`,
                           onclick: opts.onclick, role: opts.onclick ? "button" : undefined,
                           tabindex: opts.onclick ? 0 : undefined },
    el("div", { class: "label" }, opts.who ? keyDot(opts.who) : null, label),
    el("div", { class: "value" }, value),
    sub ? el("div", { class: "sub" }, sub) : null,
    opts.foot || null);
  if (opts.onclick) node.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); opts.onclick(); } });
  return node;
}

export function table(headers, rows, opts = {}) {
  if (!rows.length) return emptyState(opts.empty || "nothing here", opts.emptyHint);
  return el("div", { class: "table-wrap" },
    el("table", { class: opts.dense ? "dense" : "" },
      el("thead", {}, el("tr", {}, headers.map((h) => el("th", { class: typeof h === "object" ? h.cls : "" }, typeof h === "object" ? h.text : h)))),
      el("tbody", {}, rows.map((cells) => {
        const attrs = {};
        if (opts.onRow) {
          attrs.class = "rowlink";
          attrs.tabindex = 0;
          attrs.role = "button";
          attrs.onclick = () => opts.onRow(cells.row);
          attrs.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); opts.onRow(cells.row); } };
        }
        return el("tr", attrs, cells.cells.map((c, i) => el("td", { class: typeof headers[i] === "object" ? headers[i].cls : "" }, c)));
      }))));
}

export function kv(pairs, opts = {}) {
  return el("dl", { class: `kv${opts.wide ? " wide" : ""}` },
    pairs.filter(([, v]) => v !== null && v !== undefined && v !== "")
      .map(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
}

export function banner(kind, ...children) {
  const name = kind === "error" ? "alert" : kind === "warn" ? "alert" : kind === "good" ? "check" : "info";
  return el("div", { class: `banner ${kind}` }, icon(name), el("div", { class: "banner-body" }, ...children));
}

export function emptyState(text, hint) {
  return el("div", { class: "empty" }, el("div", { class: "empty-title" }, text),
    hint ? el("div", { class: "empty-hint" }, hint) : null);
}

// The one keyboard contract for "this element opens something": a button role, Enter or Space.
export function clickable(node, handler) {
  node.setAttribute("role", "button");
  node.setAttribute("tabindex", "0");
  node.addEventListener("click", handler);
  node.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handler(e); } });
  return node;
}

export function copyButton(text, label = "Copy") {
  return el("button", {
    class: "btn small ghost", type: "button",
    onclick: async (event) => {
      event.stopPropagation();
      try {
        await navigator.clipboard.writeText(text);
        toast("copied to clipboard", "good");
      } catch {
        toast("the browser refused the clipboard — select and copy by hand", "error");
      }
    },
  }, icon("copy", 14), label);
}

export function commandBlock(command) {
  return el("div", { class: "cmd" }, el("pre", { class: "pre mono" }, command), copyButton(command));
}

export function mono(text, cls = "") {
  return el("span", { class: `mono ${cls}`.trim() }, text);
}

export function link(hash, ...children) {
  return el("a", { href: `#/${hash}` }, ...children);
}

export function skeletons(n = 3) {
  return el("div", { class: "skeletons" }, Array.from({ length: n }, (_, i) =>
    el("div", { class: "skeleton", style: { height: `${i === 0 ? 92 : 140}px` } })));
}

// A chip row: `items` = [{key, label, count?, on}], `onPick(key)`.
export function chips(items, onPick, opts = {}) {
  return el("div", { class: `chiprow${opts.cls ? ` ${opts.cls}` : ""}` },
    items.map((item) => el("button", {
      class: `chip${item.on ? " on" : ""}${item.tone ? ` ${item.tone}` : ""}`, type: "button",
      "aria-pressed": String(Boolean(item.on)), title: item.title || undefined,
      onclick: () => onPick(item.key),
    }, item.who ? keyDot(item.who, 7) : null, item.label,
      item.count !== undefined ? el("span", { class: "count" }, String(item.count)) : null)),
    ...(opts.trailing || []));
}

// ── the "how to read this" explainer, remembered per page ────────────────────────────────────
const EXPLAIN_KEY = "stigmergy-ops-explain-hidden";

function hiddenExplainers() {
  try { return new Set(JSON.parse(localStorage.getItem(EXPLAIN_KEY) || "[]")); } catch { return new Set(); }
}

export function explainer(pageKey, bullets) {
  if (!bullets || !bullets.length) return null;
  const hidden = hiddenExplainers();
  const body = el("ul", { class: "explain-list" }, bullets.map((b) => el("li", {}, b)));
  const wrap = el("div", { class: `explainer${hidden.has(pageKey) ? " collapsed" : ""}` });
  const toggle = el("button", { class: "explain-toggle", type: "button", "aria-expanded": String(!hidden.has(pageKey)) },
    icon("help", 15), el("span", {}, "How to read this page"), icon("chevron", 14));
  toggle.addEventListener("click", () => {
    const now = hiddenExplainers();
    const collapsed = wrap.classList.toggle("collapsed");
    if (collapsed) now.add(pageKey); else now.delete(pageKey);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    localStorage.setItem(EXPLAIN_KEY, JSON.stringify([...now]));
  });
  wrap.append(toggle, body);
  return wrap;
}

// ── toasts ────────────────────────────────────────────────────────────────────────────────────
// The live region is mounted ONCE, empty, when the shell renders (`mountToasts`) — a region
// inserted together with its first message is commonly not announced.
let toastHost = null;

export function mountToasts() {
  if (toastHost && toastHost.isConnected) return toastHost;
  toastHost = el("div", { class: "toasts", role: "status", "aria-live": "polite" });
  document.body.append(toastHost);
  return toastHost;
}

export function toast(message, tone = "") {
  const host = mountToasts();
  const node = el("div", { class: `toast ${tone}` },
    icon(tone === "error" || tone === "warn" ? "alert" : tone === "good" ? "check" : "info", 15), el("span", {}, message));
  host.append(node);
  setTimeout(() => node.remove(), tone === "error" || tone === "warn" ? 10000 : 5000);
}

// ── tooltip (one for the whole page; charts move it) ─────────────────────────────────────────
let tipNode = null;

export function showTip(x, y, ...children) {
  if (!tipNode) {
    tipNode = el("div", { class: "tip", role: "tooltip" });
    document.body.append(tipNode);
  }
  render(tipNode, ...children);
  tipNode.style.display = "block";
  const rect = tipNode.getBoundingClientRect();
  const left = Math.min(x + 14, window.innerWidth - rect.width - 12);
  const top = y + 14 + rect.height > window.innerHeight ? y - rect.height - 10 : y + 14;
  tipNode.style.left = `${Math.max(8, left)}px`;
  tipNode.style.top = `${Math.max(8, top)}px`;
}

export function hideTip() {
  if (tipNode) tipNode.style.display = "none";
}

// A closed-list field: one option per `f.options` entry, `f.value` pre-selected. With no `value`
// supplied (the common case — a required choice with no honest default, e.g. an entity type) a
// blank leading option forces a deliberate pick instead of silently submitting whichever option
// happens to sort first; the shared required-field check below already treats that blank value
// as `""`, so no special-casing is needed there.
function selectInput(f) {
  const options = (f.options || []).map((opt) =>
    el("option", { value: opt, selected: opt === f.value }, opt));
  if (!f.value) options.unshift(el("option", { value: "" }, f.placeholder || "— choose —"));
  return el("select", {}, options);
}

// ── the confirm-with-form modal — every mutation goes through one ─────────────────────────────
// `fields`: [{name, label, hint, warnHint, kind: "text"|"textarea"|"checkbox"|"select", value,
// required, options (select only), placeholder, live(value, setNote, allValues)}]
// `live` is called (debounced) on every keystroke with a `setNote(node|null)` the field renders
// under itself — the entity form's registry check lives there. `note`: an optional already-built
// node shown above the form. A NODE, never markup, like every other child.
// Resolves with {values} on confirm, null on cancel. The consequence sentence is REQUIRED —
// a button that spends or posts says so before it does (honest-copy discipline).
export function confirmForm({ title, consequence, note = null, fields = [],
                              confirmLabel = "Confirm", danger = false, wide = false,
                              cancelLabel = "Cancel" }) {
  if (!consequence || !String(consequence).trim()) {
    // Loud in development rather than an empty sentence on a button that spends or posts.
    throw new Error(`confirmForm(${JSON.stringify(title)}) has no consequence sentence`);
  }
  return new Promise((resolve) => {
    const inputs = {};
    const liveNotes = {};
    const liveRuns = [];
    const invoker = document.activeElement;
    const allValues = () => Object.fromEntries(Object.entries(inputs).map(([k, fn]) => [k, fn()]));
    const form = el("form", { onsubmit: (event) => event.preventDefault() },
      fields.map((f) => {
        if (f.kind === "checkbox") {
          const box = el("input", { type: "checkbox", checked: f.value });
          inputs[f.name] = () => box.checked;
          return el("label", { class: "checkline" }, box, el("span", {}, f.label));
        }
        const input = f.kind === "textarea" ? el("textarea", { value: f.value || "", placeholder: f.placeholder || "" })
          : f.kind === "select" ? selectInput(f)
          : el("input", { type: "text", value: f.value || "", placeholder: f.placeholder || "", autocomplete: "off" });
        inputs[f.name] = () => input.value;
        const noteHost = el("div", { class: "field-live" });
        liveNotes[f.name] = noteHost;
        if (typeof f.live === "function") {
          const run = debounce(() => f.live(input.value, (node) => render(noteHost, node), allValues()), 220);
          liveRuns.push(run);
          input.addEventListener("input", run);
          queueMicrotask(() => f.live(input.value, (node) => render(noteHost, node), allValues()));
        }
        return el("label", { class: "field" },
          el("span", { class: "field-label" }, f.label, f.required ? el("em", { class: "req" }, " required") : null),
          input,
          f.hint ? el("span", { class: `hint${f.warnHint ? " warn" : ""}` }, f.hint) : null,
          noteHost);
      }));
    const close = (result) => {
      for (const run of liveRuns) run.cancel();
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      if (invoker && typeof invoker.focus === "function" && invoker.isConnected) invoker.focus();
      resolve(result);
    };
    const focusables = () => [...overlay.querySelectorAll("input, textarea, select, button, [tabindex]:not([tabindex='-1'])")]
      .filter((n) => !n.disabled);
    const onKey = (event) => {
      if (event.key === "Escape") { close(null); return; }
      if (event.key !== "Tab") return;
      // the focus cycle stays inside the dialog
      const nodes = focusables();
      if (!nodes.length) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    const overlay = el("div", {
      class: "overlay",
      onclick: (event) => { if (event.target === overlay) close(null); },
    },
      el("div", { class: `modal${wide ? " wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-label": title },
        el("h3", {}, title),
        el("p", { class: "consequence" }, icon(danger ? "alert" : "info", 15), el("span", {}, consequence)),
        note,
        form,
        el("div", { class: "actions" },
          cancelLabel ? el("button", { class: "btn", type: "button", onclick: () => close(null) }, cancelLabel) : null,
          el("button", {
            class: `btn ${danger ? "danger" : "primary"}`, type: "button",
            onclick: () => {
              for (const f of fields) {
                if (f.required && f.kind !== "checkbox" && !inputs[f.name]().trim()) {
                  toast(`${f.label} is required`, "error");
                  return;
                }
              }
              close({ values: allValues() });
            },
          }, confirmLabel))));
    document.body.append(overlay);
    document.addEventListener("keydown", onKey);
    const first = form.querySelector("input:not([type=checkbox]), textarea, select") || overlay.querySelector(".actions .btn.primary, .actions .btn.danger");
    if (first) first.focus();
  });
}
