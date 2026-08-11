# ADR 032 — the filing port, the pricing seam, and the meeting flow off Claude first

Status: accepted. Narrative:
[`docs/reference/librarian.md`](../reference/librarian.md) (the three backends and their
configuration), [`docs/reference/meeting-distiller.md`](../reference/meeting-distiller.md) (what the
meeting flow's own backend does differently). Code maps:
[`src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md),
[`evals/index.md`](../../evals/index.md). Predecessors:
[ADR 015](./015-librarian.md) (agent judges, code vetoes),
[ADR 020](./020-meeting-distiller.md) (the meeting flow as a sibling entry point, and why it is
structured and tool-less), [ADR 031](./031-suppression-gated-retry-and-cost-instrumentation.md) D2
(the spend reaches the row).

## Context

Two facts collided.

The first is structural. The librarian's agent step was a **convention**: `SdkAgent`, the offline
double and `processing.py` agreed about two method signatures, one result envelope and one fault
contract, and none of the three stated it anywhere a fourth implementation could read. Everything
provider-shaped was fused into one ~1.5k-line module — the Claude Agent SDK harness, the permission
hooks, the brief injection, the dollar figure — so "what does a backend owe the worker?" was
answerable only by reading the two that existed and hoping they agreed. Meanwhile this repo already
HAS a multi-provider mechanism (`kernel.llm`, pydantic-ai) and every other agent uses it: the
answer synthesizer, the view writer, the gardener sweep. The librarian was the one holdout.

The second is a date. Sonnet's introductory pricing ends 2026-08-31, and the librarian is the
system's only writer — the surface where provider captivity acquires a price tag rather than a
preference. ADR 031 D2 had already made the spend visible per item, which is what turned "we should
look at alternatives" into a question with numbers behind it.

And the meeting flow was already portable without anybody noticing. ADR 020's own decisions made it
so: the agent holds no page-writing tool (code is the sole author of every page in the set), it
explores nothing (the transcript, the entity registry and the drop's metadata are all handed to it
in one message), and its whole answer is one structured account of a page set. A flow shaped like
that does not need an agent harness — it needs a model that can return a typed object. It ran on
the Claude SDK for uniformity, not for any property of the flow.

## Decisions

**D1 — the seam becomes a named port, and the envelope moves with it.**
`librarian/filing_port.py` declares `FilingAgent`, a `runtime_checkable` `Protocol` with the two
keyword-only calls `processing.py` already makes (`run`, `run_meeting`), and owns what used to be
scattered: the `AgentRun` envelope, the fault contract (`AgentError`, with `priced()` attaching
`run_cost_usd` so a pass that died mid-run still reports what it spent), and the side-effect rules,
which differ per flow and must not be averaged — an ordinary run may write ONE new page inside the
worktree, a meeting run writes no page at all. The module imports `errors` and nothing else, so any
backend can depend on it without inheriting the SDK driver's imports; `agent.AgentRun` stays a
re-export, so nothing outside had to move with it.

Conformance is STRUCTURAL: no base class, no registry, no decorator. A backend is a class that
answers the two calls, `build_agent` returns the port, and a keyless test asserts all three
implementations satisfy it. Inheritance was rejected because it buys nothing here and costs the one
property that matters — the offline double must be able to be a plain object a test can reason
about, and a shared base class would tempt shared behaviour into a place where "the double did it
too" stops proving anything about production.

Two prompt builders grew a caller-declared parameter rather than a copy:
`build_meeting_system_prompt(..., header=…)` and `build_meeting_prompt(..., outcome_channel=…)`,
and the preamble itself is composed by `build_meeting_header` from three shared pieces plus ONE
per-backend paragraph. Every default reproduces the SDK backend's bytes exactly — which is checked,
because that prompt is the M0 baseline the next decision compares against.

**Three things differ between the two backends' prompts, not two, and the third is an admitted
contradiction.** The environment paragraph (which tools the agent holds) and the outcome-channel
sentence (how its account travels home) are the two the parameters exist for. The third is that the
BRIEF ITSELF — the knowledge repo's text, unchanged by this milestone and unchangeable by either
backend unilaterally — tells its reader it holds a `Write` tool and returns its account by writing
`.librarian-outcome.json`. Under a preamble saying "you have NO tools" that is a flat contradiction,
and leaving a model to resolve it is not a cosmetic prompt defect: a model that resolves it the
other way describes writing a file it cannot write, and the noise lands on the exact measurement M3
reads. So the pydantic preamble carries an explicit OVERRIDE paragraph, positioned immediately
before the brief it overrides, scoped as narrowly as it can honestly be — the tool and the file
describe the SHAPE of the account, every other word applies unchanged. Rewording the brief instead
would be a knowledge-repo PR and a two-sided contract change with the gates; this is the cheaper
half of that trade, and it is declared rather than hoped over.

**D2 — the port's usage contract is tokens-first, and pricing is configuration.**
`librarian/pricing.py` maps a model id to `(input, cached input, output)` dollars per million
tokens, from a seeded table stamped with an `AS_OF` date, merged per id with
`$STIGMERGY_LIBRARIAN_PRICING` (a JSON map of the same shape) resolved at call time. Backends that
are priced by their own provider keep passing that figure through — the SDK backend reports
`total_cost_usd` per run and nothing recomputes it — and backends that report only counts are
multiplied here. `report.cost_usd` does not change shape, and neither does anything that reads it.

**An unpriced model is refused at startup, never reported as `$0.00`.** A price table that answers
"I don't know" with zero lies in the one direction nobody audits: a cost instrument reading free.
`require_priced` names the id, the environment line that fixes it, and the date a human last set the
table. The prices themselves are configuration for the same reason model ids already are — they
move, and an introductory rate expires on a date nobody wants to learn from a bill. The override is
validated rather than trusted: a non-finite figure (JSON admits `NaN` and `Infinity` as literals,
and a `NaN` cost cannot even be stored in the row's `jsonb` column), a negative one, or a zero
OUTPUT rate is refused naming the model and the position.

**The token counts are INCLUSIVE, and that is the framework's contract rather than this repo's
convention.** `pydantic_ai.usage.UsageBase` documents its buckets as an inclusive parent with
children, and pydantic-ai normalizes the providers that do not report that way — its
`models/anthropic.py::_map_usage` folds the cache-read and cache-creation counts INTO
`input_tokens`. So the fresh count is one subtraction with no provider branch: an earlier draft
inferred the convention by comparing magnitudes, which is a heuristic that silently doubles a bill
the first time a provider's numbers land the other way round. One approximation remains and is
declared rather than hidden: a cache WRITE is billed at the input rate, where Anthropic charges
1.25x — the only figure in this seam that errs downward. A fourth element on a `PRICES` row is the
follow-up that closes it.

A `cost_source` flag ("sdk-priced" / "computed") on the REPORT was considered and dropped. It cannot
be added without touching `capture.schema`'s `base_report` shape and the client-wire strip that
keeps per-item spend off `brain_submissions` — a contract change in service of a label, where the
same question is already answered by the backend the row was filed with. The computed figure is
logged with its token counts and the table's `AS_OF` at the point of computation instead.

**D3 — the meeting flow goes first, and `pydantic` is not a worker backend.**
`librarian/pydantic_backend.py` adds `PydanticMeetingAgent`: the brief (read at the base commit, out
of the worktree, by the flow's existing reader) as instructions, the existing per-item prompt, one
pydantic-ai call with a typed output schema mirroring the meeting outcome's own JSON, and
`agent.parse_meeting_outcome` at the boundary — the SAME trust boundary the file channel goes
through, because a typed provider response is not a trusted one. `turns` and `tool_calls` are `0`,
which the port documents as legitimate rather than as a missing count. `pydantic_ai` is imported
inside the method, exactly as `claude_agent_sdk` is.

**A measurement caveat, declared here because nothing in the report can show it.** The framework
may re-ask the model when its answer does not satisfy the output schema (`OUTPUT_RETRIES`, which is
also the request ceiling — one constant, so the budget and the bound cannot disagree). Those
re-asks are INVISIBLE to `AgentPasses.count`: `attempts` means the WORKER's passes, and `turns` /
`tool_calls` are `0` by the envelope's own semantics. Their cost is banked honestly — the usage
accumulator sees every request — so a re-validated pass shows up as more dollars under an unchanged
attempt count. Anyone comparing cost-per-attempt across backends is comparing two different things,
and the exhaustion case is not silent: it arrives as an `OutcomeShapeError` carrying a finding, on
the same one-retry road a refused account from the file channel takes.

`kernel.llm.build_processor` — this repo's fake/real dispatch everywhere else — is deliberately not
reused. The librarian's offline path is `double.DoubleAgent`, a whole adversarial backend the suite
is built on, and routing this module through `resolve_backend` would create a SECOND offline path
with different semantics answering to a different variable. One offline path per subsystem.

**The worker refuses `backend="pydantic"` at startup.** A worker's queue carries ordinary captures
too, so a backend that serves one `kind` would burn deliveries one row at a time while looking
configured — config that half-works is the failure this repo refuses on principle, and
`startup_checks` is where it is refused loudly. The escape is narrow and named:
`startup_checks(settings, meeting_only=True)`, passed by the measurement rig alone
(`evals/run_filing.py --backend pydantic --kinds meeting`) and never by `worker.run` or the CLI.
That path validates what the backend genuinely needs — a provider-prefixed model string, a
configured price, the provider's own key — and refuses each out loud.

**A bare model name is refused for this backend.** pydantic-ai reads an unprefixed name as an
OpenAI Responses model (`kernel.llm.build_model` documents the same rule), so inheriting that
silently would file meetings through a provider nobody chose. `sdk` keeps the bare spelling, which
is the Claude Agent SDK's own.

**D4 — expand now, contract only on evidence.** This milestone is the EXPAND half and nothing is
retired: the default backend is still `double` (the suite's), staging is still `sdk` with Sonnet,
and the ordinary flow is 100 % the SDK's. M2 lifts the ordinary flow onto the port's second
implementation and the startup refusal with it; M3 is the CONTRACT step — retiring a backend — and
it happens only on measured evidence plus an explicit decision, never because the new one exists.
The instrument that produces the evidence is the filing golden, which grew `--kinds` for exactly
this: a subset run recomputes its own denominators and records the kinds it measured in the history
row, so a meeting-only score can never be read later as the whole set's.

## Consequences

- `processing.py` is unchanged in behaviour and its `Deps.agent` now names the port, which is what
  makes the annotation load-bearing: this module is the port's only consumer, so the two calls it
  makes ARE the contract.
- One more thing can be misconfigured, and every one of those states is a startup refusal naming
  its fix rather than a run that half-works.
- The price table is a maintenance obligation with a date on it. `AS_OF` exists so that a stale
  table is a visible fact rather than an assumption, and the introductory rate that motivated this
  ADR carries its expiry in a comment beside the number.
- The meeting brief was not touched. It lives in the knowledge repo and is a two-sided contract with
  the gates; a backend swap that needed it reworded would have been a knowledge-repo PR, and this
  one did not.
