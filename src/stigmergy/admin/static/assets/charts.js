// SVG charts, DOM-built — no library, no markup strings, nothing fetched. Every chart follows the
// same contract: thin marks, a 2px surface gap between fills, hairline solid grid, a legend for
// two or more series, a hover readout that lists EVERY series at the X, and a table twin (the
// accessibility equivalent) one toggle away. Colour follows the entity, never its rank: a series'
// colour is decided by its key (see copy.js KEY), so filtering never repaints the survivors.
// Text never wears a series colour — values and labels stay in ink.

import { el, fmtNum, hideTip, icon, showTip, svg, table } from "./ui.js";

const KEY_COLOR = {
  human: "var(--k-human)", model: "var(--k-model)", code: "var(--k-code)", git: "var(--k-git)",
  fail: "var(--k-fail)", accent: "var(--accent)", other: "var(--k-other)",
  s1: "var(--s1)", s2: "var(--s2)", s3: "var(--s3)", s4: "var(--s4)", s5: "var(--s5)", s6: "var(--s6)",
};

// A closed map: an unknown key falls to the de-emphasis colour rather than being echoed into a
// CSS value — every series key is a KEY role or a categorical slot, never data.
export function seriesColor(key) {
  return KEY_COLOR[key] || KEY_COLOR.other;
}

// A rounded-top bar: 4px radius on the data end, square at the baseline. Drawn as a path so the
// rounding is one-sided (an SVG rect rounds every corner).
function roundedTop(x, y, w, h, r) {
  if (h <= 0 || w <= 0) return svg("path", { d: "" });
  const rr = Math.min(r, w / 2, h);
  return svg("path", {
    d: `M${x} ${y + h} V${y + rr} Q${x} ${y} ${x + rr} ${y} H${x + w - rr} Q${x + w} ${y} ${x + w} ${y + rr} V${y + h} Z`,
  });
}

function roundedRight(x, y, w, h, r) {
  if (h <= 0 || w <= 0) return svg("path", { d: "" });
  const rr = Math.min(r, h / 2, w);
  return svg("path", {
    d: `M${x} ${y} H${x + w - rr} Q${x + w} ${y} ${x + w} ${y + rr} V${y + h - rr} Q${x + w} ${y + h} ${x + w - rr} ${y + h} H${x} Z`,
  });
}

function niceMax(value) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const scaled = value / magnitude;
  const nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return nice * magnitude;
}

function gridLines(width, height, top, max, steps = 3, left = 0) {
  const group = svg("g", { class: "grid" });
  for (let i = 0; i <= steps; i += 1) {
    const y = top + (height - top) * (1 - i / steps);
    group.append(svg("line", { x1: left, x2: width, y1: y, y2: y, class: "gridline" }));
    group.append(svg("text", { x: left, y: y - 4, class: "axis-label" }, fmtNum(Math.round(max * i / steps))));
  }
  return group;
}

export function legend(series) {
  if (series.length < 2) return null;
  return el("div", { class: "legend" }, series.map((s) =>
    el("span", { class: "legend-item" },
      el("span", { class: "swatch", style: { background: seriesColor(s.color || s.key) } }), s.label)));
}

function tipRows(title, rows) {
  return [el("div", { class: "tip-title" }, title),
    el("div", { class: "tip-rows" }, rows.map(([label, value, color]) => el("div", { class: "tip-row" },
      el("span", { class: "tip-key", style: { background: seriesColor(color) } }),
      el("strong", {}, String(value)), el("span", { class: "tip-label" }, label))))];
}

