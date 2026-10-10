import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Building2, CircleDollarSign, Inbox, Loader2, Plus, RefreshCw, Sparkles, Trash2, UsersRound } from "lucide-react";
import { motion } from "motion/react";
import * as React from "react";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm";
import { EmptyState, Notice, Table, Td, Th, useToast } from "@/components/ui/feedback";
import { Dialog, DialogContent, Tooltip } from "@/components/ui/overlays";
import { Badge, Card, Dot, Field, Input, Skeleton } from "@/components/ui/primitives";
import { StatCard } from "@/components/ui/stat";
import { useActAs } from "@/components/workspace-switcher";
import { TokenReveal } from "@/features/access/token-dialogs";
import { api, enc } from "@/lib/api";
import { useWorkspaces } from "@/lib/queries";
import type { CreatedWorkspace, PlatformWorkspace, TokenGrant, WorkspaceStatus } from "@/lib/types";
import { fromNow } from "@/lib/utils";
import { PAGE, errorMessage } from "../ops/common";

const STATUS_TONE: Record<WorkspaceStatus, "success" | "warning" | "neutral"> = {
  active: "success", provisioning: "warning", suspended: "neutral",
};

export interface WorkspaceDraft { name: string; email: string }

export function WorkspacesView({ draft, onDraftUsed }: { draft?: WorkspaceDraft | null; onDraftUsed?: () => void }) {
  const workspaces = useWorkspaces();
  const actAs = useActAs();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [creating, setCreating] = React.useState<WorkspaceDraft | null>(null);
  const [grant, setGrant] = React.useState<TokenGrant | null>(null);
  const [toggling, setToggling] = React.useState<PlatformWorkspace | null>(null);
  const [deleting, setDeleting] = React.useState<PlatformWorkspace | null>(null);
  const rows = workspaces.data?.tenants ?? [];
  const erasing = React.useRef(new Map<string, string>());

  React.useEffect(() => {
    if (!workspaces.data) return;
    const present = new Set(rows.map((row) => row.id));
    for (const [id, name] of erasing.current) {
      if (!present.has(id)) { toast(`${name} is deleted. Its name is free for a clean start.`); erasing.current.delete(id); }
    }
    for (const row of rows) if (row.erasure && row.erasure.status !== "failed") erasing.current.set(row.id, row.name);
  }, [workspaces.data, rows, toast]);

  React.useEffect(() => {
    if (draft) { setCreating(draft); onDraftUsed?.(); }
  }, [draft, onDraftUsed]);

  const live = rows.filter((row) => row.status !== "suspended");
  const people = live.reduce((total, row) => total + row.people, 0);
  const waiting = live.reduce((total, row) => total + row.pending, 0);
  const spend = rows.reduce((total, row) => total + row.spend_7d.usd, 0);

  return (
    <div className={PAGE}>
      <PageHeader
        eyebrow="Platform"
        title="Workspaces"
        description="Every company on this Stigmergy: who is in it, what it is filing, and what its models cost."
        actions={<Button onClick={() => setCreating({ name: "", email: "" })}><Plus />New workspace</Button>}
      />
      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Workspaces" value={live.length} icon={Building2} index={0}
          hint={rows.length - live.length ? `${rows.length - live.length} suspended` : "all active"} />
        <StatCard label="People" value={people} icon={UsersRound} index={1} tone="text-entity" hint="across every workspace" />
        <StatCard label="Waiting to be filed" value={waiting} icon={Inbox} index={2} tone="text-note" hint="queued or being filed now" />
        <SpendCard usd={spend} />
      </div>
      <Card className="overflow-hidden">
        {workspaces.isLoading ? <div className="grid gap-3 p-5">{Array.from({ length: 3 }, (_, i) => <Skeleton key={i} className="h-12" />)}</div> : null}
        {workspaces.isError ? <Notice tone="danger" className="m-5">{errorMessage(workspaces.error)}</Notice> : null}
        {!workspaces.isLoading && !rows.length ? (
          <EmptyState icon={Sparkles} title="No workspace yet" className="m-5 border-0">Create the first one; its repository is ready a few seconds later.</EmptyState>
        ) : null}
        {rows.length ? (
          <Table>
            <thead><tr><Th>Workspace</Th><Th>Status</Th><Th className="text-right">People</Th><Th>Filing</Th><Th>Index</Th><Th>Last capture</Th><Th className="text-right">Spend 7d</Th><Th /></tr></thead>
            <tbody>
              {rows.map((row, index) => (
                <motion.tr key={row.id} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * 0.03 }}
                  className="group/row transition-colors hover:bg-muted/50">
                  <Td>
                    <div className="font-medium">{row.name}</div>
                    <div className="font-mono text-[11.5px] text-muted-foreground">{row.slug} · {row.repo}</div>
                  </Td>
                  <Td><WorkspaceStatusBadge row={row} /></Td>
                  <Td className="text-right tabular-nums">{row.people}</Td>
                  <Td className="whitespace-nowrap text-[12.5px]">
                    {row.pending ? <span className="text-note">{row.pending} waiting</span> : <span className="text-muted-foreground">idle</span>}
                    {row.failed ? <span className="text-destructive"> · {row.failed} failed</span> : null}
                  </Td>
                  <Td>
                    {row.index_dirty === null ? <span className="text-muted-foreground">not built</span>
                      : <span className="flex items-center gap-1.5 text-[12.5px]"><Dot tone={row.index_dirty ? "warning" : "success"} />{row.index_dirty ? "catching up" : "current"}</span>}
                  </Td>
                  <Td className="whitespace-nowrap text-muted-foreground">{row.last_landed_at ? fromNow(row.last_landed_at) : "never"}</Td>
                  <Td className="text-right tabular-nums">${row.spend_7d.usd.toFixed(2)}</Td>
                  <Td className="text-right">
                    {row.erasure && row.erasure.status !== "failed" ? null : (
                    <div className="flex justify-end gap-1">
                      <Tooltip content="Open as its administrator">
                        <Button variant="ghost" size="sm" disabled={row.status === "provisioning"} onClick={() => void actAs(row.id)}>Open</Button>
                      </Tooltip>
                      {row.status === "active" ? (
                        <Tooltip content="Rebuild its search index on the writer's next pass">
                          <Button variant="ghost" size="icon-sm" aria-label="Rebuild index" onClick={async () => {
                            try {
                              await api.post(`platform/tenants/${enc(row.id)}/rebuild`);
                              toast(`${row.name}: index rebuild queued.`);
                            } catch (failure) {
                              toast(errorMessage(failure), "danger");
                            }
                          }}><RefreshCw /></Button>
                        </Tooltip>
                      ) : null}
                      {row.status !== "provisioning" ? (
                        <Button variant="ghost" size="sm" onClick={() => setToggling(row)}
                          className={row.status === "active" ? "hover:text-destructive" : undefined}>
                          {row.status === "active" ? "Suspend" : "Resume"}
                        </Button>
                      ) : null}
                      {row.status === "suspended" && !row.slack_team_id ? (
                        <Tooltip content="Delete the whole workspace">
                          <Button variant="ghost" size="icon-sm" aria-label={`Delete ${row.name}`} onClick={() => setDeleting(row)}
                            className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"><Trash2 /></Button>
                        </Tooltip>
                      ) : null}
                    </div>
                    )}
                  </Td>
                </motion.tr>
              ))}
            </tbody>
          </Table>
        ) : null}
      </Card>
      <CreateWorkspaceDialog draft={creating} onClose={() => setCreating(null)} onCreated={(created) => {
        setGrant({ token: created.token.token, id: "", subject: created.token.subject, label: `first administrator of ${created.tenant.name}`,
          prefix: created.token.prefix, expires_at: null });
        queryClient.invalidateQueries({ queryKey: ["platform"] });
      }} />
      <TokenReveal grant={grant} signsIn onClose={() => setGrant(null)} />
      <DeleteWorkspaceDialog workspace={deleting} onClose={() => setDeleting(null)} onQueued={(row) => {
        erasing.current.set(row.id, row.name);
        toast(`Deleting ${row.name}. The writer takes it from here.`, "info");
        queryClient.invalidateQueries({ queryKey: ["platform"] });
      }} />
      <ConfirmDialog
        open={!!toggling}
        onOpenChange={(open) => { if (!open) setToggling(null); }}
        title={toggling?.status === "active" ? `Suspend ${toggling?.name}?` : `Resume ${toggling?.name}?`}
        description={toggling?.status === "active"
          ? "Its people cannot sign in or connect, its pushes are ignored, and nothing of it is filed. Its knowledge stays."
          : "Its people can sign in again and its queued captures are filed."}
        confirmLabel={toggling?.status === "active" ? "Suspend" : "Resume"}
        destructive={toggling?.status === "active"}
        onConfirm={async () => {
          const status = toggling!.status === "active" ? "suspended" : "active";
          await api.put(`platform/tenants/${enc(toggling!.id)}/status`, { status });
          toast(status === "active" ? "Workspace resumed." : "Workspace suspended.");
          queryClient.invalidateQueries({ queryKey: ["platform"] });
        }}
      />
    </div>
  );
}

