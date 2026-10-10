import { Loader2 } from "lucide-react";
import * as React from "react";
import { Button } from "./button";
import { Dialog, DialogContent } from "./overlays";

/** A confirmation dialog whose action shows progress and stays open with the error if it fails. */
export function ConfirmDialog({ open, onOpenChange, title, description, confirmLabel, destructive, onConfirm, children }: {
  open: boolean; onOpenChange: (open: boolean) => void; title: string; description?: React.ReactNode;
  confirmLabel: string; destructive?: boolean; onConfirm: () => Promise<unknown>; children?: React.ReactNode;
}) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => { if (!open) setError(null); }, [open]);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
      onOpenChange(false);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The action failed.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={title} description={description}>
        {children}
        {error ? <p className="text-[12.5px] text-destructive">{error}</p> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant={destructive ? "destructive" : "default"} onClick={run} disabled={busy}>
            {busy ? <Loader2 className="animate-spin" /> : null}{confirmLabel}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
