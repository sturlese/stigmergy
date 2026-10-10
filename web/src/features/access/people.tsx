import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft, Ban, Bot, KeyRound, Loader2, Plus, RefreshCw, Search, ShieldCheck, Trash2, UserRound, Users,
} from "lucide-react";
import { motion } from "motion/react";
import * as React from "react";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/controls";
import { ConfirmDialog } from "@/components/ui/confirm";
import { EmptyState, Notice, Table, Td, Th, Tr, useToast } from "@/components/ui/feedback";
import { Dialog, DialogContent, Tooltip } from "@/components/ui/overlays";
import { Avatar, Badge, Card, Input, Skeleton } from "@/components/ui/primitives";
import { api, enc } from "@/lib/api";
import { navigate } from "@/lib/router";
import type { Person, Token, TokenGrant } from "@/lib/types";
import { cn, fromNow, onDay, when } from "@/lib/utils";
import { PAGE, errorMessage, useGroups } from "../ops/common";
import { EMPTY_PERSON, PersonFields, changedFields, type PersonDraft } from "./person-form";
import { IssueTokenDialog, TokenReveal } from "./token-dialogs";

export function PeopleView({ route }: { route: string }) {
  const subject = route.split("/")[1] ? decodeURIComponent(route.split("/").slice(1).join("/")) : null;
  return subject ? <PersonView subject={subject} /> : <PeopleList />;
}

function PeopleList() {
  const people = useQuery({ queryKey: ["admin", "people"], queryFn: () => api.get<{ people: Person[] }>("people") });
  const [query, setQuery] = React.useState("");
  const [creating, setCreating] = React.useState(false);
  const all = people.data?.people ?? [];
  const shown = query.trim()
    ? all.filter((person) => `${person.display_name} ${person.subject} ${person.groups.join(" ")}`.toLowerCase().includes(query.toLowerCase()))
    : all;
  const admins = all.filter((person) => person.is_admin).length;
  const agents = all.filter((person) => person.kind === "agent").length;

  return (
    <div className={PAGE}>
      <PageHeader
        eyebrow="Access"
        title="People"
        description="Everyone and everything that reads or writes the brain: their groups, their role, and their tokens."
        actions={<Button onClick={() => setCreating(true)}><Plus />New person</Button>}
      />
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="relative w-full max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search people, emails, groups" className="pl-9" />
        </div>
        <div className="flex gap-2 text-[12px] text-muted-foreground">
          <Badge>{all.length} total</Badge>
          <Badge tone="primary"><ShieldCheck />{admins} admin{admins === 1 ? "" : "s"}</Badge>
          <Badge><Bot />{agents} agent{agents === 1 ? "" : "s"}</Badge>
        </div>
      </div>
      <Card className="overflow-hidden">
        {people.isLoading ? <div className="grid gap-3 p-5">{Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="h-10" />)}</div> : null}
        {people.isError ? <Notice tone="danger" className="m-5">{errorMessage(people.error)}</Notice> : null}
        {!people.isLoading && !shown.length ? <EmptyState icon={Users} title={query ? "Nobody matches" : "Nobody here yet"} className="m-5 border-0" /> : null}
        {shown.length ? (
          <Table>
            <thead><tr><Th>Name</Th><Th>Kind</Th><Th>Groups</Th><Th className="text-right">Tokens</Th><Th>Last used</Th><Th>Status</Th></tr></thead>
            <tbody>
              {shown.map((person, index) => (
                <motion.tr
                  key={person.subject}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ delay: Math.min(index, 15) * 0.02 }}
                  onClick={() => navigate(`people/${enc(person.subject)}`)}
                  className="group/row cursor-pointer transition-colors hover:bg-muted/50"
                >
                  <Td>
                    <div className="flex items-center gap-3">
                      <Avatar name={person.display_name} />
                      <div className="min-w-0">
                        <div className="flex items-center gap-1.5 font-medium">{person.display_name}{person.is_admin ? <ShieldCheck className="size-3.5 text-primary" /> : null}</div>
                        <div className="truncate text-[12px] text-muted-foreground">{person.subject}</div>
                      </div>
                    </div>
                  </Td>
                  <Td><Badge tone={person.kind === "agent" ? "entity" : "neutral"}>{person.kind === "agent" ? <Bot /> : <UserRound />}{person.kind}</Badge></Td>
                  <Td>
                    <div className="flex max-w-72 flex-wrap gap-1">
                      {person.groups.length ? person.groups.map((group) => <Badge key={group} tone={group === "brain-admins" ? "primary" : "neutral"}>{group}</Badge>) : <span className="text-muted-foreground">—</span>}
                    </div>
                  </Td>
                  <Td className="text-right tabular-nums">{person.live_tokens}</Td>
                  <Td className="whitespace-nowrap text-muted-foreground">{person.last_used_at ? fromNow(person.last_used_at) : "Never"}</Td>
                  <Td>{person.disabled ? <Badge tone="danger">disabled</Badge> : <Badge tone="success">active</Badge>}</Td>
                </motion.tr>
              ))}
            </tbody>
          </Table>
        ) : null}
      </Card>
      <NewPersonDialog open={creating} onOpenChange={setCreating} />
    </div>
  );
}

function NewPersonDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const groups = useGroups();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [draft, setDraft] = React.useState<PersonDraft>(EMPTY_PERSON);
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => { if (open) { setDraft(EMPTY_PERSON); setError(null); } }, [open]);
  const create = useMutation({
    mutationFn: () => api.post<{ person: Person }>("people", { ...draft, subject: draft.subject.trim(), display_name: draft.display_name.trim() }),
    onSuccess: (result) => {
      toast(`${result.person.display_name} added.`);
      queryClient.invalidateQueries({ queryKey: ["admin", "people"] });
      onOpenChange(false);
      navigate(`people/${enc(result.person.subject)}`);
    },
    onError: (failure) => setError(errorMessage(failure)),
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide title="New person" description="Add someone, or an agent, and choose what they can read.">
        <form className="grid gap-5" onSubmit={(event) => { event.preventDefault(); create.mutate(); }}>
          <PersonFields draft={draft} onChange={setDraft} groups={(groups.data?.groups ?? []).map((group) => group.name)} creating />
          {error ? <Notice tone="danger">{error}</Notice> : null}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={!draft.subject.trim() || !draft.display_name.trim() || create.isPending}>
              {create.isPending ? <Loader2 className="animate-spin" /> : <Plus />}Create
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function PersonView({ subject }: { subject: string }) {
  const person = useQuery({
    queryKey: ["admin", "person", subject],
    queryFn: () => api.get<{ person: Person; tokens: Token[] }>(`people/${enc(subject)}`),
  });
  const data = person.data;
  return (
    <div className={PAGE}>
      <a href="#/people" className="mb-5 inline-flex items-center gap-1.5 text-[12.5px] text-muted-foreground transition hover:text-foreground"><ArrowLeft className="size-3.5" />All people</a>
      {person.isLoading ? <Skeleton className="h-24 rounded-2xl" /> : null}
      {person.isError ? <EmptyState icon={Users} title="Not found">{errorMessage(person.error)}</EmptyState> : null}
      {data ? <PersonDetail person={data.person} tokens={data.tokens} /> : null}
    </div>
  );
}

function PersonDetail({ person, tokens }: { person: Person; tokens: Token[] }) {
  const liveTokens = tokens.filter((token) => token.live).length;
  return (
    <>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="mb-8 flex flex-wrap items-center gap-5">
        <Avatar name={person.display_name} className="size-16 text-lg" />
        <div className="min-w-0 flex-1">
          <h1 className="text-[26px] font-semibold tracking-[-0.025em]">{person.display_name}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[13px] text-muted-foreground">
            <span>{person.subject}</span>
            <Badge tone={person.kind === "agent" ? "entity" : "neutral"}>{person.kind}</Badge>
            {person.is_admin ? <Badge tone="primary"><ShieldCheck />Administrator</Badge> : null}
            {person.unrestricted ? <Badge tone="primary">Reads everything</Badge> : null}
            {person.disabled ? <Badge tone="danger">Disabled</Badge> : <Badge tone="success">Active</Badge>}
          </div>
        </div>
      </motion.div>
      <Tabs defaultValue="profile">
        <TabsList>
          <TabsTrigger value="profile">Profile</TabsTrigger>
          <TabsTrigger value="tokens">Tokens <span className="ml-1 rounded-full bg-muted px-1.5 text-[10.5px] tabular-nums">{liveTokens}</span></TabsTrigger>
          <TabsTrigger value="danger">Danger zone</TabsTrigger>
        </TabsList>
        <TabsContent value="profile"><ProfileTab person={person} /></TabsContent>
        <TabsContent value="tokens"><TokensTab person={person} tokens={tokens} /></TabsContent>
        <TabsContent value="danger"><DangerTab person={person} /></TabsContent>
      </Tabs>
    </>
  );
}

function toDraft(person: Person): PersonDraft {
  return {
    subject: person.subject, display_name: person.display_name, kind: person.kind, is_admin: person.is_admin,
    groups: person.groups, default_audience: person.default_audience,
  };
}

function ProfileTab({ person }: { person: Person }) {
  const groups = useGroups();
  const toast = useToast();
  const queryClient = useQueryClient();
  const initial = React.useMemo(() => toDraft(person), [person]);
  const [draft, setDraft] = React.useState<PersonDraft>(initial);
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => setDraft(initial), [initial]);
  const changes = changedFields(initial, draft);
  const dirty = Object.keys(changes).length > 0;
  const save = useMutation({
    mutationFn: () => api.put(`people/${enc(person.subject)}`, changes),
    onSuccess: () => {
      toast("Saved.");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["admin", "person", person.subject] });
      queryClient.invalidateQueries({ queryKey: ["admin", "people"] });
    },
    onError: (failure) => setError(errorMessage(failure)),
  });
  return (
    <Card className="p-6">
      <form className="grid gap-5" onSubmit={(event) => { event.preventDefault(); if (dirty) save.mutate(); }}>
        <PersonFields draft={draft} onChange={setDraft} groups={(groups.data?.groups ?? []).map((group) => group.name)} creating={false} />
        {error ? <Notice tone="danger">{error}</Notice> : null}
        <div className="flex items-center justify-end gap-2">
          {dirty ? <span className="mr-auto text-[12px] text-muted-foreground">Unsaved changes: {Object.keys(changes).join(", ").replaceAll("_", " ")}</span> : null}
          <Button type="button" variant="ghost" disabled={!dirty} onClick={() => setDraft(initial)}>Discard</Button>
          <Button type="submit" disabled={!dirty || save.isPending}>{save.isPending ? <Loader2 className="animate-spin" /> : null}Save changes</Button>
        </div>
      </form>
    </Card>
  );
}

