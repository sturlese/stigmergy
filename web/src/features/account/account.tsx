import { KeyRound, Monitor, Moon, ShieldCheck, Sun, UsersRound } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { useTheme } from "@/components/theme";
import { Segmented } from "@/components/ui/controls";
import { Avatar, Badge, Card, CardContent } from "@/components/ui/primitives";
import type { Me } from "@/lib/types";

export function Account({ me }: { me: Me }) {
  const { theme, setTheme } = useTheme();
  return (
    <div className="mx-auto max-w-3xl px-8 py-12">
      <PageHeader eyebrow="Account" title="You" description="Who you are to Stigmergy, and what you can read." />
      <Card>
        <CardContent className="flex items-center gap-4">
          <Avatar name={me.display_name} className="size-14 text-base" />
          <div className="min-w-0 flex-1">
            <div className="text-lg font-semibold tracking-tight">{me.display_name}</div>
            <div className="text-[13px] text-muted-foreground">{me.subject}</div>
          </div>
          {me.is_admin ? <Badge tone="primary"><ShieldCheck />Administrator</Badge> : <Badge>Member</Badge>}
        </CardContent>
      </Card>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <Card>
          <CardContent>
            <div className="mb-3 flex items-center gap-2 text-[13px] font-semibold"><UsersRound className="size-4 text-muted-foreground" />Layers</div>
            <div className="flex flex-wrap gap-1.5">
              {me.groups.length ? me.groups.map((group) => <Badge key={group} tone={group === "brain-admins" ? "primary" : "neutral"}>{group}</Badge>) : <span className="text-[13px] text-muted-foreground">Only public.</span>}
            </div>
            <p className="mt-3 text-[12px] text-muted-foreground">
              {me.unrestricted ? "You can read every layer in the brain." : "You read public and these layers; what you see of a page is what these layers know."}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent>
            <div className="mb-3 flex items-center gap-2 text-[13px] font-semibold"><KeyRound className="size-4 text-muted-foreground" />Captures you file</div>
            <p className="text-[13px] text-muted-foreground">
              When you do not name a layer, new captures go to{" "}
              <span className="font-medium text-foreground">{me.default_layer ?? "the layer you name, since you have no default"}</span>. You can publish in {me.layers.join(", ")}.
            </p>
          </CardContent>
        </Card>
      </div>
      <Card className="mt-4">
        <CardContent className="flex items-center justify-between gap-4">
          <div>
            <div className="text-[13px] font-semibold">Appearance</div>
            <div className="text-[12px] text-muted-foreground">Follow your system, or pick a side.</div>
          </div>
          <Segmented
            layoutId="theme"
            value={theme}
            onChange={setTheme}
            options={[
              { value: "light", label: <><Sun />Light</> },
              { value: "dark", label: <><Moon />Dark</> },
              { value: "system", label: <><Monitor />System</> },
            ]}
          />
        </CardContent>
      </Card>
    </div>
  );
}
