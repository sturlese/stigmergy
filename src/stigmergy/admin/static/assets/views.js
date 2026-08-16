// The eleven views. Every one follows the same contract: `render(host, params)` fills a cleared
// container from the JSON API and returns an optional cleanup function (used by views that poll).
// All text lands via textContent (ui.el) — untrusted queue/finding strings are inert here.

import { api } from "./api.js";
import {
  banner, commandBlock, confirmForm, copyButton, el, fmtAge, fmtMs, fmtWhen, agoFrom,
  icon, kv, pill, render, severityPill, skeletons, table, tile, toast,
} from "./ui.js";

let META = { actor_default: "admin-console", github: { configured: false }, workflows: [], entity_types: [] };

export function setMeta(meta) {
  META = meta;
}

function depthLine(counts) {
  const parts = Object.entries(counts || {}).filter(([, n]) => n)
    .map(([status, n]) => `${status}=${n}`);
  return `queue: ${parts.length ? parts.join(" · ") : "empty"}`;
}

function actorField() {
  return {
    name: "actor", label: "Acting as", value: META.actor_default, required: true,
    hint: "attribution, not authorization — recorded on the row's history, like --by",
  };
}

const VERBATIM_HINT = "reaches the submitter verbatim — never a secret, never personal data";

async function loading(host, fn) {
  render(host, skeletons());
  try {
    await fn();
  } catch (ex) {
    render(host, banner("error", ex.message));
  }
}

// ── overview ──────────────────────────────────────────────────────────────────────────────────
export async function overviewView(host) {
  let alive = true;
  const draw = async () => {
    const data = await api.get("overview");
    if (!alive) return;
    const counts = data.queue.counts;
    const severity = data.gardener.severity_counts || {};
    const builtAgo = agoFrom(data.crons.index_built_at);
    render(host, 
      el("div", { class: "grid tiles" },
        tile("Parked on a human", String(data.queue.parked),
          `needs_input=${counts.needs_input || 0} · triage=${counts.triage || 0}`, { hero: true }),
        tile("Queued", String(counts.queued || 0), "waiting for the librarian"),
        tile("In flight", String(data.in_flight.length),
          data.in_flight.length ? `#${data.in_flight[0].id} held ${fmtMs(data.in_flight[0].claimed_age_ms)}` : "nothing claimed"),
        tile("Ingest errors", String(data.ingest_errors.unresolved), "unresolved"),
        tile("Index built", builtAgo === null ? "never" : fmtAge(builtAgo),
          data.crons.index_built_at ? `at ${fmtWhen(data.crons.index_built_at)}` : "no index_meta yet"),
        tile("Gardener findings", String(Object.values(severity).reduce((a, b) => a + b, 0)),
          Object.entries(severity).map(([s, n]) => `${s}=${n}`).join(" · ") || "latest completed run"),
      ),
      el("div", { class: "grid halves" },
        el("div", { class: "card" },
          el("h2", {}, "Crons — last known truth"),
          cronTruthList(data.crons)),
        el("div", { class: "card" },
          el("h2", {}, "Recent console actions"),
          adminActionsTable(data.admin_actions, "no console actions yet")),
      ),
      data.ingest_errors.rows.length
        ? el("div", { class: "card" },
            el("h2", {}, "Unresolved ingest errors"),
            table(["id", "stage", "error", "attempts", "last seen"],
              data.ingest_errors.rows.map((r) => ({
                cells: [`#${r.source_doc_id}`, r.stage, r.error, String(r.attempts), fmtWhen(r.last_at)],
              }))))
        : null,
    );
  };
  await loading(host, draw);
  const timer = setInterval(() => { if (!document.hidden) draw().catch(() => {}); }, 15000);
  return () => { alive = false; clearInterval(timer); };
}

function cronTruthList(crons) {
  const rows = crons.latest_runs || {};
  const items = [];
  for (const [file, run] of Object.entries(rows)) {
    items.push(el("li", {},
      el("span", { class: "when" }, run ? fmtWhen(run.finished_at) : "never"),
      el("div", { class: "what" },
        el("div", {}, file, " ", run ? pill(run.status) : pill("no job_runs row", "neutral")),
        run && run.error ? el("div", { class: "note" }, run.error) : null)));
  }
  items.push(el("li", {},
    el("span", { class: "when" }, crons.index_built_at ? fmtWhen(crons.index_built_at) : "never"),
    el("div", { class: "what" },
      el("div", {}, "index-rebuild.yml ", pill(crons.index_built_at ? "built" : "no index", crons.index_built_at ? "good" : "serious")),
      el("div", { class: "note" }, "truth source: index_meta.built_at — a rebuild writes no job_runs row"))));
  return el("ul", { class: "timeline" }, items);
}

