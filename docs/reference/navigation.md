# Entity navigation — the read path walks the house

How the graph is served and how to use it — the what and the where. The pattern
underneath: *search finds the door; the agent walks the house* — it reads full pages and follows
wikilinks one hop. The code is the server's, so the code map is
[`src/stigmergy/server/index.md`](../../src/stigmergy/server/index.md), shared with
[`server.md`](./server.md).

Making that literally true took five things, and they are what this document covers: the graph
becomes a stored, ACL-scoped column; `read_page` serves it; two tools (`list_entities`,
`describe_entity`) expose the entity vocabulary; entity-first resolution lives in the one place
every MCP client shares; and an entity page's own hand-written metadata becomes lexically
findable.

## The graph in the index

`pages_index` carries `links text[] NOT NULL DEFAULT '{}'` — resolved, repo-relative OUTBOUND
wikilink targets, never stems. A stem resolving to several pages stores every match (the same
"credit every match" semantics `inlinks` already had); a stem resolving to nothing stores nothing
— a dead link is the knowledge-repo linter's finding, not the index's. A page is excluded from its
own outbound list. A GIN index (`pages_index_links_gin`) turns the INBOUND view (backlinks) into a
containment lookup (`links @> ARRAY[path]`), never a table scan.

**One resolution algorithm, two snapshots.** `index.corpus.resolve_links`/`by_stem_index` is the
one place stems become paths. The full rebuild (`corpus.load_pages`) builds its `stem -> [paths]`
index from its own in-memory whole-corpus walk; the incremental webhook
(`server.webhook._resolve_outbound_links`) builds the same shape from `store.existing_paths`'s
one-query snapshot of `pages_index`'s CURRENT rows, then calls the identical `resolve_links`. A
page added in the same push as a sibling it links to cannot resolve that one link yet (the
snapshot predates the transaction that will land both) — reconciled at the next rebuild,
exactly like `inlinks` always was. `tests/server/test_webhook.py`'s parity test pins that the two
snapshots agree on the same corpus, so this stays a fact about the code, not an assumption.

**Split chains are propagated at BUILD time.** A split document's continuation parts carry an
EMPTY `superseded_by` in their own frontmatter — only the primary page gets the field stamped. Two
part-id conventions exist and both are matched. The live one is **`<id>#p<n>`**, declared: the
splitter names the FILE `<stem>-p<n>` but stamps `id: "<stem>#p<n>"` into the part's own
frontmatter, and the declared id wins over the filename stem when the corpus is read. The other is
the bare `<stem>-p<n>` stem, which is now the fallback for parts filed before the producer stamped
an id at all. Matching only one of the two left this propagation inert over a whole generation of
parts, silently — which is why both stay matched even though the live producer only writes one.

Compensating at RANK time instead would mean reconstructing chain membership from whichever
candidates happened to be in a given search's pool — which silently fails to demote a continuation
part whenever its primary falls outside that pool. `corpus.load_pages` therefore propagates the
primary's `superseded_by` onto its siblings over the WHOLE corpus — so by the time a row reaches
`rank()`, its own column already tells the truth regardless of what any one query's candidate set
contains.

The propagation is **marker-gated and directional**, not "first non-empty value in the group": only
a row whose `page_id` IS the chain base may DONATE (`corpus.is_chain_primary`), and only a row
matching `corpus.chain_part_pattern(base)` may RECEIVE. The chain key also carries the page's
DIRECTORY — exactly like `rank()`'s collapse key — because two id-less pages sharing a file stem in
different folders fall back to the same stem-derived `page_id`, and `report-p2.md` is a plausible
human filename in a way `#p2` never was. A real chain's parts sit beside their primary, so the
directory dimension never splits a genuine one.

## `read_page` serves the graph

`read_page(path)` returns, beside the trust signals and the fenced `body`:

- `type`, `status` — columns that were already fetched and never shaped.
- `supersedes` / `superseded_by` — temporal navigation.
- `links` — outbound, from the page's own row: `[{"path": ..., "title": ...}, ...]`.
- `backlinks` — inbound, via the GIN containment query: the same shape.

