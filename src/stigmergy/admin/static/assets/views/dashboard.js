// The dashboard: what is waiting on a person, and what happened to what arrived. Polls every
// 30 s while visible — the only view that does — and returns a cleanup.

import { api } from "../api.js";
import { chartCard, fillDays, histogram, partToWhole, seriesColor, sparkline, stackedColumns } from "../charts.js";
import { decisionVerb, door, itemKind, jobName, OUTCOME_ORDER, status as statusCopy } from "../copy.js";
import { getMeta, windowDays } from "../state.js";
import {
  agoFrom, card, el, fmtAge, fmtDay, fmtMs, fmtNum, fmtWhen, keyDot, link, pill, relTime, render,
  severityPill, svg, table, tile, wordPill,
} from "../ui.js";
import { loading } from "./common.js";

export async function dashboardView(host) {
  let alive = true;
  const draw = async () => {
    const days = windowDays();
    const [overview, inbox, metrics, worker] = await Promise.all([
      api.get("overview"), api.get("inbox"), api.get(`metrics?days=${days}`), api.get("worker"),
    ]);
    if (!alive) return;
    render(host,
      hero(inbox, overview, pipelineCard(metrics, days)),
      el("div", { class: "grid halves" },
        capturesChart(metrics, days),
        questionsChart(metrics, days)),
      el("div", { class: "grid tiles" }, ...healthTiles(overview, worker, metrics)),
      el("div", { class: "grid halves" },
        latencyCard(metrics, worker),
        jobsCard(overview, metrics)),
      el("div", { class: "grid halves" },
        activityCard(overview, metrics),
        overview.ingest_errors.rows.length ? ingestCard(overview) : gardenerCard(overview)),
    );
  };
  await loading(host, draw);
  const timer = setInterval(() => { if (!document.hidden) draw().catch(() => {}); }, 30000);
  return () => { alive = false; clearInterval(timer); };
}

// ── the hero: the inbox is the number that means work ─────────────────────────────────────────
function hero(inbox, overview, pipelineNode) {
  const counts = inbox.counts || {};
  const rows = [
    ["identity-proposal", counts["identity-proposal"] || 0, "proposed entities", "inbox/identity"],
    ["alias-proposal", counts["alias-proposal"] || 0, "proposed spellings", "inbox/alias"],
    ["repair-proposal", counts["repair-proposal"] || 0, "repair proposals", "inbox/repair"],
  ];
  return el("div", { class: "hero-wrap" },
    el("div", { class: "hero-card" },
      el("div", { class: "eyebrow" }, "waiting on a steward"),
      el("div", { class: `hero-number${inbox.count ? "" : " calm"}` }, String(inbox.count)),
      el("div", { class: "hero-label" }, inbox.count === 1 ? "thing owes a steward a decision"
        : inbox.count ? "things owe a steward a decision" : "nothing is waiting on anyone right now"),
      el("div", { class: "hero-breakdown" },
        rows.map(([kind, n, label, hash]) => el("a", { href: `#/${hash}` },
          keyDot(itemKind(kind).who), el("strong", {}, String(n)), el("span", {}, label)))),
      el("div", { class: "hr" }),
      el("div", { class: "statline" },
        el("span", {}, "in flight ", el("strong", {}, String(overview.in_flight.length))),
        el("span", {}, "queued ", el("strong", {}, String(overview.queue.counts.queued || 0)))),
      el("div", { class: "row", style: { marginTop: "14px" } },
        link("inbox", el("span", { class: "btn primary small" }, "Open the inbox")),
        link("captures", el("span", { class: "btn small" }, "All captures")))),
    pipelineNode);
}

