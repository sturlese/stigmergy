import { Collapsible as CollapsiblePrimitive } from "radix-ui";
import { AlertTriangle, CheckCircle2, ChevronRight, Info, XCircle } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import * as React from "react";
import { cn } from "@/lib/utils";

/** A section that expands and collapses with a height animation. */
export function Collapsible({ open, onOpenChange, trigger, children, className, triggerClassName }: {
  open: boolean; onOpenChange: (open: boolean) => void; trigger: React.ReactNode; children: React.ReactNode;
  className?: string; triggerClassName?: string;
}) {
  return (
    <CollapsiblePrimitive.Root open={open} onOpenChange={onOpenChange} className={className}>
      <CollapsiblePrimitive.Trigger className={cn("group flex w-full items-center gap-1.5 text-left", triggerClassName)}>
        <ChevronRight className="size-3.5 shrink-0 text-muted-foreground transition-transform duration-200 group-data-[state=open]:rotate-90" />
        {trigger}
      </CollapsiblePrimitive.Trigger>
      <AnimatePresence initial={false}>
        {open ? (
          <CollapsiblePrimitive.Content forceMount asChild>
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
              className="overflow-hidden"
            >
              {children}
            </motion.div>
          </CollapsiblePrimitive.Content>
        ) : null}
      </AnimatePresence>
    </CollapsiblePrimitive.Root>
  );
}

export function EmptyState({ icon: Icon, title, children, className }: {
  icon?: React.ComponentType<{ className?: string }>; title: string; children?: React.ReactNode; className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-6 py-12 text-center", className)}>
      {Icon ? (
        <div className="mb-1 flex size-11 items-center justify-center rounded-full bg-muted">
          <Icon className="size-5 text-muted-foreground" />
        </div>
      ) : null}
      <p className="text-sm font-medium">{title}</p>
      {children ? <div className="max-w-sm text-[13px] text-muted-foreground">{children}</div> : null}
    </div>
  );
}

const noticeTones = {
  info: { icon: Info, className: "border-note/25 bg-note/8 text-foreground [&>svg]:text-note" },
  success: { icon: CheckCircle2, className: "border-success/25 bg-success/8 [&>svg]:text-success" },
  warning: { icon: AlertTriangle, className: "border-warning/30 bg-warning/10 [&>svg]:text-warning" },
  danger: { icon: XCircle, className: "border-destructive/25 bg-destructive/8 [&>svg]:text-destructive" },
};

export function Notice({ tone = "info", children, className }: { tone?: keyof typeof noticeTones; children: React.ReactNode; className?: string }) {
  const { icon: Icon, className: toneClass } = noticeTones[tone];
  return (
    <div className={cn("flex items-start gap-2.5 rounded-lg border px-3 py-2.5 text-[13px] leading-relaxed", toneClass, className)}>
      <Icon className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0">{children}</div>
    </div>
  );
}

export function Table({ className, ...props }: React.HTMLAttributes<HTMLTableElement>) {
  return (
    <div className="w-full overflow-x-auto">
      <table className={cn("w-full border-collapse text-[13px]", className)} {...props} />
    </div>
  );
}
export function Th({ className, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return <th className={cn("h-9 border-b px-3 text-left text-[11px] font-medium uppercase tracking-wide text-muted-foreground first:pl-5 last:pr-5", className)} {...props} />;
}
export function Td({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn("border-b px-3 py-2.5 align-middle first:pl-5 last:pr-5 group-last/row:border-0", className)} {...props} />;
}
export function Tr({ className, ...props }: React.HTMLAttributes<HTMLTableRowElement>) {
  return <tr className={cn("group/row transition-colors hover:bg-muted/50", className)} {...props} />;
}

type Toast = { id: number; title: string; tone: "success" | "danger" | "info" };
const ToastContext = React.createContext<(title: string, tone?: Toast["tone"]) => void>(() => {});

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = React.useState<Toast[]>([]);
  const push = React.useCallback((title: string, tone: Toast["tone"] = "success") => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current.slice(-3), { id, title, tone }]);
    window.setTimeout(() => setToasts((current) => current.filter((toast) => toast.id !== id)), 3800);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-5 right-5 z-[60] flex flex-col items-end gap-2">
        <AnimatePresence>
          {toasts.map((toast) => {
            const Icon = toast.tone === "danger" ? XCircle : toast.tone === "info" ? Info : CheckCircle2;
            return (
              <motion.div
                key={toast.id}
                layout
                initial={{ opacity: 0, y: 16, scale: 0.96 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, x: 24, transition: { duration: 0.18 } }}
                transition={{ type: "spring", bounce: 0.25, duration: 0.45 }}
                className="pointer-events-auto flex max-w-sm items-center gap-2.5 rounded-xl border bg-popover px-4 py-3 text-[13px] font-medium shadow-2xl"
              >
                <Icon className={cn("size-4 shrink-0", toast.tone === "danger" ? "text-destructive" : toast.tone === "info" ? "text-note" : "text-success")} />
                {toast.title}
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return React.useContext(ToastContext);
}