function WorkspaceStatusBadge({ row }: { row: PlatformWorkspace }) {
  const erasure = row.erasure;
  if (!erasure) {
    return <Badge tone={STATUS_TONE[row.status]}>{row.status === "provisioning" ? <Loader2 className="animate-spin" /> : null}{row.status}</Badge>;
  }
  if (erasure.status === "failed") {
    return (
      <Tooltip content={`The deletion stopped for good: ${erasure.error}. Ask again once the cause is fixed.`}>
        <Badge tone="danger" className="whitespace-nowrap">deletion failed</Badge>
      </Tooltip>
    );
  }
  const step = !erasure.repo_deleted ? "repository" : !erasure.rows_deleted ? "data" : "evidence";
  const badge = <Badge tone="danger" className="whitespace-nowrap"><Loader2 className="animate-spin" />deleting · {step}</Badge>;
  return erasure.error
    ? <Tooltip content={`Attempt ${erasure.attempts} stopped: ${erasure.error}. The writer tries again shortly.`}>{badge}</Tooltip>
    : badge;
}

function DeleteWorkspaceDialog({ workspace, onClose, onQueued }: {
  workspace: PlatformWorkspace | null; onClose: () => void; onQueued: (row: PlatformWorkspace) => void;
}) {
  const [typed, setTyped] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => { setTyped(""); setError(null); }, [workspace]);
  const erase = useMutation({
    mutationFn: () => api.post(`platform/tenants/${enc(workspace!.id)}/erase`, { confirm: typed.trim() }),
    onSuccess: () => { const row = workspace!; onClose(); onQueued(row); },
    onError: (failure) => setError(errorMessage(failure)),
  });
  const matches = !!workspace && typed.trim() === workspace.slug;
  return (
    <Dialog open={!!workspace} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent title={`Delete ${workspace?.name ?? ""}`}
        description="Removes the whole workspace for good. Nothing of it can be brought back from Stigmergy.">
        <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (matches) erase.mutate(); }}>
          <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
            className="overflow-hidden rounded-xl border border-destructive/30 bg-destructive/[0.04]">
            <div className="flex items-center gap-2 border-b border-destructive/20 px-4 py-2.5 text-[12.5px] font-semibold text-destructive">
              <Trash2 className="size-3.5" />Danger zone
            </div>
            <ul className="grid gap-1.5 px-4 py-3 text-[13px]">
              <li>Its private repository <span className="font-mono text-[12px]">{workspace?.repo}</span></li>
              <li>Its {workspace?.people ?? 0} {workspace?.people === 1 ? "person" : "people"}, their tokens and sessions, and its layers</li>
              <li>Every capture, page, change, search index, and usage record</li>
              <li>The evidence only it holds, and the writer's checkout</li>
            </ul>
          </motion.div>
          <p className="text-[12.5px] leading-relaxed text-muted-foreground">
            Out of reach: GitHub keeps a deleted repository restorable for 90 days, and database backups keep rows until they expire.
            Afterwards <span className="font-mono text-foreground">{workspace?.slug}</span> and its people's emails are free, so you can create it again with a clean start.
          </p>
          <Field label="Type the workspace name to confirm">
            <Input value={typed} onChange={(event) => setTyped(event.target.value)} placeholder={workspace?.slug ?? ""}
              className="font-mono" autoComplete="off" spellCheck={false} autoFocus />
          </Field>
          {error ? <Notice tone="danger">{error}</Notice> : null}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="destructive" disabled={!matches || erase.isPending}>
              {erase.isPending ? <Loader2 className="animate-spin" /> : <Trash2 />}Delete workspace
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function SpendCard({ usd }: { usd: number }) {
  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5, delay: 0.18, ease: [0.16, 1, 0.3, 1] }}
      whileHover={{ y: -2 }} className="relative overflow-hidden rounded-2xl border bg-card p-4 text-concept shadow-elevated">
      <div className="flex items-center justify-between">
        <span className="text-[12px] font-medium text-muted-foreground">Model spend, 7 days</span>
        <CircleDollarSign className="size-4" />
      </div>
      <div className="mt-3 text-[30px] font-semibold leading-none tracking-tight tabular-nums text-foreground">${usd.toFixed(2)}</div>
      <div className="mt-2 truncate text-[11.5px] text-muted-foreground">estimated, filing and answers</div>
    </motion.div>
  );
}

