import { api, clearToken, setToken, token } from "./api.js";
import { exactPatchDisclosure, pathDiffDisclosure } from "./change-view.js";

const root = document.getElementById("app");
let meta = null;

const NAV = [
  ["dashboard", "Overview"],
  ["captures", "Captures"],
  ["changes", "Changes"],
  ["contradictions", "Contradictions"],
  ["entities", "Entities"],
  ["gardener", "Gardener"],
  ["index", "Index"],
];

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (key === "checked") node.checked = Boolean(value);
    else if (key === "value") node.value = value;
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function replace(host, ...children) {
  host.replaceChildren(...children.flat(Infinity).filter(Boolean));
}

function card(title, ...children) {
  return h("section", { class: "card" }, h("h2", {}, title), ...children);
}

function badge(value, tone = "") {
  return h("span", { class: `badge ${tone || value || ""}` }, String(value || "—").replaceAll("_", " "));
}

function field(label, control, hint = "") {
  return h("label", { class: "field" }, h("span", { class: "label" }, label), control,
    hint ? h("span", { class: "hint" }, hint) : null);
}

function button(label, onclick, cls = "") {
  return h("button", { type: "button", class: `button ${cls}`, onclick }, label);
}

function link(label, route, cls = "") {
  return h("a", { href: `#/${route}`, class: cls }, label);
}

function fmtWhen(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function fmtAge(value) {
  if (!value) return "No captures";
  const elapsed = Math.max(0, Date.now() - new Date(value).getTime());
  const minutes = Math.floor(elapsed / 60000);
  if (minutes < 1) return "Oldest just now";
  if (minutes < 60) return `Oldest ${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `Oldest ${hours}h ago`;
  return `Oldest ${Math.floor(hours / 24)}d ago`;
}

function fmtBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
}

function shortSha(value) {
  return value ? value.slice(0, 10) : "—";
}

function definition(values) {
  const list = h("dl", { class: "definition" });
  for (const [key, value] of values) {
    if (value === null || value === undefined || value === "") continue;
    list.append(h("dt", {}, key), h("dd", {}, value instanceof Node ? value : String(value)));
  }
  return list;
}

function message(text, kind = "info") {
  return h("div", { class: `message ${kind}` }, text);
}

function notify(text, kind = "good") {
  const host = document.getElementById("toasts");
  if (!host) return;
  const item = message(text, kind);
  host.append(item);
  window.setTimeout(() => item.remove(), 5000);
}

async function act(node, work, success) {
  node.disabled = true;
  const original = node.textContent;
  node.textContent = "Working…";
  try {
    const result = await work();
    notify(success || "Done.");
    return result;
  } catch (error) {
    notify(error.message, "error");
    return null;
  } finally {
    node.disabled = false;
    node.textContent = original;
  }
}

function audiencePicker() {
  const host = h("div", { class: "checks audience" },
    h("span", { class: "hint" }, "No selection means organization-wide."));
  for (const group of meta.audiences) {
    host.append(h("label", { class: "check" },
      h("input", { type: "checkbox", value: group }), group));
  }
  return host;
}

function selectedAudience(host) {
  const values = [...host.querySelectorAll("input:checked")].map((item) => item.value);
  return values.length ? values : null;
}

function login(error = "") {
  const input = h("input", { type: "password", autocomplete: "off", autofocus: true });
  const form = h("form", { class: "login-card", onsubmit: async (event) => {
    event.preventDefault();
    if (!input.value.trim()) return;
    setToken(input.value.trim());
    try {
      meta = await api.get("meta");
      shell();
    } catch (failure) {
      clearToken();
      login(failure.message);
    }
  } },
  h("div", { class: "brain" }, "◉"),
  h("h1", {}, "Stigmergy"),
  h("p", { class: "muted" }, "Master operations console"),
  error ? message(error, "error") : null,
  field("Admin token", input),
  h("button", { class: "button primary", type: "submit" }, "Open console"));
  replace(root, h("main", { class: "login-wrap" }, form));
}

function route() {
  return window.location.hash.replace(/^#\/?/, "") || "dashboard";
}

function shell() {
  const nav = h("nav", {}, ...NAV.map(([name, label]) => link(label, name, "nav-link")));
  replace(root,
    h("div", { class: "layout" },
      h("aside", {}, h("div", { class: "brand" }, h("span", { class: "brain" }, "◉"),
        h("div", {}, h("strong", {}, "Stigmergy"), h("small", {}, "Operations"))),
      nav,
      h("div", { class: "aside-foot" },
        h("small", {}, meta.actor.display_name),
        button("Sign out", () => { clearToken(); window.location.reload(); }, "quiet"))),
      h("main", { class: "main" },
        h("header", {}, h("div", {}, h("h1", { id: "page-title" }), h("p", { id: "page-subtitle" })),
          button("Refresh", () => renderRoute(), "quiet")),
        h("div", { id: "content" }))),
    h("div", { id: "toasts", class: "toasts" }));
  window.addEventListener("hashchange", renderRoute);
  renderRoute();
}

async function renderRoute() {
  const value = route();
  const [head, id] = value.split("/");
  const content = document.getElementById("content");
  if (!content) return;
  for (const item of document.querySelectorAll(".nav-link")) {
    item.classList.toggle("active", item.getAttribute("href") === `#/${head}`);
  }
  replace(content, h("div", { class: "loading" }, "Loading…"));
  const pages = {
    dashboard: ["Overview", "Current operational state", dashboardView],
    captures: ["Captures", "Submit and follow immutable evidence", id ? () => captureDetail(id) : capturesView],
    changes: ["Changes", "Every autonomous Git mutation in human and exact form", id ? () => changeDetail(id) : changesView],
    contradictions: ["Contradictions", "Explicit uncertainty that does not block the wiki", contradictionsView],
    entities: ["Entities", "Scoped identity claims and lifecycle controls", entitiesView],
    gardener: ["Gardener", "Autonomous lint-and-fix runs", gardenerView],
    index: ["Index", "Incremental convergence and nightly reconciliation", indexView],
  };
  const selected = pages[head] || pages.dashboard;
  document.getElementById("page-title").textContent = selected[0];
  document.getElementById("page-subtitle").textContent = selected[1];
  document.title = `${selected[0]} — Stigmergy`;
  try {
    replace(content, await selected[2]());
  } catch (error) {
    replace(content, message(error.message, "error"));
  }
}