// ── the pipeline: the write path with live counts — colour is who decides ─────────────────────
function pipelineCard(metrics, days) {
  // Summed over the SERVER's status vocabulary, so a status added to the queue cannot silently
  // drop out of "captured"; the frontend only knows how to say each one.
  const meta = getMeta();
  const statuses = meta.statuses && meta.statuses.length ? meta.statuses : OUTCOME_ORDER;
  const sums = { total: 0 };
  for (const s of statuses) sums[s] = 0;
  for (const row of metrics.captures_by_day) {
    for (const s of statuses) { sums[s] += row[s] || 0; sums.total += row[s] || 0; }
  }
  const landed = sums.filed || 0;
  const byHand = sums.resolved || 0;
  const refused = sums.rejected || 0;
  const broke = sums.failed || 0;
  const moving = (sums.queued || 0) + (sums.claimed || 0);
  const W = 860, H = 250;
  const node = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "pipeline", role: "img",
                            "aria-label": `pipeline: ${sums.total} captured, ${landed} landed in git, ${byHand} handled by hand, ${refused} refused, ${broke} failed, ${moving} in flight` });
  const stage = (x, y, w, h, eyebrowText, number, label, who) => {
    const g = svg("g");
    g.append(svg("rect", { x, y, width: w, height: h, rx: 12, class: "pipe-box" }));
    if (who) g.append(svg("rect", { x, y, width: 4, height: h, rx: 2, fill: seriesColor(who) }));
    g.append(svg("text", { x: x + 16, y: y + 22, class: "pipe-eyebrow" }, eyebrowText));
    if (number !== null) g.append(svg("text", { x: x + 16, y: y + 50, class: "pipe-num" }, String(number)));
    g.append(svg("text", { x: x + 16, y: number !== null ? y + 70 : y + 48, class: "pipe-label" }, label));
    return g;
  };
  const flow = (x1, y1, x2, y2, who, weight) => {
    const c = (x1 + x2) / 2;
    const path = svg("path", { d: `M${x1} ${y1} C${c} ${y1} ${c} ${y2} ${x2} ${y2}`, class: "pipe-flow" });
    path.setAttribute("stroke", seriesColor(who));
    path.setAttribute("stroke-width", String(Math.max(2, Math.min(14, weight))));
    return path;
  };
  const scale = sums.total ? 14 / sums.total : 0;
  // stages — the two in the middle carry no count: every capture passes through both, so the
  // number would repeat "captured" and invite a reconciliation the outcomes already give
  node.append(stage(0, 80, 150, 84, "captured", sums.total, `in the last ${days} days`, null));
  node.append(stage(200, 80, 150, 84, "the model drafts", null, "a page, proposed", "model"));
  node.append(stage(400, 80, 150, 84, "code gates", null, "nine deterministic gates", "code"));
  const outs = [
    [landed, "landed in git", "git", "filed — proposing what it had to"],
    [byHand, "handled by hand", "human", "legacy: closed by a steward before captures stopped parking"],
    [refused, "refused", "code", "by a gate, or declined by a steward"],
    [broke, "could not finish", "fail", "failed — deliveries exhausted"],
  ];
  outs.forEach(([n, label, who, sub], i) => {
    const y = 8 + i * 48;
    const g = svg("g");
    g.append(svg("circle", { cx: 612, cy: y + 14, r: 6, fill: seriesColor(who) }));
    g.append(svg("text", { x: 626, y: y + 12, class: "pipe-num", style: { fontSize: "18px" } }, String(n)));
    g.append(svg("text", { x: 626 + 14 + String(n).length * 11, y: y + 12, class: "pipe-label" }, label));
    g.append(svg("text", { x: 626, y: y + 28, class: "pipe-eyebrow" }, sub));
    node.append(g);
    node.append(flow(550, 122, 604, y + 14, who, n * scale));
  });
  node.append(flow(150, 122, 200, 122, "model", (sums.total - moving) * scale || 2));
  node.append(flow(350, 122, 400, 122, "code", (sums.total - moving) * scale || 2));
  if (moving) {
    node.append(svg("text", { x: 0, y: 196, class: "pipe-label" }, `${moving} still moving — queued or being filed right now`));
  }
  node.append(svg("text", { x: 0, y: 240, class: "pipe-eyebrow" }, "colour is who decides · amber a human · violet the model · grey code · green git · red broke"));
  return el("div", { class: "card", style: { marginBottom: 0 } },
    el("div", { class: "card-head" },
      el("div", { class: "card-title" }, el("h2", {}, "The write path, live"),
        el("div", { class: "sub" }, "what happened to everything that arrived in the window: the model drafts, code decides, and each capture lands, parks or is refused"))),
    node);
}

// ── charts ────────────────────────────────────────────────────────────────────────────────────
function capturesChart(metrics, days) {
  const series = OUTCOME_ORDER.map((key) => ({ key, label: statusCopy(key).short, color: statusCopy(key).who }));
  const rows = fillDays(metrics.captures_by_day, days, Object.fromEntries(OUTCOME_ORDER.map((s) => [s, 0])))
    .map((r) => ({ x: r.day, label: fmtDay(r.day), values: r }));
  const total = metrics.captures_by_day.reduce((sum, r) => sum + OUTCOME_ORDER.reduce((a, s) => a + (r[s] || 0), 0), 0);
  return chartCard({
    title: "Captures per day", sub: `${fmtNum(total)} arrived in ${days} days, by what became of them`,
    chart: stackedColumns({ series, rows, height: 190 }),
    tableSpec: { headers: ["day", ...series.map((s) => s.label)],
      rows: rows.filter((r) => OUTCOME_ORDER.some((s) => r.values[s])).map((r) => ({ cells: [r.label, ...OUTCOME_ORDER.map((s) => String(r.values[s] || 0))] })) },
  });
}

