import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, Boxes, Combine, Loader2, MoreHorizontal, PenLine, Search, Trash2, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import * as React from "react";
import { PageHeader } from "@/components/page-header";
import { LayerBadges, LayerField, physicalPath } from "@/components/ui/audience";
import { TypeIcon } from "@/components/type";
import { Button } from "@/components/ui/button";
import { EmptyState, Notice, useToast } from "@/components/ui/feedback";
import { Dialog, DialogContent, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, Sheet, SheetContent } from "@/components/ui/overlays";
import { Badge, Card, Field, Input, Skeleton, Textarea } from "@/components/ui/primitives";
import { api, enc } from "@/lib/api";
import { navigate, wikiHref } from "@/lib/router";
import type { CaptureReceipt, Dossier, EntitySummary } from "@/lib/types";
import { cn, onDay } from "@/lib/utils";
import { PAGE, errorMessage } from "./common";

type Operation = { action: "rename" | "delete"; entity: EntitySummary } | { action: "merge"; entities: EntitySummary[] };

export function EntitiesView() {
  const entities = useQuery({ queryKey: ["admin", "entities"], queryFn: () => api.get<{ entities: EntitySummary[] }>("entities") });
  const [query, setQuery] = React.useState("");
  const [selected, setSelected] = React.useState<string[]>([]);
  const [dossier, setDossier] = React.useState<EntitySummary | null>(null);
  const [operation, setOperation] = React.useState<Operation | null>(null);
  const all = entities.data?.entities ?? [];
  const byPath = new Map(all.map((entity) => [entity.path, entity]));
  const shown = query.trim()
    ? all.filter((entity) => [entity.title, ...entity.aliases, entity.entity_type].some((value) => value.toLowerCase().includes(query.toLowerCase())))
    : all;
  const toggle = (path: string) => setSelected((current) => (current.includes(path) ? current.filter((item) => item !== path) : [...current, path]));

  return (
    <div className={PAGE}>
      <PageHeader
        eyebrow="Operations"
        title="Entities"
        description="People, organizations, products, and places the brain keeps a page for. Rename, merge, or delete them as one atomic writer job."
      />
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <div className="relative w-full max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search by name, alias, or type" className="pl-9" />
        </div>
        <span className="text-[12px] text-muted-foreground">{shown.length} of {all.length}</span>
      </div>

      {entities.isLoading ? <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{Array.from({ length: 9 }, (_, i) => <Skeleton key={i} className="h-28 rounded-2xl" />)}</div> : null}
      {!entities.isLoading && !all.length ? <EmptyState icon={Boxes} title="No entity pages yet">The librarian creates one when a capture names a person, organization, or product.</EmptyState> : null}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {shown.map((entity, index) => {
          const order = selected.indexOf(entity.path);
          return (
            <motion.div key={entity.path} layout initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: Math.min(index, 18) * 0.02 }}>
              <Card className={cn("group relative h-full p-4 transition", order >= 0 ? "border-primary/60 shadow-[0_0_0_4px_var(--glow)]" : "hover:border-primary/30")}>
                <div className="flex items-start gap-3">
                  <button
                    onClick={() => toggle(entity.path)}
                    className={cn(
                      "flex size-9 shrink-0 items-center justify-center rounded-xl border transition",
                      order >= 0 ? "border-primary bg-primary text-[12px] font-semibold text-primary-foreground" : "bg-entity/10 hover:border-primary/50",
                    )}
                    aria-label={order >= 0 ? "Deselect for merge" : "Select for merge"}
                  >
                    {order >= 0 ? order + 1 : <TypeIcon type="entity" entityType={entity.entity_type} />}
                  </button>
                  <div className="min-w-0 flex-1">
                    <button onClick={() => setDossier(entity)} className="block max-w-full truncate text-left text-[14px] font-medium hover:text-primary">{entity.title}</button>
                    <div className="mt-0.5 flex flex-wrap items-center gap-1 text-[11.5px] capitalize text-muted-foreground">{entity.entity_type || "entity"}<LayerBadges layers={entity.layers} /></div>
                  </div>
                  <Menu>
                    <MenuTrigger className="rounded-md p-1 text-muted-foreground opacity-60 transition hover:bg-muted hover:text-foreground group-hover:opacity-100" aria-label="Entity actions">
                      <MoreHorizontal className="size-4" />
                    </MenuTrigger>
                    <MenuContent align="end">
                      <MenuItem onSelect={() => setDossier(entity)}><BookOpen />View dossier</MenuItem>
                      <MenuItem onSelect={() => navigate(wikiHref(entity.path))}><TypeIcon type="entity" />Open page</MenuItem>
                      <MenuSeparator />
                      <MenuItem onSelect={() => setOperation({ action: "rename", entity })}><PenLine />Rename…</MenuItem>
                      <MenuItem onSelect={() => setOperation({ action: "delete", entity })} className="text-destructive [&_svg]:text-destructive"><Trash2 />Delete…</MenuItem>
                    </MenuContent>
                  </Menu>
                </div>
                {entity.aliases.length ? (
                  <div className="mt-3 flex flex-wrap gap-1">
                    {entity.aliases.slice(0, 4).map((alias) => <span key={alias} className="rounded-md bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">{alias}</span>)}
                    {entity.aliases.length > 4 ? <span className="text-[11px] text-muted-foreground">+{entity.aliases.length - 4}</span> : null}
                  </div>
                ) : null}
              </Card>
            </motion.div>
          );
        })}
      </div>

      <AnimatePresence>
        {selected.length ? (
          <motion.div
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 30 }}
            transition={{ type: "spring", bounce: 0.2, duration: 0.45 }}
            className="fixed bottom-6 left-1/2 z-40 flex -translate-x-1/2 items-center gap-3 rounded-2xl border bg-popover py-2.5 pl-4 pr-2.5 shadow-2xl"
          >
            <Combine className="size-4 text-primary" />
            <span className="text-[13px]">
              <span className="font-semibold">{selected.length}</span> selected · the first survives
            </span>
            <Button size="sm" disabled={selected.length < 2} onClick={() => setOperation({ action: "merge", entities: selected.map((path) => byPath.get(path)!).filter(Boolean) })}>
              Merge…
            </Button>
            <Button size="icon-sm" variant="ghost" onClick={() => setSelected([])} aria-label="Clear selection"><X /></Button>
          </motion.div>
        ) : null}
      </AnimatePresence>

      <DossierSheet entity={dossier} onClose={() => setDossier(null)} />
      <OperationDialog operation={operation} onClose={() => setOperation(null)} onDone={() => setSelected([])} />
    </div>
  );
}

