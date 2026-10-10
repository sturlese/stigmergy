---
status: implemented
owner: platform
---

# Retrieval and curation

This is the contract of the read path (search, listing, entity description, page reads), of the
curation the writer does around it (aliases, filing context), of the graph measures that describe
the result, and of the evaluations that measure it. It extends `specs/karpathy-team-wiki.md` §4.1
(model boundary), §5.6 (what the librarian receives), and §5.12 (search and index); audiences
follow `specs/audience-layers.md`. Examples use invented names (Acme, Acme Capital); deployment
data stays out of this repository.

## 1. Outcome

Search ranks the pages most similar to a question, using the wiki's links; `list_pages` returns every
visible page of a kind, a period, or a connection; a detail in a long page is found, read, and quoted
through its fragment. Administrators see the graph's measures. The writer records the other names an
entity is linked by.

## 2. Constraints

- **C1. No oracle.** Every read applies the reader's audience in SQL before any limit, fuses rows by
  virtual path, and answers identically whether or not content exists in layers the reader cannot
  read: rows, totals, counts, cursors, scores, resolution failures, and errors. Graph measures count
  every layer and are served only to administrators.
- **C2. No pattern interpretation.** No regex or keyword lexicon interprets questions or prose for
  intent, link types, names, or dates; a model interprets, given complete, structured, ACL-scoped
  data and typed tools. Entity resolution matches the wiki's own titles and aliases; fragments and
  summaries follow Markdown structure and length, never meaning.
- **C3. The librarian's prompt is untouched.** Nothing here changes `knowledge/librarian_skill.md`,
  the planner prompt, or the plan schema. The alias step never calls the librarian; the filing
  context changes what the librarian receives, not what it is told.
- **C4. The index is derived.** Apart from erasure (`specs/data-erasure.md`), only the webhook's
  incremental update and the full rebuild write the index (`pages_index`, `page_fragments`,
  `embedding_cache`), which Git can always rebuild; the writer never does. Search calls no language
  model; its one model call is the query embedding.

## 3. Parts

### 3.1 Listing pages

`list_pages` returns every visible page matching its filters, one row per virtual path, 50 per call.

| Parameter | Meaning |
|---|---|
| `type` | `note`, `concept`, `entity`, `project`, or `source` |
| `entity_type` | `person`, `organization`, `product`, `tool`, `repository`, or `place`; implies `entity` |
| `layer` | One layer the reader may read, `public` included; any other name is an error listing the readable ones |
| `since`, `until` | `YYYY-MM-DD`. A page matches when a source it cites is dated in the range; a source matches on its own date, its `occurred_at`, else its `captured_at` |
| `links_to` | A page path, or an entity title or alias resolved as `describe_entity` resolves it; the pages whose links reach it |
| `sort` | `recent` (latest cited-source date first, the default), `oldest`, or `title`; undated pages last, ties by path |
| `cursor` | Opaque; the previous call's `next` |

- Each row: `path`, `title`, `type`, `layers`, the one-line `summary` of §3.3, `first_source` and
  `last_source` dates, the number of visible sources it cites, and for an entity its `entity_type`
  and `aliases`. Bodies and ACL labels never ship.
- The response carries `total` (every visible match), `count`, `next`, and the reader's readable
  layers with their descriptions, so an agent learns what each layer means on its first call.
- Filters apply to the fused page: its type and entity type as readers see them, a row in `layer`,
  a link from any visible row, a cited source in the period. Cited sources resolve in the page's own
  layer and `public`, as its links do.
- A cursor is a keyset (sort key, virtual path) bound to a digest of the caller's own arguments,
  never to what they resolved to. An unknown and a hidden `links_to` name give the same empty result.

### 3.2 Fragment index

