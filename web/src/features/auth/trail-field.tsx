import * as React from "react";

interface Node { x: number; y: number; vx: number; vy: number; r: number; hue: number }
interface Ant { from: number; to: number; t: number; speed: number }

/** Nodes drift; agents walk between them and every walk deepens a trail that slowly evaporates. */
export function TrailField({ className }: { className?: string }) {
  const ref = React.useRef<HTMLCanvasElement>(null);

  React.useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let width = 0;
    let height = 0;
    let frame = 0;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const nodes: Node[] = [];
    const ants: Ant[] = [];
    const trail = new Map<string, number>();
    const palette = ["oklch(0.7 0.13 250)", "oklch(0.82 0.08 225)", "oklch(0.84 0.13 75)", "oklch(0.92 0.02 250)"];

    const resize = () => {
      const box = canvas.getBoundingClientRect();
      width = box.width;
      height = box.height;
      canvas.width = width * ratio;
      canvas.height = height * ratio;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      if (!nodes.length) {
        const count = Math.round(Math.min(72, Math.max(34, (width * height) / 16000)));
        for (let i = 0; i < count; i += 1) {
          nodes.push({
            x: Math.random() * width, y: Math.random() * height,
            vx: (Math.random() - 0.5) * 0.12, vy: (Math.random() - 0.5) * 0.12,
            r: 1.4 + Math.random() * 2.8, hue: Math.random() < 0.72 ? 0 : 1 + Math.floor(Math.random() * 3),
          });
        }
        for (let i = 0; i < 40; i += 1) spawn();
      }
    };

    const nearest = (index: number) => {
      const origin = nodes[index];
      return nodes
        .map((node, i) => ({ i, d: (node.x - origin.x) ** 2 + (node.y - origin.y) ** 2 }))
        .filter(({ i }) => i !== index)
        .sort((a, b) => a.d - b.d)
        .slice(0, 4);
    };

    function spawn(from = Math.floor(Math.random() * nodes.length)) {
      const options = nearest(from);
      const weighted = options.map(({ i }) => 1 + (trail.get(key(from, i)) ?? 0) * 6);
      let pick = Math.random() * weighted.reduce((a, b) => a + b, 0);
      let to = options[0].i;
      for (let k = 0; k < options.length; k += 1) {
        pick -= weighted[k];
        if (pick <= 0) { to = options[k].i; break; }
      }
      ants.push({ from, to, t: 0, speed: 0.009 + Math.random() * 0.012 });
    }

    const key = (a: number, b: number) => (a < b ? `${a}:${b}` : `${b}:${a}`);

    const advance = () => {
      for (const node of nodes) {
        node.x += node.vx;
        node.y += node.vy;
        if (node.x < -20 || node.x > width + 20) node.vx *= -1;
        if (node.y < -20 || node.y > height + 20) node.vy *= -1;
      }
      for (const [edge, strength] of trail) {
        const next = strength * 0.9975;
        if (next < 0.02) trail.delete(edge); else trail.set(edge, next);
      }
      for (let i = ants.length - 1; i >= 0; i -= 1) {
        const ant = ants[i];
        ant.t += ant.speed;
        if (ant.t >= 1) {
          const edge = key(ant.from, ant.to);
          trail.set(edge, Math.min(1.4, (trail.get(edge) ?? 0) + 0.32));
          ants.splice(i, 1);
          spawn(ant.to);
        }
      }
    };

    const draw = () => {
      advance();
      context.clearRect(0, 0, width, height);
      context.lineCap = "round";
      for (const [edge, strength] of trail) {
        const [a, b] = edge.split(":").map(Number);
        const from = nodes[a];
        const to = nodes[b];
        context.strokeStyle = palette[0];
        context.globalAlpha = Math.min(0.75, 0.08 + strength * 0.5);
        context.lineWidth = 0.7 + Math.min(2.4, strength * 1.8);
        context.beginPath();
        context.moveTo(from.x, from.y);
        context.lineTo(to.x, to.y);
        context.stroke();
      }
      for (const ant of ants) {
        const from = nodes[ant.from];
        const to = nodes[ant.to];
        const x = from.x + (to.x - from.x) * ant.t;
        const y = from.y + (to.y - from.y) * ant.t;
        context.fillStyle = "oklch(0.95 0.03 245)";
        context.globalAlpha = 0.95;
        context.beginPath();
        context.arc(x, y, 1.6, 0, Math.PI * 2);
        context.fill();
      }
      for (const node of nodes) {
        context.globalAlpha = 0.85;
        context.fillStyle = palette[node.hue];
        context.beginPath();
        context.arc(node.x, node.y, node.r, 0, Math.PI * 2);
        context.fill();
      }
      context.globalAlpha = 1;
      if (!reduced) frame = requestAnimationFrame(draw);
    };

    resize();
    for (let step = 0; step < 700; step += 1) advance();
    draw();
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, []);

  return <canvas ref={ref} className={className} aria-hidden />;
}
