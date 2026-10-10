import { Command } from "cmdk";
import { Dialog as DialogPrimitive } from "radix-ui";
import { CornerDownLeft, FileSearch, Moon, Network, Sun } from "lucide-react";
import * as React from "react";
import { useCatalog, useSearch } from "@/lib/queries";
import { navigate, wikiHref } from "@/lib/router";
import type { Me } from "@/lib/types";
import { visibleGroups } from "./nav";
import { useTheme } from "./theme";
import { TYPE_META, TypeIcon } from "./type";
import { Kbd } from "./ui/primitives";

export function CommandPalette({ open, onOpenChange, me }: { open: boolean; onOpenChange: (open: boolean) => void; me: Me }) {
  const [query, setQuery] = React.useState("");
  const [debounced, setDebounced] = React.useState("");
  const catalog = useCatalog(!!me.tenant);
  const search = useSearch(open ? debounced : "", !!me.tenant);
  const { resolved, setTheme } = useTheme();

  React.useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query), 260);
    return () => window.clearTimeout(timer);
  }, [query]);
  React.useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  const go = (target: string) => {
    onOpenChange(false);
    navigate(target);
  };
  const pages = catalog.data?.pages ?? [];
  const titles = new Set(pages.map((page) => page.path));
  const contentHits = (search.data?.hits ?? []).filter((hit) => titles.has(hit.path) || hit.type === "source");

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/40 data-[state=open]:animate-[fade-in_160ms_ease-out]" />
        <DialogPrimitive.Content className="fixed left-1/2 top-[14vh] z-50 w-[min(42rem,calc(100vw-2rem))] -translate-x-1/2 overflow-hidden rounded-2xl border bg-popover shadow-2xl outline-none data-[state=open]:animate-[dialog-in_200ms_cubic-bezier(0.16,1,0.3,1)]">
          <DialogPrimitive.Title className="sr-only">Search</DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">Search pages, jump to a section, or run an action.</DialogPrimitive.Description>
          <Command loop className="flex max-h-[60vh] flex-col" filter={(value, search, keywords) => {
            const haystack = `${value} ${(keywords ?? []).join(" ")}`.toLowerCase();
            return search.toLowerCase().split(/\s+/).every((term) => haystack.includes(term)) ? 1 : 0;
          }}>
            <div className="flex items-center gap-3 border-b px-4">
              <FileSearch className="size-4 text-muted-foreground" />
              <Command.Input
                value={query}
                onValueChange={setQuery}
                placeholder="Search pages, people, sections…"
                className="h-13 flex-1 bg-transparent text-[15px] outline-none placeholder:text-muted-foreground/70"
              />
              <Kbd>esc</Kbd>
            </div>
            <Command.List className="overflow-y-auto p-2 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[0.14em] [&_[cmdk-group-heading]]:text-muted-foreground/70">
              <Command.Empty className="px-3 py-10 text-center text-[13px] text-muted-foreground">
                {search.isFetching ? "Searching the brain…" : "Nothing matches. Try other words."}
              </Command.Empty>
              <Command.Group heading="Pages">
                {pages.slice(0, query ? 400 : 8).map((page) => (
                  <Command.Item
                    key={page.path}
                    value={`${page.title} ${page.path}`}
                    keywords={[...page.aliases, page.entity_type, TYPE_META[page.type].label]}
                    onSelect={() => go(wikiHref(page.path))}
                    className="group flex cursor-default items-center gap-3 rounded-lg px-2.5 py-2 text-[13px] data-[selected=true]:bg-muted"
                  >
                    <TypeIcon type={page.type} entityType={page.entity_type} />
                    <span className="min-w-0 flex-1 truncate">{page.title}</span>
                    {page.aliases.length ? <span className="hidden truncate text-[11px] text-muted-foreground sm:inline">{page.aliases.slice(0, 2).join(" · ")}</span> : null}
                    <CornerDownLeft className="size-3.5 text-muted-foreground opacity-0 group-data-[selected=true]:opacity-100" />
                  </Command.Item>
                ))}
              </Command.Group>
              {contentHits.length ? (
                <Command.Group heading="Found in content">
                  {contentHits.map((hit) => (
                    <Command.Item
                      key={`hit-${hit.path}`}
                      value={`content ${hit.title} ${hit.path} ${query}`}
                      keywords={[query]}
                      onSelect={() => go(wikiHref(hit.path))}
                      className="flex cursor-default items-start gap-3 rounded-lg px-2.5 py-2 text-[13px] data-[selected=true]:bg-muted"
                    >
                      <TypeIcon type={hit.type} className="mt-0.5" />
                      <div className="min-w-0">
                        <div className="truncate font-medium">{hit.title}</div>
                        <div className="line-clamp-1 text-[12px] text-muted-foreground">{hit.summary || hit.snippet}</div>
                      </div>
                    </Command.Item>
                  ))}
                </Command.Group>
              ) : null}
              <Command.Group heading="Go to">
                {visibleGroups(me).flatMap((group) => group.items).map((item) => (
                  <Command.Item
                    key={item.route}
                    value={`go ${item.label}`}
                    keywords={[item.description]}
                    onSelect={() => go(item.route)}
                    className="flex cursor-default items-center gap-3 rounded-lg px-2.5 py-2 text-[13px] data-[selected=true]:bg-muted"
                  >
                    <item.icon className="size-4 text-muted-foreground" />
                    <span className="flex-1">{item.label}</span>
                    <span className="text-[11px] text-muted-foreground">{item.description}</span>
                  </Command.Item>
                ))}
              </Command.Group>
              <Command.Group heading="Actions">
                <Command.Item value="open knowledge graph" onSelect={() => go("graph")} className="flex cursor-default items-center gap-3 rounded-lg px-2.5 py-2 text-[13px] data-[selected=true]:bg-muted">
                  <Network className="size-4 text-muted-foreground" />Open the knowledge graph
                </Command.Item>
                <Command.Item
                  value="toggle theme dark light"
                  onSelect={() => { setTheme(resolved === "dark" ? "light" : "dark"); onOpenChange(false); }}
                  className="flex cursor-default items-center gap-3 rounded-lg px-2.5 py-2 text-[13px] data-[selected=true]:bg-muted"
                >
                  {resolved === "dark" ? <Sun className="size-4 text-muted-foreground" /> : <Moon className="size-4 text-muted-foreground" />}
                  Switch to {resolved === "dark" ? "light" : "dark"} theme
                </Command.Item>
              </Command.Group>
            </Command.List>
            <div className="flex items-center gap-4 border-t px-4 py-2 text-[11px] text-muted-foreground">
              <span className="flex items-center gap-1.5"><Kbd>↑</Kbd><Kbd>↓</Kbd> move</span>
              <span className="flex items-center gap-1.5"><Kbd>↵</Kbd> open</span>
              <span className="ml-auto">Full-text results appear after three letters</span>
            </div>
          </Command>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
