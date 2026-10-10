import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, MailPlus, Plus } from "lucide-react";
import { motion } from "motion/react";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { EmptyState, Notice, Table, Td, Th, useToast } from "@/components/ui/feedback";
import { Badge, Card, Skeleton } from "@/components/ui/primitives";
import { api, enc } from "@/lib/api";
import type { AccessRequest } from "@/lib/types";
import { fromNow } from "@/lib/utils";
import { PAGE, errorMessage } from "../ops/common";
import type { WorkspaceDraft } from "./workspaces";

export function RequestsView({ onCreate }: { onCreate: (draft: WorkspaceDraft) => void }) {
  const requests = useQuery({ queryKey: ["platform", "requests"], queryFn: () => api.get<{ requests: AccessRequest[] }>("platform/access-requests") });
  const queryClient = useQueryClient();
  const toast = useToast();
  const handle = useMutation({
    mutationFn: (row: AccessRequest) => api.put(`platform/access-requests/${enc(row.id)}`, { status: "handled" }),
    onSuccess: () => { toast("Marked handled."); queryClient.invalidateQueries({ queryKey: ["platform", "requests"] }); },
    onError: (failure) => toast(errorMessage(failure), "danger"),
  });
  const rows = requests.data?.requests ?? [];
  const fresh = rows.filter((row) => row.status === "new").length;
  return (
    <div className={PAGE}>
      <PageHeader eyebrow="Platform" title="Access requests"
        description={fresh ? `${fresh} waiting for an answer. Nothing is sent to anyone from here; you write to them yourself.` : "Emails left on the landing page. Nothing is sent to anyone from here."} />
      <Card className="overflow-hidden">
        {requests.isLoading ? <div className="grid gap-3 p-5">{Array.from({ length: 3 }, (_, i) => <Skeleton key={i} className="h-10" />)}</div> : null}
        {requests.isError ? <Notice tone="danger" className="m-5">{errorMessage(requests.error)}</Notice> : null}
        {!requests.isLoading && !rows.length ? (
          <EmptyState icon={MailPlus} title="No requests yet" className="m-5 border-0">When someone asks for access on the landing page, it shows up here.</EmptyState>
        ) : null}
        {rows.length ? (
          <Table>
            <thead><tr><Th>Email</Th><Th>Company</Th><Th>What they would capture</Th><Th>Received</Th><Th>Status</Th><Th /></tr></thead>
            <tbody>
              {rows.map((row, index) => (
                <motion.tr key={row.id} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: index * 0.02 }} className="transition-colors hover:bg-muted/50">
                  <Td className="font-medium"><a href={`mailto:${row.email}`} className="hover:text-primary">{row.email}</a></Td>
                  <Td>{row.company || <span className="text-muted-foreground">—</span>}</Td>
                  <Td className="max-w-xs truncate text-muted-foreground" title={row.note}>{row.note || "—"}</Td>
                  <Td className="whitespace-nowrap text-muted-foreground">{fromNow(row.created_at)}</Td>
                  <Td>{row.status === "new" ? <Badge tone="primary">new</Badge> : <Badge>handled</Badge>}</Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="sm" onClick={() => onCreate({ name: row.company, email: row.email })}><Plus />Workspace</Button>
                      {row.status === "new" ? <Button variant="ghost" size="sm" onClick={() => handle.mutate(row)}><Check />Handled</Button> : null}
                    </div>
                  </Td>
                </motion.tr>
              ))}
            </tbody>
          </Table>
        ) : null}
      </Card>
    </div>
  );
}