The vector arm ranks fragments, not whole pages. A fragment is an exact, contiguous slice of one
stored page, embedded as `{title} › {heading trail}\n{text}`. A page's fragments reassemble its
text, overlaps aside, and no fragment crosses a page or a layer. Embeddings are
`openai/text-embedding-3-large` at 2,560 dimensions through OpenRouter, stored as `halfvec`; the
endpoint rejects an input above 8,192 tokens. A token covers at least one UTF-8 byte, so byte bounds
are token bounds in any script.

| Page | How it is cut |
|---|---|
| Note, concept, project | At H2 and H3 headings; text before the first is the lead fragment |
| Entity | Never: one fragment. A page above 8,000 bytes is embedded alone, and only when the endpoint refuses it for its length (HTTP 400) is it cut as a note is; any other error fails the indexing |
| Source | At every heading level, or into windows where there is none |

- A page of at most 400 words and 4,000 bytes is one fragment, and so is a section within those
  bounds. A longer section becomes windows of about 350 words, ending on a paragraph boundary when
  one falls past 60 percent of the window, each opening with about 50 words of the previous one. A
  section under 40 words joins the next; a short last section joins the previous.
- Cuts fall on paragraph and line boundaries (a Slack thread cuts between messages); a heading in a
  code fence is text. A line longer than a window is cut between words into pieces of at most 100
  words and 4,000 bytes.
- No cut fragment exceeds 4,000 bytes and the title (200 characters) and trail (300) are capped, so
  every cut piece embeds within 8,000 bytes. An overlap never exceeds a quarter of its window in
  words or bytes, so a page yields at most 1.4 times its text.
- Fragments carry their page's audience and die with their page row. The embedding cache is keyed by
  model and text hash; each vector is cached before its fragment is written and copied into it inside
  Postgres, so a rebuild holds in memory only the vectors it newly embedded.

**Audience and the HNSW scan.** Each audience of a tenant (`public` and every layer holding a page)
has its own partial HNSW index over its fragments alone. A reader's vector statement is one scan per
audience it holds (unscoped: every audience with a page), each ordered by distance alone so that its
index serves it, under `hnsw.iterative_scan = relaxed_order` (pgvector 0.8 or later) and
`hnsw.ef_search = 160`. Content the reader cannot see is in no graph its scans walk, so it neither
appears nor shapes its neighbours (`specs/audience-layers.md`, LAY-06). The rebuild creates every
audience's index, drops stale ones, and analyzes the tables; an incremental update creates the index
of an audience's first page; an audience without one is scanned exactly. The arm takes the 160
nearest fragments, keeps each page's nearest, and passes the 40 nearest pages to fusion.

**Reading a fragment.** A hit whose nearest fragment is one of several the reader sees carries
`fragment: <virtual path>#<n>` and `fragments`, and its snippet comes from that fragment (400
characters); `n` counts from 1 in reader order, `public`'s fragments first, then each layer's.
`read_page` and the answer agent's `read_pages` accept `path#n` and return the page header with
fragment n, the end of the one before, and the start of the one after, within the 6,000-character
excerpt budget and rendered as the fused page renders its rows; a number the reader cannot see reads
as an unknown page. A plain read returns the head of the body, with a note naming the `path#n` form
when the body is longer. The answer verifier checks a quote against what the run read of that page:
its head and every fragment window it opened.

### 3.3 Search

`search_brain` (MCP; 5 results by default, at most 80) and the answer agent's `search` (16 results)
share one path. `filters` takes `zone`, `type`, `status`, and `entity` (the entity page and every
page linking it); any other name is an error.

- **GS-05 Entity names.** Unless `filters.entity` is given, search resolves every visible entity
  whose title or alias the query contains, comparing runs of letters and digits so punctuation never
  hides a name. Longer names claim their words first; a name two entity pages share resolves to
  neither; names under 3 characters never resolve; at most 4 resolve, in order of mention. Names
  come from one query of entity paths, titles, types, aliases, and layers (never bodies), memoized
  per tool call; `describe_entity` and `links_to` resolve exact paths, titles, and aliases from it.
