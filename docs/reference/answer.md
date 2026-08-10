# The answering half — `stigmergy.answer`

Generate-then-verify, applied at query time: an agent gathers evidence and writes a cited answer,
and **pure code verifies it before it leaves the server**. The LLM writes; code judges. Design
record: [ADR 007](../decisions/007-answer-layer.md). The read tools it stands on are the server
([server.md](./server.md)); the index underneath is
[ADR 012](../decisions/012-hybrid-index.md).
Code map: [`src/stigmergy/answer/index.md`](../../src/stigmergy/answer/index.md).

```
question ─▶ answering agent ─▶ deterministic ANSWER verifier ─▶ strict gate ─▶ answer + verdict
            (tools: search /     (every figure traced to THIS      (any remaining    (verified |
             read_page /         run's tool evidence; every        unverified        partial;
             describe_entity     citation quoted verbatim;         figure ⇒          refusal if
             over BrainService)   suppression-gated retry)          suppress)         suppressed)
```

The package is five modules — `synthesize.py` (the agent), `verify_answer.py` + `numbers.py` (the
deterministic verdict), `brain.py` (the evidence ledger) and `service.py` (the loop and the strict
gate). The split is load-bearing: `verify()` stays a pure judgement and the decision to SHIP lives
one layer up, in the gate.

**This verifier is the system's ONLY deterministic figure check.** Nothing verifies a figure at
write time; a figure is checked when an answer is composed, or not at all — which is why the
strict gate below is not a nicety.

The agent holds **three** tools: `search`, `read_page` and `describe_entity`. The third is the
entity-navigation surface every other client already has, rendered for the agent — own page, view
and dated timeline in one call, so a broad entity question stops spending its budget on a
search-and-read walk.

## Where it sits

`stigmergy.answer` is a layer **above** the service and **below** the MCP adapter
(`tests/test_architecture.py` enforces both edges):

- it consumes `stigmergy.server.service.BrainService` — that is the package's ONLY reach into the
  server (plus `fence`/`neutralize_fence`, re-exported from the same module, and
  `index.store.read_meta` for the `built_at` stamp). It never imports `server.acl`: every tool the
  agent holds calls the service under the caller's identity, so `acl.visible()` has already run by
  the time evidence exists, and answers/refusals are scoped by construction rather than by a second
  check here;
- `stigmergy.server.mcp_server` mounts the `ask` tool on top of it. The service surface itself does
  not change — the answering loop lives entirely in this package.

## Entity-first resolution lives one layer down, not here

"Does this query name a registered entity, and should the search scope to it first" is not decided
in this layer at all. It lives in `BrainService._search` itself
(`stigmergy/server/service.py`, [ADR 022](../decisions/022-entity-navigation.md)), so
every MCP client gets it — stdio, HTTP, Slack, `ask`, any future agent — not only this one.
`AnswerBrain.search_text` is a thin renderer over whatever the service already resolved, nothing
more. Full mechanism: [navigation.md](./navigation.md).

What DOES live in this layer is the agent's own instruction to USE that resolution: `ask`'s
search tool (`synthesize.py`) accepts a `filters` argument, and `ANSWER_SYS` tells the model to
prefer `search(filters={"entity": <id>})` once an id is known **from a previous result**, and to
read the entity's own page (`type: entity`) and follow its `links`/`backlinks` one hop before
concluding — topology guidance for the model, not a resolution mechanism of its own. The agent has
no `list_entities` tool and `ANSWER_SYS` never names one: ids come from search hits and from
`describe_entity`'s own registry layer.