function adminActionsTable(rows, empty) {
  return table(["when", "actor", "action", "outcome"],
    (rows || []).map((r) => ({
      cells: [fmtWhen(r.ts), r.actor, r.action,
        el("span", {}, pill(r.outcome), r.error_class ? ` ${r.error_class}` : "")],
    })), { empty });
}

// ── queue ─────────────────────────────────────────────────────────────────────────────────────
const QUEUE_STATUSES = ["queued", "claimed", "filed", "rejected", "resolved", "needs_input", "triage", "failed"];
const PARKED = new Set(["needs_input", "triage"]);
const queueFilter = { statuses: new Set(), submitter: "" };

export async function queueView(host) {
  await loading(host, async () => {
    const query = [...queueFilter.statuses].map((s) => `status=${s}`);
    if (queueFilter.submitter) query.push(`submitter=${encodeURIComponent(queueFilter.submitter)}`);
    query.push("limit=100");
    const data = await api.get(`queue?${query.join("&")}`);
    render(host, 
      el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h2", {}, depthLine(data.counts)),
          el("div", { class: "spacer" }),
          el("button", { class: "btn small", onclick: () => reclaimFlow() }, icon("refresh", 14), "Reclaim leases"),
          el("button", { class: "btn small", onclick: () => purgeFlow() }, "Retention purge"),
        ),
        el("div", { class: "chiprow" },
          QUEUE_STATUSES.map((s) => el("button", {
            class: `chip${queueFilter.statuses.has(s) ? " on" : ""}`,
            onclick: () => {
              queueFilter.statuses.has(s) ? queueFilter.statuses.delete(s) : queueFilter.statuses.add(s);
              queueView(host);
            },
          }, s.replaceAll("_", " "))),
          el("input", {
            type: "text", placeholder: "submitter…", value: queueFilter.submitter,
            onchange: (e) => { queueFilter.submitter = e.target.value.trim(); queueView(host); },
          })),
        table(
          ["id", "status", "kind", "submitted by", "att.", "created", "waiting on", "material"],
          data.submissions.map((row) => ({
            row,
            cells: [
              `#${row.id}`, pill(row.status), row.kind, row.submitted_by, String(row.attempts),
              fmtWhen(row.created_at),
              row.waiting_on ? `${row.waiting_on} · ${fmtAge(row.parked_age_ms)}` : "—",
              materialCell(row),
            ],
          })),
          { empty: "no submissions", onRow: (row) => { window.location.hash = `#/queue/${row.id}`; } })));
  });
}

function materialCell(row) {
  if (row.payload_purged) return el("em", {}, "(payload purged)");
  if (row.withheld_reason) return el("em", {}, `(${row.withheld_reason})`);
  const text = (row.excerpt || "").slice(0, 140);
  const parts = [text || "—"];
  if (row.flagged_hints && row.flagged_hints.length) {
    parts.push(" ", pill(`flagged: ${row.flagged_hints.join(",")}`, "serious"));
  }
  return el("span", {}, ...parts);
}

export async function queueDetailView(host, id) {
  await loading(host, async () => {
    const row = await api.get(`queue/${id}`);
    const parked = PARKED.has(row.status);
    const disabledHint = parked ? undefined
      : "only a parked row (needs_input / triage) can be disposed — a worker-held or terminal row is refused";
    render(host, 
      el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h2", {}, `capture #${row.id}`), pill(row.status), el("span", { class: "sub" }, row.kind),
          el("div", { class: "spacer" }),
          el("button", { class: "btn small", disabled: !parked, onclick: () => requeueFlow(row) }, "Requeue"),
          el("button", { class: "btn small", disabled: !parked, onclick: () => resolveFlow(row) }, "Resolve"),
          el("button", { class: "btn small danger", disabled: !parked, onclick: () => rejectFlow(row) }, "Reject"),
        ),
        !parked ? el("div", { class: "sub" }, disabledHint) : null,
        kv([
          ["submitted by", row.submitted_by],
          ["created", `${fmtWhen(row.created_at)}`],
          ["claimed", row.claimed_at ? `${fmtWhen(row.claimed_at)} (queue wait ${fmtMs(row.queue_wait_ms)})` : "—"],
          ["finished", row.finished_at ? `${fmtWhen(row.finished_at)} (total ${fmtMs(row.total_latency_ms)})` : "—"],
          ["attempts", String(row.attempts)],
          ["blob refs", row.blob_refs.length ? el("span", { class: "mono" }, row.blob_refs.join(", ")) : "(none)"],
          ["result", row.result_ref ? el("span", { class: "mono" }, row.result_ref) : null],
          ["parked", row.waiting_on ? `${fmtAge(row.parked_age_ms)} — waiting on ${row.waiting_on}` : null],
        ])),
      row.status === "needs_input" && row.error
        ? el("div", { class: "card" },
            el("h2", {}, "The question, whole"),
            el("pre", { class: "pre" }, row.error),
            row.reply_invocation ? commandBlock(row.reply_invocation) : null)
        : row.error
          ? el("div", { class: "card" }, el("h2", {}, "Note"), el("pre", { class: "pre" }, row.error))
          : null,
      row.reply
        ? el("div", { class: "card" }, el("h2", {}, "The submitter's reply"), el("pre", { class: "pre" }, row.reply))
        : row.withheld_reason
          ? el("div", { class: "card" }, el("h2", {}, "Reply"), el("em", {}, `(${row.withheld_reason})`))
          : null,
      row.payload_purged
        ? banner("info", "payload purged by retention; the evidence blob is unaffected") : null,
      el("div", { class: "card" },
        el("h2", {}, "History"),
        row.events.length
          ? el("ul", { class: "timeline" }, row.events.map((e) => el("li", {},
              el("span", { class: "when" }, fmtWhen(e.at)),
              el("div", { class: "what" },
                el("div", {}, e.event, e.actor ? ` · by ${e.actor}` : ""),
                e.note ? el("div", { class: "note" }, e.note) : null))))
          : el("div", { class: "empty" }, "no events recorded")),
    );
  });
}

