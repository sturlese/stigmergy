import { useQueryClient } from "@tanstack/react-query";
import { Building2, Check, ChevronsUpDown, LayoutGrid } from "lucide-react";
import * as React from "react";
import { api } from "@/lib/api";
import { useWorkspaces } from "@/lib/queries";
import { navigate } from "@/lib/router";
import type { Me } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useToast } from "./ui/feedback";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger } from "./ui/overlays";

/** Opens a workspace as its administrator, or leaves every workspace for the master console.
 * The server records each switch; every query of the old workspace is dropped with it. */
export function useActAs() {
  const queryClient = useQueryClient();
  const toast = useToast();
  return React.useCallback(async (tenantId: string | null, then?: string) => {
    try {
      const me = await api.post<Me>("platform/acting", { tenant_id: tenantId });
      queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== "me" });
      queryClient.setQueryData(["me"], me);
      navigate(then ?? (tenantId ? "overview" : "platform"));
      toast(tenantId ? `Now in ${me.tenant?.name ?? "the workspace"}.` : "Back to the platform.");
    } catch (failure) {
      toast(failure instanceof Error ? failure.message : "The switch failed.", "danger");
    }
  }, [queryClient, toast]);
}

export function WorkspaceSwitcher({ me, collapsed }: { me: Me; collapsed: boolean }) {
  const workspaces = useWorkspaces();
  const actAs = useActAs();
  const rows = (workspaces.data?.tenants ?? []).filter((row) => row.status !== "suspended" || row.id === me.tenant?.id);
  return (
    <Menu>
      <MenuTrigger
        className={cn(
          "flex h-9 w-full items-center gap-2 rounded-lg border bg-background/70 px-2.5 text-left text-[12.5px] shadow-xs transition hover:border-primary/40",
          collapsed && "justify-center px-0",
        )}
        aria-label="Switch workspace"
      >
        {me.tenant ? <Building2 className="size-3.5 shrink-0 text-primary" /> : <LayoutGrid className="size-3.5 shrink-0 text-primary" />}
        {!collapsed ? (
          <>
            <span className="min-w-0 flex-1 truncate font-medium">{me.tenant?.name ?? "All workspaces"}</span>
            <ChevronsUpDown className="size-3.5 shrink-0 text-muted-foreground" />
          </>
        ) : null}
      </MenuTrigger>
      <MenuContent align="start" className="w-64">
        <MenuLabel>Act as administrator of</MenuLabel>
        {rows.map((row) => (
          <MenuItem key={row.id} onSelect={() => { if (row.id !== me.tenant?.id) void actAs(row.id); }}>
            <Building2 className="size-3.5 text-muted-foreground" />
            <span className="min-w-0 flex-1 truncate">{row.name}</span>
            <span className="font-mono text-[11px] text-muted-foreground">{row.slug}</span>
            {row.id === me.tenant?.id ? <Check className="size-3.5 text-primary" /> : null}
          </MenuItem>
        ))}
        {!rows.length ? <div className="px-2 py-1.5 text-[12px] text-muted-foreground">No workspace yet.</div> : null}
        <MenuSeparator />
        <MenuItem onSelect={() => { if (me.tenant) void actAs(null); else navigate("platform"); }}>
          <LayoutGrid className="size-3.5 text-muted-foreground" />Master console
        </MenuItem>
      </MenuContent>
    </Menu>
  );
}