| Module | Does |
|---|---|
| `numbers.py` | `unverified_figures()` — numeric matching (decimal comma/point, thousands separators, magnitude suffixes) so reformatting never flags; weak signals (bare single digits) skipped. The two sides are asymmetric on purpose — see "The numeric asymmetry" below |
| `verify_answer.py` | `verify(out, evidence_text, get_page, read_paths)` (figures + citations → `verified`/`partial`/`failed`), `check_citations()`, `feedback()` (the one corrective-retry prompt). A refusal is vacuously `verified` here. The strict gate is deliberately OUTSIDE this module. `_normalize_page`/`_normalize_quote` fold whitespace and case, then apply a typographic fold (`_fold`): Unicode NFC plus a curly-quote/ellipsis map, applied AFTER the derender step — folding first would lengthen the string past the `_SPAN` bound and stop a struck span from being dropped (see the comment above `_normalize_page`). Dashes are deliberately excluded from that fold and NFKC is deliberately not used, both for stated security reasons (see the comment above `_FOLD`). The two sides are NOT symmetric. The PAGE side (`_derender_page`) consumes every MATCHED marker pair — emphasis/strong, inline code, both link forms, `_` and lone `*` only at word boundaries — plus drops a struck span (`~~…~~`) WHOLE, up to `_SPAN` (200) characters: the page's own markup is never seen by a reader, so consuming all of it is reader-equivalence, and a struck span is the page RETRACTING a value. A retraction LONGER than `_SPAN` is NOT dropped — a bounded, known residual, not a guarantee — and its text stays quotable. The QUOTE side (`_derender_pairs`, the payload-free half shared with the page) consumes only emphasis/strong and inline code: a link or a struck span in the MODEL's own quote carries a destination or a retraction that consuming would delete from the claim before it is checked, so those two forms must match the page character for character. Digits are never touched, the only punctuation removed is a matched delimiter, and a word boundary collapses only where a renderer collapses it too — so "the quote exists in what the tools returned this run" cannot soften into "resembles" |
| `brain.py` | `AnswerBrain` — the evidence ledger: turns the service's structured (JSON) results into the exact TEXT the agent and verifier see. **Three renderers**: `search_text`, `page_text` and `entity_text` (search surfaces `SEARCH_RESULTS` = 8 hits per call), plus `get_page` (the verifier's verbatim-quote base) and `known_entities` (a thin pass-through to `BrainService.scoped_entities`, which is where the ACL/existence-scope assertions in `tests/answer/test_adversarial_cat2.py` read the vocabulary from). Every renderer lays the service's results out VERBATIM — the service already decided ACL scoping and neutralized titles, so nothing here re-derives, re-neutralizes or re-fences |
| `synthesize.py` | the pydantic_ai agent and its **three** tools (`search`, `read_page`, `describe_entity`), the `AnswerOutput` schema, usage limits (`ANSWER_REQUEST_LIMIT` 6, `ANSWER_TOOL_CALLS_LIMIT` 8), `ANSWER_SYS`, `FakeSynthesizer` (offline double). `pydantic_ai` is imported lazily inside the openai branch only, so the fake path never touches it |
| `service.py` | `AnswerService.ask()` — the loop; `_shape`/`_shape_refusal`/`_shape_budget_refusal` — the **strict gate** and the transport-agnostic response; `run_facts_reason` (the server-composed refusal prose); `audit_summary` — `ask`'s one `audit_log.result` summary, shared with the Slack transport, which reduces BOTH verdicts to COUNTS (`_verdict_shape`) because `check_citations`' own problem strings embed up to 80 characters of the drafted quote and that is answer text, which never belongs in a log. It records `first_verdict` beside the shipped `verdict`: the shipped one cannot distinguish a retried ask that ended clean from one that never needed a retry, so it cannot say what the retry BOUGHT. The 2026-08 staging read of exactly that column — ~41 % of asks retrying, almost always for a single citation problem whose answer ships `partial` either way — is what confined the retry to the suppression case (ADR 031), so retries now concentrate where `first_verdict` shows figures or a `failed`. `usage` (token counts, both runs summed; `null` on the budget refusal) rides beside the verdict fields, because this table had `duration_ms` and no dollars |

## The evidence ledger (the verifier's corpus)