async function mutate(path, body, successMessage) {
  try {
    const result = await api.post(path, body);
    toast(successMessage, "good");
    if (result && result.warning) toast(result.warning, "error");
    return true;
  } catch (ex) {
    toast(ex.message, "error");
    return false;
  }
}

async function requeueFlow(row) {
  const answer = await confirmForm({
    title: `Requeue #${row.id}`,
    consequence: "sends the row back to the queue for the librarian to try again — attempts unchanged, claimable immediately.",
    fields: [actorField(), { name: "note", label: "Note", kind: "textarea", hint: "for the row's own history (not shown to the submitter)" }],
    confirmLabel: "Requeue",
  });
  if (answer && await mutate(`queue/${row.id}/requeue`, answer.values, `requeued #${row.id}`)) {
    window.location.hash = "#/queue";
  }
}

async function resolveFlow(row) {
  const answer = await confirmForm({
    title: `Resolve #${row.id}`,
    consequence: "closes the row as handled by hand; your note becomes the submitter's report.",
    fields: [
      actorField(),
      { name: "note", label: "Note", kind: "textarea", required: true, hint: VERBATIM_HINT, warnHint: true },
      { name: "page", label: "Page the material ended up in", hint: "echoed to the submitter — leave both empty and their report has no pointer" },
      { name: "commit", label: "Commit that carried it" },
    ],
    confirmLabel: "Resolve",
  });
  if (answer && await mutate(`queue/${row.id}/resolve`, answer.values, `resolved #${row.id} — the submitter's report now says so`)) {
    window.location.hash = "#/queue";
  }
}

async function rejectFlow(row) {
  const answer = await confirmForm({
    title: `Reject #${row.id}`,
    consequence: "closes the row as declined, with your name on the decision.",
    fields: [actorField(), { name: "reason", label: "Reason", kind: "textarea", required: true, hint: VERBATIM_HINT, warnHint: true }],
    confirmLabel: "Reject", danger: true,
  });
  if (answer && await mutate(`queue/${row.id}/reject`, answer.values, `rejected #${row.id} — reason recorded in the submitter's report`)) {
    window.location.hash = "#/queue";
  }
}

async function reclaimFlow() {
  const answer = await confirmForm({
    title: "Reclaim expired leases",
    consequence: "returns timed-out claims to the queue (attempts +1); a row past its delivery budget is failed instead, with an ingest error recorded. 'Release everything now' pulls EVERY claimed row — only safe with no live worker mid-item.",
    fields: [actorField(), { name: "now", label: "Release everything now (visibility-timeout 0)", kind: "checkbox" }],
    confirmLabel: "Reclaim",
  });
  if (!answer) return;
  const body = { actor: answer.values.actor };
  if (answer.values.now) body.visibility_timeout_s = 0;
  try {
    const result = await api.post("queue/reclaim", body);
    toast(`released ${result.released} expired claim(s); failed ${result.failed} that exhausted their attempts`, "good");
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } catch (ex) {
    toast(ex.message, "error");
  }
}

