// Jobs: the four scheduled workflows — purpose, schedule, the database truth, the run history,
// and the three levers (Run now, Enable, Disable) that need the GitHub token.

import { api } from "../api.js";
import { chartCard, runStrip } from "../charts.js";
import { JOB, jobConsequence, jobName } from "../copy.js";
import { getMeta } from "../state.js";
import { banner, confirmForm, el, fmtAge, fmtWhen, icon, kv, mono, pill, relTime, render, table, wordPill } from "../ui.js";
import { actorField, loading, mutate, rerender, runShape, runTable } from "./common.js";

export async function jobsView(host) {
  await loading(host, async () => {
    const [data, metrics] = await Promise.all([api.get("crons"), api.get("metrics?days=90")]);
    const children = [];
    if (!data.configured) {
      children.push(banner("info", el("p", {}, el("strong", {}, "Read-only: "), "GitHub is not configured for this console, so this page shows the database truth only. Run now and Enable/Disable need ",
        mono("STIGMERGY_ADMIN_GITHUB_TOKEN"), " + ", mono("STIGMERGY_ADMIN_GITHUB_REPO"), " (a fine-grained PAT with Actions read+write on the repository the crons run in). The gh CLI still works from a terminal.")));
    }
    if (data.github_error) children.push(banner("error", `GitHub degraded: ${data.github_error}`));
    children.push(el("div", { class: "grid halves" }, ...data.workflows.map((w) => jobCard(w, data.configured && !data.github_error, metrics))));
    const other = ["digest", "webhook-index-upsert", "capture-reclaim", "steward-doorbell"].filter((j) => (metrics.job_history[j] || []).length);
    if (other.length) {
      children.push(el("section", { class: "card" },
        el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Other recorded work"), el("div", { class: "sub" }, "jobs that run on demand or on a push, not on a schedule"))),
        table(["job", "runs on record", "last", "outcome"], other.map((j) => {
          const runs = metrics.job_history[j] || [];
          return { cells: [jobName(j), String(runs.length), runs[0] ? relTime(runs[0].finished_at) : "—", runs[0] ? wordPill(runs[0].status) : "—"] };
        }), { dense: true })));
    }
    render(host, ...children);
  });
}

