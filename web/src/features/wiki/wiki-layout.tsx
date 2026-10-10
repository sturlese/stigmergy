import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import * as React from "react";
import { usePalette } from "@/components/shell";
import { Tooltip } from "@/components/ui/overlays";
import type { Me } from "@/lib/types";
import { readStored, writeStored } from "@/lib/utils";
import { Explorer } from "./explorer";
import { PageView } from "./page-view";
import { WikiHome } from "./wiki-home";

const KEY = "stigmergy.explorer.visible";

export function WikiLayout({ me, path }: { me: Me; path: string | null }) {
  const [visible, setVisible] = React.useState(() => readStored(KEY, true));
  const openPalette = usePalette();
  const toggle = () => setVisible((value) => {
    writeStored(KEY, !value);
    return !value;
  });

  React.useEffect(() => {
    document.querySelector("main")?.scrollTo({ top: 0 });
  }, [path]);

  return (
    <div className="flex min-h-full">
      <AnimatePresence initial={false}>
        {visible ? (
          <motion.div
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: 272, opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ type: "spring", bounce: 0, duration: 0.35 }}
            className="sticky top-0 h-dvh shrink-0 overflow-hidden border-r bg-sidebar"
          >
            <div className="h-full w-[272px]"><Explorer activePath={path} /></div>
          </motion.div>
        ) : null}
      </AnimatePresence>
      <div className="relative min-w-0 flex-1">
        <Tooltip content={visible ? "Hide explorer" : "Show explorer"} side="right">
          <button
            onClick={toggle}
            className="absolute left-3 top-3 z-10 flex size-8 items-center justify-center rounded-lg text-muted-foreground transition hover:bg-muted hover:text-foreground"
            aria-label={visible ? "Hide explorer" : "Show explorer"}
          >
            {visible ? <PanelLeftClose className="size-4" /> : <PanelLeftOpen className="size-4" />}
          </button>
        </Tooltip>
        {path ? <PageView path={path} /> : <WikiHome me={me} onSearch={openPalette} />}
      </div>
    </div>
  );
}
