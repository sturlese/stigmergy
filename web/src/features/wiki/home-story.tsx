import { animate, motion, useInView, useReducedMotion, useScroll, useSpring, useTransform } from "motion/react";
import type { MotionValue } from "motion/react";
import {
  ArrowRight, Bot, Eraser, FileUp, GitCommitHorizontal, Globe, Layers, Link2, MessagesSquare, Network,
  Quote, ScrollText, Search, ShieldCheck, Sparkles, Waypoints,
} from "lucide-react";
import * as React from "react";
import { TypeIcon } from "@/components/type";
import { Button } from "@/components/ui/button";
import { navigate, wikiHref } from "@/lib/router";
import type { CatalogPage, GraphData, Me } from "@/lib/types";
import { cn } from "@/lib/utils";

const EASE = [0.16, 1, 0.3, 1] as const;
const WEEKS = 12;
const reveal = {
  initial: { opacity: 0, y: 28 },
  whileInView: { opacity: 1, y: 0 },
  viewport: { once: true, margin: "-12% 0px" },
  transition: { duration: 0.8, ease: EASE },
};

/** The story below the home's first screen: what Stigmergy does, told with this brain's own numbers. */
export function HomeStory({ me, pages, graph, onSearch }: {
  me: Me; pages: CatalogPage[]; graph: GraphData | undefined; onSearch: () => void;
}) {
  const stats = React.useMemo(() => brainStats(pages, graph), [pages, graph]);
  return (
    <div className="mt-28 grid gap-36">
      <HowItWorks stats={stats} />
      <InNumbers stats={stats} />
      <LayersStory me={me} layers={stats.layers} />
      <Trust />
      <Closing onSearch={onSearch} />
    </div>
  );
}

type Stats = ReturnType<typeof brainStats>;

export function brainStats(pages: CatalogPage[], graph: GraphData | undefined) {
  const count = (type: string) => pages.filter((page) => page.type === type).length;
  const inbound = new Map<string, number>();
  for (const edge of graph?.edges ?? []) inbound.set(edge.to, (inbound.get(edge.to) ?? 0) + 1);
  const byPath = new Map(pages.map((page) => [page.path, page]));
  const hubs = [...inbound.entries()]
    .map(([path, links]) => ({ page: byPath.get(path), links }))
    .filter((hub): hub is { page: CatalogPage; links: number } => !!hub.page && hub.page.type !== "source")
    .sort((a, b) => b.links - a.links)
    .slice(0, 6);
  const now = Date.now();
  const week = 7 * 24 * 3600 * 1000;
  const activity = Array.from({ length: WEEKS }, () => 0);
  for (const page of pages) {
    const age = Math.floor((now - new Date(page.updated).getTime()) / week);
    if (age >= 0 && age < WEEKS) activity[WEEKS - 1 - age] += 1;
  }
  const knowledge = pages.length - count("source");
  return {
    pages: pages.length,
    sources: count("source"),
    entities: count("entity"),
    written: count("note") + count("concept") + count("project"),
    links: graph?.edges.length ?? 0,
    perPage: knowledge ? (graph?.edges.length ?? 0) / knowledge : 0,
    hubs,
    activity,
    layers: layerSizes(pages),
  };
}

function layerSizes(pages: CatalogPage[]) {
  const sizes = new Map<string, number>([["public", 0]]);
  for (const page of pages) {
    for (const layer of page.layers?.length ? page.layers : ["public"]) sizes.set(layer, (sizes.get(layer) ?? 0) + 1);
  }
  return [...sizes.entries()].map(([name, count]) => ({ name, pages: count }))
    .sort((a, b) => (a.name === "public" ? -1 : b.name === "public" ? 1 : b.pages - a.pages));
}

function useScrollContainer(anchor: React.RefObject<HTMLElement | null>) {
  const container = React.useRef<HTMLElement | null>(null);
  React.useLayoutEffect(() => {
    container.current = anchor.current?.closest("main") ?? null;
  }, [anchor]);
  return container;
}

