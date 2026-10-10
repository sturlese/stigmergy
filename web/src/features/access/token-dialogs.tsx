import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Bot, KeyRound, Loader2 } from "lucide-react";
import { motion } from "motion/react";
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/controls";
import { CodeBlock, CopyButton } from "@/components/ui/copy";
import { Notice, useToast } from "@/components/ui/feedback";
import { Dialog, DialogContent } from "@/components/ui/overlays";
import { Field, Input } from "@/components/ui/primitives";
import { api, enc } from "@/lib/api";
import type { Person, Token, TokenGrant } from "@/lib/types";
import { errorMessage } from "../ops/common";

export function IssueTokenDialog({ person, open, onOpenChange, onIssued }: {
  person: Person; open: boolean; onOpenChange: (open: boolean) => void; onIssued: (grant: TokenGrant) => void;
}) {
  const queryClient = useQueryClient();
  const [label, setLabel] = React.useState("");
  const [expires, setExpires] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => { if (open) { setLabel(""); setExpires(""); setError(null); } }, [open]);
  const issue = useMutation({
    mutationFn: () => api.post<TokenGrant>(`people/${enc(person.subject)}/tokens`, { label: label.trim(), expires_at: expires || undefined }),
    onSuccess: (grant) => {
      queryClient.invalidateQueries({ queryKey: ["admin", "person", person.subject] });
      queryClient.invalidateQueries({ queryKey: ["admin", "people"] });
      onOpenChange(false);
      onIssued(grant);
    },
    onError: (failure) => setError(errorMessage(failure)),
  });
  const isPerson = person.kind === "person";
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide title={`New token for ${person.display_name}`} description="The token is shown once. Stigmergy keeps only its SHA-256.">
        <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (label.trim()) issue.mutate(); }}>
          <div className="flex items-start gap-3 rounded-xl border bg-muted/50 p-3.5 text-[13px]">
            {isPerson ? <KeyRound className="mt-0.5 size-4 shrink-0 text-primary" /> : <Bot className="mt-0.5 size-4 shrink-0 text-primary" />}
            <span className="text-muted-foreground">
              {isPerson
                ? "One token does everything: it connects Claude Code, Codex, and Claude Desktop, and it signs them in to this console."
                : "An agent's token connects its MCP client and signs it in to read the wiki. Agents never administer this console."}
            </span>
          </div>
          <div className="grid gap-4 sm:grid-cols-[1fr_12rem]">
            <Field label="Label" hint="Where it will live, e.g. “MacBook” or “CI agent”."><Input value={label} onChange={(event) => setLabel(event.target.value)} autoFocus /></Field>
            <Field label="Expires" hint="Optional"><Input type="date" value={expires} onChange={(event) => setExpires(event.target.value)} /></Field>
          </div>
          {error ? <Notice tone="danger">{error}</Notice> : null}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={!label.trim() || issue.isPending}>{issue.isPending ? <Loader2 className="animate-spin" /> : <KeyRound />}Issue token</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function snippets(token: string) {
  const host = window.location.host;
  const bridgeMcp = JSON.stringify({
    mcpServers: {
      "stigmergy-bridge": {
        command: "stigmergy-bridge",
        args: ["--url", `https://${host}`],
        env: { STIGMERGY_TOKEN: "${STIGMERGY_TOKEN}", STIGMERGY_GOOGLE_CLIENT_SECRETS: "${STIGMERGY_GOOGLE_CLIENT_SECRETS:-}" },
      },
    },
  }, null, 2);
  const codexToml = [
    "[mcp_servers.stigmergy-bridge]",
    'command = "stigmergy-bridge"',
    `args = ["--url", "https://${host}"]`,
    'env_vars = ["STIGMERGY_TOKEN", "STIGMERGY_GOOGLE_CLIENT_SECRETS"]',
    "required = true",
  ].join("\n");
  const desktopJson = JSON.stringify({
    mcpServers: {
      stigmergy: {
        command: "npx",
        args: ["-y", "mcp-remote", `https://${host}/mcp`, "--header", "Authorization:${AUTH_HEADER}"],
        env: { AUTH_HEADER: `Bearer ${token}` },
      },
    },
  }, null, 2);
  return { exportLine: `export STIGMERGY_TOKEN="${token}"`, bridgeMcp, codexToml, desktopJson };
}