// ── stacked columns over time ─────────────────────────────────────────────────────────────────
// `series`: [{key, label, color}] (color is a KEY role or a CSS colour); `rows`: [{x, label,
// values: {key: n}}]. Empty rows → an empty-state. Hover shows every series at that X.
export function stackedColumns({ series, rows, height = 180, width = 640, maxBar = 22, yLabel = "" }) {
  const pad = { top: 16, right: 8, bottom: 22, left: 28 };
  const wrap = el("div", { class: "chart" });
  if (!rows.length) {
    wrap.append(el("div", { class: "chart-empty" }, "no data in this window"));
    return wrap;
  }
  const totals = rows.map((r) => series.reduce((sum, s) => sum + (r.values[s.key] || 0), 0));
  const max = niceMax(Math.max(...totals, 1));
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.bottom;
  const slot = plotW / rows.length;
  const barW = Math.min(maxBar, Math.max(3, slot - 4));
  const node = svg("svg", { viewBox: `0 0 ${width} ${height}`, class: "chart-svg", role: "img",
                            "aria-label": yLabel || "stacked columns" });
  node.append(gridLines(width - pad.right, plotH, pad.top, max, 3, pad.left));
  const scale = (v) => (plotH - pad.top) * (v / max);
  rows.forEach((row, i) => {
    const x = pad.left + slot * i + (slot - barW) / 2;
    let yCursor = plotH;
    const group = svg("g", { class: "col" });
    series.forEach((s) => {
      const v = row.values[s.key] || 0;
      if (!v) return;
      const h = scale(v);
      const gap = yCursor === plotH ? 0 : 2;
      const top = yCursor - h;
      const bar = roundedTop(x, top, barW, Math.max(0, h - gap), 4);
      bar.setAttribute("fill", seriesColor(s.color || s.key));
      group.append(bar);
      yCursor = top;
    });
    // the hit target is the whole slot, never the painted pixels
    const hit = svg("rect", { x: pad.left + slot * i, y: 0, width: slot, height: plotH, fill: "transparent", class: "hit" });
    const show = (event) => {
      group.classList.add("hover");
      showTip(event.clientX, event.clientY, ...tipRows(row.label || row.x,
        series.filter((s) => row.values[s.key]).map((s) => [s.label, row.values[s.key], s.color || s.key])
          .concat([["total", totals[i], "other"]])));
    };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerleave", () => { group.classList.remove("hover"); hideTip(); });
    node.append(group, hit);
    const every = Math.max(1, Math.ceil(rows.length / 8));
    if (i % every === 0 || i === rows.length - 1) {
      node.append(svg("text", { x: x + barW / 2, y: height - 6, class: "axis-label middle" }, row.label || row.x));
    }
  });
  node.append(svg("line", { x1: pad.left, x2: width - pad.right, y1: plotH, y2: plotH, class: "baseline" }));
  wrap.append(node);
  const key = legend(series);
  if (key) wrap.append(key);
  return wrap;
}

// ── horizontal bars — magnitude, one hue (or one colour per row when rows carry a key) ───────
// HTML rather than SVG: the labels are real text at the page's own size whatever the card's
// width, and the bar is a flex track — nothing scales down with a viewBox.
export function hbars({ rows, color = "accent", unit = "", labelWidth = 150 }) {
  const wrap = el("div", { class: "chart hbars" });
  if (!rows.length) {
    wrap.append(el("div", { class: "chart-empty" }, "nothing to compare"));
    return wrap;
  }
  const max = Math.max(...rows.map((r) => r.value), 1);
  for (const row of rows) {
    const fill = el("div", { class: "hbar-fill", style: { width: `${Math.max(row.value ? 1.5 : 0, row.value / max * 100).toFixed(1)}%`, background: seriesColor(row.color || color) } });
    const line = el("div", { class: `hbar-row${row.onclick ? " pick" : ""}`, role: row.onclick ? "button" : undefined, tabindex: row.onclick ? 0 : undefined,
                             onclick: row.onclick, onkeydown: row.onclick ? (e) => { if (e.key === "Enter") row.onclick(); } : undefined },
      el("div", { class: "hbar-label", style: { flexBasis: `${labelWidth}px` }, title: row.sub ? `${row.label} — ${row.sub}` : row.label }, row.label),
      el("div", { class: "hbar-track" }, fill),
      el("div", { class: "hbar-value" }, `${fmtNum(row.value)}${unit}`));
    line.addEventListener("pointermove", (e) => showTip(e.clientX, e.clientY,
      ...tipRows(row.label, [[row.sub || "count", `${fmtNum(row.value)}${unit}`, row.color || color]])));
    line.addEventListener("pointerleave", hideTip);
    wrap.append(line);
  }
  return wrap;
}

