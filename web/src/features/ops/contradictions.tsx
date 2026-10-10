import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, Scale } from "lucide-react";
import { motion } from "motion/react";
import * as React from "react";
import { PageHeader } from "@/components/page-header";
import { LayerBadge, layerOf } from "@/components/ui/audience";
import { Button } from "@/components/ui/button";
import { Dropzone } from "@/components/ui/dropzone";
import { EmptyState, Notice, useToast } from "@/components/ui/feedback";
import { Dialog, DialogContent } from "@/components/ui/overlays";
import { Badge, Card, Field, Input, Skeleton, Textarea } from "@/components/ui/primitives";
import { api, enc } from "@/lib/api";
import { navigate, wikiHref } from "@/lib/router";
import type { CaptureReceipt, Contradiction } from "@/lib/types";
import { cn, onDay } from "@/lib/utils";
import { PAGE, errorMessage } from "./common";

const DECISIONS = [
  { value: "claim_a", label: "Claim A holds", hint: "The first claim is right" },
  { value: "claim_b", label: "Claim B holds", hint: "The second claim is right" },
  { value: "neither", label: "Neither / add context", hint: "Both need nuance" },
  { value: "custom", label: "Custom resolution", hint: "Write the answer" },
];

export function ContradictionsView() {
  const contradictions = useQuery({ queryKey: ["admin", "contradictions"], queryFn: () => api.get<{ contradictions: Contradiction[] }>("contradictions") });
  const [resolving, setResolving] = React.useState<Contradiction | null>(null);
  const rows = contradictions.data?.contradictions ?? [];
  return (
    <div className={PAGE}>
      <PageHeader
        eyebrow="Operations"
        title="Contradictions"
        description="Where the brain holds conflicting claims. A resolution is filed like any other capture, with its rationale and evidence."
      />
      {contradictions.isLoading ? <div className="grid gap-4">{Array.from({ length: 3 }, (_, i) => <Skeleton key={i} className="h-48 rounded-2xl" />)}</div> : null}
      {contradictions.isError ? <Notice tone="danger">{errorMessage(contradictions.error)}</Notice> : null}
      {!contradictions.isLoading && !rows.length ? (
        <EmptyState icon={CheckCircle2} title="No open contradictions">Every claim in the corpus currently agrees with the others.</EmptyState>
      ) : null}
      <div className="grid gap-4">
        {rows.map((item, index) => (
          <motion.div key={item.id} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * 0.05 }}>
            <Card className="p-5">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <Badge tone="concept"><Scale />Contradiction</Badge>
                    <LayerBadge layer={layerOf(item.acl)} />
                    <code className="font-mono text-[11px] text-muted-foreground">{item.id}</code>
                  </div>
                  <p className="mt-3 max-w-3xl font-serif text-[16px] leading-relaxed">{item.explanation}</p>
                </div>
                <Button onClick={() => setResolving(item)}>Resolve</Button>
              </div>
              <div className="relative mt-5 grid gap-3 md:grid-cols-2">
                {item.claims.map((claim, claimIndex) => (
                  <div key={claimIndex} className="rounded-xl border bg-background/60 p-4">
                    <div className="mb-2 flex items-center gap-2">
                      <span className={cn("flex size-6 items-center justify-center rounded-full text-[11px] font-semibold text-white", claimIndex ? "bg-note" : "bg-concept")}>
                        {String.fromCharCode(65 + claimIndex)}
                      </span>
                      <span className="text-[11.5px] text-muted-foreground">{claim.date ? onDay(claim.date) : "Undated"}</span>
                    </div>
                    <p className="text-[13.5px] leading-relaxed">{claim.text}</p>
                    <div className="mt-2 break-all font-mono text-[11px] text-muted-foreground">{claim.source}</div>
                  </div>
                ))}
                {item.claims.length === 2 ? (
                  <span className="absolute left-1/2 top-1/2 hidden size-8 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border bg-card text-[10px] font-semibold text-muted-foreground shadow-sm md:flex">VS</span>
                ) : null}
              </div>
              <div className="mt-4 flex flex-wrap items-center gap-1.5 text-[12px] text-muted-foreground">
                <span>Appears on</span>
                {item.paths.map((page) => (
                  <a key={page.path} href={wikiHref(page.path)} className="rounded-md border bg-muted/60 px-1.5 py-0.5 text-foreground/80 transition hover:border-primary/40 hover:text-foreground">{page.title}</a>
                ))}
              </div>
            </Card>
          </motion.div>
        ))}
      </div>
      <ResolveDialog item={resolving} onClose={() => setResolving(null)} />
    </div>
  );
}

function ResolveDialog({ item, onClose }: { item: Contradiction | null; onClose: () => void }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [decision, setDecision] = React.useState("claim_a");
  const [resolution, setResolution] = React.useState("");
  const [rationale, setRationale] = React.useState("");
  const [url, setUrl] = React.useState("");
  const [file, setFile] = React.useState<File | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => {
    setDecision("claim_a"); setResolution(""); setRationale(""); setUrl(""); setFile(null); setError(null);
  }, [item?.id]);

  const submit = useMutation({
    mutationFn: () => {
      const common = { contradiction_id: item!.id, decision, resolution, rationale };
      if (file) {
        const body = new FormData();
        for (const [key, value] of Object.entries(common)) body.append(key, value);
        body.append("file", file);
        return api.form<CaptureReceipt>("contradictions/resolve-file", body);
      }
      return api.post<CaptureReceipt>("contradictions/resolve", { ...common, support_url: url || undefined });
    },
    onSuccess: (receipt) => {
      toast("Resolution queued as a new capture.");
      queryClient.invalidateQueries({ queryKey: ["admin"] });
      onClose();
      navigate(`captures/${enc(receipt.id)}`);
    },
    onError: (failure) => setError(errorMessage(failure)),
  });

  return (
    <Dialog open={!!item} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent wide title="Resolve the contradiction" description={item?.explanation}>
        <div className="grid grid-cols-2 gap-2">
          {DECISIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => setDecision(option.value)}
              className={cn(
                "rounded-xl border p-3 text-left transition",
                decision === option.value ? "border-primary/60 bg-primary/6 shadow-[0_0_0_3px_var(--glow)]" : "hover:bg-muted/60",
              )}
            >
              <div className="text-[13px] font-medium">{option.label}</div>
              <div className="text-[11.5px] text-muted-foreground">{option.hint}</div>
            </button>
          ))}
        </div>
        <Field label="Resolution" hint="The statement the corpus should hold from now on.">
          <Textarea rows={4} value={resolution} onChange={(event) => setResolution(event.target.value)} />
        </Field>
        <Field label="Rationale" hint="Why this is right. It is recorded with the capture.">
          <Textarea rows={3} value={rationale} onChange={(event) => setRationale(event.target.value)} />
        </Field>
        <Field label="Supporting evidence" hint="Optional: a public URL, or a file.">
          <div className="grid gap-2">
            <Input type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://…" disabled={!!file} />
            <Dropzone file={file} onFile={setFile} className="min-h-24 py-5" />
          </div>
        </Field>
        {error ? <Notice tone="danger">{error}</Notice> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button disabled={!resolution.trim() || !rationale.trim() || submit.isPending} onClick={() => submit.mutate()}>
            {submit.isPending ? <Loader2 className="animate-spin" /> : null}Queue resolution
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
