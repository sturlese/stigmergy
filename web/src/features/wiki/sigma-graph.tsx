import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import * as React from "react";
import Sigma from "sigma";
import { useTheme } from "@/components/theme";
import { clearColorCache, cssVar, mix, toRgb } from "@/lib/colors";
import type { GraphData, PageType } from "@/lib/types";

export interface SigmaGraphHandle {
  zoomIn: () => void;
  zoomOut: () => void;
  reset: () => void;
  focus: (path: string) => void;
}

interface Props {
  data: GraphData;
  center?: string;
  mode: "local" | "global" | "preview";
  hiddenTypes?: Set<PageType>;
  highlight?: string | null;
  onNodeClick?: (path: string) => void;
  onNodeHover?: (path: string | null, position?: { x: number; y: number }) => void;
  /** Current running along the links, arcs and a spreading discharge around the page in focus. */
  pulse?: boolean;
  /** A slow turn of the whole graph while nobody points at it. */
  drift?: boolean;
  className?: string;
}

const TYPE_VAR: Record<PageType, string> = {
  entity: "--type-entity", project: "--type-project", concept: "--type-concept", note: "--type-note",
  source: "--type-source",
};

/** A WebGL graph: force-directed settling on mount, neighbor highlighting on hover. */
export const SigmaGraph = React.forwardRef<SigmaGraphHandle, Props>(function SigmaGraph(
  { data, center, mode, hiddenTypes, highlight, onNodeClick, onNodeHover, pulse, drift, className }, handle,
) {
  const container = React.useRef<HTMLDivElement>(null);
  const renderer = React.useRef<Sigma | null>(null);
  const graph = React.useRef<Graph | null>(null);
  const hovered = React.useRef<string | null>(null);
  const state = React.useRef({ hiddenTypes, highlight });
  const callbacks = React.useRef({ onNodeClick, onNodeHover });
  const { resolved } = useTheme();
  state.current = { hiddenTypes, highlight };
  callbacks.current = { onNodeClick, onNodeHover };

  React.useImperativeHandle(handle, () => ({
    zoomIn: () => renderer.current?.getCamera().animatedZoom({ duration: 300 }),
    zoomOut: () => renderer.current?.getCamera().animatedUnzoom({ duration: 300 }),
    reset: () => renderer.current?.getCamera().animatedReset({ duration: 500 }),
    focus: (path: string) => {
      const sigma = renderer.current;
      if (!sigma || !graph.current?.hasNode(path)) return;
      const position = sigma.getNodeDisplayData(path);
      if (position) sigma.getCamera().animate({ x: position.x, y: position.y, ratio: 0.35 }, { duration: 700 });
    },
  }));

  React.useEffect(() => {
    const element = container.current;
    if (!element) return;
    clearColorCache();
    const background = cssVar("--card") || cssVar("--background");
    const muted = mix(cssVar("--muted-foreground"), background, 22);
    const wide = mode !== "local";
    const edgeColor = mix(cssVar("--muted-foreground"), background, wide ? 18 : 30);
    const edgeStrong = mix(cssVar("--primary"), background, 80);
    const labelColor = toRgb(cssVar("--foreground"));
    const popover = toRgb(cssVar("--popover"));
    const border = toRgb(cssVar("--border"));
    const colors = Object.fromEntries(
      (Object.keys(TYPE_VAR) as PageType[]).map((type) => [type, toRgb(cssVar(TYPE_VAR[type]))]),
    ) as Record<PageType, string>;

    const next = new Graph({ type: "undirected", multi: false });
    const count = data.nodes.length;
    data.nodes.forEach((node, index) => {
      const angle = (index / Math.max(1, count)) * Math.PI * 2;
      const radius = node.path === center ? 0 : 10 + Math.random() * 10;
      next.addNode(node.path, {
        x: Math.cos(angle) * radius, y: Math.sin(angle) * radius,
        label: node.title, type: "circle", pageType: node.type, color: colors[node.type] ?? muted,
        size: 4, weight: node.weight,
      });
    });
    for (const edge of data.edges) {
      if (next.hasNode(edge.from) && next.hasNode(edge.to) && edge.from !== edge.to && !next.hasEdge(edge.from, edge.to)) {
        next.addEdge(edge.from, edge.to, { color: edgeColor, size: wide ? 0.5 : 1 });
      }
    }
    next.forEachNode((node) => {
      const weight = next.getNodeAttribute(node, "weight");
      if (typeof weight === "number") {
        next.setNodeAttribute(node, "size", Math.min(20, 3 + Math.sqrt(weight) * 1.8));
        return;
      }
      const degree = next.degree(node);
      const size = mode === "global" ? Math.min(18, 2.2 + Math.sqrt(degree) * 1.4)
        : mode === "preview" ? Math.min(11, 1.6 + Math.sqrt(degree) * 0.9)
        : node === center ? 12 : 5 + Math.min(4, Math.sqrt(degree) * 0.8);
      next.setNodeAttribute(node, "size", size);
    });
    if (center && next.hasNode(center)) {
      next.setNodeAttribute(center, "x", 0);
      next.setNodeAttribute(center, "y", 0);
      next.setNodeAttribute(center, "fixed", true);
    }
    graph.current = next;

    const sigma = new Sigma(next, element, {
      renderLabels: true,
      renderEdgeLabels: false,
      labelFont: "Inter Variable, ui-sans-serif, sans-serif",
      labelSize: mode === "local" ? 11 : 12,
      labelWeight: "500",
      labelColor: { color: labelColor },
      labelRenderedSizeThreshold: mode === "global" ? 8 : mode === "preview" ? 9 : 0,
      labelDensity: mode === "local" ? 1 : 0.5,
      stagePadding: mode === "local" ? 28 : 36,
      zIndex: true,
      minCameraRatio: 0.08,
      maxCameraRatio: 4,
      defaultDrawNodeHover: (context, node, settings) => {
        context.beginPath();
        context.arc(node.x, node.y, node.size + 3, 0, Math.PI * 2);
        context.strokeStyle = node.color;
        context.lineWidth = 2;
        context.stroke();
        if (!node.label || wide) return;
        const size = settings.labelSize;
        context.font = `600 ${size}px ${settings.labelFont}`;
        const width = context.measureText(node.label).width;
        const x = node.x + node.size + 6;
        const y = node.y;
        context.fillStyle = popover;
        context.strokeStyle = border;
        context.lineWidth = 1;
        context.beginPath();
        context.roundRect(x - 6, y - size / 2 - 6, width + 12, size + 12, 8);
        context.fill();
        context.stroke();
        context.fillStyle = labelColor;
        context.fillText(node.label, x, y + size / 3);
      },
      nodeReducer: (node, attributes) => {
        const { hiddenTypes: hidden, highlight: focus } = state.current;
        const result = { ...attributes };
        if (hidden?.has(attributes.pageType as PageType)) {
          result.hidden = true;
          return result;
        }
        const active = hovered.current ?? focus ?? null;
        if (active && next.hasNode(active)) {
          const near = node === active || next.areNeighbors(node, active);
          if (!near) {
            result.color = muted;
            result.label = "";
            result.zIndex = 0;
          } else {
            result.zIndex = 2;
            result.forceLabel = !wide || next.degree(active) <= 18 || node === active;
          }
          if (node === active) result.highlighted = true;
        }
        if (node === center) result.forceLabel = true;
        return result;
      },
      edgeReducer: (edge, attributes) => {
        const { hiddenTypes: hidden, highlight: focus } = state.current;
        const [source, target] = next.extremities(edge);
        const result = { ...attributes };
        if (hidden && (hidden.has(next.getNodeAttribute(source, "pageType")) || hidden.has(next.getNodeAttribute(target, "pageType")))) {
          result.hidden = true;
          return result;
        }
        const active = hovered.current ?? focus ?? null;
        if (active && next.hasNode(active)) {
          if (source === active || target === active) {
            result.color = edgeStrong;
            result.size = (attributes.size ?? 1) * 2;
            result.zIndex = 2;
          } else {
            result.hidden = wide;
            result.color = muted;
          }
        }
        return result;
      },
    });
    renderer.current = sigma;

    sigma.on("enterNode", ({ node }) => {
      hovered.current = node;
      element.style.cursor = node === center ? "default" : "pointer";
      const display = sigma.getNodeDisplayData(node);
      const position = display ? sigma.graphToViewport({ x: display.x, y: display.y }) : undefined;
      callbacks.current.onNodeHover?.(node, position);
      sigma.refresh({ skipIndexation: true });
    });
    sigma.on("leaveNode", () => {
      hovered.current = null;
      element.style.cursor = "default";
      callbacks.current.onNodeHover?.(null);
      sigma.refresh({ skipIndexation: true });
    });
    sigma.on("clickNode", ({ node }) => {
      if (node !== center) callbacks.current.onNodeClick?.(node);
    });

    const settings = {
      ...forceAtlas2.inferSettings(next), gravity: mode === "local" ? 3 : 0.7, scalingRatio: mode === "local" ? 6 : 22,
      slowDown: 5, barnesHutOptimize: count > 400, strongGravityMode: mode !== "local",
    };
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let frame = 0;
    let step = 0;
    const total = reduced ? 0 : mode === "local" ? 90 : 240;
    if (reduced || count < 2) {
      if (count >= 2) forceAtlas2.assign(next, { iterations: mode === "local" ? 120 : 320, settings });
    } else {
      forceAtlas2.assign(next, { iterations: 20, settings });
      const tick = () => {
        forceAtlas2.assign(next, { iterations: count > 600 ? 1 : 2, settings });
        step += 1;
        if (step < total) frame = requestAnimationFrame(tick);
      };
      frame = requestAnimationFrame(tick);
    }

    const motion = !reduced && (pulse || drift) ? ambient(sigma, next, element, hovered, state, { pulse, drift }) : null;

    const observer = new ResizeObserver(() => {
      sigma.resize();
      sigma.refresh({ skipIndexation: true });
    });
    observer.observe(element);

    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
      motion?.stop();
      sigma.kill();
      renderer.current = null;
      graph.current = null;
    };
  }, [data, center, mode, resolved, pulse, drift]);

  React.useEffect(() => {
    renderer.current?.refresh({ skipIndexation: true });
  }, [hiddenTypes, highlight]);

  return <div ref={container} className={className} />;
});

