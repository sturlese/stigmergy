// The digest: the configured pieces, Preview (byte-identical to the post) and Post now.

import { api } from "../api.js";
import { chartCard, runStrip } from "../charts.js";
import { jobName } from "../copy.js";
import { banner, confirmForm, copyButton, el, fmtWhen, icon, relTime, render, skeletons, table, wordPill } from "../ui.js";
import { actorField, loading, mutate, rerender, runShape, runTable } from "./common.js";

export async function digestView(host) {
  await loading(host, async () => {
    const data = await api.get("digest");
    const pieces = data.pieces;
    const previewHost = el("div", {});
    const pieceRow = (ok, label, missing) => el("li", { class: "row tight" },
      el("span", { class: `kdot ${ok ? "k-git" : "k-fail"}`, style: { marginTop: "7px" } }), el("span", {}, ok ? label : missing));
    const ready = pieces.bot_token && pieces.digest_channel_id;
    const runs = data.history.map((r) => runShape(r, (run) => jobName(run.job)));
    render(host,
      el("div", { class: "grid halves" },
        el("section", { class: "card" },
          el("div", { class: "card-head" },
            el("div", { class: "card-title" }, el("h2", {}, "Ready to post?"), el("div", { class: "sub" }, "command-only — no schedule exists; these buttons ARE the command")),
            el("div", { class: "spacer" }),
            el("button", { class: "btn small", type: "button", onclick: () => previewFlow(previewHost) }, icon("search", 14), "Preview (dry run)"),
            // disabled with the reason beside it (the checklist below), never a hover hint
            el("button", { class: "btn small primary", type: "button", disabled: !ready, onclick: () => postFlow() }, icon("play", 14), "Post now")),
          el("ul", { class: "stack", style: { listStyle: "none", margin: 0, padding: 0 } },
            pieceRow(pieces.bot_token, "Slack bot token configured", "Slack bot token missing — a real post is refused (preview still works)"),
            pieceRow(pieces.digest_channel_id, "digest channel configured", "digest channel id missing — a real post is refused"),
            pieceRow(pieces.channels_path && pieces.channels_file_exists, "audience-scoping file present", "no audience-scoping file here — every audience falls back to the safe empty default")),
          el("div", { class: "hr" }),
          el("div", { class: "sub" }, data.last_window_until
            ? `the last post covered up to ${fmtWhen(data.last_window_until)} (${relTime(data.last_window_until)}); the next one starts there`
            : "never posted — the first run covers 7 days back"),
          !ready ? banner("warn", "a real post is refused until both Slack pieces are configured; Preview shows what it would say") : null),
        chartCard({
          title: "Runs", sub: "posts and previews — a preview writes a job row too, which is why the history fills with them",
          chart: el("div", {}, runStrip({ runs, height: 46 }),
            el("div", { style: { marginTop: "10px" } }, table(["when", "what", "outcome", "error"], data.history.map((r) => ({
              cells: [fmtWhen(r.started_at), jobName(r.job), wordPill(r.status), r.error ? el("span", { class: "diff-del" }, r.error) : "—"],
            })), { dense: true, empty: "no digest runs yet — Preview to see what a post would say" }))),
          tableSpec: runTable(runs),
        })),
      previewHost,
    );
  });
}

async function previewFlow(previewHost) {
  render(previewHost, skeletons(1));
  try {
    const result = await api.post("digest/preview");
    render(previewHost,
      el("section", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title" }, el("h2", {}, "What it would post"), el("div", { class: "sub" }, `window ${result.since ? fmtWhen(result.since) : "?"} → ${result.until ? fmtWhen(result.until) : "?"} · byte-identical to a real post`)),
          el("div", { class: "spacer" }), copyButton(result.body)),
        el("pre", { class: "pre prose" }, result.body)));
  } catch (ex) {
    render(previewHost, banner("error", ex.message));
  }
}

async function postFlow() {
  const answer = await confirmForm({
    title: "Post the digest",
    consequence: "posts the digest to the configured channel FOR REAL and advances the watermark. If a previous post's watermark write failed, this may duplicate that window — check the runs first.",
    fields: [actorField()],
    confirmLabel: "Post", danger: true,
  });
  if (!answer) return;
  if (await mutate("digest/post", { actor: answer.values.actor }, (r) => `posted — window ${fmtWhen(r.since)} → ${fmtWhen(r.until)} (job row #${r.run_id ?? "?"})`)) rerender();
}
