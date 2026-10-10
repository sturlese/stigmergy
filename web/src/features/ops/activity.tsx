import { useQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";
import * as React from "react";
import { PageHeader } from "@/components/page-header";
import { EmptyState, Notice, Table, Td, Th, Tr } from "@/components/ui/feedback";
import { Avatar, Badge, Card, Skeleton } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import type { AdminAction } from "@/lib/types";
import { fromNow, when } from "@/lib/utils";
import { PAGE, errorMessage, statusTone } from "./common";

export function ActivityView() {
  const activity = useQuery({ queryKey: ["admin", "activity"], queryFn: () => api.get<{ actions: AdminAction[] }>("activity") });
  const [open, setOpen] = React.useState<number | null>(null);
  const rows = activity.data?.actions ?? [];
  return (
    <div className={PAGE}>
      <PageHeader eyebrow="Operations" title="Activity" description="Every administrative action, who took it, and how it ended. Tokens and sessions never appear here." />
      {activity.isError ? <Notice tone="danger">{errorMessage(activity.error)}</Notice> : null}
      <Card className="overflow-hidden">
        {activity.isLoading ? <div className="grid gap-3 p-5">{Array.from({ length: 8 }, (_, i) => <Skeleton key={i} className="h-8" />)}</div> : null}
        {!activity.isLoading && !rows.length ? <EmptyState icon={ScrollText} title="No recorded actions yet" className="m-5 border-0" /> : null}
        {rows.length ? (
          <Table>
            <thead><tr><Th>When</Th><Th>Actor</Th><Th>Action</Th><Th>Outcome</Th><Th>Details</Th></tr></thead>
            <tbody>
              {rows.map((row) => (
                <React.Fragment key={row.id}>
                  <Tr className="cursor-pointer" onClick={() => setOpen(open === row.id ? null : row.id)}>
                    <Td className="whitespace-nowrap text-muted-foreground" title={when(row.ts)}>{fromNow(row.ts)}</Td>
                    <Td><span className="flex items-center gap-2"><Avatar name={row.actor || "?"} className="size-6 text-[9px]" /><span className="truncate">{row.actor}</span></span></Td>
                    <Td><code className="font-mono text-[12px]">{row.action}</code></Td>
                    <Td><Badge tone={statusTone(row.outcome)}>{row.outcome}</Badge>{row.error_class ? <span className="ml-2 text-[11.5px] text-muted-foreground">{row.error_class}</span> : null}</Td>
                    <Td className="max-w-80 truncate font-mono text-[11.5px] text-muted-foreground">{JSON.stringify(row.args)}</Td>
                  </Tr>
                  {open === row.id ? (
                    <tr><td colSpan={5} className="border-b bg-muted/40 px-5 py-3">
                      <pre className="overflow-x-auto font-mono text-[11.5px] leading-relaxed">{JSON.stringify(row.args, null, 2)}</pre>
                    </td></tr>
                  ) : null}
                </React.Fragment>
              ))}
            </tbody>
          </Table>
        ) : null}
      </Card>
    </div>
  );
}
