import { ArrowRight, Focus, Globe2, Lock, Minus, Plus, Search, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import * as React from "react";
import { TYPE_META, TYPE_ORDER, TypeBadge, TypeIcon } from "@/components/type";
import { LayerBadges, PUBLIC_LAYER } from "@/components/ui/audience";
import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/ui/overlays";
import { Skeleton } from "@/components/ui/primitives";
import { useCatalog, useGraph } from "@/lib/queries";
import { navigate, pagePathFromRoute, wikiHref } from "@/lib/router";
import type { PageType } from "@/lib/types";
import { cn, onDay } from "@/lib/utils";
import { SigmaGraph, type SigmaGraphHandle } from "./sigma-graph";

export function GraphView({ route }: { route: string }) {
  const graph = useGraph();
  const catalog = useCatalog();
  const handle = React.useRef<SigmaGraphHandle>(null);
  const [hidden, setHidden] = React.useState<Set<PageType>>(new Set());
  const [selected, setSelected] = React.useState<string | null>(() => pagePathFromRoute(`wiki/${route.slice("graph/".length)}`));
  const [query, setQuery] = React.useState("");
  const [hovered, setHovered] = React.useState<{ path: string; x: number; y: number } | null>(null);
  const [layer, setLayer] = React.useState<string | null>(null);
  const layerNames = React.useMemo(() => {
    const names = new Set<string>();
    for (const node of graph.data?.nodes ?? []) for (const name of node.layers ?? []) names.add(name);
    return [...names].sort((a, b) => (a === PUBLIC_LAYER ? -1 : b === PUBLIC_LAYER ? 1 : a.localeCompare(b)));
  }, [graph.data]);
  const shown = React.useMemo(() => {
    if (!graph.data || !layer) return graph.data;
    const nodes = graph.data.nodes.filter((node) => node.layers?.includes(layer));
    const kept = new Set(nodes.map((node) => node.path));
    return { ...graph.data, nodes, edges: graph.data.edges.filter((edge) => kept.has(edge.from) && kept.has(edge.to)) };
  }, [graph.data, layer]);
  const pages = React.useMemo(() => new Map((catalog.data?.pages ?? []).map((page) => [page.path, page])), [catalog.data]);
  const counts = React.useMemo(() => {
    const tally: Record<string, number> = {};
    for (const node of graph.data?.nodes ?? []) tally[node.type] = (tally[node.type] ?? 0) + 1;
    return tally;
  }, [graph.data]);
  const matches = query.trim().length >= 2
    ? (graph.data?.nodes ?? []).filter((node) => node.title.toLowerCase().includes(query.toLowerCase())).slice(0, 6)
    : [];
  const selectedPage = selected ? pages.get(selected) : undefined;
  const degree = React.useMemo(() => {
    const map = new Map<string, number>();
    for (const edge of graph.data?.edges ?? []) {
      map.set(edge.from, (map.get(edge.from) ?? 0) + 1);
      map.set(edge.to, (map.get(edge.to) ?? 0) + 1);
    }
    return map;
  }, [graph.data]);

  React.useEffect(() => {
    if (!selected || !graph.data) return;
    const timer = window.setTimeout(() => handle.current?.focus(selected), 900);
    return () => window.clearTimeout(timer);
  }, [graph.data]); // focus once the layout has settled a little

  const toggle = (type: PageType) => setHidden((current) => {
    const next = new Set(current);
    if (next.has(type)) next.delete(type); else next.add(type);
    return next;
  });

  const choose = (path: string) => {
    setSelected(path);
    setQuery("");
    handle.current?.focus(path);
  };

  return (
    <div className="relative h-dvh overflow-hidden">
      <div className="grain absolute inset-0" />
      {shown ? (
        <SigmaGraph
          ref={handle}
          data={shown}
          mode="global"
          hiddenTypes={hidden}
          highlight={selected}
          onNodeClick={choose}
          onNodeHover={(path, position) => setHovered(path && position ? { path, ...position } : null)}
          className="absolute inset-0"
        />
      ) : <div className="absolute inset-0 grid place-items-center"><Skeleton className="size-72 rounded-full" /></div>}

      <motion.div
        initial={{ opacity: 0, y: -8 }}
        animate={{ opacity: 1, y: 0 }}
        className="absolute left-6 top-6 z-10 w-80 rounded-2xl border bg-popover p-4 shadow-2xl"
      >
        <div className="text-[15px] font-semibold tracking-tight">Knowledge graph</div>
        <div className="text-[12px] text-muted-foreground">
          {graph.data ? `${graph.data.nodes.length.toLocaleString()} pages you can read, ${graph.data.edges.length.toLocaleString()} links` : "Loading the graph…"}
        </div>
        <div className="relative mt-3">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Find a page in the graph"
            className="h-9 w-full rounded-lg border bg-background/70 pl-8 pr-3 text-[13px] outline-none focus:border-primary/50 focus:ring-4 focus:ring-ring/20"
          />
          <AnimatePresence>
            {matches.length ? (
              <motion.div initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="absolute inset-x-0 top-10 z-20 rounded-xl border bg-popover p-1 shadow-xl">
                {matches.map((node) => (
                  <button key={node.path} onClick={() => choose(node.path)} className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] hover:bg-muted">
                    <TypeIcon type={node.type} entityType={node.entity_type} className="size-3.5" />
                    <span className="truncate">{node.title}</span>
                  </button>
                ))}
              </motion.div>
            ) : null}
          </AnimatePresence>
        </div>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {TYPE_ORDER.map((type) => (
            <button
              key={type}
              onClick={() => toggle(type)}
              className={cn(
                "flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11.5px] font-medium transition",
                hidden.has(type) ? "text-muted-foreground opacity-50" : "bg-background",
              )}
            >
              <span className={cn("size-2 rounded-full", TYPE_META[type].bg)} />
              {TYPE_META[type].plural}
              <span className="tabular-nums text-muted-foreground">{counts[type] ?? 0}</span>
            </button>
          ))}
        </div>
        {layerNames.length > 1 ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {[null, ...layerNames].map((name) => (
              <button
                key={name ?? "all"}
                onClick={() => setLayer(name)}
                className={cn(
                  "flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11.5px] font-medium transition",
                  layer === name ? "border-primary/50 bg-primary/10 text-foreground" : "text-muted-foreground hover:bg-muted",
                )}
              >
                {name === null ? "All layers" : name === PUBLIC_LAYER ? <><Globe2 className="size-3" />{name}</> : <><Lock className="size-3" />{name}</>}
              </button>
            ))}
          </div>
        ) : null}
      </motion.div>

      <div className="absolute bottom-6 right-6 z-10 flex flex-col gap-1 rounded-xl border bg-popover p-1 shadow-xl">
        <Tooltip content="Zoom in" side="left"><Button variant="ghost" size="icon-sm" onClick={() => handle.current?.zoomIn()}><Plus /></Button></Tooltip>
        <Tooltip content="Zoom out" side="left"><Button variant="ghost" size="icon-sm" onClick={() => handle.current?.zoomOut()}><Minus /></Button></Tooltip>
        <Tooltip content="Fit everything" side="left"><Button variant="ghost" size="icon-sm" onClick={() => { setSelected(null); handle.current?.reset(); }}><Focus /></Button></Tooltip>
      </div>

      <AnimatePresence>
        {hovered && hovered.path !== selected && pages.get(hovered.path) ? (
          <motion.div
            key={hovered.path}
            initial={{ opacity: 0, scale: 0.96 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="pointer-events-none absolute z-20 w-72 rounded-xl border bg-popover p-3.5 shadow-2xl"
            style={{ left: Math.min(hovered.x + 22, window.innerWidth - 560), top: Math.max(16, hovered.y - 40) }}
          >
            <TypeBadge type={pages.get(hovered.path)!.type} entityType={pages.get(hovered.path)!.entity_type} />
            <div className="mt-2 text-[13.5px] font-semibold leading-snug">{pages.get(hovered.path)!.title}</div>
            <p className="mt-1 line-clamp-3 font-serif text-[13.5px] leading-relaxed text-muted-foreground">{pages.get(hovered.path)!.summary}</p>
          </motion.div>
        ) : null}
      </AnimatePresence>

      <AnimatePresence>
        {selectedPage ? (
          <motion.aside
            key={selectedPage.path}
            initial={{ opacity: 0, x: 40 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 40 }}
            transition={{ type: "spring", bounce: 0.1, duration: 0.5 }}
            className="absolute right-6 top-6 z-10 w-96 rounded-2xl border bg-popover p-5 shadow-2xl"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex flex-wrap items-center gap-1.5">
                <TypeBadge type={selectedPage.type} entityType={selectedPage.entity_type} />
                <LayerBadges layers={selectedPage.layers} />
              </div>
              <button onClick={() => setSelected(null)} className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground" aria-label="Close"><X className="size-4" /></button>
            </div>
            <h2 className="mt-3 font-serif text-[26px] font-medium leading-tight tracking-[-0.015em]">{selectedPage.title}</h2>
            <div className="mt-1 text-[12px] text-muted-foreground">Updated {onDay(selectedPage.updated)} · {degree.get(selectedPage.path) ?? 0} connections</div>
            {selectedPage.summary ? <p className="mt-3 font-serif text-[15px] leading-relaxed text-foreground/85">{selectedPage.summary}</p> : null}
            <Button className="mt-5 w-full" onClick={() => navigate(wikiHref(selectedPage.path))}>Open page<ArrowRight /></Button>
          </motion.aside>
        ) : null}
      </AnimatePresence>
    </div>
  );
}