type Point = { x: number; y: number };
type Comet = { from: string; to: string; t: number; speed: number; tail: number };

/** Ambient motion over a running renderer, drawn on a canvas laid over the graph with additive
 * blending: a slow turn, current running along links, and around the page in focus flickering
 * arcs, shock rings, and a discharge that spreads two hops whenever the focus moves. */
function ambient(sigma: Sigma, graph: Graph, element: HTMLElement, hovered: React.RefObject<string | null>,
  state: React.RefObject<{ hiddenTypes?: Set<PageType>; highlight?: string | null }>,
  options: { pulse?: boolean; drift?: boolean }) {
  const canvas = document.createElement("canvas");
  canvas.style.cssText = "position:absolute;inset:0;pointer-events:none;width:100%;height:100%";
  element.appendChild(canvas);
  const context = canvas.getContext("2d");
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const edges = graph.mapEdges((_edge, _attributes, source, target) => [source, target] as const);
  const glow = sprite("255,255,255", "125,190,255");
  const spark = sprite("255,255,255", "255,214,140");
  const comets: Comet[] = [];
  let wave: { origin: string; at: number; hops: Map<string, number> } | null = null;
  let lastFocus: string | null = null;
  let frame = 0;

  const point = (node: string): Point | null => {
    const display = sigma.getNodeDisplayData(node);
    return display && !display.hidden ? sigma.framedGraphToViewport(display) : null;
  };
  const visible = (node: string) => {
    const display = sigma.getNodeDisplayData(node);
    return !!display && !display.hidden && !state.current?.hiddenTypes?.has(graph.getNodeAttribute(node, "pageType") as PageType);
  };
  const launch = (from: string, to: string, speed = 0.45 + Math.random() * 0.7) =>
    comets.push({ from, to, t: 0, speed, tail: 0.1 + Math.random() * 0.12 });

  const draw = (time: number) => {
    const camera = sigma.getCamera();
    const focus = hovered.current ?? state.current?.highlight ?? null;
    if (options.drift && !hovered.current) camera.setState({ angle: camera.angle + 0.00045 });
    if (!options.pulse || !context) { frame = requestAnimationFrame(draw); return; }
    const { width, height } = element.getBoundingClientRect();
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    context.globalCompositeOperation = "lighter";

    // Current: keep a pool of comets running along links, near the focus when there is one.
    const budget = width < 640 ? 34 : 70;
    const active = focus && graph.hasNode(focus) && visible(focus) ? focus : null;
    for (let tries = 0; comets.length < budget && tries < 6; tries += 1) {
      if (active && Math.random() < 0.75) {
        const neighbors = graph.neighbors(active).filter(visible);
        const other = neighbors[Math.floor(Math.random() * neighbors.length)];
        if (other) launch(other, active);
      } else {
        const [a, b] = edges[Math.floor(Math.random() * edges.length)];
        if (visible(a) && visible(b)) Math.random() < 0.5 ? launch(a, b) : launch(b, a);
      }
    }

    // A new focus discharges outward, two hops deep.
    if (active && active !== lastFocus) {
      const hops = new Map<string, number>([[active, 0]]);
      for (const near of graph.neighbors(active)) if (visible(near)) hops.set(near, 1);
      for (const near of [...hops.keys()]) {
        if (hops.get(near) !== 1) continue;
        for (const far of graph.neighbors(near)) if (!hops.has(far) && visible(far) && hops.size < 70) hops.set(far, 2);
      }
      wave = { origin: active, at: time, hops };
      for (const [node, hop] of hops) if (hop === 1) launch(active, node, 1.6 + Math.random() * 0.6);
    }
    lastFocus = active;

    for (let index = comets.length - 1; index >= 0; index -= 1) {
      const comet = comets[index];
      comet.t += comet.speed / 60;
      const a = point(comet.from);
      const b = point(comet.to);
      if (!a || !b || comet.t >= 1 + comet.tail) { comets.splice(index, 1); continue; }
      const head = Math.min(comet.t, 1);
      const tail = Math.max(comet.t - comet.tail, 0);
      const hx = a.x + (b.x - a.x) * head; const hy = a.y + (b.y - a.y) * head;
      const tx = a.x + (b.x - a.x) * tail; const ty = a.y + (b.y - a.y) * tail;
      const gradient = context.createLinearGradient(tx, ty, hx, hy);
      gradient.addColorStop(0, "rgba(125,190,255,0)");
      gradient.addColorStop(1, "rgba(190,225,255,0.9)");
      context.strokeStyle = gradient;
      context.lineWidth = 1.6;
      context.beginPath(); context.moveTo(tx, ty); context.lineTo(hx, hy); context.stroke();
      if (comet.t <= 1) context.drawImage(glow, hx - 6, hy - 6, 12, 12);
      else if (comet.to !== active) {
        context.globalAlpha = 0.6;
        context.drawImage(spark, b.x - 7, b.y - 7, 14, 14);
        context.globalAlpha = 1;
      }
    }

    if (active) {
      const center = point(active);
      if (center) {
        // Shock rings and a breathing halo on the page in focus.
        for (let ring = 0; ring < 2; ring += 1) {
          const age = ((time / 1700) + ring * 0.5) % 1;
          context.strokeStyle = `rgba(140,200,255,${(1 - age) * 0.45})`;
          context.lineWidth = 1.5;
          context.beginPath(); context.arc(center.x, center.y, 8 + age * 70, 0, Math.PI * 2); context.stroke();
        }
        const breath = 30 + Math.sin(time / 300) * 5;
        context.globalAlpha = 0.45;
        context.drawImage(glow, center.x - breath / 2, center.y - breath / 2, breath, breath);
        context.globalAlpha = 1;
        // Arcs: jagged, re-drawn every few frames, flickering.
        const bucket = Math.floor(time / 70);
        for (const neighbor of graph.neighbors(active).slice(0, 26)) {
          const end = point(neighbor);
          if (!end || Math.random() < 0.35) continue;
          const seed = hash(neighbor) + bucket;
          const path = bolt(center, end, seed);
          context.strokeStyle = `rgba(110,180,255,${0.18 + noise(seed) * 0.3})`;
          context.lineWidth = 2.6;
          stroke(context, path);
          context.strokeStyle = `rgba(235,245,255,${0.35 + noise(seed + 7) * 0.4})`;
          context.lineWidth = 0.8;
          stroke(context, path);
        }
      }
    }

    // The discharge lights each node as it arrives.
    if (wave) {
      const elapsed = time - wave.at;
      for (const [node, hop] of wave.hops) {
        const local = elapsed - hop * 380;
        if (local < 0 || local > 900) continue;
        const at = point(node);
        if (!at) continue;
        const size = (hop === 0 ? 40 : hop === 1 ? 22 : 14) * (1 - local / 900);
        context.globalAlpha = (hop === 0 ? 0.6 : 1) * (1 - local / 900);
        context.drawImage(hop === 2 ? spark : glow, at.x - size / 2, at.y - size / 2, size, size);
        context.globalAlpha = 1;
      }
      if (elapsed > 2 * 380 + 900) wave = null;
    }

    context.globalCompositeOperation = "source-over";
    frame = requestAnimationFrame(draw);
  };
  frame = requestAnimationFrame(draw);
  return { stop: () => { cancelAnimationFrame(frame); canvas.remove(); } };
}

