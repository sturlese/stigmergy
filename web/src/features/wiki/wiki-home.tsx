import { animate, motion, useInView } from "motion/react";
import { ArrowRight, Clock, Network, Search, Sparkles } from "lucide-react";
import * as React from "react";
import { TYPE_META, TYPE_ORDER, TypeIcon } from "@/components/type";
import { Button } from "@/components/ui/button";
import { Card, Dot, Kbd, Skeleton } from "@/components/ui/primitives";
import { useCatalog, useGraph, useWikiStatus } from "@/lib/queries";
import { navigate, wikiHref } from "@/lib/router";
import type { Me } from "@/lib/types";
import { onDay } from "@/lib/utils";
import { HomeStory } from "./home-story";
import { SigmaGraph } from "./sigma-graph";

function greeting() {
  const hour = new Date().getHours();
  return hour < 6 ? "Working late" : hour < 12 ? "Good morning" : hour < 20 ? "Good afternoon" : "Good evening";
}

function CountUp({ value }: { value: number }) {
  const ref = React.useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true });
  React.useEffect(() => {
    if (!inView || !ref.current) return;
    const controls = animate(0, value, {
      duration: 1.1, ease: [0.16, 1, 0.3, 1],
      onUpdate: (latest) => { if (ref.current) ref.current.textContent = Math.round(latest).toLocaleString(); },
    });
    return () => controls.stop();
  }, [inView, value]);
  return <span ref={ref}>0</span>;
}

export function WikiHome({ me, onSearch }: { me: Me; onSearch: () => void }) {
  const catalog = useCatalog();
  const graph = useGraph();
  const status = useWikiStatus();
  const pages = catalog.data?.pages ?? [];
  const counts = Object.fromEntries(TYPE_ORDER.map((type) => [type, pages.filter((page) => page.type === type).length]));
  const recent = [...pages].filter((page) => page.type !== "source").sort((a, b) => b.updated.localeCompare(a.updated)).slice(0, 7);
  const first = me.display_name.split(" ")[0];

  return (
    <div className="mx-auto max-w-6xl px-8 pb-24 pt-12">
      <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}>
        <div className="mb-2 flex items-center gap-2 text-[12px] font-medium text-primary">
          <Sparkles className="size-3.5" />{greeting()}, {first}
        </div>
        <h1 className="max-w-3xl font-serif text-[46px] font-medium leading-[1.04] tracking-[-0.025em] text-balance">
          Everything your team knows, <span className="italic text-primary">linked.</span>
        </h1>
        <button
          onClick={onSearch}
          className="group mt-8 flex h-14 w-full max-w-2xl items-center gap-3 rounded-2xl border bg-card px-5 text-left shadow-elevated transition hover:border-primary/40 hover:shadow-[0_0_0_6px_var(--glow)]"
        >
          <Search className="size-5 text-muted-foreground transition group-hover:text-primary" />
          <span className="flex-1 text-[15px] text-muted-foreground">Search people, projects, decisions…</span>
          <Kbd className="h-6 px-1.5 text-[11px]">⌘K</Kbd>
        </button>
      </motion.div>

      <div className="mt-10 grid grid-cols-2 gap-3 lg:grid-cols-5">
        {TYPE_ORDER.map((type, index) => (
          <motion.a
            key={type}
            href={`#/wiki`}
            onClick={(event) => { event.preventDefault(); onSearch(); }}
            initial={{ opacity: 0, y: 14 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.08 * index + 0.15, ease: [0.16, 1, 0.3, 1] }}
            whileHover={{ y: -3 }}
            className="group relative overflow-hidden rounded-2xl border bg-card p-5 shadow-elevated"
          >
            <TypeIcon type={type} className="size-5" />
            <div className="mt-4 text-[30px] font-semibold tabular-nums tracking-tight">
              {catalog.isLoading ? <Skeleton className="h-8 w-14" /> : <CountUp value={counts[type] ?? 0} />}
            </div>
            <div className="text-[12px] text-muted-foreground">{TYPE_META[type].plural}</div>
          </motion.a>
        ))}
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-[1.1fr_1fr]">
        <motion.div initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6, delay: 0.35 }}>
          <Card className="h-full">
            <div className="flex items-center justify-between px-5 pb-2 pt-5">
              <div className="flex items-center gap-2 text-[13px] font-semibold"><Clock className="size-4 text-muted-foreground" />Recently updated</div>
              {status.data ? (
                <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                  <Dot tone={status.data.dirty ? "warning" : "success"} pulse={!status.data.dirty} />
                  {status.data.dirty ? "Index catching up" : "Index up to date"}
                </span>
              ) : null}
            </div>
            <div className="grid px-2 pb-3">
              {catalog.isLoading ? Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="m-3 h-10" />) : null}
              {recent.map((page, index) => (
                <motion.a
                  key={page.path}
                  href={wikiHref(page.path)}
                  initial={{ opacity: 0, x: -6 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: 0.45 + index * 0.05 }}
                  className="group flex items-start gap-3 rounded-xl px-3 py-2.5 transition-colors hover:bg-muted"
                >
                  <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg border bg-background">
                    <TypeIcon type={page.type} entityType={page.entity_type} className="size-3.5" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-2">
                      <span className="truncate text-[13.5px] font-medium">{page.title}</span>
                      <span className="shrink-0 text-[11px] text-muted-foreground">{onDay(page.updated)}</span>
                    </div>
                    {page.summary ? <p className="mt-0.5 line-clamp-1 text-[12.5px] text-muted-foreground">{page.summary}</p> : null}
                  </div>
                  <ArrowRight className="mt-1 size-3.5 shrink-0 -translate-x-1 text-muted-foreground opacity-0 transition group-hover:translate-x-0 group-hover:opacity-100" />
                </motion.a>
              ))}
            </div>
          </Card>
        </motion.div>

        <motion.div initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6, delay: 0.45 }}>
          <Card className="relative h-full min-h-[22rem] overflow-hidden">
            <div className="absolute left-5 top-5 z-10">
              <div className="flex items-center gap-2 text-[13px] font-semibold"><Network className="size-4 text-muted-foreground" />Knowledge graph</div>
              <div className="mt-0.5 text-[12px] text-muted-foreground">
                {graph.data ? `${graph.data.nodes.length.toLocaleString()} pages · ${graph.data.edges.length.toLocaleString()} links` : "Loading…"}
              </div>
            </div>
            <Button size="sm" variant="secondary" className="absolute bottom-4 right-4 z-10" onClick={() => navigate("graph")}>
              Explore<ArrowRight />
            </Button>
            {graph.data ? <SigmaGraph data={graph.data} mode="preview" onNodeClick={(path) => navigate(wikiHref(path))} className="absolute inset-0" /> : <Skeleton className="absolute inset-6" />}
          </Card>
        </motion.div>
      </div>

      {catalog.data ? <HomeStory me={me} pages={pages} graph={graph.data} onSearch={onSearch} /> : null}
    </div>
  );
}
