import { ShieldCheck } from "lucide-react";
import { GroupPicker, LayerField, PUBLIC_LAYER } from "@/components/ui/audience";
import { Segmented, Switch } from "@/components/ui/controls";
import { Field, Input } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export const ADMIN_GROUP = "brain-admins";

export interface PersonDraft {
  subject: string;
  display_name: string;
  kind: "person" | "agent";
  is_admin: boolean;
  groups: string[];
  default_audience: string[] | null;
}

export const EMPTY_PERSON: PersonDraft = {
  subject: "", display_name: "", kind: "person", is_admin: false, groups: [], default_audience: [PUBLIC_LAYER],
};

/** The fields that differ from `initial`, for a partial update. */
export function changedFields(initial: PersonDraft, draft: PersonDraft): Partial<PersonDraft> {
  const changes: Partial<PersonDraft> = {};
  const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
  for (const key of ["display_name", "kind", "is_admin", "groups", "default_audience"] as const) {
    const before = key === "groups" ? [...initial.groups].sort() : initial[key];
    const after = key === "groups" ? [...draft.groups].sort() : draft[key];
    if (!same(before, after)) (changes as Record<string, unknown>)[key] = draft[key];
  }
  return changes;
}

export function PersonFields({ draft, onChange, groups, creating }: {
  draft: PersonDraft; onChange: (draft: PersonDraft) => void; groups: string[]; creating: boolean;
}) {
  const set = <K extends keyof PersonDraft>(key: K, value: PersonDraft[K]) => {
    const next = { ...draft, [key]: value };
    if (next.kind !== "person" || !next.groups.includes(ADMIN_GROUP)) next.is_admin = false;
    const layer = next.default_audience?.[0];
    if (!layer || (layer !== PUBLIC_LAYER && !next.groups.includes(layer))) next.default_audience = [PUBLIC_LAYER];
    onChange(next);
  };
  const canAdminister = draft.kind === "person" && draft.groups.includes(ADMIN_GROUP);
  const layerChoices = draft.groups.filter((group) => group !== ADMIN_GROUP);

  return (
    <div className="grid gap-5">
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Display name"><Input value={draft.display_name} onChange={(event) => set("display_name", event.target.value)} autoFocus={creating} /></Field>
        <Field label="Email" hint={creating ? "Their identity everywhere: MCP, Slack, and the backoffice." : "The email identifies them and cannot change."}>
          <Input type="email" value={draft.subject} readOnly={!creating} onChange={(event) => set("subject", event.target.value)} className={cn(!creating && "text-muted-foreground")} />
        </Field>
      </div>
      <Field label="Kind" hint={draft.kind === "agent" ? "An agent's token connects its MCP client and signs it in to read the wiki; agents never administer." : "People can sign in to the backoffice."}>
        <Segmented layoutId={`kind-${creating ? "new" : draft.subject}`} value={draft.kind} onChange={(value) => set("kind", value)}
          options={[{ value: "person", label: "Person" }, { value: "agent", label: "Agent" }]} />
      </Field>
      <Field label="Layers" hint={draft.groups.includes(ADMIN_GROUP) ? "Members publish in their layers. brain-admins also reads every layer and can delete." : "Members read and publish in these layers; everyone reads public."}>
        <GroupPicker value={draft.groups} onChange={(value) => set("groups", value)} groups={groups} />
      </Field>
      <div className={cn("flex items-start justify-between gap-4 rounded-xl border p-4 transition", canAdminister ? "bg-background/60" : "opacity-60")}>
        <div className="flex gap-3">
          <ShieldCheck className={cn("mt-0.5 size-5", draft.is_admin ? "text-primary" : "text-muted-foreground")} />
          <div>
            <div className="text-[13px] font-medium">Administrator</div>
            <div className="text-[12px] text-muted-foreground">
              {canAdminister ? "Can see operations and manage people, tokens, layers, and channels." : "Only a person in brain-admins can administer the backoffice."}
            </div>
          </div>
        </div>
        <Switch checked={draft.is_admin} onCheckedChange={(value) => set("is_admin", value)} disabled={!canAdminister} />
      </div>
      <Field label="Default layer" hint="Where their captures go when they do not name a layer.">
        <LayerField value={draft.default_audience?.[0] ?? PUBLIC_LAYER} onChange={(value) => set("default_audience", [value])} layers={layerChoices} />
      </Field>
    </div>
  );
}
