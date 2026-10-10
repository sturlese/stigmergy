import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Eye, EyeOff, KeyRound, Loader2 } from "lucide-react";
import { motion } from "motion/react";
import * as React from "react";
import { Logo } from "@/components/logo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/primitives";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import { TrailField } from "./trail-field";

export function SignIn() {
  const queryClient = useQueryClient();
  const [token, setToken] = React.useState("");
  const [visible, setVisible] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [shake, setShake] = React.useState(0);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const me = await api.post("session", { token: token.trim() });
      setToken("");
      queryClient.setQueryData(["me"], me);
      if (!window.location.hash || window.location.hash === "#/signin") window.location.hash = "#/wiki";
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : "Sign-in failed.");
      setShake((value) => value + 1);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative grid min-h-dvh overflow-hidden bg-background lg:grid-cols-[1.25fr_1fr]">
      <div className="grain relative hidden overflow-hidden border-r bg-[oklch(0.16_0.035_262)] lg:block">
        <TrailField className="absolute inset-0 size-full" />
        <motion.div
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.9, delay: 0.25, ease: [0.16, 1, 0.3, 1] }}
          className="absolute bottom-14 left-14 right-14 text-white"
        >
          <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3 py-1 text-[11px] font-medium tracking-wide text-white/75">
            <span className="size-1.5 rounded-full bg-[oklch(0.72_0.13_250)]" /> Stigmergy
          </div>
          <h1 className="max-w-xl font-serif text-[44px] leading-[1.05] tracking-[-0.02em] text-balance">
            Every conversation leaves a trace. Every trace makes the team smarter.
          </h1>
          <p className="mt-4 max-w-md text-[15px] leading-relaxed text-white/60">
            Your team's knowledge, filed by agents, linked into one living graph, and read by everyone who is allowed to see it.
          </p>
        </motion.div>
      </div>

      <div className="relative flex items-center justify-center p-6">
        <TrailField className="absolute inset-0 size-full opacity-40 lg:hidden" />
        <motion.form
          key={shake}
          onSubmit={submit}
          initial={shake ? false : { opacity: 0, y: 18, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
          className={cn("relative w-full max-w-sm", shake && "animate-shake")}
        >
          <div className="mb-8 flex items-center gap-3">
            <Logo className="size-10" />
            <div>
              <div className="text-lg font-semibold tracking-tight">Welcome back</div>
              <div className="text-[13px] text-muted-foreground">Sign in to your team's brain</div>
            </div>
          </div>
          <label htmlFor="token" className="mb-1.5 block text-xs font-medium text-muted-foreground">Your token</label>
          <div className="relative">
            <KeyRound className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="token"
              type={visible ? "text" : "password"}
              autoComplete="off"
              spellCheck={false}
              autoFocus
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="stg_…"
              className="h-11 pl-9 pr-10 font-mono text-[13px]"
            />
            <button
              type="button"
              onClick={() => setVisible((value) => !value)}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-1.5 text-muted-foreground transition hover:text-foreground"
              aria-label={visible ? "Hide token" : "Show token"}
            >
              {visible ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
            </button>
          </div>
          {error ? (
            <motion.p initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} className="mt-2 text-[12px] text-destructive">
              {error}
            </motion.p>
          ) : null}
          <Button type="submit" size="lg" className="mt-5 w-full" disabled={busy || !token.trim()}>
            {busy ? <Loader2 className="animate-spin" /> : null}
            Sign in
            {!busy ? <ArrowRight /> : null}
          </Button>
          <p className="mt-6 text-center text-[12px] leading-relaxed text-muted-foreground">
            Use the token an administrator issued you, the same one your Claude Code, Codex, or Claude Desktop uses.
          </p>
        </motion.form>
      </div>
    </div>
  );
}