async function purgeFlow() {
  let preview;
  try {
    preview = await api.post("queue/purge", { dry_run: true });
  } catch (ex) {
    toast(ex.message, "error");
    return;
  }
  const answer = await confirmForm({
    title: "Retention purge",
    consequence: `dry run first: would purge payload+hints of ${preview.purged} terminal submission(s)`
      + `${preview.ids && preview.ids.length ? ` (ids: ${preview.ids.join(", ")})` : ""}. `
      + "id, submitter, timestamps, status and result_ref survive; evidence blobs are untouched. Confirm runs it for real.",
    fields: [actorField()],
    confirmLabel: `Purge ${preview.purged} row(s)`, danger: true,
  });
  if (!answer) return;
  try {
    const result = await api.post("queue/purge", { actor: answer.values.actor });
    toast(`purged payload+hints of ${result.purged} submission(s)`, "good");
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } catch (ex) {
    toast(ex.message, "error");
  }
}

// ── crons ─────────────────────────────────────────────────────────────────────────────────────
export async function cronsView(host) {
  await loading(host, async () => {
    const data = await api.get("crons");
    const children = [];
    if (!data.configured) {
      children.push(banner("info",
        "GitHub is not configured (no admin token for Actions) — this page shows the database ",
        "truth only; Run-now and Enable/Disable need the token. The gh CLI still works from a terminal."));
    }
    if (data.github_error) children.push(banner("error", `GitHub degraded: ${data.github_error}`));
    for (const w of data.workflows) {
      children.push(cronCard(w, data.configured && !data.github_error));
    }
    render(host, ...children);
  });
}

function cronCard(w, live) {
  const truth = w.truth === "index_meta.built_at"
    ? el("span", {}, pill(w.index_built_at ? "built" : "never built", w.index_built_at ? "good" : "serious"),
        ` index_meta.built_at · ${w.index_built_at ? fmtWhen(w.index_built_at) : "no index yet"}`)
    : w.latest_run
      ? el("span", {}, pill(w.latest_run.status), ` ${w.latest_run.job} · finished ${fmtWhen(w.latest_run.finished_at)}`)
      : el("span", {}, pill("no run recorded", "neutral"));
  const dispatchConsequence = {
    "index-rebuild.yml": "runs a FULL staging index rebuild in GitHub Actions — real embedder, real spend, against the staging database.",
    "retention-purge.yml": "runs the capture-queue retention purge in GitHub Actions against staging.",
    "gardener.yml": "runs the eight deterministic checks AND the model editorial sweep in GitHub Actions — real model spend; findings persist to staging.",
  }[w.file];
  return el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("h2", {}, w.title),
      w.state ? pill(w.state) : null,
      el("span", { class: "sub mono" }, w.file),
      el("span", { class: "sub" }, `cron: ${w.schedule_utc} UTC`),
      el("div", { class: "spacer" }),
      el("button", { class: "btn small primary", disabled: !live, onclick: () => dispatchFlow(w, dispatchConsequence) },
        icon("play", 14), "Run now"),
      w.state === "active"
        ? el("button", { class: "btn small", disabled: !live, onclick: () => enableFlow(w, false) }, "Disable")
        : el("button", { class: "btn small", disabled: !live, onclick: () => enableFlow(w, true) }, "Enable")),
    el("div", { class: "sub", style: "margin-bottom:10px" }, "database truth: ", truth),
    (w.runs || []).length
      ? table(["run", "status", "conclusion", "trigger", "started", ""],
          w.runs.map((r) => ({
            cells: [r.display_title || `#${r.id}`, pill(r.status), r.conclusion ? pill(r.conclusion) : "—",
              r.event, fmtWhen(r.created_at),
              el("a", { href: r.html_url, target: "_blank", rel: "noopener noreferrer" }, icon("external", 14), " logs")],
          })))
      : el("div", { class: "empty" }, live ? "no recent runs listed" : "recent Actions runs need the GitHub token"));
}

