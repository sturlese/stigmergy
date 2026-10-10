import { useQuery } from "@tanstack/react-query";
import { api, enc } from "./api";
import type { Catalog, GraphData, Me, PlatformWorkspace, SearchHit, WikiPage, WikiStatus } from "./types";

export const useMe = () => useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("me"), retry: false, staleTime: 60_000 });
export const useCatalog = (enabled = true) => useQuery({ queryKey: ["wiki", "catalog"], queryFn: () => api.get<Catalog>("wiki/pages"), staleTime: 60_000, enabled });
export const useWikiStatus = () => useQuery({ queryKey: ["wiki", "status"], queryFn: () => api.get<WikiStatus>("wiki/status"), staleTime: 30_000 });
export const useGraph = () => useQuery({ queryKey: ["wiki", "graph"], queryFn: () => api.get<GraphData>("wiki/graph"), staleTime: 60_000 });
export const usePage = (path: string | null) => useQuery({
  queryKey: ["wiki", "page", path],
  queryFn: () => api.get<WikiPage>(`wiki/page?path=${enc(path ?? "")}`),
  enabled: !!path,
  staleTime: 30_000,
  retry: (count, error) => (error as { status?: number }).status !== 404 && count < 1,
});
export const useSearch = (query: string, allowed = true) => useQuery({
  queryKey: ["wiki", "search", query],
  queryFn: () => api.get<{ hits: SearchHit[] }>(`wiki/search?q=${enc(query)}`),
  enabled: allowed && query.trim().length >= 3,
  staleTime: 60_000,
});
export const useWorkspaces = (enabled = true) => useQuery({
  queryKey: ["platform", "workspaces"],
  queryFn: () => api.get<{ tenants: PlatformWorkspace[]; acting: string | null }>("platform/tenants"),
  enabled,
  staleTime: 15_000,
  refetchInterval: (query) => (query.state.data?.tenants.some((tenant) => tenant.erasure) ? 2500 : false),
});
