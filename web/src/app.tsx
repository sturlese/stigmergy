import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { Logo } from "@/components/logo";
import { ADMIN_ROUTES, PLATFORM_ROUTES } from "@/components/nav";
import { Shell } from "@/components/shell";
import { Account } from "@/features/account/account";
import { AdminSection } from "@/features/admin";
import { SignIn } from "@/features/auth/sign-in";
import { PlatformSection } from "@/features/platform";
import { GraphView } from "@/features/wiki/graph-view";
import { WikiLayout } from "@/features/wiki/wiki-layout";
import { UNAUTHORIZED_EVENT } from "@/lib/api";
import { useMe } from "@/lib/queries";
import { navigate, pagePathFromRoute, useRoute } from "@/lib/router";

export function App() {
  const me = useMe();
  const route = useRoute();
  const queryClient = useQueryClient();

  React.useEffect(() => {
    const onUnauthorized = () => {
      if (!queryClient.getQueryData(["me"])) return;
      queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== "me" });
      queryClient.setQueryData(["me"], null);
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [queryClient]);

  React.useEffect(() => {
    if (!me.data) return;
    const outside = me.data.operator && !me.data.tenant;
    const first = route.split("/")[0];
    if (!route || route === "signin") navigate(outside ? "platform" : "wiki");
    else if (outside && !PLATFORM_ROUTES.has(first)) navigate("platform");
    else if (!me.data.operator && PLATFORM_ROUTES.has(first)) navigate("wiki");
    else if (!me.data.is_admin && ADMIN_ROUTES.has(first)) navigate(outside ? "platform" : "wiki");
  }, [me.data, route]);

  if (me.isLoading) {
    return (
      <div className="grid h-dvh place-items-center bg-background">
        <Logo className="size-12" />
      </div>
    );
  }
  if (!me.data) return <SignIn />;

  const section = route.split("/")[0] || (me.data.tenant ? "wiki" : "platform");
  if (PLATFORM_ROUTES.has(section) && me.data.operator) {
    return <Shell me={me.data} route={route}><PlatformSection route={route} /></Shell>;
  }
  if (!me.data.tenant) return <Shell me={me.data} route={route}>{null}</Shell>;
  if (section === "graph") {
    return <Shell me={me.data} route={route} fullBleed><GraphView route={route} /></Shell>;
  }
  return (
    <Shell me={me.data} route={route}>
      {section === "wiki" ? <WikiLayout me={me.data} path={pagePathFromRoute(route)} /> : null}
      {section === "account" ? <Account me={me.data} /> : null}
      {ADMIN_ROUTES.has(section) && me.data.is_admin ? <AdminSection route={route} /> : null}
    </Shell>
  );
}
