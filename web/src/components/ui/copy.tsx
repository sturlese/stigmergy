import { Check, Copy } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import * as React from "react";
import { cn } from "@/lib/utils";
import { Button } from "./button";

export function CopyButton({ value, label = "Copy", className, size = "sm" }: {
  value: string; label?: string; className?: string; size?: "sm" | "icon-sm";
}) {
  const [copied, setCopied] = React.useState(false);
  const timer = React.useRef<number>(0);
  React.useEffect(() => () => window.clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };
  return (
    <Button type="button" variant="secondary" size={size} onClick={copy} className={cn("relative", className)} aria-label={label}>
      <AnimatePresence mode="wait" initial={false}>
        {copied ? (
          <motion.span key="done" initial={{ scale: 0.5, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ opacity: 0 }} className="flex items-center gap-1.5 text-success">
            <Check />{size === "sm" ? "Copied" : null}
          </motion.span>
        ) : (
          <motion.span key="copy" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="flex items-center gap-1.5">
            <Copy />{size === "sm" ? label : null}
          </motion.span>
        )}
      </AnimatePresence>
    </Button>
  );
}

export function CodeBlock({ code, className }: { code: string; className?: string }) {
  return (
    <div className={cn("group relative min-w-0 rounded-xl border bg-muted/60", className)}>
      <pre className="max-h-80 overflow-auto p-4 pr-24 font-mono text-[12px] leading-relaxed"><code>{code}</code></pre>
      <CopyButton value={code} className="absolute right-2.5 top-2.5 opacity-80 transition group-hover:opacity-100" />
    </div>
  );
}
