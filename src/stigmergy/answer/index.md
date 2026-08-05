# answer — the answering loop over the brain service

Narrative doc: [`docs/reference/answer.md`](../../../docs/reference/answer.md) (the how and why
for an operator — the strict gate, the four refusal shapes, the trust model). Design record:
[ADR 007](../../../docs/decisions/007-answer-layer.md), and
[ADR 026](../../../docs/decisions/026-the-purge.md) (D2 is why this is the WHOLE of figure
verification: ingest-time checking is gone, and this package's own cites-or-refuses gate is the
reader's protection). This file is the code map — for whoever is about to edit this package, not
run it.

## Purpose

The serving half, applied at query time: an agent gathers evidence with three bounded tools
(`search`, `read_page`, `describe_entity` — the third renders the entity-navigation surface every
other client already had; see `synthesize.py`'s own module docstring) over
`stigmergy.server.service.BrainService` and writes a cited answer, and **pure code verifies it before
it leaves the server**. The LLM writes; code judges. The verifier began as an import-and-adapt of a
predecessor's and has since taken THREE deliberate behaviour changes, each with its reason on the
line: the strict gate (below) lives OUTSIDE `verify_answer.verify` so the judgement stays pure; an
empty citation quote became a problem rather than a free pass (`verified` was reachable for a
citation asserting nothing); and the containment check reads a page's own markers in full — the
agent quotes a page as a reader sees it — but reads a model-authored quote's markers only where they
carry no payload, because a link or a struck span in the quote is the model's own assertion, not the
page's, and consuming it there would delete a destination or a retraction from the claim before it
is checked.

**This is the ONLY figure verification anywhere in the system.** An ingest-time checker once judged
a page's own figures at filing time; [ADR 026](../../../docs/decisions/026-the-purge.md) D2 removed
it — with the measured false-positive it names — on the grounds that an ingest-time check taxes the
model's own prose with false positives and cannot catch the dangerous class anyway (an invented
CLAIM passes every figure check). What protects the reader is the verbatim source one click away,
plus this package's own cites-or-refuses gate: pure code, sole, and untouched by that removal.

Layering (`tests/test_architecture.py`): `stigmergy.answer` sits **above** `stigmergy.server` and
**below** the MCP adapter (`stigmergy.server.mcp_server` mounts the `ask` tool on top of it) — the
service surface itself never changes; the answering loop lives entirely here. What it consumes from
`stigmergy.server` is `service.BrainService` plus the two text primitives `fence` /
`neutralize_fence`, and `answer` never imports `stigmergy.capture`, `stigmergy.slack` or the MCP
adapter — all three pinned as separate tests.

## Key entry points

| Module | Owns |
|---|---|
| `numbers.py` | The tokenizer and the two matching functions — **`interpretations()` (generous, EVIDENCE side: a suffixed token contributes both its mantissa and its scaled value) and `claimed()` (strict, ANSWER side: `$2M` claims 2,000,000 and nothing else, `40%` claims forty percent)** — plus `number_pool()` and `unverified_figures()`. **The asymmetry is the point**: an earlier check accepted ANY overlap, so `$2M` in an answer verified against a bare `2` anywhere in the evidence. The evidence side stays generous because prose writes magnitudes out ("2,3 millones") where no tokenizer reaches. The regex also knows the `x` multiplier — `2.3x` once tokenized as a bare `2`, which withheld a correct, page-backed figure against a measured baseline. `x` is a DIMENSION, not a magnitude: it pools as 2.3 and scales nothing |
| `verify_answer.py` | `verify()` (figures + citations → `verified`/`partial`/`failed`), `check_citations()`, `feedback()` (the one corrective-retry prompt); `_derender_page`/`_derender_pairs`/`_fold`/`_normalize_page`/`_normalize_quote`, the containment check's own reading of a page AND, separately, of a model-authored quote — deliberately NOT the same reading. The strict gate is deliberately kept outside it. **The PAGE side consumes every MATCHED pair** — emphasis/strong, inline code, both link forms, `_` and a lone `*` at word boundaries, so a snake_case identifier, a footnote asterisk and a glob survive — and drops a struck span up to `_SPAN` (200) characters WHOLE, because `~~12%~~ 14%` is the page retracting a value; a retraction LONGER than `_SPAN` is a known, bounded residual — not dropped, and its text stays quotable as current. **The QUOTE side consumes only the payload-free pairs** (emphasis/strong, inline code): a link or a struck span in the MODEL's own quote carries a destination or a retraction that consuming would delete from the claim before it is checked, so those two forms must match the page character for character. `_fold` then applies a SYMMETRIC typographic fold (Unicode NFC, curly quotes/ellipsis) to both sides, AFTER the derender step so the length it adds never pushes a struck span past `_SPAN` — dashes stay excluded and NFKC stays unused, both for stated security reasons. Every pattern is bounded and newline-free: a body is attacker-influenced in size and this runs synchronously inside `async def ask` |
| `brain.py` | `AnswerBrain` — the evidence ledger: turns `BrainService`'s structured (JSON) results into the exact TEXT the agent and verifier see. **Three renderers**: `search_text`, `page_text` and `entity_text`, plus `get_page` (the verifier's verbatim-quote base) and `known_entities` (the scoped discovery primitive). Every renderer lays the service's results out VERBATIM — the service already decided ACL scoping and neutralized titles, so nothing here re-derives or re-fences (`_render_nav` is the stated pattern) |
| `synthesize.py` | the pydantic_ai agent, `AnswerOutput` schema (no `reason` field — see Notes), usage limits (≤6 requests, ≤8 tool calls), `FakeSynthesizer` (offline double). `pydantic_ai` is imported lazily inside the `openai` branch only |
| `service.py` | `AnswerService.ask()` — the loop + the **strict gate**; `run_facts_reason` — the one composer for every refusal's shipped `reason`; shapes the transport-agnostic response |

