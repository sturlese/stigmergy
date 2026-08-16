# answer — the answering loop over the brain service

Query-time generate-then-verify: an agent gathers evidence with three bounded tools (`search`,
`read_page`, `describe_entity`) over `stigmergy.server.service.BrainService` and writes a cited
answer; pure code verifies it before it leaves the server. The system's ONLY figure verification.
Sits above `stigmergy.server`, below the MCP adapter (which mounts `ask` on top). Narrative doc:
[`docs/reference/answer.md`](../../../docs/reference/answer.md).

## Modules

- `service.py` — `AnswerService.ask()`: the loop, the strict gate (`strict_gate_findings` — ONE
  scan shared by shipping and the retry trigger; any untraced figure in a shipped channel
  suppresses the whole answer), the single corrective retry (spent only when the gate would
  suppress), `run_facts_reason` (THE composer for every refusal's shipped `reason` —
  server-recorded facts only, never model text; a new refusal case is a new branch here), and
  `audit_summary` (the one `audit_log.result` shape both transports share — counts only, never
  problem strings, paths or quotes).
- `verify_answer.py` — `verify()` (figures + citations → `verified`/`partial`/`failed`),
  `check_citations()`, `feedback()` (the corrective-retry prompt). Page-side derender consumes
  matched markers and drops struck spans whole; quote-side consumes only payload-free pairs.
  Pure judgement — keep the strict gate out of it.
- `numbers.py` — the figure tokenizer: `claimed()` (strict, answer side) vs `interpretations()`
  (generous, evidence side), `number_pool()`, `unverified_figures()`. Never add a second figure
  tokenizer.
- `brain.py` — `AnswerBrain`: the evidence-ledger text view over `BrainService` (`search_text`,
  `page_text`, `entity_text`, plus `get_page`, the verifier's verbatim-quote base). New evidence
  renderers belong here; render service results verbatim, never re-derive/re-neutralize/re-fence;
  absence strings stay argument-free constants (a renderer echoing its argument lets an invented
  figure trace to itself).
- `synthesize.py` — the pydantic-ai agent and its `@agent.tool` closures, `AnswerOutput` (no
  `reason` field — refusal prose is server-composed; do not reintroduce one; `confidence` stays a
  closed enum), usage limits, `SynthesisContext` (update it only via `note_page`/`note_query`),
  `FakeSynthesizer` (offline double). `pydantic_ai` imports lazily on the `openai` branch only.

## Reuse / avoid

Call the loop through `service.call_async("ask", ..., summarize=audit_summary)` — never
`AnswerService.ask` directly from a new transport. `entity` frontmatter is a list: join it for
display, never `str()` it. Entity-first search resolution lives in `BrainService._search`
([`server/index.md`](../server/index.md)), not in this package.

`build_synthesizer` deliberately does not go through `kernel.llm.build_processor`: this call needs
the per-question model AND reasoning effort from `AnswerSettings`, which `build_model`'s env-read
signature cannot express; the usage repair is therefore installed here too. The offline double's
result envelope is `kernel.result.fake_result` — never a hand-rolled `.output`/`.usage` namespace.

Tests: `tests/answer/` (pure suites run keyless and DB-less; the rest use the fixture corpus in
Postgres and skip without `make db-up`); the MCP round-trip is `tests/server/test_ask_mcp.py`.
