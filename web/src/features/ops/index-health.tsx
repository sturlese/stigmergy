import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, Database, GitBranch, RefreshCcw } from "lucide-react";
import { motion } from "motion/react";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/feedback";
import { Badge, Card, Dot, Skeleton } from "@/components/ui/primitives";
import { Definitions, Sha } from "@/components/ui/stat";
import { api } from "@/lib/api";
import type { IndexState } from "@/lib/types";
import { cn, fromNow, when } from "@/lib/utils";
import { PAGE, errorMessage } from "./common";
import { GraphMeasuresPanel } from "./graph-measures";

export function IndexView() {
  const index = useQuery({ queryKey: ["admin", "index"], queryFn: () => api.get<IndexState>("index"), refetchInterval: 20_000 });
  const data = index.data;
  const inSync = data && !data.dirty && data.repository_head_sha === data.indexed_commit_sha;
  return (
    <div className={PAGE}>
      <PageHeader
        eyebrow="Operations"
        title="Search index"
        description="Postgres mirrors the knowledge repository for search. The webhook keeps it current; a nightly rebuild reconciles it from scratch."
        actions={<Button variant="secondary" size="sm" onClick={() => index.refetch()}><RefreshCcw className={cn(index.isFetching && "animate-spin")} />Refresh</Button>}
      />
      {index.isLoading ? <Skeleton className="h-64 rounded-2xl" /> : null}
      {index.isError ? <Notice tone="danger">{errorMessage(index.error)}</Notice> : null}
      {data ? (
        <div className="grid gap-4">
          {data.warnings.map((warning) => <Notice key={warning} tone="warning">{warning}</Notice>)}
          {data.filing_context?.due ? (
            <Notice tone="warning">
              A recent capture's filing context {data.filing_context.trimmed ? "was trimmed" : "passed half its byte budget"}:
              build it from the index (retrieval and curation, phase 4) before linking and deduplication degrade.
            </Notice>
          ) : null}
          {data.healthy ? <Notice tone="success">The index is healthy.</Notice> : null}
          <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
            <Card className="p-6">
              <div className="grid items-center gap-6 md:grid-cols-[1fr_auto_1fr]">
                <div className="rounded-2xl border bg-background/60 p-4">
                  <div className="flex items-center gap-2 text-[12px] text-muted-foreground"><GitBranch className="size-4" />Repository HEAD</div>
                  <div className="mt-2"><Sha value={data.repository_head_sha} /></div>
                </div>
                <div className="flex flex-col items-center gap-1.5">
                  <motion.div
                    animate={inSync ? { scale: [1, 1.12, 1] } : { x: [0, 3, -3, 0] }}
                    transition={{ duration: 1.6, repeat: Infinity, repeatDelay: 1.2 }}
                    className={cn("flex size-10 items-center justify-center rounded-full border-2", inSync ? "border-success text-success" : "border-warning text-warning")}
                  >
                    {inSync ? <CheckCircle2 className="size-5" /> : <RefreshCcw className="size-5" />}
                  </motion.div>
                  <Badge tone={inSync ? "success" : "warning"}>{inSync ? "In sync" : data.dirty ? `Catching up ${data.dirty_since ? fromNow(data.dirty_since) : ""}` : "Behind"}</Badge>
                </div>
                <div className="rounded-2xl border bg-background/60 p-4">
                  <div className="flex items-center gap-2 text-[12px] text-muted-foreground"><Database className="size-4" />Indexed commit</div>
                  <div className="mt-2"><Sha value={data.indexed_commit_sha} /></div>
                </div>
              </div>
              <Definitions className="mt-6" rows={[
                ["Pages indexed", data.indexed_rows.toLocaleString()],
                ["Last incremental", data.last_incremental_at ? `${fromNow(data.last_incremental_at)} · ${when(data.last_incremental_at)}` : "Never"],
                ["Last full rebuild", data.last_full_rebuild_at ? `${fromNow(data.last_full_rebuild_at)} · ${when(data.last_full_rebuild_at)}` : "Never"],
                ["Dirty", <span className="flex items-center gap-2"><Dot tone={data.dirty ? "warning" : "success"} />{data.dirty ? "Yes" : "No"}</span>],
                ["Embedding model", data.index_meta?.model],
                ["Text search", data.index_meta?.fts_config],
                ["Filing context", data.filing_context?.captures
                  ? <span className="flex items-center gap-2"><Dot tone={data.filing_context.due ? "warning" : "success"} />
                      {`at most ${Math.round(data.filing_context.max_share * 100)}% of its budget · ${data.filing_context.trimmed} trimmed · last ${data.filing_context.captures} captures`}</span>
                  : "No capture measured yet"],
              ]} />
            </Card>
          </motion.div>
        </div>
      ) : null}
      <GraphMeasuresPanel />
    </div>
  );
}