## Use these

- `service.AnswerService(brain_service).ask(question)` — the whole loop. Never re-run the agent or
  the verifier by hand; a new caller (a transport, a script) constructs this and calls `ask`.
- `verify_answer.verify(out, evidence_text, get_page, read_paths)` — the deterministic verdict.
  Pure; reuse it wherever an answer must be judged. Do not fold the strict gate into it — that
  belongs in `service.py`, outside the ported function.
- `brain.AnswerBrain(brain_service)` — the text view of a `BrainService`. A new evidence renderer
  (a new tool the agent gets) belongs here, not in `stigmergy.server` (the service surface stays
  JSON) and not in `synthesize.py` (the agent module has no rendering logic of its own).
- `service.run_facts_reason(case, question, searched, surfaced)` — THE composer for every
  refusal's shipped prose, from structured facts (`ctx.searched`/`ctx.read_paths_order`) alone,
  never from the model. A new refusal case is a new branch here, not a second composer.
- `service.audit_summary(result)` — the ONE `audit_log.result` shape both callers of
  `service.call_async("ask", ..., summarize=audit_summary)` share (`mcp_server.py`'s `ask` tool
  and `slack.mention._run_ask`), so the two transports cannot describe the same outcome
  differently. COUNTS only — never a verdict's problem strings, never a citation quote. Carries
  BOTH the shipped `verdict` and the first draft's `first_verdict`, because only the second says
  what a corrective retry was for; `tests/server/test_audit.py` pins the key set CLOSED, so a
  field reaches this column by being named there on purpose.
- `synthesize.build_synthesizer(settings)` — the ANSWER_LLM dispatch (`openai` / `fake`). An
  unknown value fails fast; do not add a silent fallback to the fake path.

## Avoid / anti-patterns

- **Never let the model's own words reach a refusal's shipped `reason`.** `AnswerOutput` has no
  `reason` field at all — removed architecturally, not merely ignored — so a refusal's prose is
  composed ENTIRELY by `service.run_facts_reason` from server-recorded facts. Reopening a
  model-authored explanation field reopens the exact defect class that once shipped a false
  explanation to a demo audience; `service.py`'s module docstring carries that account.
- **Never let a tool renderer echo its own argument.** Everything a renderer returns is recorded
  into the evidence ledger, and the verifier traces the answer's figures against that ledger — so
  an absence string carrying the query (`"no results for: {query}"`) made any figure the model
  invented trace to itself. The three absence paths are argument-free constants
  (`brain.NO_RESULTS`/`UNKNOWN_PAGE`/`UNKNOWN_ENTITY`); `ctx.note_query` is the channel for what
  the model asked. `tests/answer/test_evidence_ledger.py` pins it.
- **Never scan a composed reason against `evidence` instead of `question`.** `_shippable_queries`
  / `_compose_reason`'s defensive backstop check against the ASKER's own question text. Evidence
  is everything any tool returned this run, which is a far weaker basis for a sentence the server
  asserts in its own voice — and it was the second half of the defect above.
- **Never let `answer['confidence']` (or `AnswerOutput.confidence`) carry free text.** It is a
  CLOSED enum (`high`/`medium`/`low`), and deliberately so: any field the model fills with prose is
  a channel a steered model can smuggle a figure through. The strict gate never reads it either;
  only the code-computed `verdict` ships as the trust signal.
