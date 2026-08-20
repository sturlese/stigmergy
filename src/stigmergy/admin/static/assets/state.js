// The little shared state every view reads: the server's meta (vocabularies, what is configured)
// and the chart window. Views never keep their own copy of either.

const WINDOW_KEY = "stigmergy-ops-window-days";

let META = {
  actor_default: "admin-console", github: { configured: false }, workflows: [], entity_types: [],
  statuses: [], parked_statuses: [], terminal_statuses: [], situations: [], repair_kinds: [],
  gardener_severities: [], item_kinds: [], decision_sources: [],
  metrics: { default_days: 30, max_days: 365 },
};
const listeners = new Set();

export function setMeta(meta) {
  META = { ...META, ...meta };
}

export function getMeta() {
  return META;
}

export function windowDays() {
  const stored = Number(sessionStorage.getItem(WINDOW_KEY));
  return [7, 30, 90].includes(stored) ? stored : (META.metrics?.default_days || 30);
}

export function setWindowDays(days) {
  sessionStorage.setItem(WINDOW_KEY, String(days));
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function notify() {
  for (const fn of listeners) fn();
}