/** A soft round glow, drawn once and stamped wherever light is needed. */
function sprite(core: string, edge: string) {
  const size = 64;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const context = canvas.getContext("2d");
  if (context) {
    const gradient = context.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    gradient.addColorStop(0, `rgba(${core},1)`);
    gradient.addColorStop(0.25, `rgba(${edge},0.75)`);
    gradient.addColorStop(1, `rgba(${edge},0)`);
    context.fillStyle = gradient;
    context.fillRect(0, 0, size, size);
  }
  return canvas;
}

/** A lightning path from `a` to `b`: the straight line, kinked sideways at a few points. */
function bolt(a: Point, b: Point, seed: number): Point[] {
  const dx = b.x - a.x; const dy = b.y - a.y;
  const length = Math.hypot(dx, dy) || 1;
  const nx = -dy / length; const ny = dx / length;
  const points: Point[] = [a];
  const steps = 6;
  for (let step = 1; step < steps; step += 1) {
    const t = step / steps;
    const offset = (noise(seed + step) - 0.5) * Math.min(28, length * 0.18) * Math.sin(t * Math.PI);
    points.push({ x: a.x + dx * t + nx * offset, y: a.y + dy * t + ny * offset });
  }
  points.push(b);
  return points;
}

function stroke(context: CanvasRenderingContext2D, points: Point[]) {
  context.beginPath();
  context.moveTo(points[0].x, points[0].y);
  for (const point of points.slice(1)) context.lineTo(point.x, point.y);
  context.stroke();
}

function hash(text: string) {
  let value = 7;
  for (let index = 0; index < text.length; index += 1) value = (value * 31 + text.charCodeAt(index)) >>> 0;
  return value % 100000;
}

function noise(seed: number) {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}