Both lists are **existence-scoped**: every entry filters through `server.acl.visible()` before
shaping, so an out-of-scope target is simply ABSENT — never annotated, never a count discrepancy
hint. Titles are `neutralize_fence`d and never fall back to the raw path when empty (the
`answer/service.py::_titles_for` discipline, reapplied inside `server/service.py` since the
server may not import the answer layer). Both lists cap at `NAV_CAP` (20) entries **shown**, with
the true total and the not-shown count stated in a sibling `links_note`/`backlinks_note` field —
never a silent cap:

```json
{
  "path": "wiki/entities/acme.md",
  "title": "Acme Corp",
  "entity": ["acme"], "as_of": "2026-07",
  "type": "entity", "status": "",
  "supersedes": "", "superseded_by": "",
  "links": [{"path": "wiki/notes/acme-renewal.md", "title": "Acme Renewal"}],
  "links_note": "1 page(s) linked from this page — showing all 1.",
  "backlinks": [],
  "backlinks_note": "No pages link to this page.",
  "banner": null,
  "body": "<<<UNTRUSTED-DATA\n...\nUNTRUSTED-DATA;end>>>"
}
```

The fenced `body` contract is unchanged: stems stay in the body (it is the page's own untrusted
content), and the structured fields above are the navigation surface an agent should actually
walk — never a reason to parse `body` for links.

## `list_entities` — the vocabulary, served

`list_entities()` takes no arguments and returns the ACL-scoped entity vocabulary:
`scoped_entities()`'s id set (an id is in scope iff at least one anchoring page is visible to the
caller), each enriched from `ops/entity-registry.json`:

```json
{"count": 2, "entities": [
  {"id": "acme", "name": "Acme Corp", "type": "organization", "aliases": ["Acme"],
   "approved_by": "ana@example.com"},
  {"id": "ghostco"}
]}
```

`approved_by` is the one lifecycle fact the registry carries: the person whose capture introduced
the identity. There is no `proposed`
state to serve — an identity is born confirmed — and a record from before the field existed carries
it empty.

