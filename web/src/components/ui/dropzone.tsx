import { FileUp, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import * as React from "react";
import { bytes, cn } from "@/lib/utils";

export function Dropzone({ file, onFile, maxBytes, className }: {
  file: File | null; onFile: (file: File | null) => void; maxBytes?: number; className?: string;
}) {
  const input = React.useRef<HTMLInputElement>(null);
  const [over, setOver] = React.useState(false);
  const accept = (candidate: File | undefined) => {
    if (candidate) onFile(candidate);
  };
  return (
    <div
      onDragOver={(event) => { event.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => { event.preventDefault(); setOver(false); accept(event.dataTransfer.files[0]); }}
      onClick={() => input.current?.click()}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") input.current?.click(); }}
      className={cn(
        "relative flex min-h-36 cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-6 py-8 text-center transition-colors",
        over ? "border-primary bg-primary/8" : "border-border hover:border-primary/50 hover:bg-muted/40",
        className,
      )}
    >
      <input ref={input} type="file" className="sr-only" onChange={(event) => accept(event.target.files?.[0])} />
      <AnimatePresence mode="wait" initial={false}>
        {file ? (
          <motion.div key="file" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="flex items-center gap-3 rounded-lg border bg-card px-3 py-2 text-left shadow-sm">
            <FileUp className="size-5 text-primary" />
            <div className="min-w-0">
              <div className="max-w-64 truncate text-[13px] font-medium">{file.name}</div>
              <div className="text-[11px] text-muted-foreground">{bytes(file.size)} · {file.type || "unknown type"}</div>
            </div>
            <button
              type="button"
              onClick={(event) => { event.stopPropagation(); onFile(null); if (input.current) input.current.value = ""; }}
              className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
              aria-label="Remove file"
            >
              <X className="size-4" />
            </button>
          </motion.div>
        ) : (
          <motion.div key="empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="flex flex-col items-center gap-2">
            <motion.div animate={over ? { y: -4, scale: 1.08 } : { y: 0, scale: 1 }} className="flex size-11 items-center justify-center rounded-full bg-muted">
              <FileUp className="size-5 text-muted-foreground" />
            </motion.div>
            <div className="text-[13px] font-medium">Drop a file here, or click to choose</div>
            <div className="text-[11px] text-muted-foreground">PDF, Office documents, images, HTML, text{maxBytes ? ` · up to ${bytes(maxBytes)}` : ""}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
