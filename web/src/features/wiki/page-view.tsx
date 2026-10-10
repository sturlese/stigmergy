import { ArrowUpRight, CalendarDays, FileQuestion, Link2, Network, Quote } from "lucide-react";
import { motion } from "motion/react";
import * as React from "react";
import { TYPE_META, TYPE_ORDER, TypeBadge, TypeIcon } from "@/components/type";
import { LayerBadges } from "@/components/ui/audience";
import { Button } from "@/components/ui/button";
import { Collapsible, EmptyState, Notice } from "@/components/ui/feedback";
import { Badge, Card, Skeleton } from "@/components/ui/primitives";
import { usePage } from "@/lib/queries";
import { navigate, wikiHref } from "@/lib/router";
import type { PageRef, PageType, WikiPage } from "@/lib/types";
import { onDay, plural } from "@/lib/utils";
import { useLinkPreview } from "./link-preview";
import { SigmaGraph } from "./sigma-graph";

export function PageView({ path }: { path: string }) {
  const page = usePage(path);
  if (page.isLoading) return <PageSkeleton />;
  if (page.isError || !page.data) {
    return (
      <div className="mx-auto max-w-lg px-8 py-24">
        <EmptyState icon={FileQuestion} title="Nothing here">
          This page does not exist, or it is not visible to you. <a href="#/wiki" className="text-primary hover:underline">Back to the wiki</a>
        </EmptyState>
      </div>
    );
  }
  return <Page page={page.data} />;
}

function Page({ page }: { page: WikiPage }) {
  const body = React.useRef<HTMLDivElement>(null);
  const preview = useLinkPreview(body);
  return (
    <div className="grid min-h-full grid-cols-1 xl:grid-cols-[minmax(0,1fr)_20rem]">
      <motion.article
        key={page.path}
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
        className="min-w-0 px-8 pb-24 pt-10 lg:px-14"
      >
        <nav className="mb-6 flex items-center gap-1.5 text-[12px] text-muted-foreground">
          <a href="#/wiki" className="hover:text-foreground">Wiki</a>
          <span>/</span>
          <span className="flex items-center gap-1.5"><TypeIcon type={page.type} className="size-3.5" />{TYPE_META[page.type].plural}</span>
        </nav>
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <TypeBadge type={page.type} entityType={page.entity_type} />
          {page.status ? <Badge>{page.status}</Badge> : null}
          <LayerBadges layers={page.layers} />
        </div>
        <h1 className="max-w-3xl font-serif text-[42px] font-medium leading-[1.08] tracking-[-0.02em] text-balance">{page.title}</h1>
        {page.aliases.length ? (
          <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[12px] text-muted-foreground">
            <span>Also known as</span>
            {page.aliases.map((alias) => <span key={alias} className="rounded-md border bg-muted/60 px-1.5 py-0.5 text-foreground/80">{alias}</span>)}
          </div>
        ) : null}
        <div className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-2 border-b pb-6 text-[12px] text-muted-foreground">
          <span className="flex items-center gap-1.5"><CalendarDays className="size-3.5" />Updated {onDay(page.updated)}</span>
          <span className="flex items-center gap-1.5"><Link2 className="size-3.5" />{plural(page.backlinks.length, "backlink")}</span>
          <span className="flex items-center gap-1.5"><Quote className="size-3.5" />{plural(page.cited.length, "source")} cited</span>
        </div>
        {page.truncated ? <Notice tone="warning" className="mt-6 max-w-2xl">This page is long; only its beginning is shown here.</Notice> : null}
        <div ref={body} className="prose-wiki mt-8" dangerouslySetInnerHTML={{ __html: page.html }} />
        {preview}
      </motion.article>

      <aside className="border-t px-5 py-8 xl:sticky xl:top-0 xl:h-dvh xl:overflow-y-auto xl:border-l xl:border-t-0">
        <SidePanel page={page} />
      </aside>
    </div>
  );
}

function SidePanel({ page }: { page: WikiPage }) {
  const [open, setOpen] = React.useState<Record<string, boolean>>({ backlinks: true, links: true, cited: true });
  return (
    <div className="grid gap-5">
      <Card className="overflow-hidden">
        <div className="flex items-center justify-between px-4 pb-1 pt-3.5">
          <div className="text-[12px] font-semibold">Connections</div>
          <Button variant="ghost" size="sm" className="h-7 gap-1 px-2 text-[11px]" onClick={() => navigate(`graph/${page.path.split("/").map(encodeURIComponent).join("/")}`)}>
            <Network className="size-3.5" />Graph
          </Button>
        </div>
        {page.graph.nodes.length > 1 ? (
          <SigmaGraph data={page.graph} center={page.path} mode="local" onNodeClick={(target) => navigate(wikiHref(target))} className="h-64 w-full" />
        ) : (
          <div className="flex h-40 items-center justify-center px-6 text-center text-[12px] text-muted-foreground">No visible page links here yet.</div>
        )}
      </Card>
      <RefList id="backlinks" title="Backlinks" items={page.backlinks} open={open} setOpen={setOpen} empty="No visible page links here." />
      <RefList id="links" title="Links to" items={page.links} open={open} setOpen={setOpen} empty="This page links nowhere." />
      <RefList id="cited" title="Cited sources" items={page.cited} open={open} setOpen={setOpen} empty="No visible sources cited." />
    </div>
  );
}

function RefList({ id, title, items, open, setOpen, empty }: {
  id: string; title: string; items: PageRef[]; open: Record<string, boolean>;
  setOpen: React.Dispatch<React.SetStateAction<Record<string, boolean>>>; empty: string;
}) {
  const grouped = TYPE_ORDER.map((type) => [type, items.filter((item) => item.type === type)] as [PageType, PageRef[]]).filter(([, list]) => list.length);
  return (
    <Collapsible
      open={open[id]}
      onOpenChange={(value) => setOpen((current) => ({ ...current, [id]: value }))}
      triggerClassName="h-7 rounded-md px-1 hover:bg-muted"
      trigger={
        <span className="flex flex-1 items-center gap-2 text-[12px] font-semibold">
          {title}
          <span className="ml-auto rounded-full bg-muted px-1.5 text-[10.5px] font-medium tabular-nums text-muted-foreground">{items.length}</span>
        </span>
      }
    >
      {items.length ? (
        <div className="mt-1 grid gap-2">
          {grouped.map(([type, list]) => (
            <div key={type} className="grid gap-px">
              {grouped.length > 1 ? <div className="px-2 pb-0.5 pt-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground/70">{TYPE_META[type].plural}</div> : null}
              {list.map((item) => (
                <a key={item.path} href={wikiHref(item.path)} className="group flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
                  <TypeIcon type={item.type} className="size-3.5" />
                  <span className="min-w-0 flex-1 truncate">{item.title}</span>
                  <ArrowUpRight className="size-3.5 opacity-0 transition group-hover:opacity-60" />
                </a>
              ))}
            </div>
          ))}
        </div>
      ) : <p className="px-2 py-2 text-[12px] text-muted-foreground">{empty}</p>}
    </Collapsible>
  );
}

function PageSkeleton() {
  return (
    <div className="px-14 pt-12">
      <Skeleton className="mb-5 h-5 w-24" />
      <Skeleton className="mb-3 h-11 w-2/3" />
      <Skeleton className="mb-10 h-4 w-72" />
      <div className="grid max-w-2xl gap-3">
        {Array.from({ length: 9 }, (_, i) => <Skeleton key={i} className={i % 4 === 3 ? "h-4 w-2/3" : "h-4"} />)}
      </div>
    </div>
  );
}