The verifier does not trust the whole brain — it trusts **what the tools returned this run**.
`AnswerBrain` renders every tool result to text and the agent records each one; that concatenation
is the only corpus `unverified_figures()` accepts figures from. So an invented number cannot be
laundered by a lucky match elsewhere in the corpus (the "this-run" rule). `read_paths` collects
every page the run **surfaced** — from search hits, from `read_page`, and from every page
reference `describe_entity` shows: the entity's own page, its view, and every timeline row. A
citation to a page the run merely *found* is legitimate; its quote is still checked verbatim
(whitespace- and typographically-normalized) against that page's title + body. A citation to a path the run never
surfaced is a problem in its own right, and so is one carrying an EMPTY quote — a citation IS its
quote, and letting an empty one skip the verbatim check made `verified`, the strongest label this
system issues, reachable for a citation that asserts nothing about the page it names. An answer
with prose and no citations at all is a problem too.

Tool results are untrusted document **data**, never instructions to the reader, and the fencing
follows that: `read_page`'s body arrives already wrapped in the `UNTRUSTED-DATA` fence by the
service, and `search_text` fences its whole listing itself, because a title and a snippet are just
as page-derived as a body. `entity_text` is the exception and deliberately so — every field in it
is a service-decided reference (`_display_title`, `_neutralize_entity_record`) rather than page
prose, so it is laid out verbatim rather than fenced a second time.

## The numeric asymmetry

`numbers.interpretations` returns both readings of a suffixed token — the bare mantissa and the
scaled value — and accepting **any** overlap would let an answer saying `$2M` verify
against a bare `2` anywhere in the evidence. The two sides are therefore split:

- **The ANSWER side claims the dimensioned value only** (`claimed`): a magnitude-suffixed figure
  must trace to its scaled value, a percent to a percent. That is the laundering hole closed.
- **The EVIDENCE side stays generous** (both readings pooled), because prose writes magnitudes out
  in words ("2,3 millones") where no tokenizer reaches. Tightening that side would manufacture
  false refusals, which the gate's own design forbids buying.

The token regex also knows the `x` multiplier: `2.3x` would otherwise tokenize as a bare `2` (the
`.3x` tail failing the trailing boundary) and withhold a **correct, page-backed** figure.
`x` is a DIMENSION, never a magnitude — `2.3x` pools as 2.3 and scales nothing.

**Named accepted residual**: the mirrored prose direction regressed — an
answer's `$2M` no longer traces to evidence saying "2 millones", because the bare mantissa that
used to bridge them is the very hole this closed. The agent is told to quote figures as the page
states them, so the shape of the fix — if a real answer ever hits it — is EVIDENCE-side
word-magnitude parsing, never a wider answer-side claim. Pinned as a named test in
`tests/answer/test_numbers.py`.

## The strict gate

**The rejected alternative is the obvious one**: compute a verdict, LABEL a problematic answer
`partial`/`failed`, and ship it anyway with the label attached. That trades a hard guarantee for a
disclosure the caller has to read and act on — and the callers here are agents and Slack threads
that quote the prose and drop the metadata. The rule is therefore
*"zero unverified figures can leave the server"*, enforced in code rather than announced in a field.

The gate lives in `AnswerService._shape` (`service.py`), **outside** `verify()`, so the verifier
stays a pure judgement and only this one function decides what ships:

1. `verify()` computes the verdict from the answer prose and the citations: `verified` (no
   problems) · `partial` (exactly one) · `failed` (2+).
2. A first attempt the gate would SUPPRESS — any untraced figure (the citation-quote scan
   included), or a `failed` verdict — earns **exactly one** corrective retry carrying the gate's
   findings (`feedback()`) plus the FIRST run's own message history
   (`message_history=result.all_messages()`), so the model redrafts from evidence already in its
   context instead of re-gathering it. A lone citation problem spends no retry: it ships labelled
   `partial` with or without one, and paying a second full agent run for that label was the
   measured majority case that retired the old retry-on-anything policy (ADR 031). The retry
   replaces the first draft **only if it improves what would ship** (the gate's own rank — the
   trigger, the win comparison and the gate read ONE scan, so they cannot drift apart). A second
   failure never triggers a third attempt.