// ── part-to-whole: one horizontal stacked bar with a legend that carries the counts ───────────
export function partToWhole({ segments, height = 14, onPick }) {
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  const wrap = el("div", { class: "chart ptw" });
  const bar = el("div", { class: "ptw-bar", style: { height: `${height}px` } });
  segments.filter((s) => s.value > 0).forEach((s) => {
    // a button only when a click does something — inert focus stops are noise to a keyboard user
    const seg = el(onPick ? "button" : "div", {
      class: `ptw-seg${s.on ? " on" : ""}`, type: onPick ? "button" : undefined,
      style: { flex: `${s.value} 0 0`, background: seriesColor(s.color || s.key) },
      "aria-label": `${s.label}: ${s.value}`, role: onPick ? undefined : "img",
      "aria-pressed": onPick ? String(Boolean(s.on)) : undefined,
      onclick: onPick ? () => onPick(s.key) : undefined,
    });
    seg.addEventListener("pointermove", (e) => showTip(e.clientX, e.clientY,
      ...tipRows(s.label, [["count", s.value, s.color || s.key],
        ["share", `${total ? Math.round(s.value / total * 100) : 0}%`, "other"]])));
    seg.addEventListener("pointerleave", hideTip);
    bar.append(seg);
  });
  if (!total) bar.append(el("div", { class: "ptw-seg empty-seg" }));
  wrap.append(bar, el("div", { class: "legend counts" }, segments.map((s) =>
    el(onPick ? "button" : "span", { class: `legend-item${s.on ? " on" : ""}${onPick ? " pick" : ""}`, type: onPick ? "button" : undefined,
                   "aria-pressed": onPick ? String(Boolean(s.on)) : undefined,
                   onclick: onPick ? () => onPick(s.key) : undefined },
      el("span", { class: "swatch", style: { background: seriesColor(s.color || s.key) } }),
      el("strong", {}, fmtNum(s.value)), el("span", {}, s.label)))));
  return wrap;
}

// ── sparkline — 12-ish points, the de-emphasis hue, the last point in the accent ─────────────
export function sparkline({ values, width = 120, height = 32, color = "accent" }) {
  const node = svg("svg", { viewBox: `0 0 ${width} ${height}`, class: "spark", "aria-hidden": "true" });
  if (!values.length) return node;
  const max = Math.max(...values, 1);
  const step = values.length > 1 ? (width - 8) / (values.length - 1) : 0;
  const points = values.map((v, i) => [4 + i * step, height - 4 - (height - 8) * (v / max)]);
  const d = points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const area = svg("path", { d: `${d} L${points[points.length - 1][0]} ${height - 4} L4 ${height - 4} Z`, class: "spark-area" });
  area.setAttribute("fill", seriesColor(color));
  const line = svg("path", { d, class: "spark-line", fill: "none" });
  line.setAttribute("stroke", seriesColor(color));
  const [lx, ly] = points[points.length - 1];
  const dot = svg("circle", { cx: lx, cy: ly, r: 3.5, class: "spark-dot" });
  dot.setAttribute("fill", seriesColor(color));
  node.append(area, line, dot);
  return node;
}

// ── meter — the fill carries severity; the track is a lighter step of the same ramp ──────────
export function meter({ value, max, tone = "accent", label }) {
  const ratio = max ? Math.min(1, Math.max(0, value / max)) : 0;
  return el("div", { class: `meter tone-${tone}`, role: "meter", "aria-valuenow": value, "aria-valuemax": max, "aria-label": label },
    el("div", { class: "meter-fill", style: { width: `${(ratio * 100).toFixed(1)}%` } }));
}

