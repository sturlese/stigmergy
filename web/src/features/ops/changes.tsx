import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, FileCode2, GitCompare, Loader2, Minus, Plus } from "lucide-react";
import { motion } from "motion/react";
import * as React from "react";
import { PageHeader } from "@/components/page-header";
import { TYPE_META } from "@/components/type";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/controls";
import { DiffView } from "@/components/ui/diff";
import { Collapsible, EmptyState, Notice } from "@/components/ui/feedback";
import { Sheet, SheetContent } from "@/components/ui/overlays";
import { Badge, Card, Skeleton, type Tone } from "@/components/ui/primitives";
import { Definitions, Sha } from "@/components/ui/stat";
import { api, enc } from "@/lib/api";
import { navigate, wikiHref } from "@/lib/router";
import type { ChangeDetail, ChangeRow, PathChange } from "@/lib/types";
import { bytes, cn, fromNow, when } from "@/lib/utils";
import { PAGE, errorMessage, useMeta } from "./common";

const ACTION_TONE: Record<PathChange["action"], Tone> = { created: "success", updated: "note", deleted: "danger" };

export function ChangesView({ route }: { route: string }) {
  const selected = route.split("/")[1] ? decodeURIComponent(route.split("/")[1]) : null;
  const meta = useMeta();
  const [trigger, setTrigger] = React.useState("all");
  const changes = useQuery({
    queryKey: ["admin", "changes", trigger],
    queryFn: () => api.get<{ changes: ChangeRow[] }>(`changes?limit=100${trigger === "all" ? "" : `&trigger=${enc(trigger)}`}`),
  });
  const rows = changes.data?.changes ?? [];
  const triggers = meta.data?.change_triggers ?? [];

  return (
    <div className={PAGE}>
      <PageHeader
        eyebrow="Operations"
        title="Changes"
        description="Every commit the writer landed, explained path by path, with the exact Git patch one click away."
        actions={
          <Select
            className="w-52"
            value={trigger}
            onValueChange={setTrigger}
            options={[{ value: "all", label: "All triggers" }, ...triggers.map((value) => ({ value, label: value.replaceAll("_", " ") }))]}
          />
        }
      />
      {changes.isLoading ? <div className="grid gap-3">{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-20 rounded-xl" />)}</div> : null}
      {!changes.isLoading && !rows.length ? <EmptyState icon={GitCompare} title="No changes yet">When the writer lands a capture, its commit appears here.</EmptyState> : null}
      <div className="relative grid gap-2.5">
        {rows.length ? <div className="absolute bottom-4 left-[19px] top-4 w-px bg-border" /> : null}
        {rows.map((item, index) => (
          <motion.a
            key={item.id}
            href={`#/changes/${enc(item.id)}`}
            initial={{ opacity: 0, x: -8 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: Math.min(index, 14) * 0.03, duration: 0.35 }}
            className="group relative flex items-start gap-4"
          >
            <div className="z-10 mt-4 flex size-10 shrink-0 items-center justify-center rounded-full border bg-card shadow-sm transition group-hover:border-primary/50">
              <GitCompare className="size-4 text-primary" />
            </div>
            <Card className="flex-1 p-4 transition group-hover:border-primary/40 group-hover:shadow-[0_0_0_4px_var(--glow)]">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="text-[14px] font-medium leading-snug">{item.summary}</div>
                  <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
                    <Badge>{item.trigger.replaceAll("_", " ")}</Badge>
                    <span>{item.actor}</span>
                    <span>·</span>
                    <span>{fromNow(item.created_at)}</span>
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-3 text-[12px] tabular-nums">
                  {(["created", "updated", "deleted"] as const).map((action) => item.counts[action] ? (
                    <span key={action} className={cn("flex items-center gap-1", action === "created" ? "text-success" : action === "deleted" ? "text-destructive" : "text-note")}>
                      {action === "created" ? <Plus className="size-3" /> : action === "deleted" ? <Minus className="size-3" /> : <FileCode2 className="size-3" />}
                      {item.counts[action]}
                    </span>
                  ) : null)}
                  <code className="rounded-md border bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">{item.commit_sha.slice(0, 7)}</code>
                </div>
              </div>
            </Card>
          </motion.a>
        ))}
      </div>
      <ChangeSheet id={selected} onClose={() => navigate("changes")} />
    </div>
  );
}

function ChangeSheet({ id, onClose }: { id: string | null; onClose: () => void }) {
  const change = useQuery({
    queryKey: ["admin", "change", id],
    queryFn: () => api.get<{ change: ChangeDetail }>(`changes/${enc(id ?? "")}`),
    enabled: !!id,
  });
  const item = change.data?.change;
  const [showPatch, setShowPatch] = React.useState(false);
  React.useEffect(() => setShowPatch(false), [id]);
  const source = item?.source_summary;

  return (
    <Sheet open={!!id} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent title={item?.summary ?? "Change"} description={item ? `${item.trigger.replaceAll("_", " ")} · ${when(item.created_at)}` : undefined} className="w-[min(52rem,calc(100vw-1rem))]">
        {change.isLoading ? <div className="grid gap-3"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div> : null}
        {change.isError ? <Notice tone="danger">{errorMessage(change.error)}</Notice> : null}
        {item ? (
          <div className="grid gap-6">
            <div className="flex flex-wrap gap-2">
              {[["created", "success"], ["updated", "note"], ["deleted", "danger"]].map(([key, tone]) => (
                <Badge key={key} tone={tone as Tone}>{item.counts[key] ?? 0} {key}</Badge>
              ))}
              {item.counts.contradictions_added ? <Badge tone="concept">{item.counts.contradictions_added} contradictions added</Badge> : null}
              {item.counts.contradictions_resolved ? <Badge tone="success">{item.counts.contradictions_resolved} resolved</Badge> : null}
            </div>
            <Definitions rows={[
              ["Actor", item.actor],
              ["Commit", <Sha value={item.commit_sha} />],
              ["Parent", <Sha value={item.parent_commit_sha} />],
              ["Capture", item.capture_id ? <a className="text-primary hover:underline" href={`#/captures/${enc(item.capture_id)}`}>Open the capture</a> : null],
            ]} />

            {source ? (
              <Card className="p-4">
                <div className="mb-3 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">Captured evidence</div>
                <Definitions rows={[
                  ["Title", source.title || "Untitled"],
                  ["Adapter", source.adapter],
                  ["Captured", source.captured_at ? when(source.captured_at) : null],
                  ["Locator", source.locator],
                  ["Original URL", source.acquisition?.original_url],
                  ["Final URL", source.acquisition?.final_url],
                  ["Drive file", source.acquisition?.drive_file_id],
                  ["Artifacts", source.artifacts.map((value) => `${value.original_name || value.media_type} (${bytes(value.bytes)})`).join(", ")],
                ]} />
              </Card>
            ) : null}

            <div className="grid gap-3">
              {item.manifest.map((path) => <PathCard key={path.path} path={path} patch={item.path_patches?.[path.path] ?? ""} />)}
            </div>

            <Collapsible
              open={showPatch}
              onOpenChange={setShowPatch}
              triggerClassName="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground"
              trigger={<span>Exact Git patch · {bytes(item.exact_patch_bytes)}</span>}
            >
              <DiffView patch={item.exact_patch ?? ""} className="mt-3 max-h-[32rem]" />
            </Collapsible>
          </div>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function PathCard({ path, patch }: { path: PathChange; patch: string }) {
  const isSource = path.page_role === "source";
  const [open, setOpen] = React.useState(!isSource);
  const role = path.page_role === "ops" ? null : TYPE_META[path.page_role];
  const readable = path.action !== "deleted" && path.page_role !== "ops";
  return (
    <Card className="overflow-hidden">
      <div className="flex items-start justify-between gap-3 p-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={ACTION_TONE[path.action]}>{path.action}</Badge>
            {role ? <Badge tone={path.page_role as Tone}><role.icon />{role.label}</Badge> : <Badge>{path.page_role}</Badge>}
            <span className="text-[12px] tabular-nums"><span className="text-success">+{path.additions}</span> <span className="text-destructive">−{path.deletions}</span></span>
          </div>
          <div className="mt-2 break-all font-mono text-[12px] text-foreground/80">{path.path}</div>
          <p className="mt-1.5 text-[13px] text-muted-foreground">{path.reason}</p>
        </div>
        {readable ? (
          <Button variant="ghost" size="icon-sm" onClick={() => navigate(wikiHref(path.path))} aria-label="Open the page"><ArrowUpRight /></Button>
        ) : null}
      </div>
      <div className="border-t px-4 py-2.5">
        <Collapsible
          open={open}
          onOpenChange={setOpen}
          triggerClassName="text-[12px] font-medium text-muted-foreground hover:text-foreground"
          trigger={<span>{isSource ? "Show the archived source diff" : "Show line changes"}</span>}
        >
          <DiffView patch={patch} className="mb-1.5 mt-2.5 max-h-96" />
        </Collapsible>
      </div>
    </Card>
  );
}