async function dashboardView() {
  const data = await api.get("overview");
  const counts = data.captures.counts;
  return h("div", { class: "stack" },
    h("div", { class: "tiles" },
      ...["queued", "processing", "landed", "failed"].map((status) =>
        h("div", { class: "tile" }, h("span", {}, status), h("strong", {}, counts[status] || 0),
          h("small", {}, fmtAge(data.captures.oldest_created_at[status])))),
      h("div", { class: "tile" }, h("span", {}, "Changes"), h("strong", {}, data.changes)),
      h("div", { class: "tile" }, h("span", {}, "Contradictions"), h("strong", {}, data.contradictions))),
    data.index.warnings.map((warning) => message(warning, "warn")),
    data.worker.stale ? message("The writer heartbeat is stale.", "error") : message("The writer heartbeat is current.", "good"),
    card("Writer service", definition([
      ["State", data.worker.heartbeat?.state || "Unknown"],
      ["Last heartbeat", fmtWhen(data.worker.heartbeat?.heartbeat_at)],
    ])),
    card("Last successful write", data.worker.last_successful_write
      ? definition([
        ["Summary", data.worker.last_successful_write.summary],
        ["When", fmtWhen(data.worker.last_successful_write.created_at)],
        ["Commit", shortSha(data.worker.last_successful_write.commit_sha)],
      ])
      : h("p", { class: "muted" }, "No knowledge change has landed yet.")));
}