function CountUp({ value, decimals = 0 }: { value: number; decimals?: number }) {
  const ref = React.useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true });
  React.useEffect(() => {
    if (!inView || !ref.current) return;
    const controls = animate(0, value, {
      duration: 1.4, ease: EASE,
      onUpdate: (latest) => { if (ref.current) ref.current.textContent = latest.toLocaleString(undefined, { maximumFractionDigits: decimals, minimumFractionDigits: decimals }); },
    });
    return () => controls.stop();
  }, [inView, value, decimals]);
  return <span ref={ref}>0</span>;
}

function SectionHead({ eyebrow, icon: Icon, children, lead }: {
  eyebrow: string; icon: typeof Sparkles; children: React.ReactNode; lead?: string;
}) {
  return (
    <motion.div {...reveal} className="max-w-3xl">
      <div className="mb-3 flex items-center gap-2 text-[12px] font-medium uppercase tracking-[0.14em] text-primary">
        <Icon className="size-3.5" />{eyebrow}
      </div>
      <h2 className="font-serif text-[40px] font-medium leading-[1.06] tracking-[-0.02em] text-balance">{children}</h2>
      {lead ? <p className="mt-4 max-w-2xl text-[15.5px] leading-relaxed text-muted-foreground text-pretty">{lead}</p> : null}
    </motion.div>
  );
}

const STEPS = [
  { icon: FileUp, title: "Capture", text: "A Slack thread, a meeting transcript, a PDF, a link: from Slack, any MCP client, or here.",
    stat: (s: Stats) => `${s.sources.toLocaleString()} sources kept, original bytes and all` },
  { icon: ScrollText, title: "File", text: "The librarian reads it once and writes it in: the source, notes, and a page for every name it meets, in one commit.",
    stat: (s: Stats) => `${s.written.toLocaleString()} notes and concepts, ${s.entities.toLocaleString()} entities` },
  { icon: Link2, title: "Link", text: "Pages connect through links, facts point back to their sources, and contradictions stay in sight.",
    stat: (s: Stats) => `${s.links.toLocaleString()} links, ${s.perPage.toFixed(1)} per page` },
  { icon: MessagesSquare, title: "Ask", text: "Search here, or ask in Slack and MCP. Answers cite their pages, and say so when the wiki does not know.",
    stat: () => "Every answer carries its citations" },
];

function HowItWorks({ stats }: { stats: Stats }) {
  const ref = React.useRef<HTMLDivElement>(null);
  const container = useScrollContainer(ref);
  const reduced = useReducedMotion();
  const { scrollYProgress } = useScroll({ target: ref, container, offset: ["start 75%", "end 55%"] });
  const fill = useSpring(scrollYProgress, { stiffness: 90, damping: 24, mass: 0.4 });
  return (
    <section>
      <SectionHead eyebrow="How it works" icon={Waypoints}
        lead="Nobody files anything by hand. Whatever reaches Stigmergy is read, written into the wiki, linked, and kept answerable.">
        From a passing remark to <span className="italic text-primary">shared memory.</span>
      </SectionHead>
      <div ref={ref} className="relative mt-14 grid gap-5 pl-14">
        <div className="absolute bottom-6 left-[22px] top-6 w-px bg-border" />
        <motion.div style={{ scaleY: reduced ? 1 : fill }}
          className="absolute bottom-6 left-[22px] top-6 w-px origin-top bg-gradient-to-b from-primary via-primary to-primary/30" />
        {STEPS.map((step, index) => (
          <motion.div key={step.title} {...reveal} transition={{ ...reveal.transition, delay: 0.05 * index }}
            className="relative">
            <div className="absolute -left-14 top-5 flex size-11 items-center justify-center rounded-2xl border bg-card text-primary shadow-elevated">
              <step.icon className="size-5" />
            </div>
            <div className="group rounded-2xl border bg-card p-6 shadow-elevated transition hover:border-primary/30">
              <div className="flex items-baseline gap-3">
                <span className="font-mono text-[11px] text-muted-foreground">0{index + 1}</span>
                <h3 className="text-[19px] font-semibold tracking-tight">{step.title}</h3>
              </div>
              <p className="mt-2 max-w-2xl text-[14.5px] leading-relaxed text-muted-foreground">{step.text}</p>
              <div className="mt-4 inline-flex items-center gap-2 rounded-full bg-primary/8 px-3 py-1 text-[12px] font-medium text-primary">
                <span className="size-1.5 rounded-full bg-primary" />{step.stat(stats)}
              </div>
            </div>
          </motion.div>
        ))}
      </div>
    </section>
  );
}

