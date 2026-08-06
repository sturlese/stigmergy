# ADR 022 — entity navigation: the graph served, entity-first moved down

Status: accepted. Narrative: [`docs/reference/navigation.md`](../reference/navigation.md).
Code map: [`src/stigmergy/index/index.md`](../../src/stigmergy/index/index.md),
[`src/stigmergy/server/index.md`](../../src/stigmergy/server/index.md).
Sibling: [ADR 012](./012-hybrid-index.md) (the hybrid index this record extends),
[ADR 007](./007-answer-layer.md) (the answer layer entity-first resolution moves OUT of).

## Context

This brain promises two consumption patterns, and the second one in these words: *search finds the
door; the agent walks the house — it reads full pages and follows wikilinks one hop, because the
graph provides what no embedding can.* Verified line by line against the running system, that
sentence was false:

- `index/corpus.py` parsed every page's wikilinks and computed `inlinks` — then threw the graph
  away. `links` was not a column; nothing served the inbound set beyond a bare count.
- `read_page` returned a fenced body in which `[[stems]]` were the only navigation, and not even
  `type`/`status` — an agent could not tell an entity page from a decision, and its only way to
  walk one hop was to parse UNTRUSTED fenced content and pay a full `search_brain` call per hop.
- The `entity` filter demanded registry ids and no read tool revealed them. `BrainService.
  scoped_entities` existed (`service.py`) and was consumed only by the offline fake — the
  vocabulary was a secret to every real client.
- Entity-first resolution lived in the ANSWER layer (`answer/brain.py::_search_entity_first`), so
  the only client that had it was `ask`'s own search tool. Every other MCP client — `search_brain`
  over stdio or HTTP, Slack — got bare hybrid search. The rule that *how to query well lives BEHIND
  the API* was violated in spirit: it lived one layer above the API, not behind it.
- Entity metadata was thin and invisible: entity-page `role`/`aliases` were neither in `tsv` nor
  served anywhere; registry `type` had zero query-time readers.

The gap was product-critical rather than cosmetic: the read path was plain hybrid RAG, not the
graph-walking pattern the design promises.

## Decisions

