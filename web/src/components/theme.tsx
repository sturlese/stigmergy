import * as React from "react";
import { readStored, writeStored } from "@/lib/utils";

export type ThemeChoice = "light" | "dark" | "system";
const KEY = "stigmergy.theme";
const ThemeContext = React.createContext<{ theme: ThemeChoice; resolved: "light" | "dark"; setTheme: (theme: ThemeChoice) => void }>({
  theme: "system", resolved: "light", setTheme: () => {},
});

function systemDark() {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/** `forced` pins a page to one theme without touching the reader's stored choice. */
export function ThemeProvider({ children, forced }: { children: React.ReactNode; forced?: "light" | "dark" }) {
  const [theme, setThemeState] = React.useState<ThemeChoice>(() => forced ?? readStored<ThemeChoice>(KEY, "system"));
  const [dark, setDark] = React.useState(() => (theme === "system" ? systemDark() : theme === "dark"));

  React.useEffect(() => {
    if (forced) return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => setDark(theme === "system" ? media.matches : theme === "dark");
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme, forced]);

  React.useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);

  const setTheme = React.useCallback((next: ThemeChoice) => {
    if (forced) return;
    setThemeState(next);
    writeStored(KEY, next);
  }, [forced]);

  return (
    <ThemeContext.Provider value={{ theme, resolved: dark ? "dark" : "light", setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  return React.useContext(ThemeContext);
}