3. The same scan — **every human-readable channel that would ship**, the answer AND the citation
   quotes, since a figure fabricated inside a quote must be caught too
   (`strict_gate_findings`, ONE copy shared by the retry trigger and the gate) — then decides
   what ships for the winning draft, recomputing the verdict over the union (`_reverdict`). **On any untraced figure the drafted
   answer does not ship at all**: `answer_markdown` goes empty, `citations` goes empty, and the
   figure survives ONLY inside `verdict.unverified_figures` (`suppressed: true`, `refused: true`).
   Nothing is patched or redacted in place — the whole draft is withheld. A `failed` verdict
   (2+ problems) never ships either. What still ships is **exactly one citation-only problem,
   labelled `partial`** — a real answer with a single citation defect. A suppressed refusal carries
   the *recomputed* verdict, which may read `partial` (one untraced figure) or `failed` (2+
   problems); `suppressed`/`refused` mark it, and `failed` appears **only** when suppressed. The
   verdict object travels with **every** response.

   **The model has no `reason` field, and that is deliberate.** A refusal's `reason` used to be
   model-authored prose, and this gate merely scanned it for a smuggled figure before shipping it or
   swapping in a neutral template. That is what produced a *correct* refusal ("Borealis's ARR
   doesn't answer that") justified by a *wrong* claim about the corpus ("only a quarterly value
   exists, not monthly") that nobody had verified. `AnswerOutput` no longer HAS a `reason` field —
   the shipped `reason` on every refusal (genuine or suppressed) is composed entirely by
   `answer.service.run_facts_reason`, from `ctx.searched` (the queries this run tried) and
   `ctx.read_paths_order`/`out.citations` (the pages the ACL-scoped tools actually surfaced), never
   from anything the model wrote. See "Refusal is a first-class result", below, for the five shapes
   that composer produces. `_compose_reason` still runs a defensive figure scan over what it
   composed — the only variables are the asker's own words and page titles, neither of which should
   be a numeral, but a title that happens to contain one is cheap to imagine — and falls back to
   the generic `no_surface` sentence if it ever fires. That scan is against the QUESTION, never the
   evidence: evidence is everything any tool returned this run, which is a far weaker basis for a
   sentence the server asserts in its own voice. It used to be load-bearing for a second reason —
   the tool renderers echoed their own argument on the absence path (`no results for: <query>`), so
   a figure inside a model-chosen query entered the evidence and would have been "traced" by
   construction. The three absence strings carry no argument at all now
   (`brain.NO_RESULTS`/`UNKNOWN_PAGE`/`UNKNOWN_ENTITY`), which closes that channel at the source
   and leaves this scan as defense in depth rather than the only thing standing between a steered
   query and a verified figure. Neither half may be reopened on the grounds that the other exists.

## Refusal is a first-class result

No evidence, no answer — and refusing cleanly is *vacuously verified*, never a defect. A refusal
states what was searched and why the evidence is insufficient, and nothing more: it carries **no**
"want me to research and ingest it?" offer — `ask` never calls `brain_submit` on the caller's
behalf. A caller CAN capture what a refusal turned up, but only as a separate, explicit
action with the capture tools ([capture.md](./capture.md)), never something a refusal triggers for
them. A refusal because a page is **out of scope** is byte-shape-identical, in structure and case
selection, to one because **nothing matched**: existence itself never leaks (a page's TITLE never
appears for a scope the asker cannot see, because `read_paths`/`out.citations` are already
ACL-scoped by construction — see the strict gate section above).

**The five refusal shapes**, each composed by `run_facts_reason` from server-recorded facts alone
— never a claim about the brain as a whole:

| `refusal_case` | When | Shipped `reason` |
|---|---|---|
| `no_surface` | genuine refusal, nothing came back from any tool this run | `searched "X" — nothing came back this run.` |
| `no_match` | genuine refusal, pages surfaced, none answered the question | `searched "X", surfaced Y — it doesn't answer that.` |
| `budget_exceeded` | the agent's FIRST `agent.run()` raised `UsageLimitExceeded`, so no `AnswerOutput` ever existed to verify — the corrective retry is never spent on a run that already exhausted the budget. (A `UsageLimitExceeded` on the RETRY is caught too, but simply keeps the first run's outcome, exactly like a retry that failed to improve) | `searched "X", surfaced Y — the answer could not be completed within the tool budget.` |
| `suppressed_figures` | the strict gate withheld a drafted answer for an untraced figure | `searched "X", surfaced Y — a drafted answer used it, but it carried a figure none of that evidence could confirm, so it was withheld. No unverified number leaves the brain.` |
| `suppressed_citations` | the strict gate withheld a drafted answer for an unconfirmable citation (no figures involved) | `searched "X", surfaced Y — a drafted answer quoted it in a way the verifier couldn't confirm word-for-word, so it was withheld.` |

