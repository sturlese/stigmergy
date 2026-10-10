import { useQueryClient } from "@tanstack/react-query";
import { ChevronsLeft, ChevronsRight, LogOut, Monitor, Moon, Search, Sun, UserRound } from "lucide-react";
import { motion } from "motion/react";
import * as React from "react";
import { api } from "@/lib/api";
import { navigate } from "@/lib/router";
import type { Me } from "@/lib/types";
import { cn, readStored, writeStored } from "@/lib/utils";
import { CommandPalette } from "./command-palette";
import { Logo } from "./logo";
import { visibleGroups } from "./nav";
import { WorkspaceSwitcher } from "./workspace-switcher";
import { useTheme } from "./theme";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger, Tooltip } from "./ui/overlays";
import { Avatar, Kbd } from "./ui/primitives";

const COLLAPSED_KEY = "stigmergy.sidebar.collapsed";
const PaletteContext = React.createContext<() => void>(() => {});

export function usePalette() {
  return React.useContext(PaletteContext);
}

export function Shell({ me, route, children, fullBleed }: { me: Me; route: string; children: React.ReactNode; fullBleed?: boolean }) {
  const [collapsed, setCollapsed] = React.useState(() => readStored(COLLAPSED_KEY, false));
  const [paletteOpen, setPaletteOpen] = React.useState(false);
  const section = route.split("/")[0] || "wiki";

  React.useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
      if ((event.metaKey || event.ctrlKey) && event.key === "\\") {
        event.preventDefault();
        toggle();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const toggle = () => setCollapsed((value) => {
    writeStored(COLLAPSED_KEY, !value);
    return !value;
  });

  const openPalette = React.useCallback(() => setPaletteOpen(true), []);

  return (
    <PaletteContext.Provider value={openPalette}>
    <div className="flex h-dvh overflow-hidden bg-background">
      <motion.aside
        animate={{ width: collapsed ? 64 : 244 }}
        transition={{ type: "spring", bounce: 0, duration: 0.35 }}
        className="relative z-20 flex shrink-0 flex-col border-r bg-sidebar"
      >
        <div className={cn("flex h-14 items-center gap-2.5 px-4", collapsed && "justify-center px-0")}>
          <Logo className="size-7" />
          {!collapsed ? (
            <div className="min-w-0 leading-tight">
              <div className="text-[14px] font-semibold tracking-tight">Stigmergy</div>
              <div className="truncate text-[11px] text-muted-foreground">{me.tenant?.name ?? "Platform"}</div>
            </div>
          ) : null}
        </div>

        {me.operator ? <div className="px-3 pb-2"><WorkspaceSwitcher me={me} collapsed={collapsed} /></div> : null}

        <div className="px-3 pb-2">
          <button
            onClick={() => setPaletteOpen(true)}
            className={cn(
              "flex h-8 w-full items-center gap-2 rounded-lg border bg-background/70 px-2.5 text-[12px] text-muted-foreground shadow-xs transition hover:border-primary/40 hover:text-foreground",
              collapsed && "justify-center px-0",
            )}
          >
            <Search className="size-3.5" />
            {!collapsed ? (<><span className="flex-1 text-left">Search or jump…</span><Kbd>⌘K</Kbd></>) : null}
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-2">
          {visibleGroups(me).map((group) => (
            <div key={group.label} className="mb-4">
              {!collapsed ? (
                <div className="mb-1 px-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground/70">{group.label}</div>
              ) : <div className="mx-auto mb-2 h-px w-6 bg-border" />}
              <ul className="grid gap-0.5">
                {group.items.map((item) => {
                  const active = section === item.route || (item.route === "people" && section === "people");
                  const link = (
                    <a
                      href={`#/${item.route}`}
                      className={cn(
                        "relative flex h-8 items-center gap-2.5 rounded-lg px-2 text-[13px] font-medium text-muted-foreground transition-colors hover:text-foreground",
                        collapsed && "justify-center px-0",
                        active && "text-foreground",
                      )}
                    >
                      {active ? (
                        <motion.span layoutId="nav-active" className="absolute inset-0 rounded-lg border bg-background shadow-sm" transition={{ type: "spring", bounce: 0.15, duration: 0.4 }} />
                      ) : null}
                      <item.icon className={cn("relative size-4 shrink-0", active && "text-primary")} />
                      {!collapsed ? <span className="relative truncate">{item.label}</span> : null}
                    </a>
                  );
                  return <li key={item.route}>{collapsed ? <Tooltip content={item.label} side="right">{link}</Tooltip> : link}</li>;
                })}
              </ul>
            </div>
          ))}
        </nav>

        <div className="border-t p-3">
          <AccountMenu me={me} collapsed={collapsed} />
        </div>

        <button
          onClick={toggle}
          className="absolute -right-3 top-16 z-30 flex size-6 items-center justify-center rounded-full border bg-background text-muted-foreground shadow-sm transition hover:text-foreground"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? <ChevronsRight className="size-3.5" /> : <ChevronsLeft className="size-3.5" />}
        </button>
      </motion.aside>

      <main className={cn("relative min-w-0 flex-1 overflow-y-auto", fullBleed && "overflow-hidden")}>
        {children}
      </main>

      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} me={me} />
    </div>
    </PaletteContext.Provider>
  );
}

function AccountMenu({ me, collapsed }: { me: Me; collapsed: boolean }) {
  const { theme, setTheme } = useTheme();
  const queryClient = useQueryClient();
  const signOut = async () => {
    try {
      await api.del("session");
    } finally {
      queryClient.clear();
      navigate("wiki");
      window.location.reload();
    }
  };
  return (
    <Menu>
      <MenuTrigger className={cn("flex w-full items-center gap-2.5 rounded-lg p-1.5 text-left transition hover:bg-muted", collapsed && "justify-center")}>
        <Avatar name={me.display_name} />
        {!collapsed ? (
          <div className="min-w-0 flex-1 leading-tight">
            <div className="truncate text-[13px] font-medium">{me.display_name}</div>
            <div className="truncate text-[11px] text-muted-foreground">{me.is_admin ? "Administrator" : me.unrestricted ? "Reads everything" : me.groups.join(", ") || "No groups"}</div>
          </div>
        ) : null}
      </MenuTrigger>
      <MenuContent side="top" align="start" className="w-60">
        <MenuLabel>{me.subject}</MenuLabel>
        <MenuItem onSelect={() => navigate("account")}><UserRound />Your account</MenuItem>
        <MenuSeparator />
        <MenuLabel>Theme</MenuLabel>
        {([["light", Sun, "Light"], ["dark", Moon, "Dark"], ["system", Monitor, "System"]] as const).map(([value, Icon, label]) => (
          <MenuItem key={value} onSelect={() => setTheme(value)} className={cn(theme === value && "text-primary [&_svg]:text-primary")}>
            <Icon />{label}
          </MenuItem>
        ))}
        <MenuSeparator />
        <MenuItem onSelect={signOut}><LogOut />Sign out</MenuItem>
      </MenuContent>
    </Menu>
  );
}
