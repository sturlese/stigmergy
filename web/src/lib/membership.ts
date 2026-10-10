const PUBLIC = "public";

interface Member {
  subject: string;
  groups: string[];
  default_audience: string[] | null;
}

export interface MembershipChange {
  subject: string;
  body: { groups: string[]; default_audience?: string[] };
}

/** The per-identity updates that make `chosen` exactly the members of `layer`. A removed member
 * whose default was this layer defaults to public, the only layer every identity may use. */
export function membershipChanges(people: Member[], layer: string, chosen: Set<string>): MembershipChange[] {
  const changes: MembershipChange[] = [];
  for (const person of people) {
    const member = person.groups.includes(layer);
    const wanted = chosen.has(person.subject);
    if (member === wanted) continue;
    const groups = wanted ? [...person.groups, layer] : person.groups.filter((group) => group !== layer);
    const body: MembershipChange["body"] = { groups };
    if (!wanted && person.default_audience?.[0] === layer) body.default_audience = [PUBLIC];
    changes.push({ subject: person.subject, body });
  }
  return changes;
}