function slugOf(name: string): string {
  const base = name.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "").replace(/^[^a-z]+/, "");
  return base.slice(0, 32).replace(/-+$/g, "");
}

function CreateWorkspaceDialog({ draft, onClose, onCreated }: {
  draft: WorkspaceDraft | null; onClose: () => void; onCreated: (created: CreatedWorkspace) => void;
}) {
  const [name, setName] = React.useState("");
  const [slug, setSlug] = React.useState("");
  const [slugTouched, setSlugTouched] = React.useState(false);
  const [email, setEmail] = React.useState("");
  const [adminName, setAdminName] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (!draft) return;
    setName(draft.name); setSlug(slugOf(draft.name)); setSlugTouched(false);
    setEmail(draft.email); setAdminName(""); setError(null);
  }, [draft]);
  const create = useMutation({
    mutationFn: () => api.post<CreatedWorkspace>("platform/tenants", {
      name: name.trim(), slug: slug.trim(), admin_email: email.trim(), admin_name: adminName.trim(),
    }),
    onSuccess: (created) => { onClose(); onCreated(created); },
    onError: (failure) => setError(errorMessage(failure)),
  });
  const ready = name.trim() && slug.trim() && email.trim() && adminName.trim();
  return (
    <Dialog open={!!draft} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent wide title="New workspace" description="Its private repository is created in a few seconds; its first administrator gets the token shown next.">
        <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (ready) create.mutate(); }}>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Company"><Input value={name} autoFocus onChange={(event) => {
              setName(event.target.value);
              if (!slugTouched) setSlug(slugOf(event.target.value));
            }} placeholder="Acme Labs" /></Field>
            <Field label="Workspace name" hint="Lowercase letters, digits and hyphens; it names the repository.">
              <Input value={slug} onChange={(event) => { setSlug(event.target.value); setSlugTouched(true); }} placeholder="acme-labs" className="font-mono" />
            </Field>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="First administrator's email"><Input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="founder@acme.com" /></Field>
            <Field label="Their name"><Input value={adminName} onChange={(event) => setAdminName(event.target.value)} placeholder="Ada Lovelace" /></Field>
          </div>
          {error ? <Notice tone="danger">{error}</Notice> : null}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={!ready || create.isPending}>{create.isPending ? <Loader2 className="animate-spin" /> : <Plus />}Create workspace</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