function captureComposer() {
  const mode = h("select", {},
    h("option", { value: "text" }, "Paste text"),
    h("option", { value: "file" }, "Upload file"),
    h("option", { value: "url" }, "Public URL"));
  const inputHost = h("div");
  const title = h("input", { type: "text", placeholder: "Optional" });
  const occurred = h("input", { type: "date" });
  const audience = audiencePicker();
  const draw = () => {
    if (mode.value === "text") replace(inputHost, field("Text", h("textarea", { rows: 10, name: "text", required: true })));
    else if (mode.value === "url") replace(inputHost, field("Public URL", h("input", { type: "url", name: "url", required: true })));
    else replace(inputHost, field("File", h("input", { type: "file", name: "file", required: true })));
  };
  mode.addEventListener("change", draw);
  draw();
  const form = h("form", { class: "form-grid", onsubmit: async (event) => {
    event.preventDefault();
    const submit = event.submitter;
    const acl = selectedAudience(audience);
    let request;
    if (mode.value === "file") {
      const file = inputHost.querySelector("input[type=file]").files[0];
      if (!file) return;
      const body = new FormData();
      body.append("file", file);
      body.append("title", title.value);
      body.append("occurred_at", occurred.value);
      body.append("audience", JSON.stringify(acl));
      request = () => api.form("captures/file", body);
    } else {
      const name = mode.value;
      const value = inputHost.querySelector(name === "text" ? "textarea" : "input").value;
      request = () => api.post(`captures/${name}`, {
        [name]: value, title: title.value, occurred_at: occurred.value, audience: acl,
      });
    }
    const result = await act(submit, request, "Capture queued.");
    if (result) window.location.hash = `#/captures/${result.id}`;
  } },
  field("Input", mode), inputHost, field("Title", title), field("Occurred at", occurred),
  field("Audience", audience), h("button", { class: "button primary", type: "submit" }, "Queue capture"));
  return card("New capture", form);
}

async function capturesView() {
  const data = await api.get("captures?limit=100");
  const rows = h("div", { class: "list" }, ...data.captures.map((item) =>
    link("", `captures/${item.id}`, "list-row")));
  for (const [index, item] of data.captures.entries()) {
    replace(rows.children[index],
      h("div", {}, h("strong", {}, item.title || item.operation), h("small", {}, item.id)),
      badge(item.status),
      h("span", {}, item.adapter || "operation"),
      h("span", {}, fmtWhen(item.created_at)));
  }
  return h("div", { class: "stack" }, captureComposer(),
    card("Recent captures", data.captures.length ? rows : h("p", { class: "muted" }, "No captures yet.")),
    deleteComposer());
}

function deleteComposer() {
  const paths = h("textarea", { rows: 3, placeholder: "One wiki/ or sources/ path per line" });
  const rationale = h("input", { type: "text", placeholder: "Why this current knowledge should be removed" });
  return card("Explicit deletion",
    h("form", { class: "form-grid", onsubmit: async (event) => {
      event.preventDefault();
      const selected = paths.value.split("\n").map((value) => value.trim()).filter(Boolean);
      const result = await act(event.submitter,
        () => api.post("knowledge/delete", { paths: selected, rationale: rationale.value }),
        "Deletion queued.");
      if (result) window.location.hash = `#/captures/${result.id}`;
    } }, field("Paths", paths), field("Rationale", rationale),
    h("button", { class: "button danger", type: "submit" }, "Queue deletion")));
}

async function captureDetail(id) {
  const item = await api.get(`captures/${encodeURIComponent(id)}`);
  const artifacts = item.artifacts || [];
  const acquisition = item.provenance?.acquisition;
  return h("div", { class: "stack" },
    link("← All captures", "captures", "back"),
    card(item.title || item.operation,
      h("div", { class: "row-gap" }, badge(item.status), badge(item.adapter || item.operation)),
      definition([
        ["ID", item.id], ["Actor", item.actor?.display_name || item.submitted_by],
        ["Audience", item.audience ? item.audience.join(", ") : "Organization-wide"],
        ["Created", fmtWhen(item.created_at)], ["Attempts", item.attempts],
        ["Source", item.source_path], ["Commit", item.commit_sha],
        ["Error", item.error],
      ]),
      item.status === "failed" ? button("Retry", async (event) => {
        const result = await act(event.currentTarget, () => api.post(`captures/${id}/retry`), "Capture requeued.");
        if (result) renderRoute();
      }, "primary") : null,
      item.change_id ? link("Open friendly change →", `changes/${item.change_id}`, "button") : null),
    card("Provenance", definition([
      ["Origin", item.provenance?.adapter],
      ["Occurred", fmtWhen(item.provenance?.occurred_at)],
      ["Locator", item.provenance?.locator],
      ["Original URL", acquisition?.original_url],
      ["Final URL", acquisition?.final_url],
      ["Acquired", fmtWhen(acquisition?.acquired_at)],
      ["Drive file", acquisition?.drive_file_id],
      ["Drive media", acquisition?.drive_media_type],
      ["Export media", acquisition?.export_media_type],
    ].filter(([, value]) => value))),
    card("Evidence", artifacts.length ? h("div", { class: "list" }, ...artifacts.map((artifact) =>
      h("div", { class: "artifact" },
        h("strong", {}, artifact.original_name || "Artifact"),
        h("span", {}, artifact.media_type), h("span", {}, fmtBytes(artifact.bytes)),
        h("code", {}, artifact.sha256)))) : h("p", { class: "muted" }, "This operation has no capture artifact.")),
    card("Outcome", h("pre", { class: "json" }, JSON.stringify({ extraction: item.extraction, report: item.report }, null, 2))));
}

