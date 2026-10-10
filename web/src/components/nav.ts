import {
  Activity, BookOpen, Boxes, Building2, Cpu, GitCompare, Hash, LayoutDashboard, MailPlus, Network, ScrollText,
  Sprout, Swords, Users, UsersRound, Inbox, Gauge,
} from "lucide-react";
import type { Me } from "@/lib/types";

export interface NavItem { route: string; label: string; icon: typeof Activity; description: string }
export interface NavGroup { label: string; admin: boolean; platform?: boolean; items: NavItem[] }

export const NAV: NavGroup[] = [
  {
    label: "Platform",
    admin: false,
    platform: true,
    items: [
      { route: "platform", label: "Workspaces", icon: Building2, description: "Every workspace, its health and its spend" },
      { route: "requests", label: "Access requests", icon: MailPlus, description: "Emails left on the landing page" },
      { route: "writer", label: "Writer", icon: Cpu, description: "The one writer every workspace shares" },
    ],
  },
  {
    label: "Knowledge",
    admin: false,
    items: [
      { route: "wiki", label: "Wiki", icon: BookOpen, description: "Read everything your layers allow" },
      { route: "graph", label: "Graph", icon: Network, description: "The whole knowledge graph" },
    ],
  },
  {
    label: "Operations",
    admin: true,
    items: [
      { route: "overview", label: "Overview", icon: LayoutDashboard, description: "Pipeline, writer, and index at a glance" },
      { route: "captures", label: "Captures", icon: Inbox, description: "Submit material and follow it to a commit" },
      { route: "changes", label: "Changes", icon: GitCompare, description: "Every commit the writer landed" },
      { route: "contradictions", label: "Contradictions", icon: Swords, description: "Conflicting claims and their resolution" },
      { route: "entities", label: "Entities", icon: Boxes, description: "Rename, merge, or delete entity pages" },
      { route: "gardener", label: "Gardener", icon: Sprout, description: "Autonomous corpus health runs" },
      { route: "index", label: "Index", icon: Gauge, description: "Search index freshness" },
      { route: "activity", label: "Activity", icon: ScrollText, description: "Every administrative action" },
    ],
  },
  {
    label: "Access",
    admin: true,
    items: [
      { route: "people", label: "People", icon: Users, description: "Identities, roles, and tokens" },
      { route: "groups", label: "Layers", icon: UsersRound, description: "Who reads and writes what" },
      { route: "channels", label: "Slack channels", icon: Hash, description: "Channel layers" },
    ],
  },
];

export const ADMIN_ROUTES = new Set(NAV.filter((group) => group.admin).flatMap((group) => group.items.map((item) => item.route)));
export const PLATFORM_ROUTES = new Set(NAV.filter((group) => group.platform).flatMap((group) => group.items.map((item) => item.route)));

/** The groups a session sees: the platform's to operators, a workspace's only inside one. */
export function visibleGroups(me: Me): NavGroup[] {
  return NAV.filter((group) => {
    if (group.platform) return me.operator;
    if (!me.tenant) return false;
    return !group.admin || me.is_admin;
  });
}
