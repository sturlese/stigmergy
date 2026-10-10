import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Check, Eraser, ExternalLink, Loader2, Pencil, Plus, Search, ShieldCheck, Trash2, UserCog, UsersRound, X } from "lucide-react";
import { motion } from "motion/react";
import * as React from "react";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm";
import { EmptyState, Notice, useToast } from "@/components/ui/feedback";
import { Dialog, DialogContent, Tooltip } from "@/components/ui/overlays";
import { Checkbox } from "@/components/ui/controls";
import { Avatar, Badge, Card, Field, Input, Skeleton, Textarea } from "@/components/ui/primitives";
import { api, enc } from "@/lib/api";
import { membershipChanges } from "@/lib/membership";
import type { Group, LayerErasure, LayerInventory, Person } from "@/lib/types";
import { cn, plural } from "@/lib/utils";
import { PAGE, errorMessage, useGroups } from "../ops/common";

export function GroupsView() {
  const groups = useGroups();
  const people = usePeople();
  const [creating, setCreating] = React.useState(false);
  const [deleting, setDeleting] = React.useState<Group | null>(null);
  const [managing, setManaging] = React.useState<Group | null>(null);
  const [erasing, setErasing] = React.useState<Group | null>(null);
  const queryClient = useQueryClient();
  const toast = useToast();
  const rows = groups.data?.groups ?? [];
  return (
    <div className={PAGE}>
      <PageHeader
        eyebrow="Access"
        title="Layers"
        description="Every capture lands in one layer and everything derived from it stays there. Everyone reads public; members read and publish in their layers."
        actions={<Button onClick={() => setCreating(true)}><Plus />New layer</Button>}
      />
      {groups.isLoading ? <div className="grid gap-3 sm:grid-cols-2">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-28 rounded-2xl" />)}</div> : null}
      {!groups.isLoading && !rows.length ? <EmptyState icon={UsersRound} title="No layers yet" /> : null}
      <div className="grid gap-3 sm:grid-cols-2">
        {rows.map((group, index) => (
          <motion.div key={group.name} layout initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * 0.04 }}>
            <GroupCard
              group={group}
              members={(people.data?.people ?? []).filter((person) => person.groups.includes(group.name))}
              onDelete={() => setDeleting(group)}
              onManage={() => setManaging(group)}
              onErase={() => setErasing(group)}
            />
          </motion.div>
        ))}
      </div>
      <NewGroupDialog open={creating} onOpenChange={setCreating} />
      <MembersDialog layer={managing?.name ?? null} onOpenChange={(open) => { if (!open) setManaging(null); }} />
      <EraseDialog layer={erasing?.name ?? null} onOpenChange={(open) => { if (!open) setErasing(null); }} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(open) => { if (!open) setDeleting(null); }}
        title={`Delete ${deleting?.name}?`}
        description="A layer that has members, a Slack channel, or knowledge cannot be deleted."
        confirmLabel="Delete layer"
        destructive
        onConfirm={async () => {
          await api.del(`groups/${enc(deleting!.name)}`);
          toast("Layer deleted.");
          queryClient.invalidateQueries({ queryKey: ["admin", "groups"] });
        }}
      />
    </div>
  );
}

const usePeople = () => useQuery({ queryKey: ["admin", "people"], queryFn: () => api.get<{ people: Person[] }>("people") });

