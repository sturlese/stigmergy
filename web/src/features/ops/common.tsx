import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Group, Meta } from "@/lib/types";
import type { Tone } from "@/components/ui/primitives";

export const useMeta = () => useQuery({ queryKey: ["admin", "meta"], queryFn: () => api.get<Meta>("meta"), staleTime: 60_000 });
export const useGroups = () => useQuery({ queryKey: ["admin", "groups"], queryFn: () => api.get<{ groups: Group[] }>("groups") });

export const STATUS_TONE: Record<string, Tone> = {
  queued: "note",
  processing: "warning",
  landed: "success",
  failed: "danger",
  ok: "success",
  refused: "warning",
  error: "danger",
  succeeded: "success",
  running: "warning",
};

export function statusTone(status?: string | null): Tone {
  return STATUS_TONE[status ?? ""] ?? "neutral";
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The request failed.";
}

export function toISODate(value: string): string | undefined {
  return value ? value : undefined;
}

export const PAGE = "mx-auto w-full max-w-6xl px-8 pb-24 pt-12";