function InNumbers({ stats }: { stats: Stats }) {
  const peak = Math.max(1, ...stats.activity);
  const tiles = [
    { label: "pages", value: stats.pages },
    { label: "links between them", value: stats.links },
    { label: "people, companies and projects", value: stats.entities },
    { label: "sources behind it all", value: stats.sources },
  ];
  return (
    <section>
      <SectionHead eyebrow="Your brain, today" icon={Network}
        lead="Live from the index you can read: what it holds, where it is densest, and how fast it grows.">
        A graph that grows <span className="italic text-primary">by itself.</span>
      </SectionHead>
      <div className="mt-12 grid grid-cols-2 gap-px overflow-hidden rounded-3xl border bg-border lg:grid-cols-4">
        {tiles.map((tile, index) => (
          <motion.div key={tile.label} {...reveal} transition={{ ...reveal.transition, delay: 0.06 * index }}
            className="bg-card p-7">
            <div className="text-[44px] font-semibold leading-none tracking-[-0.03em] tabular-nums"><CountUp value={tile.value} /></div>
            <div className="mt-3 text-[13px] text-muted-foreground">{tile.label}</div>
          </motion.div>
        ))}
      </div>
      <div className="mt-5 grid gap-5 lg:grid-cols-[1.15fr_1fr]">
        <motion.div {...reveal} className="rounded-3xl border bg-card p-7 shadow-elevated">
          <div className="text-[13px] font-semibold">Most connected</div>
          <div className="mt-1 text-[12.5px] text-muted-foreground">The pages the rest of the wiki points to most.</div>
          <div className="mt-5 grid gap-1.5">
            {stats.hubs.map((hub, index) => (
              <motion.a key={hub.page.path} href={wikiHref(hub.page.path)}
                initial={{ opacity: 0, x: -10 }} whileInView={{ opacity: 1, x: 0 }} viewport={{ once: true }}
                transition={{ delay: 0.08 * index, duration: 0.5, ease: EASE }}
                className="group flex items-center gap-3 rounded-xl px-2 py-2 transition hover:bg-muted">
                <TypeIcon type={hub.page.type} entityType={hub.page.entity_type} className="size-4" />
                <span className="min-w-0 flex-1 truncate text-[14px] font-medium">{hub.page.title}</span>
                <span className="relative h-1.5 w-28 overflow-hidden rounded-full bg-muted">
                  <motion.span className="absolute inset-y-0 left-0 rounded-full bg-primary"
                    initial={{ width: 0 }} whileInView={{ width: `${(hub.links / stats.hubs[0].links) * 100}%` }}
                    viewport={{ once: true }} transition={{ delay: 0.2 + 0.08 * index, duration: 0.9, ease: EASE }} />
                </span>
                <span className="w-8 text-right text-[12px] tabular-nums text-muted-foreground">{hub.links}</span>
              </motion.a>
            ))}
            {!stats.hubs.length ? <div className="py-6 text-[13px] text-muted-foreground">Links appear as pages are filed.</div> : null}
          </div>
        </motion.div>
        <motion.div {...reveal} className="flex flex-col rounded-3xl border bg-card p-7 shadow-elevated">
          <div className="text-[13px] font-semibold">Pages written or revised</div>
          <div className="mt-1 text-[12.5px] text-muted-foreground">Week by week, the last {WEEKS} weeks.</div>
          <div className="mt-6 flex flex-1 items-end gap-1.5" role="img"
            aria-label={`Pages updated per week: ${stats.activity.join(", ")}`}>
            {stats.activity.map((value, index) => (
              <div key={index} className="group relative flex h-40 flex-1 items-end">
                <motion.div className={cn("w-full rounded-t-md", index === WEEKS - 1 ? "bg-primary" : "bg-primary/35 group-hover:bg-primary/60")}
                  initial={{ height: 0 }} whileInView={{ height: `${Math.max(3, (value / peak) * 100)}%` }}
                  viewport={{ once: true }} transition={{ delay: 0.04 * index, duration: 0.8, ease: EASE }} />
                <span className="pointer-events-none absolute -top-6 left-1/2 -translate-x-1/2 rounded bg-foreground px-1.5 py-0.5 text-[10.5px] font-medium text-background opacity-0 transition group-hover:opacity-100">{value}</span>
              </div>
            ))}
          </div>
          <div className="mt-2 flex justify-between text-[11px] text-muted-foreground"><span>{WEEKS} weeks ago</span><span>this week</span></div>
        </motion.div>
      </div>
    </section>
  );
}

