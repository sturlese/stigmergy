// Shell + hash router + login. The token lives in sessionStorage only; a 401 anywhere clears it
// and lands back here (api.js). No cookies, no external requests, no HTML-string sinks.

import { api, clearToken, onUnauthorized, storedToken, storeToken } from "./api.js";
import { page as pageCopy } from "./copy.js";
import { notify, setMeta, setWindowDays, subscribe, windowDays } from "./state.js";
import {
  banner, clear, el, explainer, icon, keyLegend, mountToasts, svg, themePicker, toast,
} from "./ui.js";
import { activityView } from "./views/activity.js";
import { captureDetailView, capturesView } from "./views/captures.js";
import { dashboardView } from "./views/dashboard.js";
import { digestView } from "./views/digest.js";
import { entitiesView, entityDetailView } from "./views/entities.js";
import { gardenerView } from "./views/gardener.js";
import { inboxView } from "./views/inbox.js";
import { indexView } from "./views/index.js";
import { jobsView } from "./views/jobs.js";
import { repairDetailView, repairsView } from "./views/repairs.js";
import { workerView } from "./views/worker.js";

// The navigation is grouped by the JOB a person came to do, not by the package that serves it.
const GROUPS = [
  { label: "", routes: [
    { hash: "dashboard", icon: "dashboard", render: dashboardView, window: true },
  ] },
  { label: "work", routes: [
    { hash: "inbox", icon: "inbox", render: inboxView, badge: "inbox" },
    { hash: "captures", icon: "captures", render: capturesView, window: true },
    { hash: "entities", icon: "entities", render: entitiesView },
    { hash: "repairs", icon: "repairs", render: repairsView, window: true },
  ] },
  { label: "health", routes: [
    { hash: "gardener", icon: "gardener", render: gardenerView, window: true },
    { hash: "index", icon: "index", render: indexView, window: true },
    { hash: "worker", icon: "worker", render: workerView, window: true },
  ] },
  { label: "operate", routes: [
    { hash: "jobs", icon: "jobs", render: jobsView },
    { hash: "digest", icon: "digest", render: digestView },
    { hash: "activity", icon: "activity", render: activityView, window: true },
  ] },
];
const ROUTES = GROUPS.flatMap((g) => g.routes);

// `id` turns the matched segment into what the view's API takes: a row id for captures and
// repairs, the registry id AS TYPED for an entity — `Number("acme-corp")` is `NaN`, and a proposal
// opened from the inbox once asked the API for `entities/NaN`.
const DETAIL_ROUTES = [
  { pattern: /^captures\/(\d+)$/, parent: "captures", render: captureDetailView, id: Number },
  { pattern: /^entities\/([^/]+)$/, parent: "entities", render: entityDetailView, id: decodeURIComponent },
  { pattern: /^repairs\/(\d+)$/, parent: "repairs", render: repairDetailView, id: Number },
];

// The old tab names keep working — a bookmark must not land on the dashboard by surprise.
const ALIASES = { overview: "dashboard", queue: "captures", crons: "jobs" };

// Why a steward was signed out, carried across the reload a 401 forces (sessionStorage, one shot).
const SIGNOUT_KEY = "stigmergy-ops-signout";

const app = document.getElementById("app");
let cleanup = null;
let contentHost = null;
let navButtons = new Map();
let badgeNode = null;
let badgeTimer = null;
// A navigation token: a view that resolves after the next navigation started hands its cleanup
// back to be run at once, so a fast second click can never leave the first view's poll alive.
let navSeq = 0;