async function dispatchFlow(w, consequence) {
  const fields = [actorField()];
  if ((w.dispatch_inputs || []).includes("dry_run")) {
    fields.push({ name: "dry_run", label: "Dry run (preview only — list what would be purged, change nothing)", kind: "checkbox" });
  }
  const answer = await confirmForm({
    title: `Run ${w.title} now`, consequence, fields, confirmLabel: "Dispatch",
  });
  if (!answer) return;
  const inputs = {};
  if (answer.values.dry_run) inputs.dry_run = true;
  await mutate(`crons/${w.file}/dispatch`, { actor: answer.values.actor, inputs },
    `dispatched ${w.file} — it appears in the runs list in a few seconds`);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

async function enableFlow(w, enabled) {
  const answer = await confirmForm({
    title: `${enabled ? "Enable" : "Disable"} ${w.title}`,
    consequence: enabled
      ? "the schedule fires again from tonight."
      : "the schedule stops firing until re-enabled — what you want while a deploy is carrying an index-schema change. Manual dispatch still works.",
    fields: [actorField()],
    confirmLabel: enabled ? "Enable" : "Disable", danger: !enabled,
  });
  if (!answer) return;
  await mutate(`crons/${w.file}/${enabled ? "enable" : "disable"}`, { actor: answer.values.actor },
    `${w.file} ${enabled ? "enabled" : "disabled"}`);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

// ── gardener ──────────────────────────────────────────────────────────────────────────────────
const gardenerFilter = { severity: "", check: "" };

export async function gardenerView(host) {
  await loading(host, async () => {
    const data = await api.get("gardener");
    const severities = {};
    const checks = new Set();
    for (const f of data.findings) {
      severities[f.severity] = (severities[f.severity] || 0) + 1;
      checks.add(f.check);
    }
    const filtered = data.findings.filter((f) =>
      (!gardenerFilter.severity || f.severity === gardenerFilter.severity)
      && (!gardenerFilter.check || f.check === gardenerFilter.check));
    const sweepError = data.run && data.run.stats && data.run.stats.sweep && data.run.stats.sweep.error;
    // a variable, not an inline object literal, so this button's props object holds no nested
    // `title:` for the disabled-elements guard to trip over (the same shape cronCard uses with `w`).
    const gardenerWorkflow = { file: "gardener.yml", title: "Gardener", dispatch_inputs: [] };
    render(host, 
      el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h2", {}, "Latest completed run"),
          el("div", { class: "spacer" }),
          el("button", {
            class: "btn small primary", disabled: !META.github.configured,
            onclick: () => dispatchFlow(
              gardenerWorkflow,
              "runs the eight deterministic checks AND the model editorial sweep in GitHub Actions — real model spend; findings persist to staging."),
          }, icon("play", 14), "Run now")),
        !META.github.configured
          ? el("div", { class: "sub" }, "needs the GitHub token — or run `stigmergy-gardener` locally")
          : null,
        data.run
          ? kv([
              ["run", `#${data.run.id}`],
              ["finished", fmtWhen(data.run.finished_at)],
              ["findings", Object.entries(severities).map(([s, n]) => `${s}=${n}`).join(" · ") || "none"],
            ])
          : el("div", { class: "empty" }, "no completed gardener run yet"),
        sweepError ? banner("warn",
          `partial run: the deterministic findings above are complete and trustworthy; the model sweep failed (${sweepError}) and produced zero findings this run.`) : null),
      el("div", { class: "card" },
        el("h2", {}, "Findings"),
        el("div", { class: "chiprow" },
          ["", "sla", "warn", "info"].map((s) => el("button", {
            class: `chip${gardenerFilter.severity === s ? " on" : ""}`,
            onclick: () => { gardenerFilter.severity = s; gardenerView(host); },
          }, s || "all severities")),
          [...checks].sort().map((c) => el("button", {
            class: `chip${gardenerFilter.check === c ? " on" : ""}`,
            onclick: () => { gardenerFilter.check = gardenerFilter.check === c ? "" : c; gardenerView(host); },
          }, c))),
        table(["severity", "check", "subject", "detail", "suggested action"],
          filtered.map((f) => ({
            cells: [severityPill(f.severity), el("span", { class: "mono" }, f.check), f.subject,
              f.detail, f.suggested_action || "—"],
          })), { empty: data.findings.length ? "nothing matches the filter" : "no findings — a healthy corpus" })),
      el("div", { class: "card" },
        el("h2", {}, "Run history"),
        jobRunsTable(data.history)),
    );
  });
}

function jobRunsTable(rows) {
  return table(["job", "status", "started", "finished", "stats"],
    (rows || []).map((r) => ({
      cells: [r.job, pill(r.status), fmtWhen(r.started_at), fmtWhen(r.finished_at),
        el("span", { class: "mono" }, JSON.stringify(r.stats))],
    })), { empty: "no job_runs rows yet" });
}

// ── digest ────────────────────────────────────────────────────────────────────────────────────
export async function digestView(host) {
  await loading(host, async () => {
    const data = await api.get("digest");
    const pieces = data.pieces;
    const pieceRow = (ok, label, missing) => el("li", { class: "timeline-piece", style: "display:flex;gap:8px;align-items:center;padding:4px 0" },
      el("span", { style: `color:var(${ok ? "--good-text" : "--critical-text"});display:inline-flex` }, icon(ok ? "check" : "x", 15)),
      el("span", {}, ok ? label : missing));
    const previewHost = el("div", {});
    render(host, 
      banner("info", "command-only — no schedule exists; these buttons ARE the command. ",
        "The watermark means each post covers exactly the window since the previous one."),
      el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h2", {}, "State"),
          el("div", { class: "spacer" }),
          el("button", { class: "btn small", onclick: () => previewFlow(previewHost) }, "Preview (dry run)"),
          el("button", { class: "btn small primary", onclick: () => postFlow() }, icon("play", 14), "Post now")),
        el("ul", { style: "list-style:none;margin:0;padding:0" },
          pieceRow(pieces.bot_token, "bot token configured", "bot token missing — a real post is refused (preview still works)"),
          pieceRow(pieces.digest_channel_id, "digest channel configured", "digest channel id missing — a real post is refused"),
          pieceRow(pieces.channels_path && pieces.channels_file_exists, "audience-scoping file present",
            "no audience-scoping file here — every audience falls back to the safe empty default")),
        el("div", { class: "sub", style: "margin-top:10px" },
          `last covered window ends: ${data.last_window_until ? fmtWhen(data.last_window_until) : "never posted — the first run covers 7 days back"}`)),
      previewHost,
      el("div", { class: "card" }, el("h2", {}, "History"), jobRunsTable(data.history)),
    );
  });
}