function LayersStory({ me, layers }: { me: Me; layers: { name: string; pages: number }[] }) {
  return (
    <section className="grid items-center gap-12 lg:grid-cols-[1fr_1.1fr]">
      <div>
        <SectionHead eyebrow="Layers" icon={Layers}
          lead="Every capture files in exactly one layer, and everything derived from it stays there. You read public and your own layers, fused into one wiki; nothing from a layer ever reaches a wider one.">
          One brain, <span className="italic text-primary">many layers.</span>
        </SectionHead>
        <motion.div {...reveal} className="mt-6 flex flex-wrap items-center gap-2">
          <span className="text-[12px] text-muted-foreground">You read</span>
          {(me.unrestricted ? ["every layer"] : me.layers).map((layer) => (
            <span key={layer} className="rounded-full border bg-card px-3 py-1 font-mono text-[12px]">{layer}</span>
          ))}
        </motion.div>
      </div>
      <LayerPlanes layers={layers} />
    </section>
  );
}

/** Layers as planes that separate as they scroll into view in the console's scrolling `main`. */
function LayerPlanes({ layers }: { layers: { name: string; pages: number }[] }) {
  const ref = React.useRef<HTMLDivElement>(null);
  const container = useScrollContainer(ref);
  const reduced = useReducedMotion();
  const { scrollYProgress } = useScroll({ target: ref, container, offset: ["start end", "center 45%"] });
  const spread = useSpring(useTransform(scrollYProgress, [0, 1], [12, 92]), { stiffness: 80, damping: 22 });
  const stack = layers.slice(0, 4);
  return (
    <div ref={ref} className="relative h-[21rem] [perspective:1600px]">
      {stack.map((layer, index) => (
        <Plane key={layer.name} layer={layer} index={index} count={stack.length} spread={spread} still={!!reduced} />
      ))}
    </div>
  );
}

function Plane({ layer, index, count, spread, still }: {
  layer: { name: string; pages: number }; index: number; count: number; spread: MotionValue<number>; still: boolean;
}) {
  const offset = (count - 1) / 2 - index;
  const y = useTransform(spread, (value) => offset * (still ? 72 : value));
  const isPublic = layer.name === "public";
  const dots = React.useMemo(() => scatter(layer.name, Math.min(28, Math.max(4, layer.pages))), [layer.name, layer.pages]);
  return (
    <motion.div style={{ y, zIndex: count - index }} className="absolute inset-x-0 top-1/2 -mt-20 h-40">
      <div className="absolute inset-y-0 left-6 right-40 [transform-style:preserve-3d]">
        <div className={cn(
          "relative h-full overflow-hidden rounded-[26px] border shadow-elevated [transform:rotateX(60deg)_rotateZ(-34deg)]",
          isPublic ? "bg-card" : "border-primary/30 bg-[color-mix(in_oklch,var(--primary)_9%,var(--card))]",
        )}>
          {dots.map((dot, i) => (
            <span key={i} className={cn("absolute size-2 rounded-full", isPublic ? "bg-muted-foreground/35" : "bg-primary/60")}
              style={{ left: `${dot.x}%`, top: `${dot.y}%` }} />
          ))}
        </div>
      </div>
      <div className="absolute right-0 top-1/2 flex -translate-y-1/2 items-center gap-2">
        <span className="h-px w-8 bg-border" />
        <span className={cn("rounded-full px-2.5 py-1 font-mono text-[12px]", isPublic ? "border bg-card" : "bg-primary/12 text-primary")}>{layer.name}</span>
        <span className="text-[11.5px] tabular-nums text-muted-foreground">{layer.pages}</span>
      </div>
    </motion.div>
  );
}

