import { describe, expect, it } from "vitest";
import { pagePathFromRoute, wikiHref } from "./router";

describe("wiki routes", () => {
  it("opens a layer's repository path at the virtual page it belongs to", () => {
    expect(wikiHref("layers/deals/wiki/entities/Acme.md")).toBe(wikiHref("wiki/entities/Acme.md"));
    expect(wikiHref("layers/deals/sources/2026/09/a.md")).toBe(wikiHref("sources/2026/09/a.md"));
  });

  it("round-trips page paths with spaces, accents, and parentheses", () => {
    for (const path of ["wiki/entities/Altamira Pharma Logistics.md", "wiki/notes/Café (Q3) review.md", "sources/2026/09/0f0e0d0c-0b0a-4908-8706-050403020100.md"]) {
      expect(pagePathFromRoute(wikiHref(path).slice(2))).toBe(path);
    }
  });

  it("reads the links the server renders into page HTML", () => {
    expect(pagePathFromRoute("wiki/wiki/entities/Acme%20Corp%20%28EU%29.md")).toBe("wiki/entities/Acme Corp (EU).md");
  });

  it("refuses malformed routes instead of throwing", () => {
    expect(pagePathFromRoute("wiki/%E0%A4%A")).toBeNull();
    expect(pagePathFromRoute("wiki/")).toBeNull();
    expect(pagePathFromRoute("people/x")).toBeNull();
  });
});