async function previewFlow(previewHost) {
  render(previewHost, skeletons(1));
  try {
    const result = await api.post("digest/preview");
    render(previewHost, 
      el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h2", {}, `Would post — window ${result.since ? fmtWhen(result.since) : "?"} → ${result.until ? fmtWhen(result.until) : "?"}`),
          el("div", { class: "spacer" }), copyButton(result.body)),
        el("pre", { class: "pre" }, result.body)));
  } catch (ex) {
    render(previewHost, banner("error", ex.message));
  }
}

async function postFlow() {
  const answer = await confirmForm({
    title: "Post the digest",
    consequence: "posts the digest to the configured channel FOR REAL and advances the watermark. If a previous post's watermark write failed, this may duplicate that window — check the history below first.",
    fields: [actorField()],
    confirmLabel: "Post", danger: true,
  });
  if (!answer) return;
  try {
    const result = await api.post("digest/post", { actor: answer.values.actor });
    toast(`posted — window ${fmtWhen(result.since)} → ${fmtWhen(result.until)} (job_runs #${result.run_id ?? "?"})`, "good");
    if (result.warning) toast(result.warning, "error");
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } catch (ex) {
    toast(ex.message, "error");
  }
}

// ── index ─────────────────────────────────────────────────────────────────────────────────────
export async function indexView(host) {
  await loading(host, async () => {
    const data = await api.get("index");
    const meta = data.meta;
    const checkHost = el("div", {});
    const zoneTiles = Object.entries(data.zones).map(([zone, n]) => tile(zone + "/", String(n), "pages"));
    // a variable, not an inline object literal — see the matching comment in gardenerView.
    const indexRebuildWorkflow = { file: "index-rebuild.yml", title: "Index rebuild", dispatch_inputs: [] };
    render(host, 
      el("div", { class: "grid tiles" },
        tile("Index built", meta && meta.built_at ? fmtAge(agoFrom(meta.built_at)) + " ago" : "never",
          meta ? `at ${fmtWhen(meta.built_at)}` : "the server refuses to serve an empty index"),
        tile("Embedding model", meta ? meta.model : "—", meta ? `dim ${meta.dim}` : null),
        ...zoneTiles),
      el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h2", {}, "Substrate"),
          el("div", { class: "spacer" }),
          el("button", { class: "btn small", onclick: () => checkFlow(checkHost) }, "Substrate check"),
          el("button", {
            class: "btn small primary", disabled: !META.github.configured,
            onclick: () => dispatchFlow(
              indexRebuildWorkflow,
              "runs a FULL staging index rebuild in GitHub Actions — real embedder, real spend, against the staging database."),
          }, icon("play", 14), "Rebuild now")),
        !META.github.configured
          ? el("div", { class: "sub" }, "needs the GitHub token — or `make rebuild-staging`")
          : null,
        el("div", { class: "sub" },
          "the check lints the LIVE pages_index: duplicate page_ids, orphan continuation parts, arm-invisible pages, dangling supersessions, unregistered anchors. Run it after registry changes."),
        checkHost),
      el("div", { class: "card" },
        el("h2", {}, "Incremental webhook — recent upserts"),
        jobRunsTable(data.webhook)),
    );
  });
}

async function checkFlow(checkHost) {
  render(checkHost, skeletons(1));
  try {
    const result = await api.post("index/check");
    render(checkHost, 
      el("div", { style: "margin-top:12px" },
        el("div", { class: "card-head" },
          pill(result.errors ? `${result.errors} error(s)` : "no errors", result.errors ? "critical" : "good"),
          pill(`${result.warnings} warning(s)`, result.warnings ? "warning" : "neutral")),
        table(["severity", "check", "detail"],
          result.findings.map((f) => ({
            cells: [severityPill(f.severity), el("span", { class: "mono" }, f.check), f.detail],
          })), { empty: "a clean substrate — no findings at all" })));
  } catch (ex) {
    render(checkHost, banner("error", ex.message));
  }
}

