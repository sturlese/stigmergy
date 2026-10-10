import { describe, expect, it } from "vitest";
import { membershipChanges } from "./membership";

const person = (subject: string, groups: string[], defaultLayer = "public") =>
  ({ subject, groups, default_audience: [defaultLayer] });

describe("layer membership changes", () => {
  it("updates only the identities whose membership changes", () => {
    const people = [person("a", ["finance"]), person("b", []), person("c", ["hr"])];
    expect(membershipChanges(people, "finance", new Set(["a", "b"]))).toEqual([
      { subject: "b", body: { groups: ["finance"] } },
    ]);
  });

  it("moves a removed member's default to public when it was this layer", () => {
    const people = [person("a", ["finance", "legal"], "finance"), person("b", ["finance", "hr"], "hr")];
    expect(membershipChanges(people, "finance", new Set())).toEqual([
      { subject: "a", body: { groups: ["legal"], default_audience: ["public"] } },
      { subject: "b", body: { groups: ["hr"] } },
    ]);
  });
});
