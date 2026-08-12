# ADR 036 — Anthropic prompt caching on the ordinary filing run

- **Status**: accepted
- **Date**: 2026-08-12
- **Related**: [ADR 032](./032-filing-port-and-pricing-seam.md) (the tokens-first pricing seam this
  widens, and the fourth-column follow-up it named), [ADR 034](./034-agentic-pydantic-harness.md)
  (the agentic harness — the iterating tool loop that makes a repeated, cacheable prefix exist at
  all), [ADR 020](./020-meeting-distiller.md) (the meeting flow's one-call shape, which is why this
  ADR excludes it)

## Context

The ordinary filing run ITERATES — up to `max_turns` (30 by default) model requests for one
capture, bounded by `UsageLimits(request_limit=...)` — and pydantic-ai resends the WHOLE growing
prefix on every one of those requests: the system prompt (the knowledge-repo skill, injected by
`agent.build_system_prompt`), the five tool schemas `_register_tools` declares, and the gathered
seed `processing._one_pass` renders before the call ever starts, are all byte-identical from the
first turn to the last. Only the tool results and the model's own replies grow underneath that
prefix. A run that goes ten turns resends that identical block roughly ten times, at the ordinary
input rate, with nothing distinguishing "the part that never changes" from "the part that just
grew" on the bill.

ADR 032 built the tokens-first pricing seam and named the gap this ADR closes: *"a cache WRITE is
billed at the input rate, where Anthropic charges 1.25x — the only figure in this seam that errs
downward. A fourth element on a `PRICES` row is the follow-up that closes it."* The pre-036
`compute_cost_usd` docstring quantified the same gap in its own words — a cache write under-billed
by 20%, an approximation a three-figure row could not express — and named the identical follow-up.
The accounting plumbing needed no change to get here — `pydantic_backend._cost` has passed
`cache_read_tokens`/`cache_write_tokens` into `pricing.compute_cost_usd` since ADR 032 landed, so
the READ half of the arithmetic was already correct and a cache write was already billed (safely,
not free) rather than ignored. What was missing was the two-sided decision this ADR makes: turning
caching ON for the run that can actually benefit from it, and pricing a write at its own rate
instead of borrowing the input one.

ADR 032 priced `anthropic:claude-sonnet-5` at the $2/$10 rate it was budgeted against, introductory
at the time. Anthropic's 2026-08-12 pricing notice confirms that rate is now PERMANENT, with no
step to $3/$15. That removes any urgency this ADR might otherwise have inherited from a closing
window, and it changes nothing below: the cache multipliers this ADR prices from — 0.1x for a
read, 1.25x for a five-minute write — are Anthropic's own standing figures, independent of which
base rate they are applied to.

## Decisions

**D1 — caching is ON by default, through one knob.** `pydantic_backend.prompt_cache_settings(model,
prompt_cache)` maps a provider-prefixed model id and a resolved setting to the `model_settings`
dict `Agent(...)` takes: for an `anthropic:` model and `prompt_cache` in `{"5m", "1h"}`, the SAME
TTL on all three of `anthropic_cache_instructions`, `anthropic_cache_tool_definitions` and
`anthropic_cache_messages` — the system prompt, the tool schemas and the growing message list all
cached together, since all three are the identical-every-turn prefix this ADR exists to stop
re-billing. `None` — no `model_settings` at all — for `"off"` or any non-Anthropic id.
`Settings.prompt_cache` defaults to `"5m"`, read from `$STIGMERGY_LIBRARIAN_PROMPT_CACHE`, refused
by name for anything outside `off|5m|1h`. Default ON because the identical prefix is where the
majority of an iterating run's bill lives past the first turn — the safe default is the one that
saves money — and `off` is the escape hatch for a deployment that wants the pre-ADR-036 bill shape,
or that has no cache access on its plan.

**D2 — the ordinary flow only; the meeting flow stays byte-for-byte untouched.** `_run` passes
`model_settings=prompt_cache_settings(self.settings.model, self.settings.prompt_cache)` to the
`Agent(...)` it builds; `_run_meeting` gains nothing — no parameter, no conditional, not one
changed line. The meeting flow makes exactly ONE model call per capture (ADR 020): there is no
second turn for a cached read to serve, so caching it would only ever WRITE a cache entry nothing
reads, at Anthropic's 1.25x write premium — a pure surcharge with no offsetting saving. A test pins
the omission on the actual `Agent(...)` construction call, not on reading the source.

**D3 — pricing gains a fourth column, and an operator's existing override still works.** `PRICES`
rows and `require_priced`'s return value become `(input, cached input, cache write, output)`.
`anthropic:claude-sonnet-5`'s cached and write figures are Anthropic's own standing multipliers —
0.1x and 1.25x — applied to the $2 base; the other two rows keep cached-equals-input and
write-equals-input, unverified as they always were, and unreachable regardless since no caching
path is built for either provider (D5 below). `$STIGMERGY_LIBRARIAN_PRICING` accepts BOTH the
current four-figure row and a LEGACY three-figure one: a three-figure row has no write figure to
read, so it is normalized with the write rate equal to the input rate — today's documented
semantics, kept so an operator's existing variable is not broken by this change rather than forcing
a synchronized edit. `require_priced` always returns the normalized four-tuple, so
`compute_cost_usd` and everything downstream of it read one shape regardless of which one
configured it.

**Rejected: `CachePoint` fine-grained markers, for now.** pydantic-ai's `CachePoint` message part
marks a cache boundary INSIDE one message rather than caching a whole block; the three
`anthropic_cache_*` fields this ADR turns on already cache the growing prefix in full, and a finer
boundary is worth the added complexity only once a measured run shows the ordinary flow's
per-turn TOOL RESULTS (which do not repeat, and so should not be cached) are large enough relative
to the prefix to be worth excluding from the write explicitly.

**Rejected: caching the `openai:` and `google-gla:` paths.** Neither `PRICES` row for those
providers carries a verified cache rate, and pydantic-ai's cache fields are Anthropic-specific
(`AnthropicModelSettings`, not the base `ModelSettings` every provider shares) — there is no
mechanism to turn on without inventing pricing for a provider this deployment does not run in
production.

**Rejected: caching the meeting flow.** Restated from D2 as its own rejected alternative because it
was genuinely considered and not merely skipped: one call per capture means a cache entry would be
WRITTEN and never READ, and a write bills at 1.25x base input — turning it on would raise every
meeting's cost with no saving to offset it.

## Consequences

- `pydantic_backend.prompt_cache_settings` is a pure, module-level function reused verbatim by
  `_run` — no second reading of "is this model Anthropic's" grows beside `provider_of`.
- An operator who has already set `$STIGMERGY_LIBRARIAN_PRICING` in the legacy three-figure shape
  keeps running unedited; widening to four figures is optional, on their own schedule, and gains
  them a correctly-priced cache write instead of one billed at the input rate.
- `AS_OF` moves to 2026-08-12, which is also the date this ADR corrects the Sonnet-5 permanence
  fact — a table this document motivated changing is a table that should say so.
- The ordinary run's real dollar figure moves in two directions on the SAME item: cache reads bill
  at a fraction of input (cheaper), cache writes bill at their own, higher rate rather than the
  input one (more expensive than the old under-billed figure, though still less than treating a
  write as ordinary input would). Which direction dominates for a given capture depends on how many
  turns it takes — a one-turn run pays the write premium with no read to offset it, the same
  economics D2 already states for the meeting flow, while a multi-turn run's later reads make up
  the difference many times over.