async function changesView() {
  const data = await api.get("changes?limit=100");
  const list = h("div", { class: "list" }, ...data.changes.map((item) => {
    const row = link("", `changes/${item.id}`, "list-row change-row");
    replace(row,
      h("div", {}, h("strong", {}, item.summary), h("small", {}, fmtWhen(item.created_at))),
      badge(item.trigger),
      h("span", {}, `${item.manifest.length} path${item.manifest.length === 1 ? "" : "s"}`),
      h("code", {}, shortSha(item.commit_sha)));
    return row;
  }));
  return card("Change ledger", data.changes.length ? list : h("p", { class: "muted" }, "No changes yet."));
}

function diffView(text) {
  const block = h("div", { class: "diff" });
  for (const line of String(text || "").split("\n")) {
    let cls = "context";
    if (line.startsWith("+") && !line.startsWith("+++")) cls = "add";
    else if (line.startsWith("-") && !line.startsWith("---")) cls = "remove";
    else if (line.startsWith("@@")) cls = "hunk";
    else if (line.startsWith("diff ") || line.startsWith("index ") || line.startsWith("---") || line.startsWith("+++")) cls = "meta";
    block.append(h("div", { class: cls }, line || " "));
  }
  return block;
}

async function changeDetail(id) {
  const { change: item } = await api.get(`changes/${encodeURIComponent(id)}`);
  const cards = item.manifest.map((path) => {
    const contents = diffView(item.path_patches[path.path] || "");
    const details = pathDiffDisclosure(h, path.page_role, contents);
    return card(path.path,
      h("div", { class: "row-gap" }, badge(path.action), badge(path.page_role)),
      h("p", {}, path.reason),
      h("p", { class: "muted" }, `+${path.additions} / −${path.deletions}`), details);
  });
  return h("div", { class: "stack" }, link("← All changes", "changes", "back"),
    card(item.summary,
      h("div", { class: "row-gap" }, badge(item.trigger)),
      h("div", { class: "row-gap" },
        badge(`${item.counts.created || 0} created`),
        badge(`${item.counts.updated || 0} updated`),
        badge(`${item.counts.deleted || 0} deleted`),
        badge(`${item.counts.contradictions_added || 0} contradictions added`),
        badge(`${item.counts.contradictions_resolved || 0} contradictions resolved`)),
      definition([["Actor", item.actor], ["When", fmtWhen(item.created_at)],
        ["Commit", item.commit_sha], ["Parent", item.parent_commit_sha]])),
    item.source_summary ? card("Captured evidence", definition([
      ["Title", item.source_summary.title || "Untitled"],
      ["Adapter", item.source_summary.adapter],
      ["Captured", fmtWhen(item.source_summary.captured_at)],
      ["Locator", item.source_summary.locator],
      ["Original URL", item.source_summary.acquisition?.original_url],
      ["Final URL", item.source_summary.acquisition?.final_url],
      ["Drive file", item.source_summary.acquisition?.drive_file_id],
      ["Artifacts", item.source_summary.artifacts.map((value) => `${value.original_name || value.media_type} (${fmtBytes(value.bytes)})`).join(", ")],
    ].filter(([, value]) => value))) : null,
    ...cards,
    card("Technical view", exactPatchDisclosure(h, item.exact_patch)));
}