**D1 — the graph is a served, ACL-scoped, first-class column, not a re-derived courtesy.**
`pages_index.links` (`text[]`, resolved repo-relative paths, GIN-indexed) is computed once, at
the SAME two points that already compute `inlinks` (the full rebuild in memory, the incremental
webhook against the table's own existing paths), through ONE shared algorithm
(`corpus.resolve_links`/`by_stem_index`). The alternative considered and rejected — resolving the
graph at READ time, per `read_page`/`describe_entity` call, by re-parsing wikilinks from `body`
— would have made navigation depend on UNTRUSTED content parsing at serve time (the exact
threat the `UNTRUSTED-DATA` fence exists to keep out of the agent's structured surface) and would
have duplicated resolution a third way beyond the two `corpus.resolve_links` already unifies.

**D2 — the rank-time compensation is deleted, not merely superseded.** A split-document demotion
bug: `rank.py` reconstructed which continuation parts were superseded from whichever
candidates happened to share a search's pool, which silently failed whenever the primary fell
outside that pool. The fix moves the computation to where the fact actually lives — build time,
over the whole corpus (`corpus.load_pages` propagates the primary's `superseded_by` onto every
sibling sharing its `chain_base`) — and the rank-time reconstruction is REMOVED, not left running
alongside the new mechanism as a redundant safety net. Two reasons: a compensation nobody deletes
ossifies into apparent permanent behavior nobody remembers is a workaround, and keeping it would
have been dead code the moment the
column was populated — `contract_factors` already turns a truthy `superseded_by` into the
"superseded" factor for ANY row, part or primary, with no help needed.

**D3 — `read_page`'s new fields ride the EXISTING existence-scoping primitive, applied twice.**
`links`/`backlinks` are not a new kind of visibility decision: each entry is ACL-checked through
`server.acl.visible()` — the SAME one predicate every other surface in this codebase uses — before
it is ever shaped into `{path, title}`. Applying it BEFORE the cap (not after) is deliberate: an
out-of-scope entry must never consume a shown slot a visible one could have had, and the total
stated in the truncation note must be the TRUE visible total, never a raw-row count that would
itself hint at hidden entries. `_capped` is the ONE place that "filter, then cap" order lives —
shared by `read_page`'s links/backlinks and `describe_entity`'s timeline, never reimplemented per
surface, because the order is the property and a second copy is a second chance to get it backwards.

**D4 — AMENDED 2026-08-04. Entity-first resolution LAYERS on the ranking; it does
not replace it.** The original ruling (kept below) moved resolution down to `BrainService._search`
with its semantics unchanged: search the resolved entity's own material FIRST, fall back to the
unscoped call only on ZERO hits. Moving it down was right. Carrying the scope-then-fallback shape
with it was not.

The cost is structural, not a ranking nit. A resolved entity with ANY hits eclipsed the blended
ranking entirely, so a page that is genuinely company-wide — `entity: []`, which is the correct
declaration for a policy, a process, a cross-cutting decision — became unreachable through EVERY
query naming a registered company. That is most real questions. Observed on staging: `demo pipeline
extracción Globex` returned only the three pages anchored to `globex`, each carrying
`factors: ["entity:globex"]`, while the page that actually answered the question ranked #2 on raw
hybrid search over the same DSN and was absent from `search_brain` altogether. The substrate was
healthy; the filter was the whole cause.

**The ruling**: one blended search. Resolution still happens, and still only when the caller passed
no explicit `entity` filter — it feeds `entity_hint`, which is what it always also fed: the
rank-time entity boost and the lexical arm's alias expansion. An anchored page still wins its tie
against an identical unanchored one, now by SCORE rather than by the other one's absence, so an
unanchored page that is genuinely the better answer can still say so.

Three consequences, each deliberate:

- **An explicit `filters={"entity": id}` is untouched.** Explicit is explicit, and `ANSWER_SYS`
  still teaches the agent to use it once an id is known.
- **A mis-resolution costs rank positions, not existence.** An entity named like a common word used
  to be able to hide the corpus; now it reorders it.
- **Anchoring stops being retrieval-fatal.** The librarian's editorial call about whether a capture
  is company-wide was, under eclipse semantics, also a decision about whether the page could ever
  be found. It is an editorial call again.

The boost's WEIGHT stays where this record already put it — arbitrated against the golden set, in
its own batch (see Known limits). If blending measurably hurts entity-question precision, the
tunable is `_BOOST_ENTITY`, not a return to the pre-filter.

*Original ruling, kept for the record (superseded above):* entity-first resolution moves DOWN, to where every client can reach it. "Does this query
name a registered entity, and should the search scope to it first"
was answered only inside `ask`'s own text-rendering layer (`AnswerBrain._search_entity_first`).
`BrainService._search` now performs the SAME resolution — unchanged semantics, only when the
caller passed no explicit `entity` filter, unscoped fallback only on zero hits — so `search_brain`
over stdio, over HTTP, through Slack, and any future client all get it for free.
`answer/brain.py`'s own wrapper is deleted rather than kept as a second, now-redundant
implementation: one entity-first resolution, one place it lives, and that place is BEHIND the API
rather than in one client's own rendering layer above it.

**D5 — AMENDED. `describe_entity` resolves through the registry OR verbatim scoped-set
membership — still one existence rule, never a second resolver.** The original ruling (kept below
for the record) held that `describe_entity` resolves STRICTLY through
`entity_aliases.resolve_exact`, so an id absent from the registry answered "unknown" even when
pages were genuinely anchored to it — `list_entities` was where an unregistered id got
acknowledged, `describe_entity` was where a registered one got explained, deliberately two
different answers to two different questions.

The cost of that split is precise: it breaks the navigation loop this whole record exists to
build. `list_entities` -> `describe_entity` -> `read_page` is the documented walk; a caller that
lists `ghostco` (anchored, unregistered, honestly served
as `{"id": "ghostco"}`) and then describes it hit a wall the loop never warned about. Two tools
answering two different questions is defensible; two tools DISAGREEING about whether the same id
*exists*, in the one surface whose whole point is a walkable graph, is not.

**The ruling**: resolution is now `entity_id = resolve_exact(aliases, entity) or (entity if
entity in scoped else None)` — the registry match first, unchanged (id/name/alias, exact,
normalized, the one loader `list_entities`/entity-first search share), falling back to EXACT
raw-string membership of the caller's own `scoped_entities()` set. This is not a second resolver:
`scoped_entities()` is the SAME existence rule the absence gate one line later already consults
(D6, unchanged in shape), so the fallback and the gate are one fact read once.

**A second defect, closed by the same change: a timing oracle.** The
pre-amendment code called `scoped_entities()` only INSIDE the gate's `entity_id is None or
entity_id not in scoped_entities()` — short-circuited away entirely for a never-registered input,
so a registered-but-out-of-scope id paid a DB query a never-registered one did not. Response
latency itself was an oracle for "does this name mean anything to the registry at all," a
narrower leak than D5's own but a real one. `scoped = set(self.scoped_entities())` is now computed
UNCONDITIONALLY, before resolution, and reused — not re-queried — by both the fallback above and
the gate: one call, every input, whether resolution succeeds or fails.

Leak-safe by construction: membership is read from THIS caller's own ACL-scoped set, so an id
anchored only behind an ACL the caller does not hold still resolves to nothing for them, exactly
as before. Never normalized (unlike the registry match): a scoped id is an index fact (a
`pages_index.entity` element), not free text a person typed, so fuzzing the comparison could only
risk a false match, never help a real one. An anchored-but-unregistered id (`ghostco`) now
resolves honestly, with no registry metadata invented — `name: ""`, `aliases: []`, its own page
reference `null` unless a `type: entity` page happens to self-anchor it too. `list_entities` and
`describe_entity` now agree on existence for every id either can see.

*Original ruling, kept for the record (superseded above):* `list_entities` answers "what entities
exist" (existence, via `scoped_entities()`) and serves an unregistered-but-anchored id honestly as
`{"id": ...}` alone. `describe_entity` answered "what do we know about X" and resolved STRICTLY
through `entity_aliases.resolve_exact` (id/name/alias, exact match, the SAME loader entity-first
search and `list_entities` share) — an id absent from the registry returned the standard absence
shape even when pages were genuinely anchored to it, because the layer that would describe it
(registry metadata: name, type, aliases) had nothing to show. This was a live design choice, not
an oversight: the alternative considered at the time (falling through to a raw anchored-id lookup
when registry resolution failed) would have given `describe_entity` a second resolution
mechanism — which the amendment above resolves differently, by recognizing that a scoped-set
membership check simply IS the existence rule already in play one line later, not a second
mechanism at all.

**D6 — existence-scoping is ONE check, reused for both "doesn't exist" and "exists but hidden".**
`describe_entity`'s absence shape (`{"error": "unknown entity: <input>"}`) fires from a single
condition — `entity_id is None or entity_id not in scoped_entities()` — deliberately reusing the
SAME existence rule `list_entities` already relies on, rather than a second, `describe_entity`
-specific existence check that could drift from it. **Entity existence is itself ACL-scoped**,
which is what makes an unknown name and a wholly-out-of-scope registered entity indistinguishable
to the caller, mirroring `read_page`'s pre-existing "unknown page" rule on a second surface.

**D7 — a schema gap, closed the same way every other frontmatter column already is.**
`describe_entity`'s view layer needs `generated_at`
(ISO-8601, view-only frontmatter) — a field no existing `pages_index` column carried, since
views set neither `updated` nor `as_of`. Rather than serve an honestly-empty field for a real,
already-authored fact, `generated_at text NOT NULL DEFAULT ''` joins `links` as a second new
column, parsed by `corpus.page_row` exactly like every other frontmatter
column. The schema is wipe-and-rebuild ([ADR 012](./012-hybrid-index.md)), so this costs nothing
beyond the column itself — no migration, no backfill.

**D8 — the view path formula is inlined, not imported.** `describe_entity` computes
`views/<id>.md` directly rather than importing `views.staleness.view_relpath`.
`stigmergy.server` has no existing, reviewed edge into `stigmergy.views` (a governed writer beside
the API, not a layer of it — the same reasoning that keeps `stigmergy.server` off
`stigmergy.entities` except through named, narrow exceptions), and importing the view package for
one string would transitively pull in its own commit-and-push path — `views.writer`, and through
it `librarian.gitcmd`/`.githubapp` — reopening, through a side door, the "the server never imports
the librarian" boundary `tests/test_architecture.py` holds everywhere else. The formula is
simple, stable, and deterministic by contract; a future change to
the view path convention is a change in exactly two places (`views/staleness.py`,
`server/service.py`), not a reason to open a new package edge for one string.

**D9 — the read site is deleted, not fixed.** `src/stigmergy/site/` (the ACL filter, the
verification banner, the Quartz build orchestration), `server/read_site.py` (the Slack link
resolver), their CI workflow, local build script, and assertion script are removed outright rather
than repaired and kept. The alternative considered and rejected — keep the site and fix the
publish predicate first (`site.filter.is_published`'s presence-excludes rule
published a page whenever its frontmatter mentioned `acl` at all, which the fast lane's own
`librarian.acl_rules.resolve` never wrote, so 21 of 22 real pages were publishable, personal
material among them) — was rejected on the plainest ground available: **zero readers, never
deployed.** The Cloudflare Pages deploy credentials the workflow gated on were never set; the
nightly build had already been disabled down to `workflow_dispatch` only; nothing in this
repository, in staging, or in any operator's own use ever served a page from it. Fixing a predicate
nobody's traffic depends on, for a surface nobody has ever loaded, is effort spent maintaining an
artifact rather than shipping the graph walk this design actually promises — which the other eight
decisions here now serve through `read_page`/`list_entities`/`describe_entity` instead.

**The deletion is a no-op in production, not a behavior change.** `STIGMERGY_READ_SITE_URL` was set
nowhere, so `slack.app.build_context` was already wiring the byte-for-byte equivalent of
`slack.settings.no_link_resolver` before this change — every citation already rendered with the
"Show it here" affordance and no link. `app.build_context` now wires `no_link_resolver` directly,
as a permanent value rather than a configured fallback; the `link_resolver` SEAM itself
(`render.render_answer`'s own contract) is untouched, so a future browsable surface wires its own
resolver back in exactly the same place, unchanged. `corpus.is_published` (presence-excludes) had
exactly one caller — `site.filter.copy_published_pages` — and is deleted with it: it removes the
only presence-excludes reader in the codebase, so a future surface that wants "published"
semantics must design its own predicate on purpose, deliberately, rather than inherit this one's
strictness by reflex — this record is that predicate's rebuild path. The
`ACL_REACHABILITY_EXCEPTIONS["server/read_site.py"]` entry is pruned with the module: the fifth
hand-rolled ACL dialect this codebase carried dies with it, proven complete (not merely claimed)
by `test_no_acl_exception_has_gone_stale`.

## Known limits

- **Every ranking change is explicitly excluded**: alias→lexical expansion, `inlinks`
  as a factor, the registry `type` factor. These decisions change what is SERVED and FOUND, never
  what SCORES — scoring factors are arbitrated against the golden set, in their own batch, so that
  a retrieval regression has exactly one suspect.
- **The registry file is read fresh on every unfiltered search and every `list_entities`/
  `describe_entity` call** — no service-level cache. Accepted: it is a small local file, not a
  database round trip; an mtime cache is the documented fix if this ever measurably hurts.
- **Two stem-resolution snapshots remain two call sites** (build-time in-memory, webhook one-query)
  sharing one algorithm rather than being unified into a single code path — the parity
  test is the guard against them drifting apart silently; it is not a claim that unifying the
  snapshots themselves was ever attempted.
