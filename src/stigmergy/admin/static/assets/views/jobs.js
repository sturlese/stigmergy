// Jobs: the night shift — what runs unattended, when each pass last ran, and what it did.
//
// There are no levers on this page, and their absence is the design rather than a gap. These
// passes used to be GitHub Actions crons the console dispatched through a PAT; they now run on
// the librarian worker's idle branch (ADR 044), so there is nothing to dispatch and no schedule
// to disable from a browser. Everything below is a database read.

import { api } from "../api.js";
import { chartCard, runStrip } from "../charts.js";
import { JOB, jobName } from "../copy.js";
import { banner, el, fmtAge, fmtWhen, kv, mono, pill, relTime, render, table, wordPill } from "../ui.js";
import { loading, runShape, runTable } from "./common.js";

export async function jobsView(host) {
  await loading(host, async () => {
    const [data, metrics] = await Promise.all([api.get("jobs"), api.get("metrics?days=90")]);
    const children = [
      banner("info", el("p", {}, el("strong", {}, "The night shift runs inside the worker. "),
        "Each pass fires on the librarian's idle branch — never while a capture is waiting, so maintenance cannot delay a filing. Nothing here needs a schedule, a token or a button.")),
      el("div", { class: "grid halves" }, ...data.jobs.map((job) => jobCard(job, metrics))),
    ];
    const other = ["repair", "digest", "webhook-index-upsert", "capture-reclaim"].filter((j) => (metrics.job_history[j] || []).length);
    if (other.length) {
      children.push(el("section", { class: "card" },
        el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h2", {}, "Other recorded work"), el("div", { class: "sub" }, "jobs that run on their own interval, on demand, or on a push"))),
        table(["job", "runs on record", "last", "outcome"], other.map((j) => {
          const runs = metrics.job_history[j] || [];
          return { cells: [jobName(j), String(runs.length), runs[0] ? relTime(runs[0].finished_at) : "—", runs[0] ? wordPill(runs[0].status) : "—"] };
        }), { dense: true })));
    }
    render(host, ...children);
  });
}

function jobCard(job, metrics) {
  const copy = JOB[job.file] || { purpose: "", truth: job.truth };
  const truthJob = job.truth.startsWith("job_runs:") ? job.truth.split(":")[1] : null;
  const history = truthJob ? (metrics.job_history[truthJob] || []) : [];
  const truth = job.truth === "index_meta.built_at"
    ? el("span", { class: "row" }, pill(job.index_built_at ? "built" : "never built", job.index_built_at ? "git" : "fail"), job.index_built_at ? `${relTime(job.index_built_at)} (${fmtWhen(job.index_built_at)})` : "no index yet")
    : job.latest_run
      ? el("span", { class: "row" }, wordPill(job.latest_run.status), `${relTime(job.latest_run.finished_at)} (${fmtWhen(job.latest_run.finished_at)})`, job.latest_run.error ? el("span", { class: "diff-del" }, job.latest_run.error) : null)
      : pill("no run recorded", "neutral");
  const stats = job.latest_run && job.latest_run.stats ? job.latest_run.stats : null;
  const statChips = stats ? Object.entries(stats).filter(([, v]) => typeof v !== "object").slice(0, 6).map(([k, v]) => el("span", { class: "entity-chip" }, el("span", { class: "type" }, k.replaceAll("_", " ")), String(v))) : [];
  const runs = history.map((r) => runShape(r));
  return el("section", { class: "card" },
    el("div", { class: "card-head" },
      el("div", { class: "card-title" },
        el("div", { class: "job-head" }, el("h2", {}, job.title),
          pill(job.runs_in === "worker" ? "the worker runs it" : "an operator runs it", job.runs_in === "worker" ? "code" : "human", { small: true })),
        el("div", { class: "sub" }, copy.purpose)),
      el("div", { class: "spacer" })),
    kv([
      ["runs", runsLine(job)],
      ["last run", truth],
      ["truth", copy.truth],
    ], { wide: true }),
    statChips.length ? el("div", { class: "row", style: { marginTop: "10px" } }, ...statChips) : null,
    history.length ? el("div", { style: { marginTop: "12px" } }, chartCard({ title: `${history.length} run(s) on record`, sub: "height is duration, colour the outcome", chart: runStrip({ runs, height: 46 }), tableSpec: runTable(runs), cls: "tight" })) : null);
}

// Two sentences, and which one a job gets is the honest difference between them: a worker pass
// says when it is next due; the rebuild says the command, because no process behind this console
// can run it (no embedding key — the deployed worker has it stripped by design).
function runsLine(job) {
  if (job.runs_in !== "worker") {
    return el("span", {}, "by hand, with the embedding key exported — ", mono(job.command || ""));
  }
  const next = nextRun(job.at_default);
  return el("span", {}, `on the worker's idle branch, daily at ${job.at_default} UTC`,
    el("span", { class: "job-next" }, next ? ` · next in ${fmtAge(next - Date.now())}` : ""),
    el("span", { class: "job-next" }, " · set with "), mono(job.at_setting, "sub"));
}

function nextRun(at) {
  const [h, m] = String(at).split(":");
  if (!/^\d+$/.test(h || "") || !/^\d+$/.test(m || "")) return null;
  const now = new Date();
  const next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), Number(h), Number(m)));
  if (next.getTime() <= now.getTime()) next.setUTCDate(next.getUTCDate() + 1);
  return next.getTime();
}
