import { useQuery } from "@tanstack/react-query";
import { ArrowDownRight, ArrowUpRight, Minus, Orbit, Waypoints } from "lucide-react";
import { animate, motion } from "motion/react";
import * as React from "react";
import { LayerBadges } from "@/components/ui/audience";
import { Segmented, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/controls";
import { Collapsible, EmptyState, Notice } from "@/components/ui/feedback";
import { Badge, Card, Skeleton } from "@/components/ui/primitives";
import { TypeIcon } from "@/components/type";
import { api, enc } from "@/lib/api";
import { navigate, wikiHref } from "@/lib/router";
import type { GraphData, GraphMeasureValues, GraphMeasures, MeasuredPage } from "@/lib/types";
import { cn, onDay } from "@/lib/utils";
import { SigmaGraph } from "../wiki/sigma-graph";
import { errorMessage } from "./common";

type Key = Exclude<keyof GraphMeasureValues, "pages_by_type" | "entities">;

/** `better`: which direction is an improvement, so a change is told good or bad, never by color alone. */
const MEASURES: { key: Key; label: string; better: "up" | "down"; kind: "count" | "ratio" | "share"; hint: string }[] = [
  { key: "pages", label: "Pages", better: "up", kind: "count", hint: "Knowledge pages and sources" },
  { key: "links_per_page", label: "Links per page", better: "up", kind: "ratio", hint: "Distinct link targets per knowledge page" },
  { key: "cited_paragraphs", label: "Cited paragraphs", better: "up", kind: "share", hint: "Paragraphs and lists, outside Entities and Connections, that cite a source" },
  { key: "multi_source_entities", label: "Multi-source entities", better: "up", kind: "count", hint: "Entities citing two or more sources" },
  { key: "orphan_entities", label: "Orphan entities", better: "down", kind: "count", hint: "Entities no other page links" },
  { key: "dead_links", label: "Dead links", better: "down", kind: "count", hint: "Wikilinks that resolve to no page" },
  { key: "entities_without_aliases", label: "Entities without aliases", better: "down", kind: "count", hint: "Entities with no other name recorded" },
  { key: "thin_entities", label: "Thin entities", better: "down", kind: "count", hint: "Entities with at most one fact" },
];

function format(kind: string, value: number): string {
  if (kind === "share") return `${Math.round(value * 1000) / 10}%`;
  if (kind === "ratio") return value.toFixed(2);
  return Math.round(value).toLocaleString();
}

function scopeLabel(scope: string): string {
  return scope === "brain" ? "Whole brain" : scope;
}

function AnimatedValue({ value, kind }: { value: number; kind: string }) {
  const ref = React.useRef<HTMLSpanElement>(null);
  const last = React.useRef(0);
  React.useEffect(() => {
    const controls = animate(last.current, value, {
      duration: 0.9, ease: [0.16, 1, 0.3, 1],
      onUpdate: (latest) => { if (ref.current) ref.current.textContent = format(kind, latest); },
    });
    last.current = value;
    return () => controls.stop();
  }, [value, kind]);
  return <span ref={ref}>{format(kind, 0)}</span>;
}

function Sparkline({ values }: { values: number[] }) {
  const holder = React.useRef<HTMLDivElement>(null);
  const [width, setWidth] = React.useState(0);
  React.useEffect(() => {
    const element = holder.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const height = 32, pad = 5;
  if (values.length < 2 || width < 20) return <div ref={holder} className="h-8 w-full" />;
  const low = Math.min(...values), high = Math.max(...values);
  const span = high - low || 1;
  const x = (i: number) => pad + (i / (values.length - 1)) * (width - pad * 2);
  const y = (v: number) => (high === low ? height / 2 : height - pad - ((v - low) / span) * (height - pad * 2));
  const path = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const end = values.length - 1;
  return (
    <div ref={holder} className="h-8 w-full">
      <svg width={width} height={height} className="block" aria-hidden>
        <motion.path d={path} fill="none" className="stroke-muted-foreground/45" strokeWidth={1.5} strokeLinecap="round"
          strokeLinejoin="round" initial={{ pathLength: 0 }} animate={{ pathLength: 1 }}
          transition={{ duration: 0.9, ease: [0.16, 1, 0.3, 1] }} />
        <motion.circle cx={x(end)} cy={y(values[end])} r={3} className="fill-primary stroke-card" strokeWidth={1.5}
          initial={{ scale: 0 }} animate={{ scale: 1 }} transition={{ delay: 0.75, duration: 0.3 }} />
      </svg>
    </div>
  );
}

function Change({ measure, now, before }: { measure: (typeof MEASURES)[number]; now: number; before?: number }) {
  if (before === undefined) return <span className="text-[11px] text-muted-foreground">No value 7 days ago</span>;
  const delta = now - before;
  if (Math.abs(delta) < 1e-9) {
    return <Badge><Minus />Unchanged in 7 days</Badge>;
  }
  const improved = (delta > 0) === (measure.better === "up");
  const shown = measure.kind === "share" ? `${Math.abs(Math.round(delta * 1000) / 10)} pts`
    : measure.kind === "ratio" ? Math.abs(delta).toFixed(2) : Math.abs(Math.round(delta)).toLocaleString();
  return (
    <Badge tone={improved ? "success" : "warning"}>
      {delta > 0 ? <ArrowUpRight /> : <ArrowDownRight />}
      {delta > 0 ? "+" : "−"}{shown} in 7 days · {improved ? "better" : "worse"}
    </Badge>
  );
}

function Tile({ measure, data, selected, onSelect, index }: {
  measure: (typeof MEASURES)[number]; data: GraphMeasures; selected: boolean; onSelect: () => void; index: number;
}) {
  const now = data.measures[measure.key];
  const before = data.week_ago?.[measure.key];
  const series = data.series.slice(-24).map((point) => point.measures[measure.key]).filter((v) => typeof v === "number");
  return (
    <motion.button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.45, delay: 0.04 * index, ease: [0.16, 1, 0.3, 1] }}
      whileHover={{ y: -2 }}
      className={cn("flex flex-col gap-2 rounded-2xl border bg-card p-4 text-left shadow-elevated transition-colors",
        selected ? "border-primary/60 ring-2 ring-primary/15" : "hover:border-primary/30")}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-[12px] font-medium text-muted-foreground">{measure.label}</span>
      </div>
      <div className="text-[28px] font-semibold leading-none tracking-tight text-foreground">
        <AnimatedValue value={now} kind={measure.kind} />
      </div>
      {measure.key === "pages" ? (
        <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
          {(["note", "concept", "entity", "project", "source"] as const).map((type) => (
            <span key={type} className="inline-flex items-center gap-1"><TypeIcon type={type} className="size-3" />{data.measures.pages_by_type[type]}</span>
          ))}
        </div>
      ) : <div className="text-[11px] text-muted-foreground">{measure.hint}</div>}
      <Sparkline values={series} />
      <div><Change measure={measure} now={now} before={before} /></div>
    </motion.button>
  );
}

function niceMax(value: number): number {
  if (value <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((s) => s * power >= value) ?? 10;
  return step * power;
}

function SeriesChart({ data, measure }: { data: GraphMeasures; measure: (typeof MEASURES)[number] }) {
  const holder = React.useRef<HTMLDivElement>(null);
  const [width, setWidth] = React.useState(640);
  const [hover, setHover] = React.useState<number | null>(null);
  const [table, setTable] = React.useState(false);
  React.useEffect(() => {
    const element = holder.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(280, entry.contentRect.width)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const points = data.series.map((point) => ({
    at: new Date(point.taken_at).getTime(), value: point.measures[measure.key], commit: point.indexed_commit, taken: point.taken_at,
  })).filter((point) => typeof point.value === "number");
  const height = 220, left = 44, right = 16, top = 14, bottom = 28;
  const top_value = measure.kind === "share" ? 1 : niceMax(Math.max(...points.map((p) => p.value), 0) * 1.08);
  const first = points[0]?.at ?? 0, last = points[points.length - 1]?.at ?? 1;
  const x = (at: number) => left + (points.length < 2 ? (width - left - right) / 2 : ((at - first) / (last - first || 1)) * (width - left - right));
  const y = (v: number) => top + (1 - v / top_value) * (height - top - bottom);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * top_value);
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(p.at).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const area = points.length > 1 ? `${line} L${x(last).toFixed(1)},${y(0)} L${x(first).toFixed(1)},${y(0)} Z` : "";
  const active = hover !== null ? points[hover] : null;

  function onMove(event: React.PointerEvent<SVGSVGElement>) {
    if (!points.length) return;
    const box = event.currentTarget.getBoundingClientRect();
    const px = event.clientX - box.left;
    let best = 0;
    points.forEach((p, i) => { if (Math.abs(x(p.at) - px) < Math.abs(x(points[best].at) - px)) best = i; });
    setHover(best);
  }

  return (
    <div ref={holder} className="relative">
      {points.length < 2 ? (
        <p className="mb-2 text-[12px] text-muted-foreground">The series starts here; every indexed commit adds a point.</p>
      ) : null}
      <svg width={width} height={height} className="block touch-none select-none" onPointerMove={onMove} onPointerLeave={() => setHover(null)}
        role="img" aria-label={`${measure.label} over time`}>
        {ticks.map((tick) => (
          <g key={tick}>
            <line x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} className="stroke-border" strokeWidth={1} />
            <text x={left - 8} y={y(tick)} dy="0.32em" textAnchor="end" className="fill-muted-foreground text-[10.5px] tabular-nums">{format(measure.kind, tick)}</text>
          </g>
        ))}
        {points.length > 1 ? (
          <>
            <text x={left} y={height - 8} className="fill-muted-foreground text-[10.5px]">{onDay(points[0].taken)}</text>
            <text x={width - right} y={height - 8} textAnchor="end" className="fill-muted-foreground text-[10.5px]">{onDay(points[points.length - 1].taken)}</text>
          </>
        ) : null}
        {area ? <motion.path key={`${measure.key}-area`} d={area} className="fill-primary/10" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.6 }} /> : null}
        {points.length > 1 ? (
          <motion.path key={measure.key} d={line} fill="none" className="stroke-primary" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round"
            initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.9, ease: [0.16, 1, 0.3, 1] }} />
        ) : null}
        {points.length ? (
          <circle cx={x(points[points.length - 1].at)} cy={y(points[points.length - 1].value)} r={4} className="fill-primary stroke-card" strokeWidth={2} />
        ) : null}
        {active ? (
          <g pointerEvents="none">
            <line x1={x(active.at)} x2={x(active.at)} y1={top} y2={height - bottom} className="stroke-muted-foreground/50" strokeWidth={1} />
            <circle cx={x(active.at)} cy={y(active.value)} r={4.5} className="fill-primary stroke-card" strokeWidth={2} />
          </g>
        ) : null}
      </svg>
      {active ? (
        <div className="pointer-events-none absolute z-10 rounded-xl border bg-popover px-3 py-2 shadow-elevated"
          style={{ left: Math.min(Math.max(0, x(active.at) - 80), width - 170), top: Math.max(0, y(active.value) - 76) }}>
          <div className="text-[15px] font-semibold text-foreground">{format(measure.kind, active.value)}</div>
          <div className="text-[11px] text-muted-foreground">{measure.label} · {onDay(active.taken)}</div>
          <div className="mt-0.5 font-mono text-[10.5px] text-muted-foreground">{active.commit ? active.commit.slice(0, 7) : "uncommitted rebuild"}</div>
        </div>
      ) : null}
      <Collapsible open={table} onOpenChange={setTable} className="mt-2"
        triggerClassName="text-[11.5px] font-medium text-muted-foreground"
        trigger={<span>{table ? "Hide" : "Show"} the series as a table</span>}>
        <div className="mt-2 max-h-56 overflow-auto rounded-xl border">
          <table className="w-full text-[12px]">
            <thead className="sticky top-0 bg-muted/80 text-muted-foreground"><tr><th className="px-3 py-1.5 text-left font-medium">Snapshot</th><th className="px-3 py-1.5 text-left font-medium">Commit</th><th className="px-3 py-1.5 text-right font-medium">{measure.label}</th></tr></thead>
            <tbody>
              {[...points].reverse().map((p) => (
                <tr key={`${p.taken}-${p.commit}`} className="border-t"><td className="px-3 py-1.5">{onDay(p.taken)}</td><td className="px-3 py-1.5 font-mono text-[11px]">{p.commit ? p.commit.slice(0, 7) : "—"}</td><td className="px-3 py-1.5 text-right tabular-nums">{format(measure.kind, p.value)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </Collapsible>
    </div>
  );
}

function Constellation({ data }: { data: GraphMeasures }) {
  const [hovered, setHovered] = React.useState<{ path: string; x: number; y: number } | null>(null);
  const nodes = React.useMemo(() => new Map(data.constellation.nodes.map((node) => [node.path, node])), [data]);
  const graph: GraphData = React.useMemo(() => ({
    nodes: data.constellation.nodes.map((node) => ({ path: node.path, title: node.title, type: node.type, weight: node.inbound })),
    edges: data.constellation.edges,
  }), [data]);
  if (!graph.nodes.length) {
    return <EmptyState icon={Orbit} title="No links yet">The constellation appears once pages link each other.</EmptyState>;
  }
  const node = hovered ? nodes.get(hovered.path) : undefined;
  return (
    <div className="relative h-[380px] overflow-hidden rounded-xl border bg-background/40">
      <SigmaGraph key={data.scope} data={graph} mode="global" className="absolute inset-0"
        onNodeClick={(path) => navigate(wikiHref(path))}
        onNodeHover={(path, position) => setHovered(path && position ? { path, ...position } : null)} />
      {node && hovered ? (
        <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
          className="pointer-events-none absolute z-10 max-w-64 rounded-xl border bg-popover px-3 py-2 shadow-elevated"
          style={{ left: Math.max(8, hovered.x + 16), top: Math.max(8, hovered.y - 30) }}>
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground"><TypeIcon type={node.type} className="size-3" />{node.type}</div>
          <div className="mt-1 text-[13px] font-semibold leading-snug">{node.title}</div>
          <div className="mt-0.5 text-[11.5px] text-muted-foreground">{node.orphan ? "No page links it" : `${node.inbound} page${node.inbound === 1 ? "" : "s"} link it`}</div>
        </motion.div>
      ) : null}
      <div className="pointer-events-none absolute bottom-2 left-3 text-[11px] text-muted-foreground">Hubs and their neighbours; orphan entities drift at the rim. Size is inbound links.</div>
    </div>
  );
}

function PageTable({ pages, empty, extra }: { pages: MeasuredPage[]; empty: string; extra?: (page: MeasuredPage) => React.ReactNode }) {
  if (!pages.length) return <p className="py-6 text-center text-[12.5px] text-muted-foreground">{empty}</p>;
  return (
    <div className="max-h-[332px] divide-y overflow-auto rounded-xl border">
      {pages.map((page) => (
        <a key={page.path} href={wikiHref(page.path)} className="flex items-center gap-3 px-3 py-2 text-[13px] transition-colors hover:bg-muted/60">
          <TypeIcon type={page.type} entityType={page.entity_type} className="size-4 shrink-0 text-muted-foreground" />
          <span className="min-w-0 flex-1 truncate">{page.title}</span>
          <LayerBadges layers={page.layers} />
          <span className="w-24 shrink-0 text-right text-[11.5px] tabular-nums text-muted-foreground">{extra ? extra(page) : `${page.inbound} inbound`}</span>
        </a>
      ))}
    </div>
  );
}

export function GraphMeasuresPanel() {
  const [scope, setScope] = React.useState("brain");
  const [selected, setSelected] = React.useState<Key>("links_per_page");
  const query = useQuery({
    queryKey: ["admin", "graph-measures", scope],
    queryFn: () => api.get<GraphMeasures>(`graph-measures?scope=${enc(scope)}`),
    placeholderData: (previous) => previous,
  });
  const data = query.data;
  const measure = MEASURES.find((item) => item.key === selected) ?? MEASURES[1];
  return (
    <section className="mt-4 grid gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-[15px] font-semibold"><Waypoints className="size-4 text-primary" />Graph measures</h2>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">How the wiki is connected, cited, and named, with a snapshot at every indexed commit. These describe the graph; none of them is a task.</p>
        </div>
        {data ? (
          <Segmented layoutId="graph-measures-scope" value={scope} onChange={setScope}
            options={data.scopes.map((name) => ({ value: name, label: scopeLabel(name) }))} />
        ) : null}
      </div>
      {query.isLoading ? <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{Array.from({ length: 8 }, (_, i) => <Skeleton key={i} className="h-40 rounded-2xl" />)}</div> : null}
      {query.isError ? <Notice tone="danger">{errorMessage(query.error)}</Notice> : null}
      {data ? (
        <div className={cn("grid gap-4 transition-opacity", query.isFetching && "opacity-70")}>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {MEASURES.map((item, index) => (
              <Tile key={`${data.scope}-${item.key}`} measure={item} data={data} index={index}
                selected={item.key === selected} onSelect={() => setSelected(item.key)} />
            ))}
          </div>
          <Card className="p-5">
            <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="text-[13.5px] font-semibold">{measure.label} over time</h3>
              <span className="text-[11.5px] text-muted-foreground">{scopeLabel(data.scope)} · {data.series.length} snapshot{data.series.length === 1 ? "" : "s"} · choose a tile to chart it</span>
            </div>
            <SeriesChart data={data} measure={measure} />
          </Card>
          <div className="grid gap-4 xl:grid-cols-[1.25fr_1fr]">
            <Card className="p-5">
              <h3 className="mb-3 text-[13.5px] font-semibold">Constellation</h3>
              <Constellation data={data} />
            </Card>
            <Card className="p-5">
              <Tabs defaultValue="hubs">
                <TabsList>
                  <TabsTrigger value="hubs">Hubs</TabsTrigger>
                  <TabsTrigger value="orphans">Orphans <span className="ml-1 text-muted-foreground">{data.measures.orphan_entities}</span></TabsTrigger>
                  <TabsTrigger value="thin">Thin <span className="ml-1 text-muted-foreground">{data.measures.thin_entities}</span></TabsTrigger>
                  <TabsTrigger value="aliases">No aliases <span className="ml-1 text-muted-foreground">{data.measures.entities_without_aliases}</span></TabsTrigger>
                </TabsList>
                <TabsContent value="hubs"><PageTable pages={data.hubs} empty="No page is linked yet." /></TabsContent>
                <TabsContent value="orphans"><PageTable pages={data.lists.orphan_entities} empty="Every entity is linked from another page." /></TabsContent>
                <TabsContent value="thin"><PageTable pages={data.lists.thin_entities} empty="Every entity records more than one fact." extra={(page) => `${page.facts ?? 0} fact${page.facts === 1 ? "" : "s"}`} /></TabsContent>
                <TabsContent value="aliases"><PageTable pages={data.lists.entities_without_aliases} empty="Every entity records another name." /></TabsContent>
              </Tabs>
            </Card>
          </div>
        </div>
      ) : null}
    </section>
  );
}