An anchored id absent from the registry serves as `{"id": ...}` alone — honest, and the
gardener's business, not an error. `count` states how many; nothing is dropped past it (the
registry is small). No registry at all — neither an index snapshot nor a readable
`--entity-registry` file — serves every id that way (the loader's documented fail-open); a
MALFORMED one raises loudly as `RegistryError` (an operator-visible fault, never silently degraded
navigation).

## `describe_entity` — layered and dated, never a flat list

`describe_entity(entity)` answers "everything anchored to X" structurally, in three layers.
`entity` accepts a registered id, its canonical name, or a
declared alias — normalized exact match through `entity_aliases.resolve_exact`, the SAME registry
loader `list_entities` and entity-first search read — **or** EXACT raw-string
membership of the caller's own scoped-id set, never
normalized, when no registry match exists: the same existence rule the absence check below already
consults, not a second resolver.

1. **`entity`** — registry metadata (`id`, `name`, `type`, `aliases`, `approved_by`) plus the
   entity's own page reference `{path, title}` (found via `type = 'entity' AND <the id> = ANY(entity)` — entity pages
   self-anchor — `ORDER BY path LIMIT 1`, so a multiply-self-anchored id returns the same row
   across rebuilds instead of whatever Postgres's scan order happened to be), when that page is
   visible; `null` otherwise. The lookup itself is UNSCOPED on purpose: the path is needed to
   exclude that page from the timeline structurally, whether or not THIS caller can see it.
2. **`timeline`** — every OTHER page anchored to the entity (excluding its own page):
   `{path, title, type, status, as_of}`, dated entries first (newest `as_of` first),
   then undated entries by path (`ORDER BY (as_of = ''), as_of DESC, path ASC`). Existence-scoped
   per member, capped at the same `NAV_CAP` (20) `read_page` uses, with the truncation stated the
   same way.

**This IS the per-entity rollup — there is no stored one.** "What do we know about X" is answered
at READ time: `describe_entity` assembles the territory above per caller, and `ask` writes the
prose from it, under that caller's own identity. There used to be a `views/<id>.md` page holding a
skeleton and an agent-written synthesis, kept fresh by a convergence sweep. Both are gone, and what
replaced them is strictly better scoped: one stored page has to be true for everybody at once, so
it carried no `acl:` at all and could only ever summarise the OPEN subset — a finance reader's own
material was named nowhere in it. The timeline below is that reader's own.

**Absence is existence-scoped, not merely "not found."** An unregistered AND unanchored input, a
registered id resolved but never anchored anywhere visible, and a registered id anchored ONLY
behind an ACL the caller does not hold all return the byte-identical
`{"error": "unknown entity: <input>"}` — one check (`entity_id is None or entity_id not in
scoped`) decides all three, mirroring `read_page`'s own "unknown page" rule: existence itself
is scoped.

**An anchored-but-unregistered id agrees with `list_entities`, not against it**.
`list_entities` surfaces such an id honestly as
`{"id": ...}` alone, because it enumerates EXISTENCE — and `describe_entity` resolves it too,
via the scoped-set fallback above, with no registry metadata invented (`name: ""`, `aliases: []`).
Resolving STRICTLY through the registry made the same id "known" to one tool and "unknown" to the
other — which broke the very navigation loop
(`list_entities` -> `describe_entity` -> `read_page`) this surface exists to build; see
D5 for the full reasoning and the ruling that replaced it.

## Entity-first search, everywhere — and `ask` knows the topology

"Does this query name a registered entity, and should the search scope to it first" is answered in
`BrainService._search`, so every MCP client gets it — `search_brain` over stdio or HTTP, Slack,
`ask`, any future agent. When the caller passed
**no explicit `entity` filter** (key presence, not truthiness — `filters={"entity": ""}` still
counts as explicit), the query is resolved against registry aliases
(`entity_aliases.aliases_from_text` + `resolve_entity`, over the registry this service instance
resolved — the index's snapshot where the database has one, the `--entity-registry` file where it
does not; see "Which registry the server serves" in [server.md](./server.md). Re-read at every
`_call` seam: the registry is small, and freshness is the point). `resolve_entity` takes the LONGEST registered id/name/alias that
appears as a whole-word phrase in the question, so "Acme Corp" wins over a shorter alias "Acme" that
also matches, and a two-letter alias like "gx" cannot match inside an unrelated word. On a match the resolved id is TOLD to the ranker
(`entity_hint`) and one blended search runs — it is not scoped to that entity. Scoping was the
original shape and it eclipsed rather than layered: any hits at all for the resolved entity meant
the blended ranking never ran, so a company-wide page (`entity: []` — a policy, a process, a
cross-cutting decision) was unreachable through every query naming a registered company. Resolving
an entity can now only change the ORDER of the results, never their membership (
amended).

**There is no filter left to drop, and the hint is the whole mechanism.** The rank-time boost and
the lexical alias expansion below are what "entity-first" now means: the named entity's own
material leads because it SCORES higher, not because everything else was removed.
`AnswerBrain.search_text` delegates straight through (8 hits per call), with a `filters`
passthrough of its own.

`ask`'s agent-facing search tool (`synthesize.py`) accepts `filters` too — the agent can pass
`{"entity": "<id>"}` once an id is known from a previous result. An unknown filter column returns
the error AS THE TOOL'S OWN RETURNED STRING (a repair brief the agent reads), never a crash of the
run. `ANSWER_SYS` states the topology explicitly: the index resolves known entity names
automatically, prefer `filters={"entity": <id>}` when an id is known, and read the entity's own
page (`type: entity`) and follow the `links`/`backlinks` `read_page` serves — one hop — before
concluding. The agent's budgets (`ANSWER_REQUEST_LIMIT` 6 requests / `ANSWER_TOOL_CALLS_LIMIT` 8
tool calls) are untouched by any of this.

**The same seam feeds two more things, and both are TOLD, never inferred.** The id
`_search` resolved travels on as `entity_hint` into `_run_search`, where it feeds the rank-time
entity boost, and hands the LEXICAL arm the registry's
other spellings for that entity (`_expansion_terms` — canonical name plus aliases) as extra
OR-lexemes, so a query naming an alias lexically matches pages naming the canonical form and vice
versa. The vector arm still embeds the raw query untouched. `entity_hint=None` fires neither.
`ask`'s third tool, `describe_entity` (`answer/brain.py::entity_text`), lets the agent ask for the
layered account directly instead of reconstructing it from search hits.

## Entity metadata pays — indexed

