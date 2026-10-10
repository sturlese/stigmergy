import { ChevronsDownUp, ChevronsUpDown, Lock, Search, X } from "lucide-react";
import * as React from "react";
import { TYPE_META, TYPE_ORDER, TypeIcon, entityIcon } from "@/components/type";
import { Collapsible } from "@/components/ui/feedback";
import { Tooltip } from "@/components/ui/overlays";
import { Skeleton } from "@/components/ui/primitives";
import { useCatalog } from "@/lib/queries";
import { wikiHref } from "@/lib/router";
import type { CatalogPage, PageType } from "@/lib/types";
import { cn, readStored, writeStored } from "@/lib/utils";

const OPEN_KEY = "stigmergy.explorer.open";
const DEFAULT_OPEN: Record<string, boolean> = { project: true, entity: true, concept: true, note: true, source: false };

type Group = { key: string; label: string; icon?: React.ReactNode; pages: CatalogPage[] };
const ENTITY_GROUPS: Record<string, string> = {
  person: "People", organization: "Organizations", product: "Products", tool: "Tools",
  repository: "Repositories", place: "Places", other: "Other",
};

function subgroups(type: PageType, pages: CatalogPage[]): Group[] | null {
  if (type === "entity") {
    const byType = new Map<string, CatalogPage[]>();
    for (const page of pages) {
      const key = page.entity_type || "other";
      byType.set(key, [...(byType.get(key) ?? []), page]);
    }
    if (byType.size < 2) return null;
    return [...byType.entries()]
      .sort((a, b) => b[1].length - a[1].length)
      .map(([key, members]) => {
        const Icon = entityIcon(key);
        return { key: `entity:${key}`, label: ENTITY_GROUPS[key] ?? key.charAt(0).toUpperCase() + key.slice(1), icon: <Icon className="size-3.5 text-entity/80" />, pages: members };
      });
  }
  if (type === "source") {
    const byMonth = new Map<string, CatalogPage[]>();
    for (const page of pages) {
      const [, year, month] = page.path.split("/");
      const key = `${year}-${month}`;
      byMonth.set(key, [...(byMonth.get(key) ?? []), page]);
    }
    const format = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
    return [...byMonth.entries()]
      .sort((a, b) => b[0].localeCompare(a[0]))
      .map(([key, members]) => ({ key: `source:${key}`, label: format.format(new Date(`${key}-01T00:00:00`)), pages: members }));
  }
  return null;
}

function Highlight({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const index = text.toLowerCase().indexOf(query.toLowerCase());
  if (index < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, index)}
      <mark className="rounded-[3px] bg-primary/20 px-0.5 text-foreground">{text.slice(index, index + query.length)}</mark>
      {text.slice(index + query.length)}
    </>
  );
}

