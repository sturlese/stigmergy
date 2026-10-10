import * as React from "react";
import { RequestsView } from "./requests";
import { WorkspacesView, type WorkspaceDraft } from "./workspaces";
import { WriterView } from "./writer";
import { navigate } from "@/lib/router";

/** The master console's sections, keyed by their first route segment. */
export function PlatformSection({ route }: { route: string }) {
  const [draft, setDraft] = React.useState<WorkspaceDraft | null>(null);
  const clear = React.useCallback(() => setDraft(null), []);
  switch (route.split("/")[0]) {
    case "requests": return <RequestsView onCreate={(next) => { setDraft(next); navigate("platform"); }} />;
    case "writer": return <WriterView />;
    default: return <WorkspacesView draft={draft} onDraftUsed={clear} />;
  }
}