`corpus.page_row` folds a `type: entity` page's own `role` and `aliases` frontmatter into the
`tsv` source text (`corpus._entity_meta_text`, joined in `store._TSV_SQL`) — the context a person
wrote on the page becomes lexically findable exactly like `tags`/`mentions` already were. No
ranking factor is added or changed; this only widens what a plain lexical search can FIND.

**The direction is one way, and it matters for anyone editing an entity page.** The entity page in
`wiki/entities/` is the ONE source: the `role`, `aliases` and everything else written there. Two
things derive from it, independently and in one direction each:

- **the registry** — `entities.generator` reads `wiki/entities/` and nothing else, and writes
  `ops/entity-registry.json` as a pure function of those pages (the canonical id is
  `slugify(title)`, so nothing in the file is unrecoverable). It is a library, not a command: the
  librarian worker regenerates the whole file inside the commit that introduces an identity or
  teaches a registered one a spelling.
- **the index** — a rebuild folds the entity page's OWN `role`/`aliases` frontmatter into `tsv`.

So editing `aliases` on the page changes BOTH, at their own cadences: `tsv` at the next index
rebuild, the registry at the worker's next write into the identity zone. Nobody hand-edits either
side — a page and the registry that disagree are drift, which the knowledge repo's own contract
linter reports and which the librarian refuses to introduce an identity against. No ranking or
retrieval query reads the registry, and the registry generator never reads the index. Three
artifacts, one direction each time data moves between them, never a cycle.

The index does CACHE the registry — one `ops_file_snapshot` row, written by the push webhook and
by a rebuild — but purely as a courier for the server, which holds no checkout in
production; the bytes are stored verbatim and interpreted only by `server/entity_aliases.py`. See
[hybrid-index.md](./hybrid-index.md#the-ops-files-ride-along).

## Where the code lives

| Concern | Module |
|---|---|
| Wikilink resolution (both directions), build-time split-chain propagation | `index/corpus.py` (`resolve_links`, `by_stem_index`, the `load_pages` tail) |
| What counts as a chain primary / a chain part | `index/corpus.py` (`is_chain_primary`, `chain_part_pattern`), `index/rank.py` (`chain_base`) — one definition, shared by the build and the webhook |
| Schema (`links`, its GIN index, `generated_at`) | `index/store.py` |
| Webhook's one-query outbound resolution + its incremental supersession window | `server/webhook.py` (`_resolve_outbound_links`, `_propagate_split_chain_supersession`) |
| `read_page`'s graph shaping, the shared cap+note base | `server/service.py` (`fetch_page_raw`, `_read_page`, `_capped`/`_cap_note`/`_nav_section`) |
| `list_entities` / `describe_entity` | `server/service.py` (same class, same `_call` seam every other tool rides) |
| Registry reading (full records + exact-match resolution) | `server/entity_aliases.py` (`registry_from_text`, `resolve_exact`) |
| WHICH registry copy is read (index snapshot, else the `--entity-registry` file) | `server/service.py` (`BrainService._registry_source`), refreshed by `server/webhook.py` and `index/build.py` |
| MCP tool closures | `server/mcp_server.py` |
| Entity-first resolution | `server/service.py::BrainService._search` |
| The entity boost + lexical alias expansion the resolution feeds | `server/service.py` (`_run_search`, `_expansion_terms`), `index/rank.py`, `index/search.py` |
| `ask`'s topology instructions + `filters`-accepting search tool | `answer/synthesize.py` (`ANSWER_SYS`, the `search` agent tool) |
| The graph and the entity account as the AGENT reads them | `answer/brain.py` (`page_text`'s `_render_nav`, `entity_text`) |
| Entity-page metadata → tsv | `index/corpus.py` (`_entity_meta_text`), `index/store.py` (`_TSV_SQL`) |

## Tests

`tests/index/test_r1_split_chain_pg.py`, `tests/index/test_entity_meta_tsv_pg.py`,
`tests/server/test_read_page_graph.py`, `tests/server/test_entity_tools_pg.py`,
`tests/server/test_entity_first_search_pg.py`, `tests/server/test_webhook.py` (the parity test
this document's "one algorithm, two snapshots" claim rests on, plus the split-chain propagation
over BOTH part conventions), `tests/answer/test_synthesize_entity_topology.py`,
`tests/answer/test_navigation_rendering.py` and `tests/answer/test_describe_entity_tool.py`
— see each package's own `index.md` Tests table for what specifically each one covers.