The sentences agree with their own counts rather than reading like a template: `no_match` ends
"it doesn't answer that" / "neither answers that" / "none of them answer that" for one, two and
more surfaced pages; the suppressed cases say "used it" / "used them" and drop the pronoun clause
entirely at zero (a suppressed answer can genuinely cite nothing at all). Both list clauses cap at
three named items and fold the rest into `and N more`, and a lead clause that would be empty
contributes no stray separator. `no_surface` with no recorded query at all — which the agent's own
instructions should make unreachable, but a composer must not crash — falls back to
`nothing came back this run — no tool call found anything to work with.`

`refusal_case`, `searched` and `surfaced` (page TITLES, never paths) ride ADDITIVELY beside
`reason` on every refusal — a client that only ever read `.reason` keeps working unchanged; one
that wants the facts structurally can now read them beside it, the same posture `reason_code`
already takes beside a capture rejection.

**`searched` is not simply "what the agent searched for".** `ctx.searched` is populated from the
AGENT's own tool-call arguments, and the agent is steerable by hostile page content — a steered
agent can search for a literal string it wants quoted verbatim into server prose, and neutralizing
a fence token in that string does not stop it reading as a persuasive sentence about the corpus. So
`_shippable_queries` ships a recorded query verbatim ONLY when it is itself a case-insensitive
substring of the asker's OWN `question`; every other recorded query is dropped from both the field
and the sentence, folded into an `and N other search(es)` count instead. What does ship is still
`neutralize_fence`d and capped at 200 characters, and page titles get the same treatment once, in
`_titles_for` — a page with no title becomes the placeholder `a page`, never its path.

## Serving it

