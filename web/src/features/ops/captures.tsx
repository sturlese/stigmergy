import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUpRight, BookOpen, CheckCircle2, CircleDashed, ClipboardPaste, FileUp, GitCompare, Inbox, Link2, Loader2,
  Plus, RotateCcw, Sparkles, Trash2, XCircle,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import * as React from "react";
import { PageHeader } from "@/components/page-header";
import { LayerBadge, LayerField, PUBLIC_LAYER, layerOf } from "@/components/ui/audience";
import { Button } from "@/components/ui/button";
import { Segmented, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/controls";
import { Dropzone } from "@/components/ui/dropzone";
import { Collapsible, EmptyState, Notice, useToast } from "@/components/ui/feedback";
import { Dialog, DialogContent, Sheet, SheetContent } from "@/components/ui/overlays";
import { Badge, Card, Field, Input, Skeleton, Textarea } from "@/components/ui/primitives";
import { Definitions, Sha } from "@/components/ui/stat";
import { api, enc } from "@/lib/api";
import { useMe } from "@/lib/queries";
import { navigate, wikiHref } from "@/lib/router";
import type { CaptureDetail, CaptureReceipt, CaptureRow, CaptureStatus } from "@/lib/types";
import { bytes, cn, fromNow, when } from "@/lib/utils";
import { PAGE, errorMessage, statusTone, useMeta } from "./common";

type Filter = "all" | CaptureStatus | "classified";

const LAYER_DECISION_LABEL: Record<string, string> = {
  explicit: "named by the sender",
  channel: "set by the Slack channel",
  default: "sender's default",
  classified: "chosen by the classifier",
};

function capturesQuery(filter: Filter): string {
  if (filter === "all") return "captures?limit=100";
  return filter === "classified" ? "captures?limit=100&decision=classified" : `captures?limit=100&status=${filter}`;
}

const STATUS_ICON: Record<CaptureStatus, typeof Inbox> = {
  queued: CircleDashed, processing: Loader2, landed: CheckCircle2, failed: XCircle,
};

export function CapturesView({ route }: { route: string }) {
  const selected = route.split("/")[1] ? decodeURIComponent(route.split("/")[1]) : null;
  const [filter, setFilter] = React.useState<Filter>("all");
  const [composing, setComposing] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const me = useMe();
  const captures = useQuery({
    queryKey: ["admin", "captures", filter],
    queryFn: () => api.get<{ captures: CaptureRow[] }>(capturesQuery(filter)),
    refetchInterval: (query) => (query.state.data?.captures.some((item) => item.status === "queued" || item.status === "processing") ? 4000 : false),
  });
  const rows = captures.data?.captures ?? [];

  return (
    <div className={PAGE}>
      <PageHeader
        eyebrow="Operations"
        title="Captures"
        description="Everything submitted to the brain, from the moment it arrives until it lands as a commit."
        actions={
          me.data?.operator ? null : (
            <>
              <Button variant="ghost" size="sm" onClick={() => setDeleting(true)}><Trash2 />Delete knowledge</Button>
              <Button onClick={() => setComposing(true)}><Plus />New capture</Button>
            </>
          )
        }
      />
      <div className="mb-4 flex items-center justify-between gap-3">
        <Segmented
          layoutId="capture-filter"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: "All" },
            { value: "queued", label: "Queued" },
            { value: "processing", label: "Processing" },
            { value: "landed", label: "Landed" },
            { value: "failed", label: "Failed" },
            { value: "classified", label: "Classified" },
          ]}
        />
        {captures.isFetching && !captures.isLoading ? <Loader2 className="size-4 animate-spin text-muted-foreground" /> : null}
      </div>

      <Card className="overflow-hidden">
        {captures.isLoading ? <div className="grid gap-3 p-5">{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-10" />)}</div> : null}
        {!captures.isLoading && !rows.length ? (
          <EmptyState icon={Inbox} title={filter === "all" ? "Nothing captured yet" : `No ${filter} captures`} className="m-5 border-0">
            Paste text, upload a document, or point at a public URL to start.
          </EmptyState>
        ) : null}
        <div className="divide-y">
          <AnimatePresence initial={false}>
            {rows.map((item, index) => {
              const Icon = STATUS_ICON[item.status] ?? CircleDashed;
              return (
                <motion.a
                  key={item.id}
                  layout
                  href={`#/captures/${enc(item.id)}`}
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  transition={{ delay: Math.min(index, 12) * 0.025 }}
                  className="group flex items-center gap-4 px-5 py-3.5 transition-colors hover:bg-muted/50"
                >
                  <div className={cn("flex size-9 shrink-0 items-center justify-center rounded-xl border bg-background",
                    item.status === "failed" ? "text-destructive" : item.status === "landed" ? "text-success" : item.status === "processing" ? "text-warning" : "text-note")}>
                    <Icon className={cn("size-4", item.status === "processing" && "animate-spin")} />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[13.5px] font-medium">{item.title || item.operation}</div>
                    <div className="mt-0.5 flex items-center gap-2 text-[11.5px] text-muted-foreground">
                      <span>{item.adapter || item.operation}</span>
                      <span>·</span>
                      <span className="truncate">{item.submitted_by}</span>
                      <span>·</span>
                      <span className="inline-flex items-center gap-1" title={item.layer_decision ? LAYER_DECISION_LABEL[item.layer_decision] : undefined}>
                        {layerOf(item.audience)}
                        {item.layer_decision === "classified" ? <Sparkles className="size-3 text-primary" /> : null}
                      </span>
                    </div>
                  </div>
                  <Badge tone={statusTone(item.status)}>{item.status}</Badge>
                  <span className="hidden w-28 text-right text-[12px] text-muted-foreground sm:block">{fromNow(item.created_at)}</span>
                </motion.a>
              );
            })}
          </AnimatePresence>
        </div>
      </Card>

      <CaptureSheet id={selected} onClose={() => navigate("captures")} />
      <CaptureComposer open={composing} onOpenChange={setComposing} />
      <DeleteComposer open={deleting} onOpenChange={setDeleting} />
    </div>
  );
}

