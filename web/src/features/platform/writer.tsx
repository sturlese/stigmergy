import { useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, Cpu } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { EmptyState, Notice, Table, Td, Th } from "@/components/ui/feedback";
import { Badge, Card, CardContent, CardHeader, CardTitle, Dot, Skeleton } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { PlatformWriter } from "@/lib/types";
import { fromNow } from "@/lib/utils";
import { PAGE, errorMessage } from "../ops/common";

export function WriterView() {
  const writer = useQuery({ queryKey: ["platform", "writer"], queryFn: () => api.get<PlatformWriter>("platform/worker"), refetchInterval: 15_000 });
  const beat = writer.data?.heartbeat;
  const asleep = beat?.state === "sleeping";
  const stale = beat ? !asleep && Date.now() - new Date(beat.heartbeat_at).getTime() > 5 * 60_000 : true;
  return (
    <div className={PAGE}>
      <PageHeader eyebrow="Platform" title="Writer" description="One writer serves every workspace in turn. Its heartbeat, what failed recently, and what operators did." />
      {writer.isError ? <Notice tone="danger" className="mb-5">{errorMessage(writer.error)}</Notice> : null}
      <div className="grid gap-5 lg:grid-cols-[18rem_1fr]">
        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2"><Cpu className="size-4 text-primary" />Heartbeat</CardTitle></CardHeader>
          <CardContent>
            {writer.isLoading ? <Skeleton className="h-12" /> : beat ? (
              <div className="grid gap-1.5">
                <span className="flex items-center gap-2 text-[15px] font-medium"><Dot tone={stale ? "danger" : asleep ? "neutral" : "success"} pulse={!stale && !asleep} />{asleep ? "asleep until work arrives" : beat.state}</span>
                <span className="text-[12.5px] text-muted-foreground">{fromNow(beat.heartbeat_at)}{stale ? " · stale" : ""}</span>
              </div>
            ) : <span className="text-muted-foreground">No heartbeat yet.</span>}
          </CardContent>
        </Card>
        <Card className="overflow-hidden">
          <CardHeader><CardTitle className="flex items-center gap-2"><AlertTriangle className="size-4 text-destructive" />Failed recently</CardTitle></CardHeader>
          {writer.data && !writer.data.failed.length ? <EmptyState icon={Activity} title="Nothing failed" className="m-5 mt-0 border-0">Every recent job landed or is still queued.</EmptyState> : null}
          {writer.data?.failed.length ? (
            <Table>
              <thead><tr><Th>Workspace</Th><Th>Operation</Th><Th>Why</Th><Th>When</Th></tr></thead>
              <tbody>
                {writer.data.failed.map((row) => (
                  <tr key={row.id} className="hover:bg-muted/50">
                    <Td className="font-mono text-[12.5px]">{row.tenant}</Td>
                    <Td><Badge>{row.operation}</Badge></Td>
                    <Td className="text-muted-foreground">{row.error_category || "—"}</Td>
                    <Td className="whitespace-nowrap text-muted-foreground">{fromNow(row.finished_at)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : null}
        </Card>
      </div>
      <Card className="mt-5 overflow-hidden">
        <CardHeader><CardTitle>Platform activity</CardTitle></CardHeader>
        {writer.data?.actions.length ? (
          <Table>
            <thead><tr><Th>When</Th><Th>Who</Th><Th>What</Th><Th>Workspace</Th></tr></thead>
            <tbody>
              {writer.data.actions.map((row) => (
                <tr key={row.id} className="hover:bg-muted/50">
                  <Td className="whitespace-nowrap text-muted-foreground">{fromNow(row.ts)}</Td>
                  <Td>{row.actor}</Td>
                  <Td className="font-mono text-[12.5px]">{row.action}</Td>
                  <Td className="font-mono text-[12.5px] text-muted-foreground">{row.tenant || "—"}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : <CardContent className="text-muted-foreground">No platform action yet.</CardContent>}
      </Card>
    </div>
  );
}