// ── entities ──────────────────────────────────────────────────────────────────────────────────
export async function entitiesView(host) {
  await loading(host, async () => {
    const data = await api.get("entities");
    render(host,
      el("div", { class: "card" },
        el("h2", {}, `${data.situations.length} pending entity situation(s)`),
        table(["id", "situation", "subject", "asked", "parked"],
          data.situations.map((row) => ({
            row,
            cells: [`#${row.id}`, el("span", { class: "mono" }, row.situation),
              row.subject || "(nothing recorded)", row.asked_at ? "asked" : "—", fmtAge(row.parked_age_ms)],
          })),
          { empty: "no pending entity situations — nothing is parked on an identity decision",
            onRow: (row) => { window.location.hash = `#/entities/${row.id}`; } })),
    );
  });
}

export async function entityDetailView(host, id) {
  await loading(host, async () => {
    const row = await api.get(`entities/${id}`);
    const report = row.report || {};
    render(host,
      el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h2", {}, `capture #${row.id}`), el("span", { class: "mono sub" }, row.situation),
          pill(row.status)),
        kv([
          ["subject", row.subject || "(nothing recorded)"],
          ["submitted by", row.submitted_by],
          ["parked", `${fmtAge(row.parked_age_ms)} ago`],
          ["agent's reading", report.agent_rationale || null],
          ["asked", row.asked_at ? `${fmtWhen(row.asked_at)}${row.reply ? " — answered" : " — no answer yet"}` : null],
        ]),
        row.reply ? el("pre", { class: "pre", style: "margin-top:10px" }, row.reply) : null,
        row.excerpt ? el("pre", { class: "pre", style: "margin-top:10px" }, row.excerpt) : null,
        row.withheld_reason ? el("em", {}, `(${row.withheld_reason})`) : null),
      el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h2", {}, "Approve"),
          el("div", { class: "spacer" }),
          el("button", { class: "btn small primary", onclick: () => entityApproveFlow(row) },
            "Approve & mint")),
        el("div", { class: "sub" },
          "mints through the governed door: one commit to the knowledge repo, authored by the "
          + "librarian App with your name in an Approved-by trailer (attribution, not a second "
          + "authorization check — ADR 030)."),
        el("div", { class: "sub", style: "margin-top:6px" },
          "to decline instead: this row is also #", String(row.id), " in the Queue tab — ",
          el("a", { href: `#/queue/${row.id}` }, "open it there"), " and reject it; same row.")),
    );
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
// per-name list, is listed for the steward to pick from. The Slack mint modal obeys the same
// decided value, so neither door can disagree about WHEN a default is safe. The offered STRING
// can still differ: this console strips control characters out of what it renders and Slack does
// not, so a ragged name reaches the two forms with different bytes (issue #46).
async function entityApproveFlow(row) {
  const names = (row.subjects || []).map((n) => String(n)).filter((n) => n.trim());
  const proposed = String(row.mint_name_prefill || "");
  const answer = await confirmForm({
    title: `Approve #${row.id} — mint a new entity`,
    consequence: "mints a real entity: pushes ONE commit to the knowledge repo (authored by the "
      + "librarian App, Approved-by you) and regenerates the registry. Not something cancelling "
      + "after this point can undo.",
    note: !proposed && names.length
      ? banner("warn",
          // No count in this sentence: the several-names decision was taken on the raw row, and
          // this list is what survived sanitizing — a name made entirely of control characters
          // counts towards "no default is safe" and then has nothing left to show. Naming a
          // number here would contradict the bullets on exactly the park that motivated the rule.
          el("div", {}, "this capture names several entities the registry does not recognize — "
            + "these are the ones it can show:"),
          el("ul", { class: "names" }, names.map((name) => el("li", {}, name))),
          el("div", {}, "they are minted one at a time — type the single name you are approving "
            + "now; the others stay unresolved on this capture until each gets its own decision."))
      : null,
    fields: [
      actorField(),
      { name: "name", label: "Name", value: proposed, required: true,
        hint: "the entity's page title" },
      { name: "entity_type", label: "Type", kind: "select", options: META.entity_types, required: true },
      { name: "aliases", label: "Aliases (optional, comma-separated)" },
      { name: "role", label: "Role (optional)", hint: "one line on what this entity is" },
      { name: "requeue", label: "Requeue this capture once the push lands", kind: "checkbox", value: true },
    ],
    confirmLabel: "Approve & mint",
  });
  if (!answer) return;
  try {
    const result = await api.post(`entities/${row.id}/approve`, answer.values);
    const short = result.commit.slice(0, 12);
    toast(`minted ${result.name} (${result.entity_id}) — commit ${short}`
      + (result.requeued ? `; capture #${row.id} requeued` : "; NOT requeued — still parked"),
      "good");
    window.location.hash = "#/entities";
  } catch (ex) {
    toast(ex.message, "error");
  }
}