function CaptureSheet({ id, onClose }: { id: string | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const capture = useQuery({
    queryKey: ["admin", "capture", id],
    queryFn: () => api.get<CaptureDetail>(`captures/${enc(id ?? "")}`),
    enabled: !!id,
    refetchInterval: (query) => (["queued", "processing"].includes(query.state.data?.status ?? "") ? 3000 : false),
  });
  const retry = useMutation({
    mutationFn: () => api.post<CaptureReceipt>(`captures/${enc(id ?? "")}/retry`),
    onSuccess: () => {
      toast("Capture requeued.");
      queryClient.invalidateQueries({ queryKey: ["admin"] });
    },
    onError: (error) => toast(errorMessage(error), "danger"),
  });
  const item = capture.data;
  const acquisition = item?.provenance?.acquisition;
  const [showOutcome, setShowOutcome] = React.useState(false);

  return (
    <Sheet open={!!id} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent title={item?.title || item?.operation || "Capture"} description={id ?? undefined}>
        {capture.isLoading ? <div className="grid gap-3">{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-6" />)}</div> : null}
        {capture.isError ? <Notice tone="danger">{errorMessage(capture.error)}</Notice> : null}
        {item ? (
          <div className="grid gap-6">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={statusTone(item.status)}>{item.status}</Badge>
              {item.adapter ? <Badge>{item.adapter}</Badge> : null}
              <Badge>{item.operation}</Badge>
              <LayerBadge layer={layerOf(item.audience)} />
              {item.layer_decision ? (
                <Badge tone={item.layer_decision === "classified" ? "primary" : "neutral"}>
                  {item.layer_decision === "classified" ? <Sparkles /> : null}{LAYER_DECISION_LABEL[item.layer_decision]}
                </Badge>
              ) : null}
            </div>

            {item.layer_probabilities ? (
              <LayerOdds probabilities={item.layer_probabilities} chosen={layerOf(item.audience)} />
            ) : null}

            <Timeline item={item} />

            {item.status === "failed" ? (
              <Notice tone="danger">
                <div className="font-medium">{item.error_category || "Failed"}</div>
                {item.error ? <div className="mt-1 text-muted-foreground">{item.error}</div> : null}
                <Button size="sm" className="mt-3" onClick={() => retry.mutate()} disabled={retry.isPending}>
                  {retry.isPending ? <Loader2 className="animate-spin" /> : <RotateCcw />}Retry capture
                </Button>
              </Notice>
            ) : null}

            <div className="flex flex-wrap gap-2">
              {item.change_id ? <Button variant="secondary" size="sm" onClick={() => navigate(`changes/${enc(item.change_id!)}`)}><GitCompare />Open the change</Button> : null}
              {item.source_path ? <Button variant="secondary" size="sm" onClick={() => navigate(wikiHref(item.source_path!))}><BookOpen />Read the source</Button> : null}
            </div>

            <section>
              <h4 className="mb-3 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">Details</h4>
              <Definitions rows={[
                ["Submitted by", item.actor?.display_name ? `${item.actor.display_name} · ${item.submitted_by}` : item.submitted_by],
                ["Created", when(item.created_at)],
                ["Attempts", String(item.attempts)],
                ["Source", item.source_path ? <code className="font-mono text-[12px]">{item.source_path}</code> : null],
                ["Commit", item.commit_sha ? <Sha value={item.commit_sha} /> : null],
              ]} />
            </section>

            <section>
              <h4 className="mb-3 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">Provenance</h4>
              <Definitions rows={[
                ["Origin", item.provenance?.adapter],
                ["Occurred", item.provenance?.occurred_at ? when(item.provenance.occurred_at) : null],
                ["Locator", item.provenance?.locator],
                ["Original URL", acquisition?.original_url ? <ExternalText value={acquisition.original_url} /> : null],
                ["Final URL", acquisition?.final_url ? <ExternalText value={acquisition.final_url} /> : null],
                ["Acquired", acquisition?.acquired_at ? when(acquisition.acquired_at) : null],
                ["Drive file", acquisition?.drive_file_id],
                ["Drive media", acquisition?.drive_media_type],
                ["Export media", acquisition?.export_media_type],
              ]} />
            </section>

            <section>
              <h4 className="mb-3 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">Evidence</h4>
              {item.artifacts?.length ? (
                <div className="grid gap-2">
                  {item.artifacts.map((artifact, index) => (
                    <div key={artifact.sha256 ?? index} className="flex items-center gap-3 rounded-xl border bg-background/60 p-3">
                      <div className="flex size-9 items-center justify-center rounded-lg bg-muted"><FileUp className="size-4 text-muted-foreground" /></div>
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-[13px] font-medium">{artifact.original_name || "Artifact"}</div>
                        <div className="text-[11.5px] text-muted-foreground">{artifact.media_type} · {bytes(artifact.bytes)}</div>
                      </div>
                      {artifact.sha256 ? <Sha value={artifact.sha256} /> : null}
                    </div>
                  ))}
                </div>
              ) : <p className="text-[13px] text-muted-foreground">This operation carries no capture artifact.</p>}
            </section>

            <FilingContext report={item.report} />

            <AliasDecisions report={item.report} />

            <Collapsible
              open={showOutcome}
              onOpenChange={setShowOutcome}
              triggerClassName="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground"
              trigger={<span>Extraction and filing report</span>}
            >
              <pre className="mt-3 max-h-96 overflow-auto rounded-xl border bg-muted/50 p-4 font-mono text-[11.5px] leading-relaxed">
                {JSON.stringify({ extraction: item.extraction, report: item.report }, null, 2)}
              </pre>
            </Collapsible>
          </div>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

type ContextMeasure = {
  bytes: number; budget_bytes: number; catalog: number; candidates: number; evidence: number; trimmed: number;
};

function contextMeasure(report: unknown): ContextMeasure | null {
  const value = (report as { filing_context?: Partial<ContextMeasure> } | null)?.filing_context;
  return value && typeof value.bytes === "number" && typeof value.budget_bytes === "number"
    ? { catalog: 0, candidates: 0, evidence: 0, trimmed: 0, ...value } as ContextMeasure
    : null;
}

function FilingContext({ report }: { report: unknown }) {
  const measure = contextMeasure(report);
  if (!measure) return null;
  const share = Math.min(1, measure.bytes / Math.max(1, measure.budget_bytes));
  const trimmed = measure.trimmed > 0;
  return (
    <section>
      <h4 className="mb-3 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">Filing context</h4>
      <div className="grid gap-3 rounded-xl border bg-background/60 p-4">
        <div className="flex items-baseline justify-between text-[12.5px]">
          <span className="font-medium tabular-nums">{bytes(measure.bytes)}</span>
          <span className="text-muted-foreground tabular-nums">of {bytes(measure.budget_bytes)} budget</span>
        </div>
        <div className="relative h-2 overflow-hidden rounded-full bg-muted">
          <motion.div
            className={cn("h-full rounded-full", trimmed ? "bg-warning" : share > 0.75 ? "bg-warning/70" : "bg-primary")}
            initial={{ width: 0 }}
            animate={{ width: `${Math.max(2, Math.round(share * 100))}%` }}
            transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
          />
        </div>
        <div className="grid grid-cols-3 gap-2 text-center">
          {([["Catalog lines", measure.catalog], ["Pages in full", measure.candidates], ["Evidence pages", measure.evidence]] as const).map(([label, value]) => (
            <div key={label} className="rounded-lg bg-muted/50 px-2 py-2">
              <div className="text-[15px] font-semibold tabular-nums">{value}</div>
              <div className="text-[11px] text-muted-foreground">{label}</div>
            </div>
          ))}
        </div>
        <p className={cn("text-[11.5px]", trimmed ? "text-warning" : "text-muted-foreground")}>
          {trimmed
            ? `The budget dropped ${measure.trimmed} entr${measure.trimmed === 1 ? "y" : "ies"}; the librarian filed without them.`
            : "Everything the librarian was given fit the budget."}
        </p>
      </div>
    </section>
  );
}

type AliasDecision = { entity?: string; alias?: string; decision: string; probability?: number; reason?: string };

const ALIAS_TONE: Record<string, "success" | "neutral" | "warning"> = {
  accepted: "success", rejected: "neutral", skipped: "warning", reverted: "warning",
};

function AliasDecisions({ report }: { report: unknown }) {
  const entries = (report as { aliases?: AliasDecision[] } | null)?.aliases ?? [];
  const decisions = entries.filter((entry) => entry.alias);
  const step = entries.find((entry) => !entry.alias);
  if (!entries.length) return null;
  return (
    <section>
      <h4 className="mb-3 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">Other names linked</h4>
      {step ? <Notice tone="warning">The alias step was skipped: {step.reason ?? "no reason recorded"}.</Notice> : null}
      <div className="grid gap-2">
        {decisions.map((entry, index) => (
          <motion.div key={`${entry.entity}-${entry.alias}`} initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }}
            transition={{ delay: 0.05 * index }} className="flex items-center gap-3 rounded-xl border bg-background/60 px-3 py-2 text-[12.5px]">
            <span className="min-w-0 flex-1 truncate">
              <span className="font-medium">{entry.alias}</span>
              <span className="text-muted-foreground"> → {(entry.entity ?? "").split("/").pop()?.replace(/\.md$/, "")}</span>
            </span>
            {typeof entry.probability === "number" ? <span className="tabular-nums text-muted-foreground">{Math.round(entry.probability * 100)}%</span> : null}
            <Badge tone={ALIAS_TONE[entry.decision] ?? "neutral"}>{entry.decision}{entry.reason ? ` · ${entry.reason}` : ""}</Badge>
          </motion.div>
        ))}
      </div>
    </section>
  );
}

const NO_SINGLE_LAYER = "no_single_layer";

function LayerOdds({ probabilities, chosen }: { probabilities: Record<string, number>; chosen: string }) {
  const threshold = useMeta().data?.layer_threshold ?? 0.75;
  const ranked = Object.entries(probabilities).sort((a, b) => b[1] - a[1]);
  return (
    <section>
      <h4 className="mb-3 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">Layer decision</h4>
      <div className="grid gap-2.5">
        {ranked.map(([layer, probability], index) => {
          const won = layer === chosen;
          return (
            <div key={layer} className="grid grid-cols-[7rem_1fr_3rem] items-center gap-3 text-[12.5px]">
              <span className={cn("truncate", won ? "font-medium text-foreground" : "text-muted-foreground", layer === NO_SINGLE_LAYER && "italic")}>
                {layer === NO_SINGLE_LAYER ? "no single layer" : layer}
              </span>
              <div className="relative h-2 overflow-hidden rounded-full bg-muted">
                <motion.div
                  className={cn("h-full rounded-full", won ? "bg-primary" : "bg-muted-foreground/35")}
                  initial={{ width: 0 }}
                  animate={{ width: `${Math.round(probability * 100)}%` }}
                  transition={{ duration: 0.6, delay: index * 0.06, ease: [0.22, 1, 0.36, 1] }}
                />
                <div className="absolute inset-y-0 w-px bg-foreground/40" style={{ left: `${threshold * 100}%` }} />
              </div>
              <span className="text-right tabular-nums text-muted-foreground">{Math.round(probability * 100)}%</span>
            </div>
          );
        })}
      </div>
      <p className="mt-2.5 text-[11.5px] text-muted-foreground">
        The line marks the {Math.round(threshold * 100)}% a layer needs; below it the capture files in the sender's default layer.
      </p>
    </section>
  );
}

function ExternalText({ value }: { value: string }) {
  return <span className="break-all font-mono text-[12px]">{value}</span>;
}

function Timeline({ item }: { item: CaptureDetail }) {
  const steps = [
    { label: "Received", at: item.created_at, done: true },
    { label: "Processing", at: item.processing_started_at, done: !!item.processing_started_at },
    { label: item.status === "failed" ? "Failed" : "Landed", at: item.finished_at, done: !!item.finished_at },
  ];
  return (
    <div className="relative grid grid-cols-3 gap-2">
      <div className="absolute left-[16%] right-[16%] top-3 h-px bg-border" />
      {steps.map((step, index) => (
        <motion.div key={step.label} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * 0.08 }} className="relative flex flex-col items-center gap-1.5 text-center">
          <span className={cn(
            "z-10 flex size-6 items-center justify-center rounded-full border-2 bg-popover",
            step.done ? (item.status === "failed" && index === 2 ? "border-destructive" : "border-primary") : "border-border",
          )}>
            <span className={cn("size-2 rounded-full", step.done ? (item.status === "failed" && index === 2 ? "bg-destructive" : "bg-primary") : "bg-transparent")} />
          </span>
          <span className="text-[12px] font-medium">{step.label}</span>
          <span className="text-[11px] text-muted-foreground">{step.at ? fromNow(step.at) : "—"}</span>
        </motion.div>
      ))}
    </div>
  );
}

function CaptureComposer({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const meta = useMeta();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [mode, setMode] = React.useState<"text" | "file" | "url">("text");
  const [text, setText] = React.useState("");
  const [url, setUrl] = React.useState("");
  const [file, setFile] = React.useState<File | null>(null);
  const [title, setTitle] = React.useState("");
  const [occurred, setOccurred] = React.useState("");
  const defaultLayer = meta.data?.default_layer ?? PUBLIC_LAYER;
  const [layer, setLayer] = React.useState(defaultLayer);
  const [error, setError] = React.useState<string | null>(null);
  const audience = [layer];

  React.useEffect(() => {
    if (!open) return;
    setText(""); setUrl(""); setFile(null); setTitle(""); setOccurred(""); setLayer(defaultLayer); setError(null);
  }, [open, defaultLayer]);

  const submit = useMutation({
    mutationFn: () => {
      if (mode === "file") {
        const body = new FormData();
        body.append("file", file as File);
        body.append("title", title);
        body.append("occurred_at", occurred);
        body.append("audience", JSON.stringify(audience));
        return api.form<CaptureReceipt>("captures/file", body);
      }
      return api.post<CaptureReceipt>(`captures/${mode}`, { [mode]: mode === "text" ? text : url, title, occurred_at: occurred, audience });
    },
    onSuccess: (receipt) => {
      toast("Capture queued.");
      queryClient.invalidateQueries({ queryKey: ["admin"] });
      onOpenChange(false);
      navigate(`captures/${enc(receipt.id)}`);
    },
    onError: (failure) => setError(errorMessage(failure)),
  });

  const ready = mode === "text" ? text.trim().length > 0 : mode === "url" ? url.trim().length > 0 : !!file;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide title="New capture" description="The writer files it into the wiki; the original is kept as immutable evidence.">
        <form onSubmit={(event) => { event.preventDefault(); if (ready) submit.mutate(); }} className="grid gap-4">
          <Tabs value={mode} onValueChange={(value) => setMode(value as typeof mode)}>
            <TabsList>
              <TabsTrigger value="text"><span className="flex items-center gap-1.5"><ClipboardPaste className="size-3.5" />Paste</span></TabsTrigger>
              <TabsTrigger value="file"><span className="flex items-center gap-1.5"><FileUp className="size-3.5" />Upload</span></TabsTrigger>
              <TabsTrigger value="url"><span className="flex items-center gap-1.5"><Link2 className="size-3.5" />Public URL</span></TabsTrigger>
            </TabsList>
            <TabsContent value="text">
              <Textarea rows={9} value={text} onChange={(event) => setText(event.target.value)} placeholder="Paste meeting notes, a decision, an email thread…" className="min-h-48 font-serif text-[15px]" autoFocus />
            </TabsContent>
            <TabsContent value="file">
              <Dropzone file={file} onFile={setFile} maxBytes={meta.data?.max_artifact_bytes} />
            </TabsContent>
            <TabsContent value="url">
              <Input type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://…" />
              <p className="mt-2 text-[11.5px] text-muted-foreground">Only public addresses; private networks and redirects to them are refused.</p>
            </TabsContent>
          </Tabs>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Title" hint="Optional"><Input value={title} onChange={(event) => setTitle(event.target.value)} /></Field>
            <Field label="Occurred on" hint="Optional"><Input type="date" value={occurred} onChange={(event) => setOccurred(event.target.value)} /></Field>
          </div>
          <Field label="Layer" hint="Everything the writer derives from it stays in this layer.">
            <LayerField value={layer} onChange={setLayer} layers={meta.data?.layers ?? []} />
          </Field>
          {error ? <Notice tone="danger">{error}</Notice> : null}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={!ready || submit.isPending}>
              {submit.isPending ? <Loader2 className="animate-spin" /> : <ArrowUpRight />}Queue capture
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function DeleteComposer({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [paths, setPaths] = React.useState("");
  const [rationale, setRationale] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => { if (!open) { setPaths(""); setRationale(""); setError(null); } }, [open]);
  const selected = paths.split("\n").map((value) => value.trim()).filter(Boolean);
  const remove = useMutation({
    mutationFn: () => api.post<CaptureReceipt>("knowledge/delete", { paths: selected, rationale }),
    onSuccess: (receipt) => {
      toast("Deletion queued.");
      queryClient.invalidateQueries({ queryKey: ["admin"] });
      onOpenChange(false);
      navigate(`captures/${enc(receipt.id)}`);
    },
    onError: (failure) => setError(errorMessage(failure)),
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide title="Delete knowledge" description="Removes current pages and sweeps every link to them in one commit. History stays in Git.">
        <Field label="Paths" hint="One wiki/ or sources/ path per line.">
          <Textarea rows={4} value={paths} onChange={(event) => setPaths(event.target.value)} className="font-mono text-[12px]" placeholder="wiki/notes/Old decision.md" />
        </Field>
        <Field label="Rationale"><Input value={rationale} onChange={(event) => setRationale(event.target.value)} placeholder="Why this knowledge should no longer be current" /></Field>
        {error ? <Notice tone="danger">{error}</Notice> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="destructive" disabled={!selected.length || !rationale.trim() || remove.isPending} onClick={() => remove.mutate()}>
            {remove.isPending ? <Loader2 className="animate-spin" /> : <Trash2 />}Queue deletion of {selected.length || ""} {selected.length === 1 ? "path" : "paths"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