- **Never write `verdict` (or any verifier problem-string list) into `audit_log.result`
  verbatim.** `service._verdict_shape` ships COUNTS (`unverified_figures`, `citation_problems`);
  the verbatim lists embed up to 80 characters of a drafted citation quote — drafted-answer text
  that must never land in a log column. `audit_summary` is the one place this is enforced.
- **Never iterate a scalar `entity` value as if it were already a list.** `brain.py`'s
  `search_text`/`page_text` join `page.get("entity") or ()` for display — the field is a list — so
  a new renderer touching `entity` follows the same `", ".join(...)` shape, not a bare
  `str(entity)`.
- **Never call the answering loop from a new call site without going through
  `service.call_async`.** (This is `stigmergy.server`'s seam, not this package's, but it is the one
  a new `ask`-shaped caller in THIS package's own consumers must still ride — see
  [`server/index.md`](../server/index.md) and [`slack/index.md`](../slack/index.md)'s own "Never
  call `AnswerService(...).ask(...)` directly" rule.) Skipping it produces an answer nobody can
  audit and no rate limit spends.

## Data & contracts

- **`synthesize.AnswerOutput`** — `answer_markdown`, `citations` (`list[Citation]`, each `path` +
  `quote`), `confidence` (closed enum), `refused` (bool). No `reason` field. `Citation.quote` is
  capped at 200 characters by a pydantic `max_length`, not only by the Field description — a
  description is prose the model may ignore, and `service._QUERY_CAP` justifies its own bound by
  pointing at this one, so the two must both be real.
- **`synthesize.SynthesisContext`** — per-question state: `evidence` (every tool result,
  verbatim), `read_paths` (set — the verifier's membership check), `read_paths_order` (same
  facts, first-surfaced order — a refusal names pages in a stable, testable order), `searched`
  (every query/lookup tried, first-tried order, deduped). `note_page`/`note_query` are the ONLY
  seam that updates these — a new tool wrapper must call them rather than appending to the lists
  directly, or the ordered and unordered views can drift apart.
- **`verify_answer.verify`'s verdict dict** — `{verdict, unverified_figures, citation_problems}`.
  A refusal (`out.refused`) is vacuously `verified` — refusing with no evidence is correct, never
  a defect.
- **The strict gate's five refusal shapes** (`refusal_case`) — `no_surface`, `no_match`,
  `budget_exceeded` (`AnswerService.ask` catching `UsageLimitExceeded` around each `agent.run()`
  call), `suppressed_figures`, `suppressed_citations`. See
  [`docs/reference/answer.md`](../../../docs/reference/answer.md#refusal-is-a-first-class-result)
  for the exact sentence each produces. `refusal_case`/`searched`/`surfaced` ride ADDITIVELY
  beside `reason` — a client reading only `.reason` is unaffected by a new case.
- **Entity-first resolution does not live in this package.** It once did, as
  `brain.AnswerBrain._search_entity_first`: query → registry alias → scoped search → fall back to
  unscoped on zero hits. That resolution now lives in `stigmergy.server.service.BrainService._search`
  — every client gets it, not only `ask` — and the wrapper here was deleted, not merely bypassed.
  `brain.AnswerBrain.search_text` is a thin renderer: it passes `filters` straight through to
  `BrainService.search` and renders whatever comes back. The `entity` filter is a MEMBERSHIP match
  (`pages_index.entity` is `text[]`), so a page anchored to several entities is found by any one of
  them; that part of the contract never changed, only WHERE it is enforced. If you are looking for
  "how does `ask` know a question names a registered entity", the answer is
  `BrainService._search`, in `stigmergy.server` — see [`server/index.md`](../server/index.md). It is
  stated here because the narrative doc this file points to does not cover it, so a reader who
  followed only that doc has no way to learn where the mechanism lives.

## Tests

`tests/answer/` — the keyless, DB-less suites are `test_verify.py`, `test_numbers.py`,
`test_config.py`, `test_refusal_composer.py`, `test_evidence_ledger.py`,
`test_synthesize_entity_topology.py` and `test_adversarial_cat1.py`; the rest build the fixture
corpus into Postgres with the fake embedder and skip cleanly without `make db-up`.

| Suite | Covers |
|---|---|
| `test_verify.py` | `verify()`/`check_citations()`, ported and pure |
| `test_config.py` | `synthesize`'s settings resolution |
| `test_adversarial_cat1.py` | injected-figure, fence-spoofing, citation-laundering, hostile-title cases — model-independent, named `adversarial_cat1_*` so the eval gate can select them |
| `test_service_ask.py` | the full loop end to end, incl. a Postgres-backed hostile-title renderer case |
| `test_existence_leak.py` | an out-of-scope refusal is byte-shape-identical to a genuine no-match refusal — existence never leaks |
| `test_refusal_composer.py` | `run_facts_reason`'s FIVE cases (`no_surface`, `no_match`, `budget_exceeded`, `suppressed_figures`, `suppressed_citations`), `_shippable_queries`'s question-substring check, `_titles_for`'s neutralize-and-cap. The `_compose_reason` backstop itself is exercised in `test_service_ask.py`, through the loop |
| `test_evidence_ledger.py` | what may enter the corpus the verifier traces against: the three absence renderers record none of their own argument, absence is byte-identical whatever was asked for, and the benign twins — a real hit, a real page — still land whole |
| `test_numbers.py` | the tokenizer and the two asymmetries — the `x` multiplier, and `claimed()` vs `interpretations()` (a `$2M` answer must trace to 2,000,000; the evidence pool stays generous). Includes the named test pinning the accepted residual: `$2M` no longer traces to prose saying "2 millones" |
| `test_adversarial_cat2.py` | the permanent category-2 cases against the answer path |
| `test_describe_entity_tool.py` | the third tool — `AnswerBrain.entity_text`'s rendering, its `note_query`/`note_page` bookkeeping, and the unknown-entity absence line |
| `test_navigation_rendering.py` | `_render_nav` — links/backlinks laid out verbatim from the service's already-scoped, already-neutralized entries |
| `test_entity_first_pg.py` | entity-first resolution witnessed through `AnswerBrain.search_text` (the observable behavior, delegated to `BrainService.search`): registry-alias resolution, the blend: a resolved entity ranks first and removes nothing, isolated from `conftest.py`'s shared fixture (which already names entities directly in its questions). The mechanism's own tests (`BrainService.search` directly, and through `search_brain` MCP) live in `tests/server/test_entity_first_search_pg.py` |
| `test_synthesize_entity_topology.py` | the agent-facing `search` tool's `filters` passthrough and unknown-filter error-string (never a crash), the 6/8 budgets, `ANSWER_SYS`'s topology paragraph |

The `ask` round-trip over the real MCP protocol is `tests/server/test_ask_mcp.py`. Adversarial
cat. 1 and the golden QA set are the measured, not assumed, half — see
[`docs/reference/answer.md#measured-not-assumed`](../../../docs/reference/answer.md#measured-not-assumed).

## Common tasks

| Task | Touch |
|---|---|
| Add a new agent tool | `synthesize.build_synthesizer`'s `@agent.tool` closures, calling a new (or existing) `AnswerBrain` renderer that records into `ctx` via `deps.record`/`note_page`/`note_query` |
| Add a new evidence renderer | `brain.py` — text view only, ACL-scoped through the wrapped `BrainService`; fence with `service.fence`/`neutralize_fence` exactly as `search_text`/`page_text` do |
| Change what a refusal says | `service.run_facts_reason` (the composer) — never `synthesize.AnswerOutput` (no `reason` field to reintroduce) |
| Change the strict gate's threshold | `service._reverdict`/`AnswerService._shape` — keep `verify_answer.verify`'s ported thresholds untouched |
| Change what `audit_log.result` records for `ask` | `service._verdict_shape`/`audit_summary` — counts/paths only |
| Change entity-first resolution's matching rule | `stigmergy.server.entity_aliases.resolve_entity` (registry aliases only); `BrainService._search`, in `stigmergy.server`, is the caller — NOT this package |

## Notes

- **The verifier's evidence corpus is "what the tools returned this run," never the whole
  brain.** `AnswerBrain` renders every tool result to text; the agent records each one via
  `ctx.record`; that concatenation is the only corpus `unverified_figures()` accepts figures from.
  An invented number cannot be laundered by a lucky match elsewhere in the corpus.
- **The model's own refusal explanation was removed architecturally, not just by convention.**
  `out.reason` was once model-authored prose, scanned for smuggled figures before shipping; the
  field no longer exists on `AnswerOutput` at all, which closes the channel rather than guarding
  it. See `service.py`'s module docstring for the incident this fixes.
- **`numbers.py` once had a declared twin in the ingestion side's trust layer, and needed no code
  change when that twin was deleted.** The two were never coupled by an import — "the ingestion
  half and the serving half share no code by design" is why — so the removal
  ([ADR 026](../../../docs/decisions/026-the-purge.md) D2) cost this module nothing. Worth knowing
  before anyone reintroduces a second figure tokenizer somewhere and reaches for an import to
  keep the two honest: the declaration is the mechanism, not the import.
- **This file's structure matches [`librarian/index.md`](../librarian/index.md)**, the first `src/`
  package to set the convention every code map in this repo follows.