// ── activity ──────────────────────────────────────────────────────────────────────────────────
export async function activityView(host) {
  await loading(host, async () => {
    const data = await api.get("activity");
    const shape = data.report.answer_shape;
    const filed = data.report.capture_to_filed_latency;
    const searchable = data.report.capture_to_searchable_latency;
    const latencySub = (s) => s.enough_data
      ? `p50 ${fmtMs(s.p50_ms)} · p95 ${fmtMs(s.p95_ms)} · ${s.samples} samples`
      : `${s.samples} sample(s) — ${s.min_samples} needed before p50/p95 mean anything`;
    render(host, 
      el("div", { class: "grid tiles" },
        tile("Questions asked", String(shape.total), "successful ask calls with a recorded shape"),
        tile("Answered with citation", shape.answered_with_citation_pct === null ? "—" : `${shape.answered_with_citation_pct.toFixed(0)}%`,
          `${shape.answered_with_citation} of ${shape.total}`),
        tile("Honest refusals", shape.refused_pct === null ? "—" : `${shape.refused_pct.toFixed(0)}%`,
          "a system that never refuses is the failure, not the success"),
        tile("Capture → filed", filed.enough_data ? fmtMs(filed.p50_ms) : "—", latencySub(filed)),
        tile("Capture → searchable", searchable.enough_data ? fmtMs(searchable.p50_ms) : "—", latencySub(searchable)),
      ),
      el("div", { class: "grid halves" },
        el("div", { class: "card" },
          el("h2", {}, "Activity by identity and tool"),
          table(["identity", "tool", "calls", "avg", "last"],
            data.by_identity_tool.map((r) => ({
              cells: [r.identity, el("span", { class: "mono" }, r.tool), String(r.calls),
                fmtMs(r.avg_duration_ms), fmtWhen(r.last_at)],
            })), { empty: "no audit rows yet" })),
        el("div", { class: "card" },
          el("h2", {}, "Rate-limit refusals"),
          table(["when", "identity", "tool"],
            data.rate_limited.map((r) => ({ cells: [fmtWhen(r.ts), r.identity, r.tool] })),
            { empty: "no rate-limit trips recorded" }))),
      el("div", { class: "card" },
        el("h2", {}, "Real ask questions (golden-set quarry)"),
        data.ask_questions.length
          ? el("ul", { class: "timeline" }, data.ask_questions.map((q) =>
              el("li", {}, el("div", { class: "what" }, q))))
          : el("div", { class: "empty" }, "no successful ask calls recorded yet")),
      el("div", { class: "card" },
        el("h2", {}, "Console actions"),
        adminActionsTable(data.admin_actions, "no console actions yet")),
    );
  });
}

// ── worker ────────────────────────────────────────────────────────────────────────────────────
export async function workerView(host) {
  await loading(host, async () => {
    const data = await api.get("worker");
    const inFlight = data.in_flight.map((row) => {
      const ratio = Math.min(1, (row.claimed_age_ms || 0) / (data.visibility_timeout_s * 1000));
      return el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h2", {}, `#${row.id} (${row.kind}) by ${row.submitted_by}`),
          pill(row.lease_expired ? "lease expired" : "within lease", row.lease_expired ? "serious" : "good")),
        el("div", { class: "sub" },
          `attempts ${row.attempts}/${data.max_attempts} · held ${fmtMs(row.claimed_age_ms)} of ${data.visibility_timeout_s}s`),
        el("div", { class: `meter${row.lease_expired ? " hot" : ""}` },
          el("div", { style: `width:${(ratio * 100).toFixed(1)}%` })),
        el("div", { class: "sub", style: "margin-top:8px" }, row.verdict));
    });
    render(host, 
      el("div", { class: "card" },
        el("h2", {}, depthLine(data.counts)),
        el("div", { class: "sub" },
          `worker lease ${data.visibility_timeout_s}s · ${data.max_attempts} deliveries before an item is failed · `,
          data.latency.enough_data
            ? `capture→filed p50 ${fmtMs(data.latency.p50_ms)} · p95 ${fmtMs(data.latency.p95_ms)} over ${data.latency.samples} filings`
            : `capture→filed: ${data.latency.samples} filing(s) so far, ${data.latency.min_samples} needed before p50/p95 mean anything`)),
      inFlight.length ? el("div", {}, ...inFlight)
        : el("div", { class: "card" }, el("div", { class: "empty" }, "in flight: nothing claimed")),
      banner("info", "draining, walking and Fly scaling stay in the terminal on purpose — this panel is the ",
        el("span", { class: "mono" }, "stigmergy-librarian status"), " read, live."),
    );
  });
}