// ── histogram of samples (ms) into `bins` equal-width buckets ─────────────────────────────────
export function histogram({ samples, bins = 12, color = "accent", unit = "s", divide = 1000 }) {
  if (!samples.length) return el("div", { class: "chart" }, el("div", { class: "chart-empty" }, "no samples yet"));
  const values = samples.map((v) => v / divide);
  const max = Math.max(...values);
  const width = max / bins || 1;
  const counts = Array.from({ length: bins }, () => 0);
  values.forEach((v) => { counts[Math.min(bins - 1, Math.floor(v / width))] += 1; });
  const rows = counts.map((n, i) => ({
    x: `${Math.round(i * width)}`, label: `${Math.round(i * width)}–${Math.round((i + 1) * width)}${unit}`, values: { n },
  }));
  return stackedColumns({ series: [{ key: "n", label: "filings", color }], rows, height: 120, maxBar: 40 });
}

// ── a run strip: one small column per run, height = duration, colour = outcome ───────────────
export function runStrip({ runs, height = 54, width = 640, tone = (status) => status }) {
  const wrap = el("div", { class: "chart" });
  if (!runs.length) {
    wrap.append(el("div", { class: "chart-empty" }, "no runs recorded"));
    return wrap;
  }
  const ordered = [...runs].reverse();
  const durations = ordered.map((r) => r.duration_s || 0);
  const max = Math.max(...durations, 1);
  const slot = width / Math.max(ordered.length, 12);
  const barW = Math.max(4, Math.min(14, slot - 3));
  const node = svg("svg", { viewBox: `0 0 ${width} ${height}`, class: "chart-svg", role: "img", "aria-label": "runs" });
  ordered.forEach((run, i) => {
    const h = Math.max(4, (height - 8) * ((run.duration_s || 0) / max));
    const x = i * slot + (slot - barW) / 2;
    const bar = roundedTop(x, height - 4 - h, barW, h, 3);
    bar.setAttribute("fill", seriesColor(tone(run.status)));
    const hit = svg("rect", { x: i * slot, y: 0, width: slot, height, fill: "transparent", class: "hit" });
    hit.addEventListener("pointermove", (e) => showTip(e.clientX, e.clientY, ...tipRows(run.when || "", [
      ["status", run.status, tone(run.status)],
      ["duration", run.duration_s !== null && run.duration_s !== undefined ? `${run.duration_s}s` : "—", "other"],
      ...(run.detail ? [["", run.detail, "other"]] : []),
    ])));
    hit.addEventListener("pointerleave", hideTip);
    node.append(bar, hit);
  });
  node.append(svg("line", { x1: 0, x2: width, y1: height - 4, y2: height - 4, class: "baseline" }));
  wrap.append(node);
  return wrap;
}

// ── the chart card: title, the chart, and its table twin one toggle away ──────────────────────
export function chartCard({ title, sub, chart, tableSpec, actions = [], cls = "" }) {
  let showingTable = false;
  const body = el("div", { class: "chart-body" }, chart);
  const toggle = tableSpec ? el("button", {
    class: "btn small ghost", type: "button", "aria-pressed": "false",
    onclick: () => {
      showingTable = !showingTable;
      toggle.setAttribute("aria-pressed", String(showingTable));
      body.replaceChildren(showingTable ? table(tableSpec.headers, tableSpec.rows, { dense: true, empty: "no rows" }) : chart);
      toggle.replaceChildren(icon(showingTable ? "chart" : "table", 14), showingTable ? "Chart" : "Table");
    },
  }, icon("table", 14), "Table") : null;
  return el("section", { class: `card chart-card ${cls}`.trim() },
    el("div", { class: "card-head" },
      el("div", { class: "card-title" }, el("h2", {}, title), sub ? el("div", { class: "sub" }, sub) : null),
      el("div", { class: "spacer" }), ...actions, toggle),
    body);
}

// Fill a day series so every day in the window has a row (charts must not skip quiet days).
export function fillDays(rows, days, blank) {
  const byDay = new Map(rows.map((r) => [r.day, r]));
  const out = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i -= 1) {
    const d = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() - i));
    const key = d.toISOString().slice(0, 10);
    out.push(byDay.get(key) || { day: key, ...blank });
  }
  return out;
}