function currentRoute() {
  let raw = window.location.hash.replace(/^#\/?/, "") || "dashboard";
  const [head, ...rest] = raw.split("/");
  if (ALIASES[head]) raw = [ALIASES[head], ...rest].join("/");
  for (const d of DETAIL_ROUTES) {
    const match = raw.match(d.pattern);
    if (match) return { parent: d.parent, detail: true, render: (host) => d.render(host, d.id(match[1])) };
  }
  // A page may carry a sub-path of its own (`#/inbox/identity` is the inbox, filtered); the page
  // reads it off the hash itself, so only the head picks the route.
  const route = ROUTES.find((r) => r.hash === raw.split("/")[0]);
  return route ? { parent: route.hash, render: route.render, window: route.window }
    : { parent: "dashboard", render: dashboardView, window: true };
}

async function navigate() {
  if (!contentHost) return;
  const token = ++navSeq;
  if (cleanup) { try { cleanup(); } catch { /* view cleanup is best-effort */ } cleanup = null; }
  const { parent, render, detail, window: hasWindow } = currentRoute();
  for (const [hash, button] of navButtons) button.classList.toggle("active", hash === parent);
  const copy = pageCopy(parent);
  document.title = `${copy.title} — Stigmergy Ops`;
  const heading = document.getElementById("view-title");
  const purpose = document.getElementById("view-purpose");
  const tools = document.getElementById("view-tools");
  if (heading) heading.textContent = copy.title;
  if (purpose) purpose.textContent = copy.purpose;
  if (tools) clear(tools).append(...(hasWindow && !detail ? [windowPicker()] : []),
    el("button", { class: "btn small ghost", type: "button", onclick: () => navigate() }, icon("refresh", 14), "Refresh"));
  clear(contentHost);
  if (!detail) {
    const note = explainer(parent, copy.read);
    if (note) contentHost.append(note);
  }
  const viewHost = el("div", { class: "view" });
  contentHost.append(viewHost);
  const done = (await render(viewHost)) || null;
  if (token !== navSeq) {
    // another navigation won while this view was loading: its poll must not outlive it
    if (done) { try { done(); } catch { /* best-effort */ } }
    return;
  }
  cleanup = done;
  window.scrollTo({ top: 0 });
  notify();
}

function windowPicker() {
  const wrap = el("div", { class: "segmented", role: "group", "aria-label": "time window" });
  for (const days of [7, 30, 90]) {
    wrap.append(el("button", {
      class: windowDays() === days ? "on" : "", type: "button", "aria-pressed": String(windowDays() === days),
      onclick: () => { setWindowDays(days); navigate(); },
    }, `${days}d`));
  }
  return wrap;
}

async function refreshBadge(force = false) {
  // The periodic refresh skips a hidden tab (no point polling a page nobody is looking at); the
  // first load and a view change always fetch, or the badge would read "…" until the tab was
  // focused once.
  if (!badgeNode || (document.hidden && !force)) return;
  try {
    const inbox = await api.get("inbox");
    badgeNode.textContent = String(inbox.count);
    badgeNode.classList.toggle("zero", inbox.count === 0);
    badgeNode.title = `${inbox.count} waiting on a human`;
  } catch { /* the badge is a convenience; the inbox page says what is wrong */ }
}

function brandMark() {
  // three traces meeting — stigmergy: agents following each other's marks.
  const mark = svg("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
                            "stroke-linecap": "round", "aria-hidden": "true" });
  mark.append(svg("path", { d: "M4 18c3-6 6-9 9-9" }), svg("path", { d: "M4 12c4-3 8-4 12-3" }),
    svg("path", { d: "M6 6c5 1 9 4 12 8" }), svg("circle", { cx: 18, cy: 15, r: 2, fill: "currentColor", stroke: "none" }));
  return mark;
}

