import { cn } from "@/lib/utils";

type Kind = "add" | "remove" | "hunk" | "meta" | "context";

function kindOf(line: string): Kind {
  if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")) return "meta";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  return "context";
}

const STYLES: Record<Kind, string> = {
  add: "bg-success/10 text-foreground [&>span:first-child]:text-success",
  remove: "bg-destructive/10 text-foreground [&>span:first-child]:text-destructive",
  hunk: "bg-note/8 text-note",
  meta: "text-muted-foreground",
  context: "text-muted-foreground",
};

/** A unified diff rendered line by line as text, never as markup. */
export function DiffView({ patch, className }: { patch: string; className?: string }) {
  const lines = String(patch || "").replace(/\n$/, "").split("\n");
  return (
    <div className={cn("overflow-x-auto rounded-xl border bg-background font-mono text-[12px] leading-[1.65]", className)}>
      <div className="min-w-max py-1.5">
        {lines.map((line, index) => {
          const kind = kindOf(line);
          const marker = kind === "add" ? "+" : kind === "remove" ? "−" : " ";
          const text = kind === "add" || kind === "remove" ? line.slice(1) : line;
          return (
            <div key={index} className={cn("flex whitespace-pre px-3", STYLES[kind])}>
              <span className="w-5 shrink-0 select-none text-center">{marker}</span>
              <span>{text || " "}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