- **GS-08 Lexical arm.** Postgres full text over title, aliases, and body, the query's normalized
  lexemes joined by OR and expanded with the resolved entities' titles first, then their aliases in
  turn (at most 12 terms of 100 characters). Top 40 pages.
- **Vector arm.** The query embedding (3-second timeout) against the fragment index; top 40 pages.
- **GS-07 Intersection.** With two or more resolved entities, the pages linking all of them, newest
  first and at most 20, join as their own ranking even when neither arm found them.
- **GS-06 Fusion.** Reciprocal-rank fusion (k = 60), then contract factors: a page that is or links
  a resolved entity scores double once, and once more when it links every one of two or more
  resolved entities, never further; `evergreen` status scores 1.25 times; a page last updated over
  365 days ago scores 1/1.3. Ties break by path.
- **GS-09, GS-10 Neighbours.** The best 8 fused candidates are seeds. Each enumerates at most 200
  visible neighbours per direction (pages it links, pages linking it), newest first, and votes its
  score divided by `log2(2 + its visible degree)`. The 160 best-voted neighbours rank by their votes
  divided by `log2(2 + their visible degree)` (degrees capped at 256 per direction), newest first on
  ties; at most 40. Edges and degrees count only pages the reader can see, neighbours satisfy the
  query's filters, and the step is one SQL statement.
- **GS-11 Final ranking.** The lexical, vector, intersection (`entity`), and neighbour (`graph`)
  rankings fuse again, the neighbours at a quarter of an arm's weight, so they break ties and extend
  the tail; neighbours outside the candidates join them. Hits report their `arms`.
- **GS-12 Degradation.** When the query embedding times out, the vector arm is empty, seeds come
  from the other arms, and the response reports `vector_search: "timed_out"`.
- **GS-01, GS-02 Hits.** Each hit carries `path`, `title`, `type`, `layers`, `updated`, `score`,
  `arms`, `factors`, a 240-character `snippet` around the longest query word it contains, `summary`,
  and `links`. The summary is the first sentence of the first prose paragraph after the title (else
  the first list item), links shown as text, citations and emphasis removed, at most 240 characters,
  computed at query time from the body's first 8,192 characters with bounded patterns. `links` are up
  to 8 reader-visible outbound links as `{path, title}` in path order, capped after visibility. The
  response states how many pages it ranked and that search is not exhaustive (`ranked`, `note`).

**Answer agent.** **GS-03** Its `search` renders 16 hits with path, title, type, date, summary,
fragment reference, snippet (200 characters, 400 for a fragment), and link paths. **GS-13** Before
the first model request the server runs one search with the question, through the same scoped path
and evidence ledger as the tool, and places the fenced result in the first message; it spends none
of the agent's tool calls. **GS-16** A database statement cancellation during that search rewinds
the ledger, and the agent starts with an instruction to search first. **GS-14** `read_pages` reads
up to 5 paths for one rate-limit token, each rendered and recorded as `read_page` does, a hidden and
an unknown path alike; MCP keeps the single `read_page`. **GS-15** The instructions say the first
search already ran, the pages relied on are read in one `read_pages` call, a new search is for gaps
only, enumeration uses `list_pages`, and a hit's `fragment` is read with it.

**Describing an entity.** `describe_entity` returns the entity page (6,000-character excerpt) and
the visible knowledge pages that link it, newest first and at most 20, each with a 1,200-character
excerpt, up to 6 sentences connecting it to other visible pages, and up to 6 of its sources; plus up
to 40 visible sources overall. An unknown, an ambiguous, and a hidden name read alike.

### 3.4 Graph measures

