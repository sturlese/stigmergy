import { useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, ArrowUpRight, GitCommitHorizontal, GitCompare, Inbox, RefreshCw, Swords } from "lucide-react";
import { motion } from "motion/react";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/feedback";
import { Badge, Card, Dot, Skeleton } from "@/components/ui/primitives";
import { Definitions, Sha, StatCard } from "@/components/ui/stat";
import { api } from "@/lib/api";
import type { CaptureStatus, Overview } from "@/lib/types";
import { cn, fromNow, when } from "@/lib/utils";
import { PAGE } from "./common";

const PIPELINE: { status: CaptureStatus; label: string; bar: string; dot: string }[] = [
  { status: "queued", label: "Queued", bar: "bg-note", dot: "bg-note" },
  { status: "processing", label: "Processing", bar: "bg-warning", dot: "bg-warning" },
  { status: "landed", label: "Landed", bar: "bg-success", dot: "bg-success" },
  { status: "failed", label: "Failed", bar: "bg-destructive", dot: "bg-destructive" },
];

export function OverviewView() {
  const overview = useQuery({ queryKey: ["admin", "overview"], queryFn: () => api.get<Overview>("overview"), refetchInterval: 15_000 });
  const data = overview.data;
  const counts = data?.captures.counts ?? {};
  const total = PIPELINE.reduce((sum, item) => sum + (counts[item.status] ?? 0), 0);
  const worker = data?.worker;
  const asleep = worker?.heartbeat?.state === "sleeping";
  const index = data?.index;
  const last = worker?.last_successful_write;

  return (
    <div className={PAGE}>
      <PageHeader
        eyebrow="Operations"
        title="Overview"
        description="The capture pipeline, the writer, and the search index at a glance. Refreshes every 15 seconds."
        actions={<Button variant="secondary" size="sm" onClick={() => overview.refetch()}><RefreshCw className={cn(overview.isFetching && "animate-spin")} />Refresh</Button>}
      />

      {overview.isLoading ? (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-28 rounded-2xl" />)}</div>
      ) : null}

      {data ? (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatCard index={0} label="Captures in flight" value={(counts.queued ?? 0) + (counts.processing ?? 0)} icon={Inbox} tone="text-note" href="#/captures"
              hint={data.captures.oldest_created_at.queued ? `Oldest queued ${fromNow(data.captures.oldest_created_at.queued)}` : "Nothing waiting"} />
            <StatCard index={1} label="Failed captures" value={counts.failed ?? 0} icon={AlertTriangle} tone={counts.failed ? "text-destructive" : "text-muted-foreground"} href="#/captures"
              hint={counts.failed ? "Open one to retry it" : "No failures"} />
            <StatCard index={2} label="Changes landed" value={data.changes} icon={GitCompare} tone="text-primary" href="#/changes"
              hint={last ? `Last ${fromNow(last.created_at)}` : "No change yet"} />
            <StatCard index={3} label="Open contradictions" value={data.contradictions} icon={Swords} tone="text-concept" href="#/contradictions"
              hint={data.contradictions ? "Waiting for a resolution" : "The corpus agrees with itself"} />
          </div>

          <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.25, duration: 0.5 }}>
            <Card className="mt-4 p-5">
              <div className="flex items-center justify-between">
                <div className="text-[13px] font-semibold">Capture pipeline</div>
                <div className="text-[12px] text-muted-foreground">{total.toLocaleString()} captures</div>
              </div>
              <div className="mt-4 flex h-3 w-full overflow-hidden rounded-full bg-muted">
                {PIPELINE.map((item, i) => {
                  const share = total ? ((counts[item.status] ?? 0) / total) * 100 : 0;
                  return share ? (
                    <motion.div
                      key={item.status}
                      className={cn("h-full first:rounded-l-full last:rounded-r-full", item.bar)}
                      initial={{ width: 0 }}
                      animate={{ width: `${share}%` }}
                      transition={{ duration: 0.9, delay: 0.3 + i * 0.08, ease: [0.16, 1, 0.3, 1] }}
                    />
                  ) : null;
                })}
              </div>
              <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2">
                {PIPELINE.map((item) => (
                  <div key={item.status} className="flex items-center gap-2 text-[12.5px]">
                    <span className={cn("size-2 rounded-full", item.dot)} />
                    <span className="text-muted-foreground">{item.label}</span>
                    <span className="font-semibold tabular-nums">{(counts[item.status] ?? 0).toLocaleString()}</span>
                  </div>
                ))}
              </div>
            </Card>
          </motion.div>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.35, duration: 0.5 }}>
              <Card className="h-full p-5">
                <div className="mb-4 flex items-center justify-between">
                  <div className="flex items-center gap-2 text-[13px] font-semibold"><Activity className="size-4 text-muted-foreground" />Writer</div>
                  <Badge tone={worker?.stale ? "danger" : asleep ? "neutral" : "success"}>
                    <Dot tone={worker?.stale ? "danger" : asleep ? "neutral" : "success"} pulse={!worker?.stale && !asleep} className="size-1.5" />
                    {worker?.stale ? "Heartbeat stale" : asleep ? "Asleep until work arrives" : "Alive"}
                  </Badge>
                </div>
                <Definitions rows={[
                  ["State", worker?.heartbeat?.state ?? "Unknown"],
                  ["Last heartbeat", worker?.heartbeat?.heartbeat_at ? `${fromNow(worker.heartbeat.heartbeat_at)} · ${when(worker.heartbeat.heartbeat_at)}` : "Never"],
                ]} />
                {worker?.stale ? <Notice tone="danger" className="mt-4">The writer has not reported for over ten minutes. Captures will queue until it is back.</Notice> : null}
              </Card>
            </motion.div>

            <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.42, duration: 0.5 }}>
              <Card className="h-full p-5">
                <div className="mb-4 flex items-center justify-between">
                  <div className="flex items-center gap-2 text-[13px] font-semibold"><GitCommitHorizontal className="size-4 text-muted-foreground" />Search index</div>
                  <Badge tone={index?.healthy ? "success" : "warning"}>
                    <Dot tone={index?.healthy ? "success" : "warning"} className="size-1.5" />
                    {index?.healthy ? (index.dirty ? "Catching up" : "In sync") : "Needs attention"}
                  </Badge>
                </div>
                <Definitions rows={[
                  ["Indexed commit", <Sha value={index?.indexed_commit_sha} />],
                  ["Pages indexed", index?.indexed_rows?.toLocaleString()],
                  ["Last update", index?.last_incremental_at ? fromNow(index.last_incremental_at) : "—"],
                  ["Full rebuild", index?.last_full_rebuild_at ? fromNow(index.last_full_rebuild_at) : "Never"],
                ]} />
                {(index?.warnings ?? []).map((warning) => <Notice key={warning} tone="warning" className="mt-3">{warning}</Notice>)}
              </Card>
            </motion.div>
          </div>

          <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.5, duration: 0.5 }}>
            <Card className="mt-4 p-5">
              <div className="mb-3 text-[13px] font-semibold">Last successful write</div>
              {last ? (
                <a href={`#/changes/${last.id}`} className="group flex items-start justify-between gap-4 rounded-xl border bg-background/60 p-4 transition hover:border-primary/40">
                  <div className="min-w-0">
                    <div className="text-[14px] font-medium">{last.summary}</div>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
                      <Badge>{last.trigger}</Badge>
                      <span>{when(last.created_at)}</span>
                      <span>·</span>
                      <span>{last.manifest.length} path{last.manifest.length === 1 ? "" : "s"}</span>
                    </div>
                  </div>
                  <ArrowUpRight className="size-4 shrink-0 text-muted-foreground transition group-hover:text-primary" />
                </a>
              ) : <p className="text-[13px] text-muted-foreground">No knowledge change has landed yet.</p>}
            </Card>
          </motion.div>
        </>
      ) : null}
      {overview.isError ? <Notice tone="danger">{(overview.error as Error).message}</Notice> : null}
    </div>
  );
}