/** Shows a freshly issued token once; closing drops it from memory. */
export function TokenReveal({ grant, signsIn, onClose, previous, onRevokePrevious }: {
  grant: TokenGrant | null; signsIn: boolean; onClose: () => void; previous?: Token | null;
  onRevokePrevious?: () => Promise<unknown>;
}) {
  const toast = useToast();
  const [revoking, setRevoking] = React.useState(false);
  if (!grant) return <Dialog open={false} />;
  const code = snippets(grant.token);
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent wide title={`Token · ${grant.label}`} description={`For ${grant.subject}`}>
        <div className="flex items-start gap-2.5 rounded-xl border border-warning/30 bg-warning/10 p-3 text-[13px]">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />
          <span>This is the only time the token is shown. Copy it now; Stigmergy cannot show it again.</span>
        </div>
        <motion.div initial={{ opacity: 0, scale: 0.98 }} animate={{ opacity: 1, scale: 1 }} transition={{ type: "spring", bounce: 0.3 }}
          className="relative overflow-hidden rounded-2xl border bg-muted/50 p-4">
          <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">Token</div>
          <div className="mt-2 flex items-center gap-3">
            <code className="min-w-0 flex-1 break-all font-mono text-[14px] font-medium">{grant.token}</code>
            <CopyButton value={grant.token} />
          </div>
        </motion.div>
        {signsIn ? (
          <p className="text-[12.5px] text-muted-foreground">
            Paste it on the sign-in screen to open this console, and use it in any of the clients below.
          </p>
        ) : null}
        <Tabs defaultValue="code">
            <TabsList>
              <TabsTrigger value="code">Claude Code</TabsTrigger>
              <TabsTrigger value="codex">Codex</TabsTrigger>
              <TabsTrigger value="desktop">Claude Desktop</TabsTrigger>
            </TabsList>
            <TabsContent value="code" className="grid gap-3">
              <p className="text-[12.5px] text-muted-foreground">Install the local bridge, export the token, and add it to <code className="font-mono">.mcp.json</code>.</p>
              <CodeBlock code={code.exportLine} />
              <CodeBlock code={code.bridgeMcp} />
            </TabsContent>
            <TabsContent value="codex" className="grid gap-3">
              <p className="text-[12.5px] text-muted-foreground">Export the token, then add the bridge to <code className="font-mono">.codex/config.toml</code>.</p>
              <CodeBlock code={code.exportLine} />
              <CodeBlock code={code.codexToml} />
            </TabsContent>
            <TabsContent value="desktop" className="grid gap-3">
              <p className="text-[12.5px] text-muted-foreground">Add this to <code className="font-mono">claude_desktop_config.json</code> and restart Claude Desktop.</p>
              <CodeBlock code={code.desktopJson} />
            </TabsContent>
          </Tabs>
        <div className="flex items-center justify-end gap-2">
          {previous && onRevokePrevious ? (
            <Button
              variant="destructive"
              disabled={revoking}
              onClick={async () => {
                setRevoking(true);
                try {
                  await onRevokePrevious();
                  toast(`Revoked “${previous.label}”.`);
                  onClose();
                } catch (failure) {
                  toast(errorMessage(failure), "danger");
                } finally {
                  setRevoking(false);
                }
              }}
            >
              {revoking ? <Loader2 className="animate-spin" /> : null}Revoke the old token
            </Button>
          ) : null}
          <Button onClick={onClose}>I've copied it</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
