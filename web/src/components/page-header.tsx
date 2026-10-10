import { motion } from "motion/react";
import type * as React from "react";

export function PageHeader({ title, description, actions, eyebrow }: {
  title: React.ReactNode; description?: React.ReactNode; actions?: React.ReactNode; eyebrow?: React.ReactNode;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
      className="mb-6 flex flex-wrap items-end justify-between gap-4"
    >
      <div className="grid gap-1">
        {eyebrow ? <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-primary">{eyebrow}</div> : null}
        <h1 className="text-[26px] font-semibold tracking-[-0.025em]">{title}</h1>
        {description ? <p className="max-w-2xl text-[13px] text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </motion.div>
  );
}
