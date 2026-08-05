// DOM construction helpers. THE house rule of this frontend: every node is built with
// createElement/createElementNS and filled through textContent — there is no HTML-string path
// anywhere, which is what makes untrusted queue/finding text inert by construction. A test greps
// these files for the banned sinks.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (key === "value") node.value = value;
    else if (key === "checked") node.checked = Boolean(value);
    else if (key === "disabled") node.disabled = Boolean(value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
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

// ── icons — feather-style 24×24 strokes, DOM-built (no markup strings) ────────────────────────
const SVG_NS = "http://www.w3.org/2000/svg";

export function icon(name, size = 17) {
  const paths = ICONS[name] || ICONS.dot;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const d of paths) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

const ICONS = {
  dot: ["M12 11a1 1 0 1 0 0 2 1 1 0 0 0 0-2"],
  overview: ["M3 3h8v8H3z", "M13 3h8v5h-8z", "M13 10h8v11h-8z", "M3 13h8v8H3z"],
  queue: ["M4 6h16", "M4 12h16", "M4 18h10"],
  crons: ["M12 2v4", "M12 18v4", "M4.9 4.9l2.9 2.9", "M16.2 16.2l2.9 2.9", "M2 12h4", "M18 12h4",
          "M4.9 19.1l2.9-2.9", "M16.2 7.8l2.9-2.9"],
  gardener: ["M12 22c6-3 8-8 8-13V5l-8-3-8 3v4c0 5 2 10 8 13z"],
  digest: ["M4 4h16v12H8l-4 4z"],
  index: ["M12 2c-5 0-8 1.5-8 3.5S7 9 12 9s8-1.5 8-3.5S17 2 12 2z", "M4 5.5v13c0 2 3 3.5 8 3.5s8-1.5 8-3.5v-13",
          "M4 12c0 2 3 3.5 8 3.5s8-1.5 8-3.5"],
  entities: ["M20 12l-8 8-9-9V4h7z", "M7.5 7.5h.01"],
  activity: ["M22 12h-4l-3 8L9 4l-3 8H2"],
  worker: ["M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8", "M12 2v3", "M12 19v3", "M4.2 4.2l2.2 2.2",
           "M17.6 17.6l2.2 2.2", "M2 12h3", "M19 12h3", "M4.2 19.8l2.2-2.2", "M17.6 6.4l2.2-2.2"],
  check: ["M20 6L9 17l-5-5"],
  x: ["M18 6L6 18", "M6 6l12 12"],
  alert: ["M12 3l10 18H2z", "M12 10v4", "M12 18h.01"],
  info: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18", "M12 11v5", "M12 8h.01"],
  play: ["M6 4l14 8-14 8z"],
  copy: ["M9 9h11v11H9z", "M5 15H4V4h11v1"],
  refresh: ["M21 12a9 9 0 1 1-2.6-6.4", "M21 3v6h-6"],
  logout: ["M9 21H4V3h5", "M16 17l5-5-5-5", "M21 12H9"],
  external: ["M15 3h6v6", "M10 14L21 3", "M21 14v7H3V3h7"],
};

// ── formatting — the CLI's own renderings, ported ─────────────────────────────────────────────
export function fmtAge(ms) {
  if (ms === null || ms === undefined) return "—";
  const minutes = Math.floor(Math.max(0, ms) / 60000);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 24) return rest ? `${hours}h ${rest} min` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const h = hours % 24;
  return h ? `${days}d ${h}h` : `${days}d`;
}

export function fmtMs(ms) {
  return ms === null || ms === undefined ? "—" : `${(ms / 1000).toFixed(1)}s`;
}

