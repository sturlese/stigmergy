import { describe, expect, it } from "vitest";
import type { CatalogPage, GraphData } from "@/lib/types";
import { brainStats } from "./home-story";

const page = (path: string, type: CatalogPage["type"], layers: string[] = [], updated = new Date().toISOString()): CatalogPage =>
  ({ path, title: path, type, entity_type: "", aliases: [], updated, layers, summary: "" });

describe("brainStats", () => {
  const weeksAgo = (weeks: number) => new Date(Date.now() - weeks * 7 * 24 * 3600 * 1000 - 3600 * 1000).toISOString();
  const pages = [
    page("e/Acme.md", "entity"), page("e/Ana.md", "entity", ["finance"]), page("n/Plan.md", "note", [], weeksAgo(3)),
    page("s/Call.md", "source", [], weeksAgo(20)),
  ];
  const graph: GraphData = {
    nodes: pages.map(({ path, title, type }) => ({ path, title, type })),
    edges: [{ from: "n/Plan.md", to: "e/Acme.md" }, { from: "e/Ana.md", to: "e/Acme.md" },
            { from: "n/Plan.md", to: "s/Call.md" }, { from: "e/Acme.md", to: "e/Ana.md" }],
  };

  it("ranks hubs by inbound links and leaves sources out", () => {
    const stats = brainStats(pages, graph);
    expect(stats.hubs.map((hub) => [hub.page.path, hub.links])).toEqual([["e/Acme.md", 2], ["e/Ana.md", 1]]);
    expect(stats.perPage).toBeCloseTo(4 / 3);
  });

  it("buckets pages by week and counts every layer a page is in", () => {
    const stats = brainStats(pages, graph);
    expect(stats.activity.at(-1)).toBe(2);
    expect(stats.activity.at(-4)).toBe(1);
    expect(stats.activity.reduce((a, b) => a + b, 0)).toBe(3);
    expect(stats.layers).toEqual([{ name: "public", pages: 3 }, { name: "finance", pages: 1 }]);
  });
});
