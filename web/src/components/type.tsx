import {
  Atom, Building2, FileText, FolderKanban, GitBranch, Lightbulb, MapPin, NotebookPen, Package, User, Wrench,
} from "lucide-react";
import type { PageType } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Badge } from "./ui/primitives";

export const TYPE_META: Record<PageType, { label: string; plural: string; icon: typeof Atom; text: string; bg: string; color: string }> = {
  entity: { label: "Entity", plural: "Entities", icon: Atom, text: "text-entity", bg: "bg-entity", color: "var(--type-entity)" },
  project: { label: "Project", plural: "Projects", icon: FolderKanban, text: "text-project", bg: "bg-project", color: "var(--type-project)" },
  concept: { label: "Concept", plural: "Concepts", icon: Lightbulb, text: "text-concept", bg: "bg-concept", color: "var(--type-concept)" },
  note: { label: "Note", plural: "Notes", icon: NotebookPen, text: "text-note", bg: "bg-note", color: "var(--type-note)" },
  source: { label: "Source", plural: "Sources", icon: FileText, text: "text-source", bg: "bg-source", color: "var(--type-source)" },
};

export const TYPE_ORDER: PageType[] = ["project", "entity", "concept", "note", "source"];

const ENTITY_ICONS: Record<string, typeof Atom> = {
  person: User, organization: Building2, product: Package, tool: Wrench, repository: GitBranch,
  place: MapPin,
};

export function entityIcon(entityType?: string) {
  return ENTITY_ICONS[entityType ?? ""] ?? Atom;
}

export function TypeIcon({ type, entityType, className }: { type: PageType; entityType?: string; className?: string }) {
  const Icon = type === "entity" && entityType ? entityIcon(entityType) : TYPE_META[type].icon;
  return <Icon className={cn("size-4 shrink-0", TYPE_META[type].text, className)} />;
}

export function TypeBadge({ type, entityType }: { type: PageType; entityType?: string }) {
  const Icon = type === "entity" && entityType ? entityIcon(entityType) : TYPE_META[type].icon;
  return (
    <Badge tone={type}>
      <Icon />
      {type === "entity" && entityType ? entityType : TYPE_META[type].label}
    </Badge>
  );
}