export function Explorer({ activePath }: { activePath: string | null }) {
  const catalog = useCatalog();
  const [filter, setFilter] = React.useState("");
  const [open, setOpen] = React.useState<Record<string, boolean>>(() => ({ ...DEFAULT_OPEN, ...readStored(OPEN_KEY, {}) }));
  const activeRef = React.useRef<HTMLAnchorElement>(null);
  const query = filter.trim();

  const setGroup = (key: string, value: boolean) => setOpen((current) => {
    const next = { ...current, [key]: value };
    writeStored(OPEN_KEY, next);
    return next;
  });

  const pages = catalog.data?.pages ?? [];
  const matching = query
    ? pages.filter((page) => [page.title, ...page.aliases].some((name) => name.toLowerCase().includes(query.toLowerCase())))
    : pages;
  const byType = new Map<PageType, CatalogPage[]>();
  for (const page of matching) byType.set(page.type, [...(byType.get(page.type) ?? []), page]);
  for (const list of byType.values()) list.sort((a, b) => (a.type === "source" ? b.updated.localeCompare(a.updated) : a.title.localeCompare(b.title)));

  const active = pages.find((page) => page.path === activePath);
  React.useEffect(() => {
    if (!active) return;
    const keys = [active.type];
    const extra = active.type === "entity" ? `entity:${active.entity_type || "other"}` : active.type === "source" ? `source:${active.path.split("/").slice(1, 3).join("-")}` : null;
    setOpen((current) => {
      if (current[active.type] && (!extra || current[extra] !== false)) return current;
      const next = { ...current, [keys[0]]: true, ...(extra ? { [extra]: true } : {}) };
      writeStored(OPEN_KEY, next);
      return next;
    });
  }, [active?.path]);

  React.useEffect(() => {
    activeRef.current?.scrollIntoView({ block: "nearest" });
  }, [activePath, open]);

  const allOpen = TYPE_ORDER.every((type) => open[type]);
  const toggleAll = () => {
    const value = !allOpen;
    setOpen((current) => {
      const next = { ...current };
      for (const type of TYPE_ORDER) next[type] = value;
      writeStored(OPEN_KEY, next);
      return next;
    });
  };

  const row = (page: CatalogPage, indent: boolean) => {
    const isActive = page.path === activePath;
    return (
      <a
        key={page.path}
        ref={isActive ? activeRef : undefined}
        href={wikiHref(page.path)}
        className={cn(
          "group relative flex h-7 items-center gap-2 rounded-md pr-2 text-[13px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
          indent ? "pl-7" : "pl-5",
          isActive && "bg-primary/10 font-medium text-foreground hover:bg-primary/12",
        )}
      >
        {isActive ? <span className="absolute left-1 top-1.5 bottom-1.5 w-[3px] rounded-full bg-primary" /> : null}
        <TypeIcon type={page.type} entityType={page.entity_type} className="size-3.5 opacity-80" />
        <span className="min-w-0 flex-1 truncate"><Highlight text={page.title} query={query} /></span>
        {page.layers.some((layer) => layer !== "public") ? <Lock className="size-3 shrink-0 text-muted-foreground/70" /> : null}
      </a>
    );
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-1.5 px-3 pb-2 pt-4">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="Filter pages"
            className="h-8 w-full rounded-lg border bg-background/70 pl-8 pr-7 text-[12.5px] outline-none transition focus:border-primary/50 focus:ring-4 focus:ring-ring/20"
          />
          {filter ? (
            <button onClick={() => setFilter("")} className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted-foreground hover:text-foreground" aria-label="Clear filter">
              <X className="size-3.5" />
            </button>
          ) : null}
        </div>
        <Tooltip content={allOpen ? "Collapse all" : "Expand all"}>
          <button onClick={toggleAll} className="flex size-8 items-center justify-center rounded-lg text-muted-foreground transition hover:bg-muted hover:text-foreground" aria-label={allOpen ? "Collapse all" : "Expand all"}>
            {allOpen ? <ChevronsDownUp className="size-4" /> : <ChevronsUpDown className="size-4" />}
          </button>
        </Tooltip>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-6">
        {catalog.isLoading ? (
          <div className="grid gap-2 px-2 pt-2">{Array.from({ length: 12 }, (_, i) => <Skeleton key={i} className="h-5" />)}</div>
        ) : null}
        {TYPE_ORDER.map((type) => {
          const list = byType.get(type) ?? [];
          if (!list.length && !(!query && pages.some((page) => page.type === type))) return null;
          const groups = subgroups(type, list);
          const isOpen = query ? list.length > 0 : !!open[type];
          return (
            <Collapsible
              key={type}
              open={isOpen}
              onOpenChange={(value) => setGroup(type, value)}
              className="mb-1"
              triggerClassName="h-8 rounded-md px-1.5 transition-colors hover:bg-muted"
              trigger={
                <span className="flex flex-1 items-center gap-2">
                  <TypeIcon type={type} className="size-3.5" />
                  <span className="text-[12px] font-semibold tracking-tight">{TYPE_META[type].plural}</span>
                  <span className="ml-auto rounded-full bg-muted px-1.5 text-[10.5px] font-medium tabular-nums text-muted-foreground">{list.length}</span>
                </span>
              }
            >
              <div className="grid gap-px py-0.5">
                {groups
                  ? groups.map((group) => (
                    <Collapsible
                      key={group.key}
                      open={query ? true : open[group.key] ?? type !== "source"}
                      onOpenChange={(value) => setGroup(group.key, value)}
                      triggerClassName="h-7 rounded-md pl-4 pr-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                      trigger={
                        <span className="flex flex-1 items-center gap-1.5 text-[12px] font-medium">
                          {group.icon}
                          {group.label}
                          <span className="ml-auto text-[10.5px] tabular-nums text-muted-foreground/80">{group.pages.length}</span>
                        </span>
                      }
                    >
                      <div className="grid gap-px">{group.pages.map((page) => row(page, true))}</div>
                    </Collapsible>
                  ))
                  : list.map((page) => row(page, false))}
              </div>
            </Collapsible>
          );
        })}
        {query && !matching.length ? (
          <p className="px-3 py-8 text-center text-[12px] text-muted-foreground">No page title or alias contains “{query}”.</p>
        ) : null}
      </div>
    </div>
  );
}