async function contradictionsView() {
  const data = await api.get("contradictions");
  if (!data.contradictions.length) return message("There are no unresolved contradictions.", "good");
  return h("div", { class: "stack" }, ...data.contradictions.map((item) => contradictionCard(item)));
}

function contradictionCard(item) {
  const decision = h("select", {},
    h("option", { value: "claim_a" }, "Claim A"), h("option", { value: "claim_b" }, "Claim B"),
    h("option", { value: "neither" }, "Neither / add context"), h("option", { value: "custom" }, "Custom resolution"));
  const resolution = h("textarea", { rows: 4 });
  const rationale = h("textarea", { rows: 3 });
  const url = h("input", { type: "url", placeholder: "Optional public URL" });
  const file = h("input", { type: "file" });
  const form = h("details", { class: "resolve" }, h("summary", {}, "Contribute a resolution"),
    h("form", { class: "form-grid", onsubmit: async (event) => {
      event.preventDefault();
      const common = {
        contradiction_id: item.id, decision: decision.value,
        resolution: resolution.value, rationale: rationale.value,
      };
      let request;
      if (file.files[0]) {
        const body = new FormData();
        for (const [key, value] of Object.entries(common)) body.append(key, value);
        body.append("file", file.files[0]);
        request = () => api.form("contradictions/resolve-file", body);
      } else {
        request = () => api.post("contradictions/resolve", { ...common, support_url: url.value });
      }
      const result = await act(event.submitter, request, "Resolution queued as a new capture.");
      if (result) window.location.hash = `#/captures/${result.id}`;
    } }, field("Decision", decision), field("Resolution text", resolution),
    field("Rationale", rationale), field("Supporting public URL", url),
    field("Or supporting file", file), h("button", { class: "button primary", type: "submit" }, "Queue resolution")));
  return card(item.id, h("p", {}, item.explanation),
    h("div", { class: "claims" }, ...item.claims.map((claim, index) =>
      h("article", {}, h("strong", {}, `Claim ${String.fromCharCode(65 + index)}`),
        h("p", {}, claim.text), h("small", {}, `${claim.date || "Undated"} · ${claim.source}`)))),
    h("p", { class: "muted" }, `Visible on ${item.paths.map((value) => value.title).join(", ")}`), form);
}

async function entitiesView() {
  const data = await api.get("entities");
  const selected = new Set();
  const rationale = h("input", { type: "text", placeholder: "Why these records should merge" });
  const sourcePath = h("input", { type: "text", placeholder: "sources/YYYY/MM/<capture-id>.md" });
  const sourceAssertion = h("textarea", { rows: 2, placeholder: "Exact source sentence stating they are the same identity" });
  const externalNamespace = h("input", { type: "text", placeholder: "External ID namespace" });
  const externalValue = h("input", { type: "text", placeholder: "Shared external ID value" });
  const merge = button("Merge selected", async (event) => {
    let evidence = null;
    if (sourcePath.value.trim() || sourceAssertion.value.trim()) {
      evidence = { source_assertions: [{
        path: sourcePath.value.trim(), assertion: sourceAssertion.value.trim(),
      }] };
    } else if (externalNamespace.value.trim() || externalValue.value.trim()) {
      evidence = { shared_external_id: {
        namespace: externalNamespace.value.trim(), value: externalValue.value.trim(),
      } };
    }
    const result = await act(event.currentTarget, () => api.post("entities/operation", {
      action: "merge", entity_ids: [...selected], rationale: rationale.value, evidence,
    }), "Entity merge queued.");
    if (result) window.location.hash = `#/captures/${result.id}`;
  }, "primary");
  const entities = data.entities.map((entity) => {
    const check = h("input", { type: "checkbox", onchange: () => {
      if (check.checked) selected.add(entity.id); else selected.delete(entity.id);
      merge.disabled = selected.size < 2;
    } });
    const deletionRationale = h("input", {
      type: "text",
      required: true,
      placeholder: "Why this identity should be removed",
    });
    const deletion = h("details", { class: "resolve" },
      h("summary", {}, "Delete identity"),
      h("form", { class: "inline-form", onsubmit: async (event) => {
        event.preventDefault();
        const result = await act(event.submitter, () => api.post("entities/operation", {
          action: "delete", entity_ids: [entity.id], rationale: deletionRationale.value,
        }), "Entity deletion queued.");
        if (result) window.location.hash = `#/captures/${result.id}`;
      } }, deletionRationale,
      h("button", { class: "button danger", type: "submit" }, "Queue deletion")));
    const claimRows = entity.claims.map((claim) => h("tr", {},
      h("td", {}, claim.value), h("td", {}, badge(claim.kind)),
      h("td", {}, claim.acl ? claim.acl.join(", ") : "Organization-wide"),
      h("td", {}, claim.source), h("td", {}, claim.actor), h("td", {}, fmtWhen(claim.introduced_at))));
    const externalIds = entity.external_ids.length
      ? h("p", { class: "muted" }, `External IDs: ${entity.external_ids.map((item) => `${item.namespace}:${item.value}`).join(", ")}`)
      : null;
    return card(entity.id,
      h("div", { class: "entity-head" }, h("label", { class: "check" }, check, "Select"), badge(entity.entity_type)),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ...["Name", "Kind", "Audience", "Source", "Actor", "Introduced"].map((name) => h("th", {}, name)))),
        h("tbody", {}, ...claimRows))), externalIds, deletion);
  });
  merge.disabled = true;
  return h("div", { class: "stack" }, card("Merge identities",
    h("p", { class: "muted" }, "Provide either one exact immutable-source assertion or an external ID present on every selected identity."),
    h("div", { class: "stack" }, field("Rationale", rationale),
      field("Source path", sourcePath), field("Exact assertion", sourceAssertion),
      field("Or external namespace", externalNamespace), field("External value", externalValue), merge)),
    data.redirects && Object.keys(data.redirects).length ? card("Redirects", h("pre", { class: "json" }, JSON.stringify(data.redirects, null, 2))) : null,
    ...entities);
}