`ask` is one of the ten tools `stigmergy-server` mounts, over BOTH transports — stdio (one process =
one identity) and streamable HTTP (identity per bearer token) — and `stigmergy.slack` runs the same
`AnswerService` behind its own per-identity `BrainService`. There is no per-call identity parameter
a client could spoof on any of the three. Config and the response shape are in
[server.md](./server.md#the-ask-tool--the-answering-loop). Keyless everywhere with
`ANSWER_LLM=fake` (the deterministic `FakeSynthesizer` answers from the first lexically-relevant
search hit and refuses when nothing matches) — enough to exercise the whole path in demos and CI.
The real model (`ANSWER_LLM=openai`, default `gpt-5.6-terra`) needs `OPENAI_API_KEY`; a missing key
yields a clean error, never a traceback.

## Measured, not assumed

- **The adversarial categories** (deterministic, keyless, `make adversarial`): **three are armed**
  — cat. 1 injection (`tests/answer/test_adversarial_cat1.py`: injected-figure,
  fence-spoofing, citation-laundering and hostile-title cases, proving the verifier and the
  renderers block them regardless of model), cat. 2 ACL/existence
  (`tests/answer/test_adversarial_cat2.py`) and cat. 7 forged frontmatter
  (`tests/capture/test_adversarial_cat7.py`, with more cat-1 and cat-7 cases under
  `tests/librarian/`). The gate selects them by NAME
  (`-k "adversarial_cat1 or adversarial_cat2 or adversarial_cat7"`), and a `-k` expression fails
  OPEN — rename the tests and the gate green-lights an empty run. `tests/test_adversarial_gate.py`
  is the keyless floor that closes that: it counts `def test_<category>_` across the whole suite and
  fails below the count each armed category had the day the gate armed (cat 1: 14, cat 2: 6,
  cat 7: 12). The hostile-title case also has a Postgres-backed renderer test in
  `test_service_ask.py`. Note cat 2 needs Postgres — `make adversarial` runs it against the real
  index, unlike the keyless cat-1 cases.
- **Golden QA** (on-demand, real model, needs a key) over the frozen, committed `evals/corpus/`
  — **38 pages**, committed rather than generated precisely so a score series stays comparable.
  [`evals/qa_golden.json`](../../evals/qa_golden.json) carries **26** questions on
  three axes, each with its own denominator so a change to one family cannot silently move another:
  **honesty** (the 9 `refusal` questions — the fraction of genuinely unanswerable ones correctly
  refused), **groundedness** (the 14 answerable `exact`/`prose` questions) and **refutation** (the
  3 corrective `refute` questions, reported rather than gated — a cited correction of the false
  premise passes as well as a refusal, which is why they left the honesty denominator). Bars,
  defined once in
  [`evals/bars.py`](../../evals/bars.py) and judged by `make gates`: honesty ≥ 0.90 ·
  groundedness ≥ 0.84 · retrieval R@5 ≥ 0.80. Every run appends to
  [`evals/history.ndjson`](../../evals/history.ndjson). See
  [`evals/run_qa.py`](../../evals/run_qa.py), [`evals/run_gates.py`](../../evals/run_gates.py) and
  [`evals/README.md`](../../evals/README.md).

## Trust model

- The verifier's evidence corpus is **what the tools returned this run** — an invented figure
  cannot be laundered by a lucky corpus match, and a fuzzy LLM reader steered by hostile page
  content is bounded by the verifier to non-numeric, non-citation steering (the designed
  defense-in-depth).
- Refusals are first-class and vacuously verified: no evidence, no answer, no hallucination.
- The gate can over-refuse (a legitimately reformatted figure flagged); the matcher already skips
  weak signals, golden groundedness watches the false-refusal rate, and findings feed the matcher —
  they never loosen the gate. The citation check reads the PAGE as a READER sees it — every matched
  marker pair consumed, link forms and a struck span included — but reads the MODEL's own quote
  more strictly: only the payload-free pairs (emphasis, strong, inline code) are consumed there, so
  a quote cannot smuggle a fabricated link destination or hide a retracted figure behind markup the
  page itself never asserted (a fabricated quote must still fail, on either side). The twins in
  `test_verify.py` hold that line at its two sharpest edges — a delimiter that is not a matched pair
  (a snake_case identifier, a footnote asterisk, a glob) is never stripped from the page, and a
  STRUCK figure up to `_SPAN` (200) characters is never quotable as current (a longer retraction is
  a known, bounded residual — see the `verify_answer.py` row above) — plus the asymmetry itself: a
  quote carrying a live link or a struck span must match the page character for character, never
  merely in substance.

## Reuse these seams

- `stigmergy.answer.service.AnswerService(brain_service)` — the whole loop; call it, never re-run the
  agent or the verifier by hand.
- `stigmergy.answer.verify_answer.verify(out, evidence_text, get_page, read_paths)` — the deterministic
  verdict. Pure; reuse it wherever an answer must be judged. Do not fold the strict gate into it.
- `stigmergy.answer.brain.AnswerBrain(brain_service)` — the text view of a `BrainService`. New evidence
  renderers belong here, not in the server package (the service surface stays JSON).

## Tests

`tests/answer/` — the pure, keyless, DB-less suites are `test_verify.py`, `test_numbers.py`
(including the named residual above), `test_config.py`, `test_refusal_composer.py` (the five refusal
shapes `run_facts_reason` produces), `test_evidence_ledger.py` (that a renderer's absence path
records nothing the model chose, with benign twins proving a real result still lands whole),
`test_synthesize_entity_topology.py` (that `ANSWER_SYS`
actually carries the topology guidance this document claims it does, and that the budgets are 6/8)
and `test_adversarial_cat1.py`. The Postgres-backed suites — `test_service_ask.py`,
`test_existence_leak.py`, `test_adversarial_cat2.py`, `test_navigation_rendering.py` (that the
agent can SEE the graph it is told to walk), `test_describe_entity_tool.py` (the third tool end to
end, over both a service double and the real service) and `test_entity_first_pg.py` — build a
fixture corpus with the fake embedder and skip cleanly without `make db-up`. The `ask` round-trip
over the real MCP protocol is `tests/server/test_ask_mcp.py`.
