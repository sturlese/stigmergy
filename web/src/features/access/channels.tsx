import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe2, Hash, Loader2, Lock, Plus, Trash2 } from "lucide-react";
import { motion } from "motion/react";
import * as React from "react";
import { PageHeader } from "@/components/page-header";
import { LayerField, PUBLIC_LAYER } from "@/components/ui/audience";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm";
import { EmptyState, Notice, Table, Td, Th, useToast } from "@/components/ui/feedback";
import { Dialog, DialogContent, Tooltip } from "@/components/ui/overlays";
import { Badge, Card, Field, Input, Skeleton } from "@/components/ui/primitives";
import { api, enc } from "@/lib/api";
import type { Channel } from "@/lib/types";
import { fromNow } from "@/lib/utils";
import { PAGE, errorMessage, useGroups } from "../ops/common";

export function ChannelsView() {
  const channels = useQuery({ queryKey: ["admin", "channels"], queryFn: () => api.get<{ channels: Channel[] }>("channels") });
  const [editing, setEditing] = React.useState<Channel | "new" | null>(null);
  const [removing, setRemoving] = React.useState<Channel | null>(null);
  const queryClient = useQueryClient();
  const toast = useToast();
  const rows = channels.data?.channels ?? [];
  return (
    <div className={PAGE}>
      <PageHeader
        eyebrow="Access"
        title="Slack channels"
        description="A :brain: reaction captures a thread into its channel's layer. Unmapped channels capture nothing."
        actions={<Button onClick={() => setEditing("new")}><Plus />Map a channel</Button>}
      />
      <Card className="overflow-hidden">
        {channels.isLoading ? <div className="grid gap-3 p-5">{Array.from({ length: 3 }, (_, i) => <Skeleton key={i} className="h-10" />)}</div> : null}
        {channels.isError ? <Notice tone="danger" className="m-5">{errorMessage(channels.error)}</Notice> : null}
        {!channels.isLoading && !rows.length ? (
          <EmptyState icon={Hash} title="No channels mapped" className="m-5 border-0">Map a channel to let people capture its threads with a :brain: reaction.</EmptyState>
        ) : null}
        {rows.length ? (
          <Table>
            <thead><tr><Th>Channel</Th><Th>Layer</Th><Th>Updated</Th><Th /></tr></thead>
            <tbody>
              {rows.map((channel, index) => (
                <motion.tr key={channel.channel_id} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: index * 0.03 }} className="group/row transition-colors hover:bg-muted/50">
                  <Td><span className="flex items-center gap-2 font-mono text-[13px]"><Hash className="size-4 text-muted-foreground" />{channel.channel_id}</span></Td>
                  <Td>
                    {channel.groups
                      ? <div className="flex flex-wrap gap-1"><Lock className="mr-0.5 size-3.5 text-warning" />{channel.groups.map((group) => <Badge key={group}>{group}</Badge>)}</div>
                      : <Badge tone="primary"><Globe2 />public</Badge>}
                  </Td>
                  <Td className="whitespace-nowrap text-muted-foreground">{fromNow(channel.updated_at)}{channel.updated_by ? ` · ${channel.updated_by}` : ""}</Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="sm" onClick={() => setEditing(channel)}>Edit</Button>
                      <Tooltip content="Unmap"><Button variant="ghost" size="icon-sm" onClick={() => setRemoving(channel)} aria-label="Unmap" className="hover:text-destructive"><Trash2 /></Button></Tooltip>
                    </div>
                  </Td>
                </motion.tr>
              ))}
            </tbody>
          </Table>
        ) : null}
      </Card>
      <ChannelDialog channel={editing} onClose={() => setEditing(null)} />
      <ConfirmDialog
        open={!!removing}
        onOpenChange={(open) => { if (!open) setRemoving(null); }}
        title={`Unmap ${removing?.channel_id}?`}
        description="Reactions in this channel stop capturing anything."
        confirmLabel="Unmap"
        destructive
        onConfirm={async () => {
          await api.del(`channels/${enc(removing!.channel_id)}`);
          toast("Channel unmapped.");
          queryClient.invalidateQueries({ queryKey: ["admin", "channels"] });
        }}
      />
    </div>
  );
}

function ChannelDialog({ channel, onClose }: { channel: Channel | "new" | null; onClose: () => void }) {
  const groups = useGroups();
  const queryClient = useQueryClient();
  const toast = useToast();
  const existing = channel && channel !== "new" ? channel : null;
  const [id, setId] = React.useState("");
  const [layer, setLayer] = React.useState(PUBLIC_LAYER);
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => {
    setId(existing?.channel_id ?? "");
    setLayer(existing?.groups?.[0] ?? PUBLIC_LAYER);
    setError(null);
  }, [channel]);
  const save = useMutation({
    mutationFn: () => api.put(`channels/${enc(id.trim().toUpperCase())}`, { groups: layer === PUBLIC_LAYER ? null : [layer] }),
    onSuccess: () => {
      toast("Channel mapped.");
      queryClient.invalidateQueries({ queryKey: ["admin", "channels"] });
      onClose();
    },
    onError: (failure) => setError(errorMessage(failure)),
  });
  return (
    <Dialog open={!!channel} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent wide title={existing ? `Edit #${existing.channel_id}` : "Map a Slack channel"} description="Captures from this channel file in the layer you choose; people in it cannot change it.">
        <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (id.trim()) save.mutate(); }}>
          <Field label="Channel ID" hint="In Slack: channel details → the ID at the bottom, e.g. C0123456789.">
            <Input value={id} onChange={(event) => setId(event.target.value)} readOnly={!!existing} placeholder="C0123456789" className="font-mono uppercase" autoFocus={!existing} />
          </Field>
          <Field label="Layer"><LayerField value={layer} onChange={setLayer} layers={(groups.data?.groups ?? []).map((group) => group.name)} /></Field>
          {error ? <Notice tone="danger">{error}</Notice> : null}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={!id.trim() || save.isPending}>{save.isPending ? <Loader2 className="animate-spin" /> : null}Save mapping</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