async function gardenerView() {
  const data = await api.get("gardener");
  const trigger = button("Run lint-and-fix", async (event) => {
    const result = await act(event.currentTarget, () => api.post("gardener/trigger", {
      rationale: "Master-triggered corpus health run",
    }), "Gardener run queued.");
    if (result) window.location.hash = `#/captures/${result.id}`;
  }, "primary");
  const rows = data.runs.map((run) => card(fmtWhen(run.started_at),
    h("div", { class: "row-gap" }, badge(run.status)),
    definition([
      ["Finished", fmtWhen(run.finished_at)],
      ["Base commit", run.base_commit_sha || "—"],
      ["Head commit", run.head_commit_sha || "—"],
      ["Detected", run.stats.detected ?? "—"],
      ["Fixed", run.stats.fixed ?? "—"],
      ["Final violations", run.stats.final_violations ?? "—"],
      ["Failure", run.error || run.error_category || "—"],
      ["Run ID", run.id],
    ])));
  return h("div", { class: "stack" }, card("Autonomous operation", h("p", {},
    "One operation detects deterministic violations, repairs what is bounded, runs every gate, and lands at most one commit."), trigger),
    card("Run history", rows.length ? h("div", { class: "stack" }, ...rows) : h("p", { class: "muted" }, "No runs yet.")));
}

async function indexView() {
  const data = await api.get("index");
  return h("div", { class: "stack" },
    ...data.warnings.map((warning) => message(warning, "warn")),
    data.healthy ? message("The index is healthy.", "good") : null,
    card("Reconciliation state", definition([
      ["Repository HEAD", data.repository_head_sha || "Unknown"],
      ["Indexed commit", data.indexed_commit_sha || "Unknown"],
      ["Dirty", data.dirty ? "Yes" : "No"], ["Dirty since", fmtWhen(data.dirty_since)],
      ["Last incremental", fmtWhen(data.last_incremental_at)],
      ["Last full rebuild", fmtWhen(data.last_full_rebuild_at)],
      ["Indexed rows", data.indexed_rows],
      ["Embedding model", data.index_meta?.model],
    ])));
}

async function boot() {
  if (!token()) {
    login();
    return;
  }
  try {
    meta = await api.get("meta");
    shell();
  } catch (error) {
    clearToken();
    login(error.message);
  }
}

boot();