function TokensTab({ person, tokens }: { person: Person; tokens: Token[] }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [issuing, setIssuing] = React.useState(false);
  const [grant, setGrant] = React.useState<TokenGrant | null>(null);
  const [previous, setPrevious] = React.useState<Token | null>(null);
  const [revoking, setRevoking] = React.useState<Token | null>(null);
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["admin", "person", person.subject] });
    queryClient.invalidateQueries({ queryKey: ["admin", "people"] });
  };
  const revoke = (token: Token) => api.del(`tokens/${enc(token.id)}`).then(refresh);
  const regenerate = async (token: Token) => {
    try {
      const fresh = await api.post<TokenGrant>(`people/${enc(person.subject)}/tokens`, { label: `${token.label} (regenerated)` });
      refresh();
      setPrevious(token);
      setGrant(fresh);
    } catch (failure) {
      toast(errorMessage(failure), "danger");
    }
  };
  const status = (token: Token) => token.revoked_at ? <Badge tone="danger">revoked</Badge> : token.live ? <Badge tone="success">live</Badge> : <Badge tone="warning">expired</Badge>;

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center justify-between gap-4 px-5 pb-3 pt-5">
        <div>
          <div className="text-[13px] font-semibold">Tokens</div>
          <div className="text-[12px] text-muted-foreground">Only the prefix is kept visible. A lost token is revoked and replaced, never recovered.</div>
        </div>
        <Button onClick={() => setIssuing(true)} disabled={person.disabled}><KeyRound />New token</Button>
      </div>
      {tokens.length ? (
        <Table>
          <thead><tr><Th>Label</Th><Th>Prefix</Th><Th>Created</Th><Th>Last used</Th><Th>Expires</Th><Th>Status</Th><Th /></tr></thead>
          <tbody>
            {tokens.map((token) => (
              <Tr key={token.id} className={cn(!token.live && "opacity-60")}>
                <Td className="font-medium">{token.label}</Td>
                <Td><code className="font-mono text-[12px] text-muted-foreground">{token.prefix}…</code></Td>
                <Td className="whitespace-nowrap text-muted-foreground" title={when(token.created_at)}>{onDay(token.created_at)}</Td>
                <Td className="whitespace-nowrap text-muted-foreground">{token.last_used_at ? fromNow(token.last_used_at) : "Never"}</Td>
                <Td className="whitespace-nowrap text-muted-foreground">{token.expires_at ? onDay(token.expires_at) : "Never"}</Td>
                <Td>{status(token)}</Td>
                <Td className="text-right">
                  {token.live ? (
                    <div className="flex justify-end gap-1">
                      <Tooltip content="Issue a replacement"><Button variant="ghost" size="icon-sm" onClick={() => regenerate(token)} aria-label="Regenerate"><RefreshCw /></Button></Tooltip>
                      <Tooltip content="Revoke"><Button variant="ghost" size="icon-sm" onClick={() => setRevoking(token)} aria-label="Revoke" className="hover:text-destructive"><Ban /></Button></Tooltip>
                    </div>
                  ) : null}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      ) : <EmptyState icon={KeyRound} title="No tokens yet" className="m-5">{person.kind === "person" ? "One token connects their MCP clients and signs them in to the console." : "A token connects this agent's MCP client and signs it in to read the wiki."}</EmptyState>}

      <IssueTokenDialog person={person} open={issuing} onOpenChange={setIssuing} onIssued={(fresh) => { setPrevious(null); setGrant(fresh); }} />
      <TokenReveal
        grant={grant}
        signsIn={person.kind === "person"}
        previous={previous}
        onRevokePrevious={previous ? () => revoke(previous) : undefined}
        onClose={() => { setGrant(null); setPrevious(null); }}
      />
      <ConfirmDialog
        open={!!revoking}
        onOpenChange={(open) => { if (!open) setRevoking(null); }}
        title="Revoke token"
        description={revoking ? `Revoke “${revoking.label}” (${revoking.prefix}…)? Every client and session using it stops working on its next request.` : undefined}
        confirmLabel="Revoke"
        destructive
        onConfirm={async () => { if (revoking) { await revoke(revoking); toast("Token revoked."); } }}
      />
    </Card>
  );
}

function DangerTab({ person }: { person: Person }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [confirming, setConfirming] = React.useState<"disable" | "delete" | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const toggle = async () => {
    await api.put(`people/${enc(person.subject)}/disabled`, { disabled: !person.disabled });
    toast(person.disabled ? "Enabled." : "Disabled. Their tokens and sessions stop working now.");
    queryClient.invalidateQueries({ queryKey: ["admin", "person", person.subject] });
    queryClient.invalidateQueries({ queryKey: ["admin", "people"] });
  };
  const remove = async () => {
    await api.del(`people/${enc(person.subject)}`);
    toast(`${person.display_name} deleted.`);
    queryClient.invalidateQueries({ queryKey: ["admin", "people"] });
    navigate("people");
  };
  return (
    <div className="grid gap-3">
      {error ? <Notice tone="danger">{error}</Notice> : null}
      <Card className="flex flex-wrap items-center justify-between gap-4 p-5">
        <div>
          <div className="text-[13.5px] font-medium">{person.disabled ? "Enable access" : "Disable access"}</div>
          <div className="text-[12.5px] text-muted-foreground">
            {person.disabled ? "Their existing tokens work again." : "Every token and session stops on the next request. Nothing is deleted."}
          </div>
        </div>
        {person.disabled
          ? <Button onClick={() => toggle().catch((failure) => setError(errorMessage(failure)))}>Enable</Button>
          : <Button variant="secondary" onClick={() => setConfirming("disable")}><Ban />Disable</Button>}
      </Card>
      <Card className="flex flex-wrap items-center justify-between gap-4 border-destructive/30 p-5">
        <div>
          <div className="text-[13.5px] font-medium text-destructive">Delete {person.kind}</div>
          <div className="text-[12.5px] text-muted-foreground">Removes them and every token. Their past captures and commits stay in history.</div>
        </div>
        <Button variant="destructive" onClick={() => setConfirming("delete")}><Trash2 />Delete</Button>
      </Card>
      <ConfirmDialog
        open={confirming === "disable"}
        onOpenChange={(open) => { if (!open) setConfirming(null); }}
        title={`Disable ${person.display_name}?`}
        description="Every token and session they hold stops working on its next request."
        confirmLabel="Disable"
        destructive
        onConfirm={toggle}
      />
      <ConfirmDialog
        open={confirming === "delete"}
        onOpenChange={(open) => { if (!open) setConfirming(null); }}
        title={`Delete ${person.display_name}?`}
        description={`${person.subject} and all of their tokens are removed. This cannot be undone.`}
        confirmLabel="Delete"
        destructive
        onConfirm={remove}
      />
    </div>
  );
}
