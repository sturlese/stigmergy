import { animate, motion, useInView } from "motion/react";
import * as React from "react";
import { cn } from "@/lib/utils";
import { CopyButton } from "./copy";
import { Tooltip } from "./overlays";

export function CountUp({ value, className }: { value: number; className?: string }) {
  const ref = React.useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true });
  const last = React.useRef(0);
  React.useEffect(() => {
    if (!inView || !ref.current) return;
    const controls = animate(last.current, value, {
      duration: 1, ease: [0.16, 1, 0.3, 1],
      onUpdate: (latest) => { if (ref.current) ref.current.textContent = Math.round(latest).toLocaleString(); },
    });
    last.current = value;
    return () => controls.stop();
  }, [inView, value]);
  return <span ref={ref} className={className}>0</span>;
}

export function StatCard({ label, value, icon: Icon, hint, tone = "text-primary", index = 0, href }: {
  label: string; value: number; icon: React.ComponentType<{ className?: string }>; hint?: React.ReactNode;
  tone?: string; index?: number; href?: string;
}) {
  const body = (
    <>
      <div className="flex items-center justify-between">
        <span className="text-[12px] font-medium text-muted-foreground">{label}</span>
        <Icon className="size-4" />
      </div>
      <div className="mt-3 text-[30px] font-semibold leading-none tracking-tight tabular-nums text-foreground"><CountUp value={value} /></div>
      {hint ? <div className="mt-2 truncate text-[11.5px] text-muted-foreground">{hint}</div> : null}
    </>
  );
  const className = cn("group relative block overflow-hidden rounded-2xl border bg-card p-4 shadow-elevated", tone);
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, delay: 0.06 * index, ease: [0.16, 1, 0.3, 1] }}
      whileHover={{ y: -2 }}
    >
      {href ? <a href={href} className={className}>{body}</a> : <div className={className}>{body}</div>}
    </motion.div>
  );
}

/** Label/value rows; rows whose value is empty are left out. */
export function Definitions({ rows, className }: { rows: [string, React.ReactNode][]; className?: string }) {
  const shown = rows.filter(([, value]) => value !== null && value !== undefined && value !== "" && value !== false);
  return (
    <dl className={cn("grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-6 gap-y-2.5 text-[13px]", className)}>
      {shown.map(([label, value]) => (
        <React.Fragment key={label}>
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="min-w-0 break-words">{value}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

export function Sha({ value, className }: { value?: string | null; className?: string }) {
  if (!value) return <span className="text-muted-foreground">—</span>;
  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      <Tooltip content={value}>
        <code className="rounded-md border bg-muted px-1.5 py-0.5 font-mono text-[11.5px]">{value.slice(0, 8)}</code>
      </Tooltip>
      <CopyButton value={value} size="icon-sm" label="Copy SHA" className="size-6 border-0 bg-transparent" />
    </span>
  );
}