function jobCard(w, live, metrics) {
  const copy = JOB[w.file] || { purpose: "", truth: w.truth };
  const truthJob = w.truth.startsWith("job_runs:") ? w.truth.split(":")[1] : null;
  const history = truthJob ? (metrics.job_history[truthJob] || []) : [];
  const next = nextRun(w.schedule_utc);
  const truth = w.truth === "index_meta.built_at"
    ? el("span", { class: "row" }, pill(w.index_built_at ? "built" : "never built", w.index_built_at ? "git" : "fail"), w.index_built_at ? `${relTime(w.index_built_at)} (${fmtWhen(w.index_built_at)})` : "no index yet")
    : w.latest_run
      ? el("span", { class: "row" }, wordPill(w.latest_run.status), `${relTime(w.latest_run.finished_at)} (${fmtWhen(w.latest_run.finished_at)})`, w.latest_run.error ? el("span", { class: "diff-del" }, w.latest_run.error) : null)
      : pill("no run recorded", "neutral");
  const stats = w.latest_run && w.latest_run.stats ? w.latest_run.stats : null;
  const statChips = stats ? Object.entries(stats).filter(([, v]) => typeof v !== "object").slice(0, 6).map(([k, v]) => el("span", { class: "entity-chip" }, el("span", { class: "type" }, k.replaceAll("_", " ")), String(v))) : [];
  const dispatchConsequence = jobConsequence(w.file, w.title, getMeta().retention ? getMeta().retention.default_days : undefined);
  const runs = history.map((r) => runShape(r));
  return el("section", { class: "card" },
    el("div", { class: "card-head" },
      el("div", { class: "card-title" },
        el("div", { class: "job-head" }, el("h2", {}, w.title), w.state ? wordPill(w.state) : null, mono(w.file, "sub")),
        el("div", { class: "sub" }, copy.purpose)),
      el("div", { class: "spacer" }),
      ...(live ? [
        el("button", { class: "btn small primary", type: "button", onclick: () => dispatchFlow(w, dispatchConsequence) }, icon("play", 14), "Run now"),
        w.state === "active"
          ? el("button", { class: "btn small", type: "button", onclick: () => enableFlow(w, false) }, "Disable")
          : el("button", { class: "btn small", type: "button", onclick: () => enableFlow(w, true) }, "Enable"),
      ] : [pill("read-only here", "code", { small: true })])),
    kv([
      ["schedule", el("span", {}, `daily at ${cronTime(w.schedule_utc)} UTC`, el("span", { class: "job-next" }, next ? ` · next in ${fmtAge(next - Date.now())}` : ""), el("span", { class: "job-next" }, " · cron "), mono(w.schedule_utc, "sub"))],
      ["last run", truth],
      ["truth", copy.truth],
    ], { wide: true }),
    statChips.length ? el("div", { class: "row", style: { marginTop: "10px" } }, ...statChips) : null,
    history.length ? el("div", { style: { marginTop: "12px" } }, chartCard({ title: `${history.length} run(s) on record`, sub: "height is duration, colour the outcome", chart: runStrip({ runs, height: 46 }), tableSpec: runTable(runs), cls: "tight" })) : null,
    (w.runs || []).length
      ? el("div", { style: { marginTop: "12px" } }, table(["run", "status", "conclusion", "trigger", "started", ""],
          w.runs.map((r) => ({
            cells: [r.display_title || `#${r.id}`, wordPill(r.status), r.conclusion ? wordPill(r.conclusion) : "—",
              r.event, fmtWhen(r.created_at),
              // a link only to GitHub itself — the URL comes from an API response, so it is data
              String(r.html_url || "").startsWith("https://github.com/")
                ? el("a", { href: r.html_url, target: "_blank", rel: "noopener noreferrer" }, icon("external", 14), " logs")
                : el("span", { class: "muted" }, `#${r.id}`)],
          })), { dense: true }))
      : (live ? el("div", { class: "sub", style: { marginTop: "10px" } }, "no recent Actions runs listed") : null));
}

// The four schedules are `M H * * *` — daily at a fixed UTC time. Anything fancier renders raw.
function cronTime(expr) {
  const [m, h] = String(expr).split(/\s+/);
  if (!/^\d+$/.test(m) || !/^\d+$/.test(h)) return expr;
  return `${h.padStart(2, "0")}:${m.padStart(2, "0")}`;
}

function nextRun(expr) {
  const [m, h, dom, mon, dow] = String(expr).split(/\s+/);
  if (!/^\d+$/.test(m) || !/^\d+$/.test(h) || dom !== "*" || mon !== "*" || dow !== "*") return null;
  const now = new Date();
  const next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), Number(h), Number(m)));
  if (next.getTime() <= now.getTime()) next.setUTCDate(next.getUTCDate() + 1);
  return next.getTime();
}

export async function dispatchFlow(w, consequence) {
  const fields = [actorField()];
  if ((w.dispatch_inputs || []).includes("dry_run")) {
    // ticked by default: the safe path is the default path on the one workflow that deletes
    fields.push({ name: "dry_run", label: "Dry run — list what would be purged, change nothing", kind: "checkbox", value: true });
  }
  const answer = await confirmForm({ title: `Run ${w.title} now`, consequence, fields, confirmLabel: "Dispatch" });
  if (!answer) return;
  const inputs = {};
  if (answer.values.dry_run) inputs.dry_run = true;
  await mutate(`crons/${w.file}/dispatch`, { actor: answer.values.actor, inputs }, `dispatched ${w.file} — it appears in the runs list in a few seconds`);
  rerender();
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
  await mutate(`crons/${w.file}/${enabled ? "enable" : "disable"}`, { actor: answer.values.actor }, `${w.file} ${enabled ? "enabled" : "disabled"}`);
  rerender();
}
