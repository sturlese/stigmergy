import { motion } from "motion/react";
import { cn } from "@/lib/utils";

/** The mark: two nodes joined by a trail, drawn in on first paint. */
export function Logo({ className, animated = true }: { className?: string; animated?: boolean }) {
  return (
    <svg viewBox="0 0 32 32" className={cn("size-8", className)} aria-hidden>
      <rect width="32" height="32" rx="9" fill="oklch(0.36 0.1 260)" />
      <motion.path
        d="M9 21c3-7 11-3 14-10"
        fill="none"
        strokeWidth="2.4"
        strokeLinecap="round"
        stroke="oklch(0.8 0.09 245)"
        initial={animated ? { pathLength: 0 } : false}
        animate={{ pathLength: 1 }}
        transition={{ duration: 1.1, ease: [0.65, 0, 0.35, 1] }}
      />
      <motion.circle cx="9" cy="21" r="3" fill="oklch(0.97 0.01 250)" initial={animated ? { scale: 0 } : false} animate={{ scale: 1 }} transition={{ delay: 0.1, type: "spring" }} />
      <motion.circle cx="23" cy="11" r="3" fill="oklch(0.97 0.01 250)" initial={animated ? { scale: 0 } : false} animate={{ scale: 1 }} transition={{ delay: 0.9, type: "spring" }} />
      <motion.circle cx="16" cy="16" r="1.8" fill="oklch(0.84 0.13 75)" initial={animated ? { scale: 0 } : false} animate={{ scale: 1 }} transition={{ delay: 0.55, type: "spring" }} />
    </svg>
  );
}