/** Stable pseudo-random positions, so a layer's dots do not move between renders. */
function scatter(seed: string, count: number) {
  let state = [...seed].reduce((hash, char) => (hash * 31 + char.charCodeAt(0)) >>> 0, 7);
  const next = () => ((state = (state * 1664525 + 1013904223) >>> 0) / 4294967296);
  return Array.from({ length: count }, () => ({ x: 8 + next() * 84, y: 12 + next() * 76 }));
}

const PROMISES = [
  { icon: GitCommitHorizontal, title: "Git is the memory", text: "Every change is one commit with its exact patch; nothing is lost or rewritten in silence." },
  { icon: Quote, title: "Sources, always", text: "Original files are kept as they arrived, and pages cite the sources they were written from." },
  { icon: ShieldCheck, title: "Honest answers", text: "Answers quote the wiki and say so when it does not know, instead of guessing." },
  { icon: Globe, title: "Runs in Europe", text: "The app, the database, and the original files are in the EU; the wiki's history lives on GitHub." },
  { icon: Eraser, title: "Erasable", text: "Administrators erase a whole layer, history included, and get a receipt of what went." },
  { icon: Bot, title: "Models keep nothing", text: "Every model call goes through zero-retention providers only." },
];

function Trust() {
  return (
    <section>
      <SectionHead eyebrow="Built to be trusted" icon={ShieldCheck}
        lead="A team memory is only useful if people dare to put things in it.">
        Kept carefully, <span className="italic text-primary">answered honestly.</span>
      </SectionHead>
      <PromiseGrid />
    </section>
  );
}

function PromiseGrid() {
  return (
    <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {PROMISES.map((promise, index) => (
        <motion.div key={promise.title}
          initial={{ opacity: 0, y: 24, scale: 0.98 }} whileInView={{ opacity: 1, y: 0, scale: 1 }}
          viewport={{ once: true, margin: "-8% 0px" }} transition={{ delay: 0.06 * index, duration: 0.7, ease: EASE }}
          whileHover={{ y: -4 }}
          className="group relative overflow-hidden rounded-3xl border bg-card p-6 shadow-elevated">
          <div className="pointer-events-none absolute -right-10 -top-10 size-32 rounded-full bg-primary/10 opacity-0 blur-2xl transition duration-500 group-hover:opacity-100" />
          <div className="flex size-10 items-center justify-center rounded-xl bg-primary/10 text-primary"><promise.icon className="size-5" /></div>
          <h3 className="mt-5 text-[16px] font-semibold tracking-tight">{promise.title}</h3>
          <p className="mt-1.5 text-[13.5px] leading-relaxed text-muted-foreground">{promise.text}</p>
        </motion.div>
      ))}
    </div>
  );
}

function Closing({ onSearch }: { onSearch: () => void }) {
  return (
    <motion.section {...reveal}
      className="relative overflow-hidden rounded-[36px] border bg-card px-8 py-20 text-center shadow-elevated">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(60%_80%_at_50%_0%,var(--glow),transparent)]" />
      <div className="relative">
        <div className="mx-auto mb-4 flex size-12 items-center justify-center rounded-2xl bg-primary/10 text-primary"><Sparkles className="size-5" /></div>
        <h2 className="font-serif text-[44px] font-medium leading-[1.05] tracking-[-0.02em]">Ask it something.</h2>
        <p className="mx-auto mt-3 max-w-xl text-[15px] text-muted-foreground">The more your team captures, the more it can answer.</p>
        <div className="mt-8 flex flex-wrap justify-center gap-3">
          <Button size="lg" onClick={onSearch}><Search />Search the wiki</Button>
          <Button size="lg" variant="secondary" onClick={() => navigate("graph")}><Network />Explore the graph<ArrowRight /></Button>
        </div>
      </div>
    </motion.section>
  );
}