function GroupCard({ group, members, onDelete, onManage, onErase }: {
  group: Group; members: Person[]; onDelete: () => void; onManage: () => void; onErase: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = React.useState(false);
  const [description, setDescription] = React.useState(group.description);
  React.useEffect(() => setDescription(group.description), [group.description]);
  const save = useMutation({
    mutationFn: () => api.put(`groups/${enc(group.name)}`, { description: description.trim() }),
    onSuccess: () => {
      toast("Layer updated.");
      setEditing(false);
      queryClient.invalidateQueries({ queryKey: ["admin", "groups"] });
    },
    onError: (failure) => toast(errorMessage(failure), "danger"),
  });
  return (
    <Card className="group h-full p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <div className="flex size-9 items-center justify-center rounded-xl bg-primary/10 text-primary">
            {group.unrestricted ? <ShieldCheck className="size-4" /> : <UsersRound className="size-4" />}
          </div>
          <div>
            <code className="font-mono text-[13.5px] font-medium">{group.name}</code>
            <div className="text-[12px] text-muted-foreground">{plural(group.members, "member")}</div>
          </div>
        </div>
        {group.unrestricted ? <Badge tone="primary">reads everything</Badge> : (
          <div className="flex items-center gap-1 opacity-60 transition group-hover:opacity-100">
            {group.erasing ? <Badge tone="warning">erasing</Badge> : null}
            <Tooltip content="Edit description"><Button variant="ghost" size="icon-sm" onClick={() => setEditing(true)} aria-label="Edit"><Pencil /></Button></Tooltip>
            {group.name === "public" ? null : (
              <Tooltip content="Erase content"><Button variant="ghost" size="icon-sm" onClick={onErase} aria-label="Erase content" className="hover:text-destructive"><Eraser /></Button></Tooltip>
            )}
            <Tooltip content="Delete"><Button variant="ghost" size="icon-sm" onClick={onDelete} aria-label="Delete" className="hover:text-destructive"><Trash2 /></Button></Tooltip>
          </div>
        )}
      </div>
      {editing ? (
        <form className="mt-4 grid gap-2" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
          <Textarea value={description} onChange={(event) => setDescription(event.target.value)} autoFocus rows={6}
            placeholder="What this layer is for" />
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11.5px] text-muted-foreground">Captures that name no layer are routed by this: say what belongs here and what does not.</span>
            <div className="flex shrink-0 gap-1">
              <Button size="icon-sm" type="submit" disabled={save.isPending} aria-label="Save">{save.isPending ? <Loader2 className="animate-spin" /> : <Check />}</Button>
              <Button size="icon-sm" variant="ghost" type="button" onClick={() => { setEditing(false); setDescription(group.description); }} aria-label="Cancel"><X /></Button>
            </div>
          </div>
        </form>
      ) : (
        <p className="mt-4 line-clamp-4 text-[13px] text-muted-foreground" title={group.description || undefined}>{group.description || "No description."}</p>
      )}
      {group.unrestricted ? null : (
        <div className="mt-4 flex items-center justify-between gap-3 border-t pt-3">
          <div className="flex -space-x-2">
            {members.slice(0, 6).map((person) => (
              <Tooltip key={person.subject} content={person.display_name}>
                <span><Avatar name={person.display_name} className="size-7 ring-2 ring-card" /></span>
              </Tooltip>
            ))}
            {members.length > 6 ? <span className="inline-flex size-7 items-center justify-center rounded-full bg-muted text-[10.5px] font-semibold text-muted-foreground ring-2 ring-card">+{members.length - 6}</span> : null}
            {!members.length ? <span className="text-[12px] text-muted-foreground">No members yet</span> : null}
          </div>
          <Button variant="secondary" size="sm" onClick={onManage}><UserCog />Members</Button>
        </div>
      )}
    </Card>
  );
}

function NewGroupDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => { if (open) { setName(""); setDescription(""); setError(null); } }, [open]);
  const create = useMutation({
    mutationFn: () => api.post("groups", { name: name.trim(), description: description.trim() }),
    onSuccess: () => {
      toast("Layer created.");
      queryClient.invalidateQueries({ queryKey: ["admin", "groups"] });
      onOpenChange(false);
    },
    onError: (failure) => setError(errorMessage(failure)),
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="New layer" description="2 to 32 lowercase letters, digits, or hyphens, starting with a letter.">
        <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (name.trim()) create.mutate(); }}>
          <Field label="Name"><Input value={name} onChange={(event) => setName(event.target.value)} placeholder="finance" className="font-mono" autoFocus /></Field>
          <Field label="Description" hint="Captures that name no layer are routed by this: say what belongs here and what does not.">
            <Textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={5} placeholder="What this layer is for" />
          </Field>
          {error ? <Notice tone="danger">{error}</Notice> : null}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={!name.trim() || create.isPending}>{create.isPending ? <Loader2 className="animate-spin" /> : <Plus />}Create layer</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function MembersDialog({ layer, onOpenChange }: { layer: string | null; onOpenChange: (open: boolean) => void }) {
  const people = usePeople();
  const queryClient = useQueryClient();
  const toast = useToast();
  const all = React.useMemo(() => people.data?.people ?? [], [people.data]);
  const [chosen, setChosen] = React.useState<Set<string>>(new Set());
  const [query, setQuery] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (!layer) return;
    setChosen(new Set(all.filter((person) => person.groups.includes(layer)).map((person) => person.subject)));
    setQuery("");
    setError(null);
  }, [layer, all]);
  const changes = layer ? membershipChanges(all, layer, chosen) : [];
  const added = changes.filter((change) => chosen.has(change.subject)).length;
  const save = useMutation({
    mutationFn: async () => {
      for (const change of changes) await api.put(`people/${enc(change.subject)}`, change.body);
    },
    onSuccess: () => {
      toast(`Members of ${layer} updated.`);
      onOpenChange(false);
    },
    onError: (failure) => setError(errorMessage(failure)),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "people"] });
      queryClient.invalidateQueries({ queryKey: ["admin", "groups"] });
    },
  });
  const needle = query.trim().toLowerCase();
  const rows = all
    .filter((person) => !needle || `${person.display_name} ${person.subject}`.toLowerCase().includes(needle))
    .sort((a, b) => Number(layer ? b.groups.includes(layer) : 0) - Number(layer ? a.groups.includes(layer) : 0)
      || a.display_name.localeCompare(b.display_name));
  const toggle = (subject: string) => setChosen((current) => {
    const next = new Set(current);
    if (next.has(subject)) next.delete(subject); else next.add(subject);
    return next;
  });
  return (
    <Dialog open={!!layer} onOpenChange={onOpenChange}>
      <DialogContent
        title={`Members of ${layer ?? ""}`}
        description="Members read and publish in this layer. Removing someone whose default layer this is moves their default to public."
      >
        <div className="grid gap-3">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Find a person or agent" className="pl-8" />
          </div>
          <div className="max-h-[52vh] overflow-y-auto rounded-xl border">
            {people.isLoading ? <div className="grid gap-2 p-3">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-10" />)}</div> : null}
            {rows.map((person) => {
              const on = chosen.has(person.subject);
              const changed = layer ? on !== person.groups.includes(layer) : false;
              return (
                <label
                  key={person.subject}
                  className={cn(
                    "flex cursor-pointer items-center gap-3 border-b px-3 py-2.5 transition last:border-b-0 hover:bg-muted/60",
                    changed && "bg-primary/5",
                    person.disabled && "opacity-60",
                  )}
                >
                  <Checkbox checked={on} onCheckedChange={() => toggle(person.subject)} aria-label={person.display_name} />
                  <Avatar name={person.display_name} className="size-7" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5 text-[13px] font-medium">
                      {person.display_name}
                      {person.kind === "agent" ? <Bot className="size-3.5 text-muted-foreground" /> : null}
                    </div>
                    <div className="truncate font-mono text-[11.5px] text-muted-foreground">{person.subject}</div>
                  </div>
                  <div className="flex shrink-0 gap-1">
                    {person.is_admin ? <Badge tone="primary">admin</Badge> : null}
                    {layer && person.default_audience?.[0] === layer ? <Badge tone="neutral">default</Badge> : null}
                    {person.disabled ? <Badge tone="neutral">disabled</Badge> : null}
                  </div>
                </label>
              );
            })}
            {!people.isLoading && !rows.length ? <div className="p-6 text-center text-[13px] text-muted-foreground">Nobody matches.</div> : null}
          </div>
          {error ? <Notice tone="danger">{error}</Notice> : null}
          <div className="flex items-center justify-between gap-2">
            <span className="text-[12px] text-muted-foreground">
              {changes.length ? `${added} to add · ${changes.length - added} to remove` : `${plural(chosen.size, "member")}`}
            </span>
            <div className="flex gap-2">
              <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button type="button" disabled={!changes.length || save.isPending} onClick={() => save.mutate()}>
                {save.isPending ? <Loader2 className="animate-spin" /> : <Check />}Save
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

type ErasureView = { inventory: LayerInventory; latest: LayerErasure | null };

const OPEN_ERASURE = new Set(["queued", "running"]);

function EraseDialog({ layer, onOpenChange }: { layer: string | null; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [typed, setTyped] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => { setTyped(""); setError(null); }, [layer]);
  const view = useQuery({
    queryKey: ["admin", "erasure", layer],
    queryFn: () => api.get<ErasureView>(`groups/${enc(layer!)}/erasure`),
    enabled: !!layer,
    refetchInterval: (query) => (OPEN_ERASURE.has(query.state.data?.latest?.status ?? "") ? 2500 : false),
  });
  const erase = useMutation({
    mutationFn: () => api.post(`groups/${enc(layer!)}/erasure`, { confirm: typed.trim() }),
    onSuccess: () => {
      toast(`Erasure of ${layer} queued.`, "info");
      setTyped("");
      queryClient.invalidateQueries({ queryKey: ["admin", "erasure", layer] });
      queryClient.invalidateQueries({ queryKey: ["admin", "groups"] });
    },
    onError: (failure) => setError(errorMessage(failure)),
  });
  const inventory = view.data?.inventory;
  const latest = view.data?.latest ?? null;
  const open = OPEN_ERASURE.has(latest?.status ?? "");
  const kinds = Object.entries(inventory?.pages ?? {});
  return (
    <Dialog open={!!layer} onOpenChange={onOpenChange}>
      <DialogContent
        title={`Erase ${layer ?? ""}`}
        description="Removes everything filed in this layer from the wiki, its Git history, the index, and evidence storage. It cannot be undone."
      >
        <div className="grid gap-4">
          {view.isLoading ? <Skeleton className="h-24 rounded-xl" /> : null}
          {inventory ? (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Count label="pages" value={inventory.pages_total}
                detail={kinds.length ? kinds.map(([kind, total]) => `${total} ${kind}`).join(" · ") : undefined} />
              <Count label="captures" value={inventory.captures} />
              <Count label="originals" value={inventory.originals} />
              <Count label="changes" value={inventory.changes} />
            </div>
          ) : null}
          {latest ? <ErasureStatus erasure={latest} /> : null}
          {open ? null : (
            <form className="grid gap-3" onSubmit={(event) => { event.preventDefault(); if (typed.trim() === layer) erase.mutate(); }}>
              <Notice tone="warning">
                The writer rewrites the repository history without this folder and force-pushes every branch and tag. Captures to this layer are refused until it finishes; members and the layer itself stay.
              </Notice>
              <Field label="Type the layer's name to confirm">
                <Input value={typed} onChange={(event) => setTyped(event.target.value)} placeholder={layer ?? ""}
                  className="font-mono" autoComplete="off" spellCheck={false} />
              </Field>
              {error ? <Notice tone="danger">{error}</Notice> : null}
              <div className="flex justify-end gap-2">
                <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
                <Button type="submit" variant="destructive" disabled={typed.trim() !== layer || erase.isPending}>
                  {erase.isPending ? <Loader2 className="animate-spin" /> : <Eraser />}Erase content
                </Button>
              </div>
            </form>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function Count({ label, value, detail }: { label: string; value: number; detail?: string }) {
  return (
    <div className="rounded-xl border bg-muted/40 px-3 py-2.5" title={detail}>
      <div className="text-[20px] font-semibold tabular-nums leading-tight">{value}</div>
      <div className="text-[11.5px] text-muted-foreground">{label}</div>
    </div>
  );
}

function ErasureStatus({ erasure }: { erasure: LayerErasure }) {
  if (erasure.status === "queued" && erasure.error) {
    return (
      <Notice tone="warning">
        Attempt {erasure.attempts} stopped: {erasure.error}. The writer tries again at {new Date(erasure.next_attempt_at).toLocaleTimeString()}
        {erasure.pushed ? "; the history is already rewritten, so it keeps trying until the rest is done." : "."}
      </Notice>
    );
  }
  if (OPEN_ERASURE.has(erasure.status)) {
    return (
      <Notice tone="info">
        <span className="inline-flex items-center gap-2">
          <Loader2 className="size-3.5 animate-spin" />
          {erasure.status === "queued" ? "Queued for the writer." : "The writer is erasing this layer; other writes wait until it finishes."}
        </span>
      </Notice>
    );
  }
  if (erasure.status === "failed") {
    return <Notice tone="danger">The erasure stopped after {plural(erasure.attempts, "attempt")} without pushing anything: {erasure.error || "no detail"}. Nothing was erased, and it can be requested again.</Notice>;
  }
  const receipt = erasure.receipt as Partial<LayerErasure["receipt"]> & { remaining?: { what: string; until: string; link?: string }[] };
  return (
    <div className="grid gap-2 rounded-xl border p-3 text-[12.5px]">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium">Erased {erasure.finished_at ? new Date(erasure.finished_at).toLocaleString() : ""}</span>
        <Badge tone="success">succeeded</Badge>
      </div>
      <div className="text-muted-foreground">
        {plural(Number(receipt.commits_removed ?? 0), "commit")} removed · {plural(Number(receipt.commits_rewritten ?? 0), "commit")} rewritten · {plural(Number(receipt.objects_deleted ?? 0), "stored object")} deleted · requested by {erasure.requested_by}
      </div>
      {receipt.remaining?.length ? (
        <ul className="grid gap-1 border-t pt-2">
          {receipt.remaining.map((item) => (
            <li key={item.what} className="flex items-start gap-1.5 text-muted-foreground">
              <span>Still elsewhere: {item.what}, until {item.until}.</span>
              {item.link ? <a href={item.link} target="_blank" rel="noreferrer" className="inline-flex shrink-0 items-center gap-0.5 text-primary hover:underline">how<ExternalLink className="size-3" /></a> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