function DossierSheet({ entity, onClose }: { entity: EntitySummary | null; onClose: () => void }) {
  const dossier = useQuery({
    queryKey: ["admin", "dossier", entity?.path],
    queryFn: () => api.get<Dossier>(`entities/${enc(entity?.title ?? "")}/dossier`),
    enabled: !!entity,
  });
  const data = dossier.data;
  return (
    <Sheet open={!!entity} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent title={entity?.title ?? "Entity"} description={entity?.path}>
        {dossier.isLoading ? <div className="grid gap-3">{Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="h-12" />)}</div> : null}
        {dossier.isError ? <Notice tone="danger">{errorMessage(dossier.error)}</Notice> : null}
        {data && !data.found ? <Notice tone="warning">The entity page could not be found.</Notice> : null}
        {data?.entity ? (
          <div className="grid gap-6">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="entity"><TypeIcon type="entity" entityType={data.entity.entity_type} className="size-3" />{data.entity.entity_type}</Badge>
              {data.entity.status ? <Badge>{data.entity.status}</Badge> : null}
              {data.entity.updated ? <span className="text-[12px] text-muted-foreground">Updated {onDay(data.entity.updated)}</span> : null}
            </div>
            {data.entity.aliases.length ? (
              <div className="flex flex-wrap gap-1.5">{data.entity.aliases.map((alias) => <span key={alias} className="rounded-md border bg-muted/60 px-1.5 py-0.5 text-[12px]">{alias}</span>)}</div>
            ) : null}
            {data.entity.excerpt ? <p className="whitespace-pre-line font-serif text-[15px] leading-relaxed">{data.entity.excerpt}</p> : null}
            <section>
              <h4 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">Pages that link it · {data.knowledge.length}</h4>
              <div className="grid gap-2">
                {data.knowledge.map((item) => (
                  <a key={item.path} href={wikiHref(item.path)} onClick={onClose} className="rounded-xl border bg-background/60 p-3 transition hover:border-primary/40">
                    <div className="text-[13px] font-medium">{item.title}</div>
                    {item.relationships?.length ? (
                      <ul className="mt-1.5 grid gap-1">
                        {item.relationships.slice(0, 3).map((relationship, index) => <li key={index} className="text-[12.5px] text-muted-foreground">{relationship.statement}</li>)}
                      </ul>
                    ) : item.excerpt ? <p className="mt-1 line-clamp-2 text-[12.5px] text-muted-foreground">{item.excerpt}</p> : null}
                  </a>
                ))}
                {!data.knowledge.length ? <p className="text-[13px] text-muted-foreground">{data.knowledge_note || "No visible page links it."}</p> : null}
              </div>
            </section>
            {data.sources.length ? (
              <section>
                <h4 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">Sources · {data.sources.length}</h4>
                <div className="grid gap-1">
                  {data.sources.map((source) => (
                    <a key={source.path} href={wikiHref(source.path)} onClick={onClose} className="flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-muted-foreground hover:bg-muted hover:text-foreground">
                      <TypeIcon type="source" className="size-3.5" /><span className="truncate">{source.title}</span>
                    </a>
                  ))}
                </div>
              </section>
            ) : null}
          </div>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function OperationDialog({ operation, onClose, onDone }: { operation: Operation | null; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [rationale, setRationale] = React.useState("");
  const [newTitle, setNewTitle] = React.useState("");
  const [sourcePath, setSourcePath] = React.useState("");
  const [assertion, setAssertion] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const choices = !operation ? [] : operation.action === "merge"
    ? operation.entities.reduce<string[]>((common, entity, index) => index === 0 ? entity.layers : common.filter((layer) => entity.layers.includes(layer)), [])
    : operation.entity.layers;
  const [layer, setLayer] = React.useState("");
  React.useEffect(() => {
    setRationale(""); setSourcePath(""); setAssertion(""); setError(null);
    setNewTitle(operation?.action === "rename" ? operation.entity.title : "");
    setLayer(choices[0] ?? "");
  }, [operation]);

  const run = useMutation({
    mutationFn: () => {
      if (!operation) throw new Error("No operation");
      if (operation.action === "merge") {
        return api.post<CaptureReceipt>("entities/operation", {
          action: "merge", entities: operation.entities.map((entity) => physicalPath(layer, entity.path)), rationale,
          evidence: { source_assertions: [{ path: sourcePath.trim(), assertion: assertion.trim() }] },
        });
      }
      return api.post<CaptureReceipt>("entities/operation", {
        action: operation.action, entities: [operation.action === "delete" ? physicalPath(layer, operation.entity.path) : operation.entity.path], rationale,
        ...(operation.action === "rename" ? { new_title: newTitle.trim() } : {}),
      });
    },
    onSuccess: (receipt) => {
      toast(`Entity ${operation?.action} queued.`);
      queryClient.invalidateQueries({ queryKey: ["admin"] });
      onDone();
      onClose();
      navigate(`captures/${enc(receipt.id)}`);
    },
    onError: (failure) => setError(errorMessage(failure)),
  });

  if (!operation) return <Dialog open={false} />;
  const title = operation.action === "merge" ? "Merge entity pages" : operation.action === "rename" ? `Rename ${operation.entity.title}` : `Delete ${operation.entity.title}`;
  const ready = rationale.trim()
    && (operation.action === "rename" || layer)
    && (operation.action !== "rename" || newTitle.trim())
    && (operation.action !== "merge" || (sourcePath.trim() && assertion.trim()));

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent wide={operation.action === "merge"} title={title} description={
        operation.action === "merge"
          ? "The first page survives; the others fold into it, and every link is rewritten in one commit."
          : operation.action === "rename" ? "Every layer's page and every link to it are renamed in the same commit." : "That layer's page is removed and links that no longer resolve are swept in one commit."
      }>
        {operation.action !== "rename" ? (
          <Field label="Layer" hint={operation.action === "merge" ? "Only pages of one layer merge; these entities share the layers shown." : "Each layer keeps its own page for this entity."}>
            {choices.length ? <LayerField value={layer} onChange={setLayer} layers={choices} withPublic={false} /> : <span className="text-[12px] text-warning">These entities share no layer, so they cannot be merged.</span>}
          </Field>
        ) : null}
        {operation.action === "merge" ? (
          <div className="flex flex-wrap items-center gap-1.5">
            {operation.entities.map((entity, index) => (
              <Badge key={entity.path} tone={index === 0 ? "primary" : "neutral"}>{index === 0 ? "Keeps " : ""}{entity.title}</Badge>
            ))}
          </div>
        ) : null}
        {operation.action === "rename" ? <Field label="New title"><Input value={newTitle} onChange={(event) => setNewTitle(event.target.value)} autoFocus /></Field> : null}
        <Field label="Rationale"><Input value={rationale} onChange={(event) => setRationale(event.target.value)} placeholder="Why this change is right" /></Field>
        {operation.action === "merge" ? (
          <>
            <Field label="Source path" hint="The immutable source that says these are the same identity.">
              <Input value={sourcePath} onChange={(event) => setSourcePath(event.target.value)} className="font-mono text-[12px]" placeholder="sources/YYYY/MM/<capture-id>.md" />
            </Field>
            <Field label="Exact assertion" hint="Copied verbatim from that source.">
              <Textarea rows={2} value={assertion} onChange={(event) => setAssertion(event.target.value)} />
            </Field>
          </>
        ) : null}
        {error ? <Notice tone="danger">{error}</Notice> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant={operation.action === "delete" ? "destructive" : "default"} disabled={!ready || run.isPending} onClick={() => run.mutate()}>
            {run.isPending ? <Loader2 className="animate-spin" /> : null}
            Queue {operation.action}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
