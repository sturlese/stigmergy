import { AnimatePresence, motion } from "motion/react";
import * as React from "react";
import { createPortal } from "react-dom";
import { TypeBadge } from "@/components/type";
import { useCatalog } from "@/lib/queries";
import { pagePathFromRoute } from "@/lib/router";
import { onDay } from "@/lib/utils";

/** Hover cards for internal links inside rendered wiki HTML, driven by event delegation. */
export function useLinkPreview(container: React.RefObject<HTMLElement | null>) {
  const catalog = useCatalog();
  const [preview, setPreview] = React.useState<{ path: string; rect: DOMRect } | null>(null);
  const timer = React.useRef<number>(0);

  React.useEffect(() => {
    const element = container.current;
    if (!element) return;
    const over = (event: MouseEvent) => {
      const link = (event.target as HTMLElement).closest<HTMLAnchorElement>("a.wikilink, a.citation");
      if (!link || !element.contains(link)) return;
      const path = pagePathFromRoute((link.getAttribute("href") ?? "").replace(/^#\/?/, ""));
      if (!path) return;
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setPreview({ path, rect: link.getBoundingClientRect() }), 260);
    };
    const out = (event: MouseEvent) => {
      const link = (event.target as HTMLElement).closest("a.wikilink, a.citation");
      if (!link) return;
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setPreview(null), 120);
    };
    const hide = () => setPreview(null);
    element.addEventListener("mouseover", over);
    element.addEventListener("mouseout", out);
    window.addEventListener("scroll", hide, true);
    return () => {
      element.removeEventListener("mouseover", over);
      element.removeEventListener("mouseout", out);
      window.removeEventListener("scroll", hide, true);
      window.clearTimeout(timer.current);
    };
  }, [container]);

  const page = preview ? catalog.data?.pages.find((item) => item.path === preview.path) : undefined;
  const card = (
    <AnimatePresence>
      {preview && page ? (
        <motion.div
          key={preview.path}
          initial={{ opacity: 0, y: 6, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 4, transition: { duration: 0.12 } }}
          transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
          className="pointer-events-none fixed z-50 w-80 rounded-xl border bg-popover p-4 shadow-2xl"
          style={{
            left: Math.min(window.innerWidth - 336, Math.max(16, preview.rect.left)),
            top: preview.rect.bottom + 8 + 220 > window.innerHeight ? preview.rect.top - 8 - 180 : preview.rect.bottom + 8,
          }}
        >
          <div className="mb-2 flex items-center gap-2">
            <TypeBadge type={page.type} entityType={page.entity_type} />
            <span className="text-[11px] text-muted-foreground">{onDay(page.updated)}</span>
          </div>
          <div className="text-[14px] font-semibold leading-snug tracking-tight">{page.title}</div>
          {page.summary ? <p className="mt-1.5 line-clamp-4 font-serif text-[14px] leading-relaxed text-muted-foreground">{page.summary}</p> : null}
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
  return createPortal(card, document.body);
}