export function fmtWhen(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function agoFrom(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : Date.now() - t;
}

// ── pills — the word always rides with the color (status is never color alone) ────────────────
const STATUS_TONE = {
  queued: "accent", claimed: "warning", filed: "good", resolved: "good",
  rejected: "critical", failed: "critical", needs_input: "serious", triage: "warning",
  ok: "good", error: "critical", partial: "serious",
  success: "good", failure: "critical", cancelled: "neutral", in_progress: "accent",
  active: "good", disabled_manually: "serious", disabled_inactivity: "serious",
  refused: "serious",
};

export function pill(text, tone) {
  const cls = tone || STATUS_TONE[text] || "neutral";
  return el("span", { class: `pill ${cls}` }, el("span", { class: "dot" }), text.replaceAll("_", " "));
}

// Two severity vocabularies share this pill: the substrate check's error/warn and the
// gardener's info/warn/sla (sla = the urgent one — it is what triggers the Slack notice).
export function severityPill(severity) {
  const tone = severity === "error" || severity === "sla" ? "critical"
    : severity === "warn" ? "warning" : "neutral";
  return pill(severity, tone);
}

// ── composites ────────────────────────────────────────────────────────────────────────────────
export function tile(label, value, sub, opts = {}) {
  return el("div", { class: `tile${opts.hero ? " hero" : ""}` },
    el("div", { class: "label" }, label),
    el("div", { class: "value" }, value),
    sub ? el("div", { class: "sub" }, sub) : null);
}

export function table(headers, rows, opts = {}) {
  if (!rows.length) return el("div", { class: "empty" }, opts.empty || "nothing here");
  return el("div", { class: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.map((cells) => {
        const attrs = {};
        if (opts.onRow) {
          attrs.class = "rowlink";
          attrs.onclick = () => opts.onRow(cells.row);
        }
        return el("tr", attrs, cells.cells.map((c) => el("td", {}, c)));
      }))));
}

export function kv(pairs) {
  return el("dl", { class: "kv" },
    pairs.filter(([, v]) => v !== null && v !== undefined && v !== "")
      .map(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
}

export function banner(kind, ...children) {
  const name = kind === "error" ? "alert" : kind === "warn" ? "alert" : "info";
  return el("div", { class: `banner ${kind}` }, icon(name), el("div", {}, ...children));
}

export function copyButton(text) {
  return el("button", {
    class: "btn small", title: "Copy to clipboard",
    onclick: async (event) => {
      event.stopPropagation();
      await navigator.clipboard.writeText(text);
      toast("copied to clipboard", "good");
    },
  }, icon("copy", 14), "Copy");
}

export function commandBlock(command) {
  return el("div", { class: "cmd" }, el("pre", { class: "pre mono" }, command), copyButton(command));
}

export function skeletons(n = 3) {
  return el("div", {}, Array.from({ length: n }, (_, i) =>
    el("div", { class: "skeleton", style: `height:${i === 0 ? 92 : 140}px;margin-bottom:14px` })));
}

// ── toasts ────────────────────────────────────────────────────────────────────────────────────
let toastHost = null;

export function toast(message, tone = "") {
  if (!toastHost) {
    toastHost = el("div", { class: "toasts" });
    document.body.append(toastHost);
  }
  const node = el("div", { class: `toast ${tone}` }, message);
  toastHost.append(node);
  setTimeout(() => node.remove(), tone === "error" ? 9000 : 5000);
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
// required, options (select only), placeholder (select only)}]
// Resolves with {values} on confirm, null on cancel. The consequence sentence is REQUIRED —
// a button that spends or posts says so before it does (honest-copy discipline).
export function confirmForm({ title, consequence, fields = [], confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    const inputs = {};
    const form = el("form", {},
      fields.map((f) => {
        if (f.kind === "checkbox") {
          const box = el("input", { type: "checkbox", checked: f.value });
          inputs[f.name] = () => box.checked;
          return el("label", { class: "checkline" }, box, el("span", {}, f.label));
        }
        const input = f.kind === "textarea" ? el("textarea", { value: f.value || "" })
          : f.kind === "select" ? selectInput(f)
          : el("input", { type: "text", value: f.value || "" });
        inputs[f.name] = () => input.value;
        return el("label", { class: "field" },
          el("span", {}, f.label),
          input,
          f.hint ? el("span", { class: `hint${f.warnHint ? " warn" : ""}` }, f.hint) : null);
      }));
    const close = (result) => { overlay.remove(); resolve(result); };
    const overlay = el("div", {
      class: "overlay",
      onclick: (event) => { if (event.target === overlay) close(null); },
    },
      el("div", { class: "modal", role: "dialog", "aria-modal": "true" },
        el("h3", {}, title),
        el("p", { class: "consequence" }, consequence),
        form,
        el("div", { class: "actions" },
          el("button", { class: "btn", onclick: () => close(null) }, "Cancel"),
          el("button", {
            class: `btn ${danger ? "danger" : "primary"}`,
            onclick: (event) => {
              event.preventDefault();
              for (const f of fields) {
                if (f.required && f.kind !== "checkbox" && !inputs[f.name]().trim()) {
                  toast(`${f.label} is required`, "error");
                  return;
                }
              }
              close({ values: Object.fromEntries(Object.entries(inputs).map(([k, fn]) => [k, fn()])) });
            },
          }, confirmLabel))));
    document.body.append(overlay);
    const first = form.querySelector("input, textarea");
    if (first) first.focus();
  });
}