function renderShell() {
  navButtons = new Map();
  const nav = GROUPS.map((group) => el("div", { class: "nav-group" },
    group.label ? el("div", { class: "eyebrow" }, group.label) : null,
    ...group.routes.map((route) => {
      const copy = pageCopy(route.hash);
      const children = [icon(route.icon), copy.title];
      if (route.badge === "inbox") {
        badgeNode = el("span", { class: "badge zero" }, "…");
        children.push(badgeNode);
      }
      const button = el("a", { class: "nav-item", href: `#/${route.hash}`, title: copy.purpose }, ...children);
      navButtons.set(route.hash, button);
      return button;
    })));
  contentHost = el("div", { id: "content" });
  mountToasts();
  clear(app).append(
    el("a", { class: "skip-link", href: "#content" }, "Skip to content"),
    el("div", { class: "shell" },
      el("aside", { class: "sidebar" },
        el("div", { class: "brand" },
          el("div", { class: "brand-mark" }, brandMark()),
          el("div", {},
            el("div", { class: "brand-name" }, "Stigmergy Ops"),
            el("div", { class: "brand-sub" }, "the control room"))),
        el("nav", { "aria-label": "sections" }, ...nav),
        el("div", { class: "nav-spacer" }),
        keyLegend(),
        el("div", { class: "themerow" }, el("div", { class: "eyebrow" }, "appearance"), themePicker()),
        el("button", {
          class: "nav-item", type: "button",
          onclick: () => { clearToken(); window.location.reload(); },
        }, icon("logout"), "Sign out"),
        el("div", { class: "nav-foot" }, "an operations surface — it reads no page of the brain")),
      el("main", { class: "main" },
        el("div", { class: "topbar" },
          el("div", {},
            el("h1", { id: "view-title" }, "Dashboard"),
            el("div", { class: "purpose", id: "view-purpose" })),
          el("div", { class: "spacer" }),
          el("div", { class: "tools", id: "view-tools" })),
        contentHost)));
  window.addEventListener("hashchange", navigate);
  onUnauthorized(() => {
    sessionStorage.setItem(SIGNOUT_KEY, "You were signed out — the token was refused. It may have been rotated or revoked; ask for the current one, or mint a new pair with stigmergy-admin-token.");
    window.location.reload();
  });
  subscribe(() => refreshBadge(true));
  refreshBadge(true);
  badgeTimer = setInterval(() => refreshBadge(), 60000);
  navigate();
}

function renderLogin(message) {
  clear(app).append(
    el("div", { class: "login-wrap" },
      el("div", { class: "card login" },
        el("div", { class: "brand" },
          el("div", { class: "brand-mark" }, brandMark()),
          el("div", {},
            el("div", { class: "brand-name" }, "Stigmergy Ops"),
            el("div", { class: "brand-sub" }, "the control room"))),
        el("p", { class: "lead" },
          "Paste the admin token (minted with ", el("code", {}, "stigmergy-admin-token"),
          "). It stays in this browser session only — no cookie, nothing stored."),
        message ? banner("error", message) : null,
        el("form", {
          onsubmit: async (event) => {
            event.preventDefault();
            const token = event.target.querySelector("input").value.trim();
            if (!token) return;
            storeToken(token);
            try {
              const meta = await api.get("meta");
              setMeta(meta);
              renderShell();
            } catch (ex) {
              clearToken();
              toast(ex.message, "error");
            }
          },
        },
          el("label", { class: "field" },
            el("span", { class: "field-label" }, "Admin token"),
            el("input", { type: "password", autocomplete: "off", autofocus: true })),
          el("button", { class: "btn primary", type: "submit", style: { width: "100%", justifyContent: "center" } },
            "Open the console")),
        el("div", { class: "login-key" }, keyLegend(),
          el("div", { class: "themerow" }, el("div", { class: "eyebrow" }, "appearance"), themePicker())))));
}

async function boot() {
  const signout = sessionStorage.getItem(SIGNOUT_KEY) || "";
  sessionStorage.removeItem(SIGNOUT_KEY);
  if (!storedToken()) {
    renderLogin(signout);
    return;
  }
  try {
    const meta = await api.get("meta");
    setMeta(meta);
    renderShell();
  } catch (ex) {
    // A rotated token reloads via api.js and lands here with the reason it stashed; anything
    // else carries its own sentence.
    renderLogin(ex.status === 401 ? (signout || "the token was refused — it may have been rotated or revoked") : ex.message);
  }
}

window.addEventListener("beforeunload", () => { if (badgeTimer) clearInterval(badgeTimer); });
boot();