One module (`index/measures.py`) defines the measures; the panel, the tests, and the evaluations
read it. They are analytics (main specification §4.9): they describe the graph, create no task, and
none is a corpus-health finding. A scope is `brain` (every row), `public`, or a layer that holds a
row (its rows fused with `public`'s by virtual path, as its readers see them).

| Measure | Definition |
|---|---|
| Pages | Knowledge pages and sources, by type |
| Links per page | Distinct wikilink targets per knowledge page, itself excluded |
| Dead links | Wikilink names that resolve to no page, or to several, in the row's layer and `public` |
| Orphan entities | Entity pages no other page in the scope links |
| Multi-source entities | Entity pages citing two or more sources |
| Cited paragraphs | Share of knowledge-body paragraphs and lists that cite a source, the Entities, Connections, People and organizations, and Notes lists excluded; a block holding only a citation cites the block before it |
| Entities without aliases | Entity pages whose alias list is empty |
| Thin entities | Entity pages with at most one item under `## Facts` |
| Hubs | The ten pages with the most inbound links |

- Every index update that moves the indexed commit stores one snapshot per scope, once per commit,
  as kept history; a failed snapshot is logged and never fails the update.
- `GET /admin/api/graph-measures?scope=` returns, to administrators only and without model calls,
  the current measures, the last 400 snapshots, the value seven days earlier, the hubs, the
  drill-down lists (orphan entities, thin entities, entities without aliases), and the constellation.
- The Graph measures panel in Index health shows a tile per measure with its sparkline and weekly
  trend, a scope filter, a constellation of the hubs, their neighbours, and the orphan entities, a
  chart of one measure over time, and the drill-down tables.

### 3.5 Aliases at filing time

The librarian links an entity under another of its names (`[[Title|variant]]`) but rarely adds the
variant to `aliases`, which would need a complete `update` of the entity page. The writer records it
when the link is made: no job, no new writer operation, no screen, no table of decisions.

- **Candidates.** After the plan passes the gates, every `[[E|T]]` link the plan added to a created
  or updated body (a link the page already held is not asked again), where E is an entity page of
  the writing view and the normalized T is neither E's title nor an alias. Excluded: a T any page
  already answers to as a link name, a T the plan uses to link another entity, a T without letters
  or digits, and a T whose words all appear in another entity's name (a first name two people
  share). At most 8 per capture; the rest are reported as skipped.
- **Decision.** `kernel.llm.choose` asks the typed-decision model (`typesafe/jev-1.13`) to choose
  `alias` (a spelling, abbreviation, short form, first, full, or former name of the same entity) or
  `other` (a description, role, title, part, possession, product, or different entity), given E's
  title, entity type, one-line summary, and aliases, the candidate, and the words around the link.
  Decisions run one at a time, one request each, no retry, within a 10-second window that leaves the
  operation budget 60 seconds. `alias` at 0.75 or above accepts; a lower score, a failure, or a spent
  window writes nothing and the capture lands. A deadline or lease abort of the capture propagates.
- **Apply.** Accepted aliases are written into E's frontmatter in the candidate tree, all or none,
  and checked by the gates (alias uniqueness within the layer, page contract) before the layer
  projection and the commit; a violation reverts every alias. The capture report's `aliases` lists
  every candidate with its decision and probability or reason. A recompile keeps an entity's prior
  aliases that no other entity page now claims.
- **Scope.** E is eligible only where the writing view stores it whole: always for a public capture,
  and for a layer's capture when the layer holds the page or the page is new. Text from a restricted
  capture never becomes an alias of a public page.
- **Backlog.** The daily garden asks the same question, layer by layer on a clean corpus and under
  the same scope rule, for the `[[E|T]]` links already in each layer's pages: at most 32 candidates
  per layer within one 60-second window, starting at a point that rotates by day so a refused
  variant cannot hold the cap. The run counts the entity pages it changed as `aliases_recorded`.

### 3.6 Filing context