function questionsChart(metrics, days) {
  const series = [
    { key: "answered_with_citation", label: "answered, cited", color: "git" },
    { key: "answered_no_citation", label: "answered, uncited", color: "human" },
    { key: "refused", label: "honest refusal", color: "code" },
    { key: "errors", label: "errored", color: "fail" },
  ];
  const rows = fillDays(metrics.asks_by_day, days, { answered_with_citation: 0, answered_no_citation: 0, refused: 0, errors: 0, unrecorded: 0 })
    .map((r) => ({ x: r.day, label: fmtDay(r.day), values: r }));
  const totals = metrics.asks_by_day.reduce((acc, r) => {
    for (const s of series) acc[s.key] = (acc[s.key] || 0) + (r[s.key] || 0);
    return acc;
  }, {});
  const asked = Object.values(totals).reduce((a, b) => a + b, 0);
  const cited = asked ? Math.round((totals.answered_with_citation || 0) / asked * 100) : null;
  return chartCard({
    title: "Questions per day",
    sub: asked ? `${fmtNum(asked)} asked · ${cited}% answered with a citation · a system that never refuses is the failure` : "no questions asked in this window",
    chart: stackedColumns({ series, rows, height: 190 }),
    tableSpec: { headers: ["day", ...series.map((s) => s.label)],
      rows: rows.filter((r) => series.some((s) => r.values[s.key])).map((r) => ({ cells: [r.label, ...series.map((s) => String(r.values[s.key] || 0))] })) },
  });
}

function healthTiles(overview, worker, metrics) {
  const builtAgo = agoFrom(overview.crons.index_built_at);
  const severity = overview.gardener.severity_counts || {};
  const findings = Object.values(severity).reduce((a, b) => a + b, 0);
  const webhook = metrics.job_history["webhook-index-upsert"] || [];
  const perDay = fillDays(Object.values(webhook.reduce((acc, r) => {
    const day = (r.started_at || "").slice(0, 10);
    acc[day] = acc[day] || { day, n: 0 };
    acc[day].n += 1;
    return acc;
  }, {})), 14, { n: 0 }).map((r) => r.n);
  const filedPerDay = fillDays(metrics.captures_by_day, 14, { filed: 0 }).map((r) => r.filed || 0);
  return [
    tile("Index freshness", builtAgo === null ? "never" : fmtAge(builtAgo),
      overview.crons.index_built_at ? `rebuilt ${fmtWhen(overview.crons.index_built_at)} · ${webhook.length ? `${webhook.length} incremental upserts on record` : "no incremental upserts yet"}` : "no index yet",
      { tone: builtAgo !== null && builtAgo > 2 * 86400000 ? "warn" : "", onclick: () => { window.location.hash = "#/index"; },
        foot: el("div", { class: "spark" }, sparkline({ values: perDay, color: "accent" })) }),
    tile("Filed per day", String(filedPerDay.at(-1) || 0), "landed in git today · last 14 days",
      { who: "git", onclick: () => { window.location.hash = "#/captures"; }, foot: el("div", { class: "spark" }, sparkline({ values: filedPerDay, color: "git" })) }),
    tile("Worker", worker.in_flight.length ? `${worker.in_flight.length} in flight` : "idle",
      worker.in_flight.length ? `#${worker.in_flight[0].id} held ${fmtMs(worker.in_flight[0].claimed_age_ms)} of a ${worker.visibility_timeout_s}s lease` : `lease ${worker.visibility_timeout_s}s · ${worker.counts.queued || 0} queued`,
      { who: "model", tone: worker.in_flight.some((r) => r.lease_expired) ? "warn" : "", onclick: () => { window.location.hash = "#/worker"; } }),
    tile("Ingest errors", String(overview.ingest_errors.unresolved), overview.ingest_errors.unresolved ? "unresolved — a capture the librarian could not finish" : "nothing unresolved",
      { who: overview.ingest_errors.unresolved ? "fail" : "code", onclick: () => { window.location.hash = "#/worker"; } }),
    tile("Gardener findings", String(findings),
      overview.gardener.run ? `${Object.entries(severity).map(([s, n]) => `${n} ${s}`).join(" · ") || "none"} · run ${relTime(overview.gardener.run.finished_at)}` : "no completed run yet",
      { who: "code", onclick: () => { window.location.hash = "#/gardener"; } }),
  ];
}

