import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Hammer, Loader2, RefreshCcw, Sprout } from "lucide-react";
import { motion } from "motion/react";
import * as React from "react";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { EmptyState, Notice, useToast } from "@/components/ui/feedback";
import { Dialog, DialogContent } from "@/components/ui/overlays";
import { Badge, Card, Field, Input, Skeleton } from "@/components/ui/primitives";
import { Sha } from "@/components/ui/stat";
import { api, enc } from "@/lib/api";
import { useMe } from "@/lib/queries";
import { navigate } from "@/lib/router";
import type { CaptureReceipt, JobRun } from "@/lib/types";
import { cn, fromNow, when } from "@/lib/utils";
import { PAGE, errorMessage, statusTone } from "./common";

type Kind = "garden" | "recompile";

function duration(run: JobRun) {
  if (!run.finished_at) return "running";
  const seconds = (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000;
  return seconds < 90 ? `${Math.round(seconds)}s` : `${Math.round(seconds / 60)} min`;
}

export function GardenerView() {
  const data = useQuery({
    queryKey: ["admin", "gardener"],
    queryFn: () => api.get<{ runs: JobRun[]; recompilations: JobRun[] }>("gardener"),
    refetchInterval: (query) => ([...(query.state.data?.runs ?? []), ...(query.state.data?.recompilations ?? [])].some((run) => !run.finished_at) ? 5000 : false),
  });
  const [trigger, setTrigger] = React.useState<Kind | null>(null);
  const operator = useMe().data?.operator ?? false;
  const runs = [...(data.data?.runs ?? []), ...(data.data?.recompilations ?? [])].sort((a, b) => b.started_at.localeCompare(a.started_at));

  return (
    <div className={PAGE}>
      <PageHeader eyebrow="Operations" title="Gardener" description="The autonomous caretaker. It finds deterministic problems, repairs what is bounded, runs every gate, and lands at most one commit." />
      <div className="grid gap-4 md:grid-cols-2">
        {([
          ["garden", Sprout, "Lint and fix", "Detect violations and repair what can be repaired safely. A clean corpus records a zero-change run.", "text-success"],
          ["recompile", Hammer, "Recompile from sources", "Rebuild derived notes, concepts, and entities from the immutable sources. Advances everything or nothing.", "text-concept"],
        ] as const).map(([kind, Icon, title, text, tone], index) => (
          <motion.div key={kind} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * 0.07 }}>
            <Card className="relative h-full overflow-hidden p-5">
              <Icon className={cn("size-5", tone)} />
              <div className="mt-3 text-[15px] font-semibold tracking-tight">{title}</div>
              <p className="mt-1 text-[13px] text-muted-foreground">{text}</p>
              <Button className="mt-4" variant={kind === "garden" ? "default" : "secondary"} disabled={operator} title={operator ? "The workspace's administrators run it" : undefined} onClick={() => setTrigger(kind)}>Run now</Button>
            </Card>
          </motion.div>
        ))}
      </div>

      <div className="mb-3 mt-10 flex items-center justify-between">
        <h2 className="text-[14px] font-semibold">Run history</h2>
        <Button variant="ghost" size="sm" onClick={() => data.refetch()}><RefreshCcw className={cn(data.isFetching && "animate-spin")} />Refresh</Button>
      </div>
      {data.isLoading ? <div className="grid gap-3">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-20 rounded-xl" />)}</div> : null}
      {!data.isLoading && !runs.length ? <EmptyState icon={Sprout} title="No runs yet">The gardener runs on a schedule; you can also start one above.</EmptyState> : null}
      <div className="grid gap-2.5">
        {runs.map((run, index) => (
          <motion.div key={run.id} initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: Math.min(index, 12) * 0.03 }}>
            <Card className="p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2.5">
                  {run.job === "recompile" ? <Hammer className="size-4 text-concept" /> : <Sprout className="size-4 text-success" />}
                  <span className="text-[13.5px] font-medium">{run.job === "recompile" ? "Recompilation" : "Garden run"}</span>
                  <Badge tone={statusTone(run.status)}>{!run.finished_at ? <Loader2 className="animate-spin" /> : null}{run.status}</Badge>
                </div>
                <div className="text-[12px] text-muted-foreground">{fromNow(run.started_at)} · {duration(run)}</div>
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-[12px] text-muted-foreground">
                {(["detected", "fixed", "final_violations"] as const).map((key) => run.stats[key] != null ? (
                  <span key={key}><span className="font-semibold tabular-nums text-foreground">{String(run.stats[key])}</span> {key.replace("_", " ")}</span>
                ) : null)}
                {run.base_commit_sha ? <span className="flex items-center gap-1">base <Sha value={run.base_commit_sha} /></span> : null}
                {run.head_commit_sha && run.head_commit_sha !== run.base_commit_sha ? <span className="flex items-center gap-1">head <Sha value={run.head_commit_sha} /></span> : null}
                <span title={when(run.started_at)} className="ml-auto font-mono text-[11px]">{run.id.slice(0, 8)}</span>
              </div>
              {run.error || run.error_category ? <Notice tone="danger" className="mt-3">{run.error || run.error_category}</Notice> : null}
            </Card>
          </motion.div>
        ))}
      </div>
      <TriggerDialog kind={trigger} onClose={() => setTrigger(null)} />
    </div>
  );
}

function TriggerDialog({ kind, onClose }: { kind: Kind | null; onClose: () => void }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [rationale, setRationale] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => { setRationale(""); setError(null); }, [kind]);
  const run = useMutation({
    mutationFn: () => api.post<CaptureReceipt>(kind === "recompile" ? "knowledge/recompile" : "gardener/trigger", { rationale }),
    onSuccess: (receipt) => {
      toast(kind === "recompile" ? "Recompilation queued." : "Gardener run queued.");
      queryClient.invalidateQueries({ queryKey: ["admin"] });
      onClose();
      navigate(`captures/${enc(receipt.id)}`);
    },
    onError: (failure) => setError(errorMessage(failure)),
  });
  return (
    <Dialog open={!!kind} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent
        title={kind === "recompile" ? "Recompile derived knowledge" : "Run the gardener"}
        description={kind === "recompile" ? "Rewrites every derived page from the immutable sources. Sources themselves never change." : "Repairs only what is bounded and passes every gate."}
      >
        <Field label="Rationale" hint="Optional; recorded with the run.">
          <Input value={rationale} onChange={(event) => setRationale(event.target.value)} autoFocus />
        </Field>
        {error ? <Notice tone="danger">{error}</Notice> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button onClick={() => run.mutate()} disabled={run.isPending}>{run.isPending ? <Loader2 className="animate-spin" /> : null}Queue run</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
