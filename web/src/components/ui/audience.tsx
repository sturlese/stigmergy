import { Globe2, Lock } from "lucide-react";
import { motion } from "motion/react";
import { Badge } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

const ADMIN_GROUP = "brain-admins";

export const PUBLIC_LAYER = "public";

/** The repository path of one layer's page for a virtual path. */
export function physicalPath(layer: string, path: string): string {
  return layer === PUBLIC_LAYER ? path : `layers/${layer}/${path}`;
}

/** The layer a stored audience denotes: no audience is `public`. */
export function layerOf(acl: string[] | null | undefined): string {
  return acl && acl.length ? acl[0] : PUBLIC_LAYER;
}

export function LayerBadge({ layer, className }: { layer: string; className?: string }) {
  const open = layer === PUBLIC_LAYER;
  return (
    <Badge tone={open ? "neutral" : "warning"} className={className}>
      {open ? <Globe2 /> : <Lock />}{layer}
    </Badge>
  );
}

/** The layers a reader sees a fused page in; nothing when it is public alone. */
export function LayerBadges({ layers }: { layers: string[] | undefined }) {
  if (!layers?.length || (layers.length === 1 && layers[0] === PUBLIC_LAYER)) return null;
  return <>{layers.map((layer) => <LayerBadge key={layer} layer={layer} />)}</>;
}

/** Exactly one layer: `public` or one restricted layer. */
export function LayerField({ value, onChange, layers, withPublic = true }: {
  value: string; onChange: (value: string) => void; layers: string[]; withPublic?: boolean;
}) {
  const restricted = layers.filter((layer) => layer !== PUBLIC_LAYER && layer !== ADMIN_GROUP);
  const choices = withPublic || layers.includes(PUBLIC_LAYER) ? [PUBLIC_LAYER, ...restricted] : restricted;
  return (
    <div className="flex flex-wrap gap-1.5">
      {choices.map((layer) => {
        const on = layer === value;
        const Icon = layer === PUBLIC_LAYER ? Globe2 : Lock;
        return (
          <motion.button
            key={layer}
            type="button"
            layout
            onClick={() => onChange(layer)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12.5px] font-medium transition",
              on ? "border-primary/60 bg-primary/10 text-foreground shadow-[0_0_0_3px_var(--glow)]"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            <Icon className={cn("size-3.5", on ? "text-primary" : "text-muted-foreground")} />
            {layer}
          </motion.button>
        );
      })}
    </div>
  );
}

export function GroupPicker({ value, onChange, groups }: { value: string[]; onChange: (value: string[]) => void; groups: string[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {groups.length ? groups.map((group) => {
        const on = value.includes(group);
        return (
          <button
            key={group}
            type="button"
            onClick={() => onChange(on ? value.filter((item) => item !== group) : [...value, group])}
            className={cn(
              "rounded-full border px-2.5 py-1 text-[12px] font-medium transition",
              on ? (group === ADMIN_GROUP ? "border-primary/60 bg-primary/15 text-foreground" : "border-primary/40 bg-primary/10 text-foreground")
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            {group}
          </button>
        );
      }) : <span className="text-[12px] text-muted-foreground">No groups exist yet.</span>}
    </div>
  );
}