function latencyCard(metrics, worker) {
  const lat = worker.latency;
  return chartCard({
    title: "Capture → filed",
    sub: lat.enough_data ? `p50 ${fmtMs(lat.p50_ms)} · p95 ${fmtMs(lat.p95_ms)} over ${lat.samples} filings` : `${lat.samples} filing(s) so far — ${lat.min_samples} needed before percentiles mean anything`,
    chart: histogram({ samples: metrics.filed_latency_ms, color: "git" }),
    tableSpec: { headers: ["sample", "seconds"], rows: metrics.filed_latency_ms.slice(0, 50).map((ms, i) => ({ cells: [`#${i + 1}`, (ms / 1000).toFixed(1)] })) },
  });
}

function jobsCard(overview, metrics) {
  const rows = [];
  for (const [file, run] of Object.entries(overview.crons.latest_runs || {})) {
    rows.push({ cells: [jobName(run ? run.job : file.replace(".yml", "")), run ? wordPill(run.status) : pill("never ran", "neutral"),
      run ? relTime(run.finished_at) : "—", run && run.error ? el("span", { class: "muted" }, run.error) : ""] });
  }
  rows.push({ cells: ["Index rebuild", pill(overview.crons.index_built_at ? "built" : "no index", overview.crons.index_built_at ? "git" : "fail"),
    overview.crons.index_built_at ? relTime(overview.crons.index_built_at) : "—", el("span", { class: "muted" }, "truth: the index's built_at")] });
  const digest = (metrics.job_history.digest || [])[0];
  rows.push({ cells: ["Weekly digest", digest ? wordPill(digest.status) : pill("never posted", "neutral"), digest ? relTime(digest.finished_at) : "—", el("span", { class: "muted" }, overview.digest.last_window_until ? `window ends ${fmtWhen(overview.digest.last_window_until)}` : "command-only")] });
  return card({ title: "Scheduled work", sub: "the last known truth for each job", actions: [link("jobs", el("span", { class: "btn small ghost" }, "Jobs"))] },
    table(["job", "last run", "when", ""], rows, { dense: true, empty: "no job has run yet — the four workflows run nightly in GitHub Actions" }));
}

function activityCard(overview, metrics) {
  const events = [];
  for (const a of overview.admin_actions || []) {
    events.push({ at: a.ts, who: "human", head: `${a.actor} · ${a.action}`, note: a.outcome === "ok" ? "" : `${a.outcome} ${a.error_class}`.trim() });
  }
  for (const d of (metrics.decisions || []).slice(0, 6)) {
    events.push({ at: d.created_at, who: "human", head: `${d.actor} ${decisionVerb(d.verdict)} ${itemKind(d.kind).label.toLowerCase()} #${d.id}`, note: `via ${door(d.source)}` });
  }
  events.sort((a, b) => (b.at || "").localeCompare(a.at || ""));
  return card({ title: "Recent decisions and console actions", sub: "the two ledgers, merged", actions: [link("activity", el("span", { class: "btn small ghost" }, "Activity"))] },
    events.length ? el("ul", { class: "timeline" }, events.slice(0, 8).map((e) => el("li", {},
      el("div", { class: `tl-dot k-${e.who}` }),
      el("div", { class: "what" },
        el("div", { class: "head" }, el("span", {}, e.head), el("span", { class: "when" }, fmtWhen(e.at))),
        e.note ? el("div", { class: "note" }, e.note) : null))))
      : el("div", { class: "empty" }, el("div", { class: "empty-title" }, "no decisions or console actions yet")));
}

function ingestCard(overview) {
  return card({ title: "Unresolved ingest errors", sub: "captures the librarian could not finish — each burned its attempts" },
    table(["capture", "stage", "error", "attempts", "last seen"],
      overview.ingest_errors.rows.map((r) => ({
        cells: [link(`captures/${r.source_doc_id}`, `#${r.source_doc_id}`), r.stage, el("span", { class: "wrap" }, r.error), String(r.attempts), fmtWhen(r.last_at)],
      }))));
}

function gardenerCard(overview) {
  const severity = overview.gardener.severity_counts || {};
  const segments = ["sla", "warn", "info"].map((s) => ({ key: s, label: s, value: severity[s] || 0, color: s === "sla" ? "fail" : s === "warn" ? "human" : "code" }));
  return card({ title: "Corpus health", sub: overview.gardener.run ? `latest gardener run ${relTime(overview.gardener.run.finished_at)}` : "no completed gardener run yet", actions: [link("gardener", el("span", { class: "btn small ghost" }, "Gardener"))] },
    partToWhole({ segments }),
    el("div", { class: "row", style: { marginTop: "10px" } }, Object.entries(severity).map(([s, n]) => el("span", { class: "row" }, severityPill(s), el("span", { class: "sub" }, `${n}`)))));
}
