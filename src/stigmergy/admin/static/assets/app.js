// Shell + hash router + login. The token lives in sessionStorage only; a 401 anywhere clears it
// and lands back here (api.js). No cookies, no external requests, no HTML-string sinks.

import { api, clearToken, onUnauthorized, storedToken, storeToken } from "./api.js";
import { banner, clear, el, icon, toast } from "./ui.js";
import {
  activityView, cronsView, digestView, entitiesView, entityDetailView, gardenerView,
  indexView, overviewView, queueDetailView, queueView, setMeta, workerView,
} from "./views.js";

const ROUTES = [
  { hash: "overview", label: "Overview", icon: "overview", render: overviewView },
  { hash: "queue", label: "Queue", icon: "queue", render: queueView },
  { hash: "crons", label: "Crons", icon: "crons", render: cronsView },
  { hash: "gardener", label: "Gardener", icon: "gardener", render: gardenerView },
  { hash: "digest", label: "Digest", icon: "digest", render: digestView },
  { hash: "index", label: "Index", icon: "index", render: indexView },
  { hash: "entities", label: "Entities", icon: "entities", render: entitiesView },
  { hash: "activity", label: "Activity", icon: "activity", render: activityView },
  { hash: "worker", label: "Worker", icon: "worker", render: workerView },
];

const DETAIL_ROUTES = [
  { pattern: /^queue\/(\d+)$/, parent: "queue", render: queueDetailView },
  { pattern: /^entities\/(\d+)$/, parent: "entities", render: entityDetailView },
];

const app = document.getElementById("app");
let cleanup = null;
let contentHost = null;
let navButtons = new Map();

function currentRoute() {
  const raw = window.location.hash.replace(/^#\/?/, "") || "overview";
  for (const d of DETAIL_ROUTES) {
    const match = raw.match(d.pattern);
    if (match) return { parent: d.parent, render: (host) => d.render(host, Number(match[1])) };
  }
  const route = ROUTES.find((r) => r.hash === raw);
  return route ? { parent: route.hash, render: route.render } : { parent: "overview", render: overviewView };
}

async function navigate() {
  if (!contentHost) return;
  if (cleanup) { try { cleanup(); } catch { /* view cleanup is best-effort */ } cleanup = null; }
  const { parent, render } = currentRoute();
  for (const [hash, button] of navButtons) button.classList.toggle("active", hash === parent);
  const title = ROUTES.find((r) => r.hash === parent)?.label || "Stigmergy Ops";
  document.title = `${title} — Stigmergy Ops`;
  const heading = document.getElementById("view-title");
  if (heading) heading.textContent = title;
  clear(contentHost);
  cleanup = (await render(contentHost)) || null;
}

function renderShell() {
  navButtons = new Map();
  const nav = ROUTES.map((route) => {
    const button = el("a", { class: "nav-item", href: `#/${route.hash}` }, icon(route.icon), route.label);
    navButtons.set(route.hash, button);
    return button;
  });
  contentHost = el("div", { id: "content" });
  clear(app).append(
    el("div", { class: "shell" },
      el("aside", { class: "sidebar" },
        el("div", { class: "brand" },
          el("div", { class: "brand-mark" }, "S"),
          el("div", {},
            el("div", { class: "brand-name" }, "Stigmergy Ops"),
            el("div", { class: "brand-sub" }, "the operations console"))),
        ...nav,
        el("div", { class: "nav-spacer" }),
        el("button", {
          class: "nav-item",
          onclick: () => { clearToken(); window.location.reload(); },
        }, icon("logout"), "Sign out"),
        el("div", { class: "nav-foot" }, "an ops surface — never reads the brain")),
      el("main", { class: "main" },
        el("div", { class: "topbar" },
          el("h1", { id: "view-title" }, "Overview"),
          el("div", { class: "spacer" }),
          el("button", {
            class: "btn small", title: "Reload this view",
            onclick: () => navigate(),
          }, icon("refresh", 14), "Refresh")),
        contentHost)));
  window.addEventListener("hashchange", navigate);
  onUnauthorized(() => window.location.reload());
  navigate();
}

function renderLogin(message) {
  clear(app).append(
    el("div", { class: "login-wrap" },
      el("div", { class: "card login" },
        el("div", { class: "brand" },
          el("div", { class: "brand-mark" }, "S"),
          el("div", {},
            el("div", { class: "brand-name" }, "Stigmergy Ops"),
            el("div", { class: "brand-sub" }, "the operations console"))),
        el("p", { class: "lead" },
          "Paste the admin token (minted with ", el("code", {}, "stigmergy-admin-token"),
          "). It stays in this browser session only."),
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
            el("span", {}, "Admin token"),
            el("input", { type: "password", autocomplete: "off", autofocus: true })),
          el("button", { class: "btn primary", type: "submit", style: "width:100%;justify-content:center" },
            "Open the console")))));
}

async function boot() {
  if (!storedToken()) {
    renderLogin();
    return;
  }
  try {
    const meta = await api.get("meta");
    setMeta(meta);
    renderShell();
  } catch (ex) {
    // A cleared/rotated token already reloads via api.js; anything else lands here with a reason.
    renderLogin(ex.status === 401 ? "" : ex.message);
  }
}

boot();