The writer builds the librarian's context (`knowledge/context.py`) from the writing view's checkout
(`public` plus the capture's layer) for each filing and each recompiled source, admitting only pages
the actor may read and that flow into the capture's audience.

| Part | Content |
|---|---|
| Catalog | Every admitted knowledge page: path, title, type, its first prose line (160 characters); entity type and aliases for an entity |
| Candidates, in full | Every entity and project page whose title or alias the source names, then the 12 pages sharing the most words with the source; each with status, sources, body, filed contradictions, and `capture_may_update` |
| Source evidence | Up to 12 sources the candidates cite, ranked by shared words; at most 64 references, none above 128 KiB, 2 MiB read in total |

- The rendered context is bounded at 768 KiB. Above it, the builder drops candidates down to one,
  then evidence down to one, then the last of each, then catalog lines, and marks it truncated.
- Each capture's report (`filing_context`) records the rendered bytes, the budget, the catalog,
  candidate, and evidence counts, and how many entries the budget dropped; the capture detail shows
  them. Index health reports the largest share of the budget and the trims over the last 50 landed
  captures, and warns when a capture was trimmed or passed half the budget.

**Deferred: the context from the index** (the Index health warning's "phase 4"). When the warning
fires, the context is to come from every entity name in the tree, the index's top 40 pages for the
source's fragments under the writing view's audience (top 12 in full, bodies from the tree), and
the pages of the writer's last ten commits, with no disk-scan fallback. It ships only when every
filing evaluation case keeps every page the full catalog led the plan to touch, and the gate passes.

## 4. Surfaces

MCP and the local bridge expose `search_brain`, `list_pages`, `read_page` (also `path#n`), and
`describe_entity`. The answer agent has the server's first search, then `search`, `list_pages`,
`read_pages`, and `describe_entity`. The backoffice shows graph measures and filing-context figures
in Index health and each capture's filing context in its detail.

## 5. Budgets

| Bound | Value |
|---|---|
| Query embedding | 3 seconds; on timeout the vector arm is skipped |
| Serving statement | 30 seconds |
| Search width | 5 by default on MCP, 16 for the answer agent, at most 80 |
| Page excerpt | 6,000 characters; a fragment window fits the same budget |
| Answer run | 6 model requests and 8 tool calls, then one evidence-only completion; 90 seconds per phase |
| Alias decisions | 10 seconds per capture, 60 per garden, each leaving 60 seconds of the operation budget; 10 per decision, 2 in flight per process |
| Filing context | 768 KiB rendered |
| Embedding request | 128 texts and 250,000 bytes, under the provider's 300,000 tokens |

Paid evaluation runs for this specification share a 3 USD cap (`evals/spend.py`). Each paid runner
takes `--live` and `--max-usd`, prices every call from its recorded usage, stops at its own cap, and
appends to `evals/out/spend.ndjson`; no run starts when the ledger total, or the OpenRouter key's
usage since the ledger's baseline, plus its cap would pass 3 USD. The keyless suite costs nothing.

## 6. Acceptance criteria

Numbers absent from this table are retired and not reused.

| ID | Criterion |
|---|---|
| BQ-01 | `list_pages` returns every visible page matching its filters, paged without loss or repetition, with the fields of §3.1 |
| BQ-02 | `list_pages` responses (rows, `total`, `next`, resolution failures, errors) are byte-identical with and without content in layers the reader cannot read |
| BQ-03 | `since`/`until` select pages by the dates of the sources they cite, not by edit time |
| BQ-04 | The answer agent uses `list_pages` for enumeration and period questions and `search` for lookups, measured by the listing golden (§7) |
| BQ-05 | Every stored page is covered by fragments that reassemble its text without loss beyond the overlaps; no fragment crosses a page or a layer |
| BQ-06 | The fragment index meets its detail bar (§7) and no retrieval family regresses |
| BQ-07 | A page whose text exceeds 8,192 tokens is indexed; every cut piece embeds within 8,000 bytes, and only a whole entity page is ever sent above that, alone |
| BQ-08 | On a corpus larger than `ef_search`, a reader of a small layer receives that layer's nearest fragments, and its vector statement reads only its own audiences' indexes, so the result does not depend on whether other layers exist |
| BQ-09 | A hit whose evidence is a fragment carries `fragment`; `read_page` with it returns that fragment and its neighbours; a quote copied from it passes verification; a plain read is unchanged |
| BQ-12 | Graph measures match §3.4 on a fixture repository, per scope, and are served only to administrators |
| BQ-13 | One snapshot per scope is stored for every indexed commit and kept as history; a read returns the current values, the series, and the value seven days earlier; every part of the panel reads the same module |
| BQ-14 | An alias candidate comes only from the plan's own bodies (in the garden, the layer's pages) and the writing view, and no text from a restricted capture reaches a public page |
| BQ-15 | Accepted aliases land in the capture's commit through the existing gates; a failed, unsure, or timed-out decision writes nothing and the capture still lands; every candidate and its decision is in the capture report |
| BQ-16 | The alias calibration accepts no non-alias at the threshold |
| BQ-17 | Filing-context figures are recorded for every capture and shown in the backoffice |

## 7. Evaluations

Search and answers are measured on `evals/graph_corpus/`, a frozen fictional wiki filed by the
production librarian, with a `finance` layer and four long sources (about 73,000 words) that
`evals/make_long_sources.py` writes with planted details; identities are in
`evals/graph_identities.json`. Each run appends to `evals/history.ndjson`, the baseline for the next.

| Set | Runner | Bar |
|---|---|---|
| Retrieval: `evals/graph_golden.json`, 238 questions (lookup, relational, neighbor, global, alias, refute, ACL and absent probes) | `run_graph_retrieval.py` through `BrainService` search: recall at 8, 16, 32, MRR, entity share, leaks, latency; `--ablate` turns graph features off in the runner only | No leak; a ranking change costs no family more than 0.03 MRR |
| Fragments: `evals/graph_detail_golden.json`, 50 details in the long sources | The retrieval runner with `--golden`; `run_qa.py --golden` | Recall at 16 at least 0.8; at least 80 percent answered correctly and cited |
| Listing: `evals/graph_listing_golden.json`, 8 enumeration and period questions | `run_qa.py --golden` | At least 7 of 8 list completely |
| Answers: `evals/graph_qa_subset.json` (22), `evals/qa_golden.json` | `run_qa.py`: groundedness, honesty, refutation, requests, seconds, tokens | The bars of `evals/bars.py`; no regression |
| Aliases: `evals/alias_golden.json`, 36 invented candidates (true other names; roles, descriptions, parts, products named after their company, possessions) | `run_aliases.py`, real classifier | No non-alias accepted at the threshold; recall reported |

Graph measures have no evaluation bar; BQ-12 and BQ-13 are tests on fixture repositories.

## 8. Decisions

| Decision | Reason |
|---|---|
| `list_pages` enumerates; search ranks and says it is not exhaustive | One exhaustive catalog tool, and an agent that knows which to call |
| Dates come from cited sources, not edits | "What did we see in September" is about when things happened |
| Notes, concepts, and projects follow their headings; entity pages stay whole; long sources are windowed | An entity's long sections are lists whose windows each touch many subjects; cut, a hub page's best fragment wins almost any query |
| A fragment is readable, and quotes are verified against what the run read | A detail the search finds must be quotable |
| Neighbours count a quarter of an arm | At full weight they push the page answering a direct question down; at a quarter they break ties and extend the tail |
| The answer agent sees 16 hits, and the server runs its first search | Most of the recall of 32 at half the tokens; one model turn saved on every answer |
| Aliases are written by the writer at filing time, decided by the typed-decision model | The judgment exists when the librarian links the variant; typed answers with abstention leave the librarian's prompt untouched |

## 9. Excluded scope

- Indexing the sentences that link to a page: restricted text would become searchable text of a
  broader page, and entity pages already accumulate those facts.
- A model reranker, query rewriting or expansion, typed relations, an intent router, community
  summaries, a link-count authority prior, traversal beyond one hop, or a graph database.
- A librarian-written description field, a batch read tool on MCP, enrichment of thin pages from
  outside sources, or a curator job or agent of any kind.
