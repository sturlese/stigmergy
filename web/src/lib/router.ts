import { useSyncExternalStore } from "react";

function subscribe(callback: () => void) {
  window.addEventListener("hashchange", callback);
  return () => window.removeEventListener("hashchange", callback);
}

function snapshot() {
  return window.location.hash.replace(/^#\/?/, "");
}

/** The current hash route, e.g. "wiki/wiki/entities/Acme.md" for "#/wiki/wiki/entities/Acme.md". */
export function useRoute(): string {
  return useSyncExternalStore(subscribe, snapshot, () => "");
}

export function navigate(to: string) {
  const target = to.startsWith("#") ? to : `#/${to.replace(/^\//, "")}`;
  if (window.location.hash !== target) window.location.hash = target;
}

/** Readers address pages by virtual path; a layer's repository path opens the page it belongs to. */
export function wikiHref(path: string): string {
  const virtual = path.replace(/^layers\/[a-z][a-z0-9-]{1,31}\//, "");
  return `#/wiki/${virtual.split("/").map(encodeURIComponent).join("/")}`;
}

export function pagePathFromRoute(route: string): string | null {
  if (!route.startsWith("wiki/")) return null;
  const rest = route.slice("wiki/".length);
  if (!rest) return null;
  try {
    return rest.split("/").map(decodeURIComponent).join("/");
  } catch {
    return null;
  }
}
