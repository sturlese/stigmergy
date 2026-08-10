# ADR 031 — the suppression-gated retry, and cost that reaches the row

Status: accepted. Narrative:
[`docs/reference/answer.md`](../reference/answer.md) (the strict gate and the retry),
[`docs/reference/librarian.md`](../reference/librarian.md) (the report's `cost_usd`). Code maps:
[`src/stigmergy/answer/index.md`](../../src/stigmergy/answer/index.md),
[`src/stigmergy/librarian/index.md`](../../src/stigmergy/librarian/index.md).

## Context

`ask`'s corrective retry ran on ANY non-`verified` first draft. A week of staging traffic
(2026-08) measured what that bought: ~41 % of asks paid a second full agent run at ~6.8 s each,
and not one answer was ever suppressed. The two cases the retry serves are worth opposite
amounts — a draft the strict gate would suppress (an untraced figure, or a `failed` verdict) gets
the answer itself rescued, while a draft with a single citation problem ships labelled `partial`
with or without the retry, so the second run bought a label and a prettier quote at the price of
a full run. The measurement said the label case dominated.

The trigger also read the RAW verifier verdict while the gate ships on its own wider scan (the
citation-quote figures included) — two privately-held copies of "would this suppress?" that could
drift apart, and a corrective brief built from the raw findings, so a figure fabricated inside a
citation quote was never named to the retry that had to repair it.

Separately, neither paid path could say what it cost. The librarian's SDK reports a real dollar
figure per run (`AgentRun.cost_usd`) that died with the run object; `ask`'s SDK result carries
token usage that was never read. The cost plan that motivated this ADR had to estimate both from
session durations.

## Decisions

**D1 — the retry is spent only where suppression looms.** The trigger and the shipping gate read
the same arithmetic — `service.strict_gate_findings`, one copy of the every-shipped-channel scan
(quote figures included) — and the retry fires only when that scan says the draft would be
suppressed: any untraced figure, or a `failed` verdict. A lone citation problem ships `partial`,
unretried, labelled as what it is. The corrective brief is built from the gate's findings too, so
a quote-fabricated figure now reaches the retry prompt by name. The retry wins only if it
improves *what would ship* (the gate's rank) — the trigger, the win comparison and the gate read
one scan, so no draft can win the comparison and then lose at the gate.

The trade, accepted with eyes open: answers that were previously polished from `partial` to
`verified` by a second run now ship `partial` on the first. `first_verdict` in the audit row is
how the policy is watched — retries now concentrate where it shows figures or a `failed` — and
`make qa-golden` (honesty · groundedness · refutation · retry rate) is the instrument any future
revision of this policy must run before and after.

**D2 — the spend reaches the row that answers questions, and only the row.** The librarian's
per-run cost is summed per item (`AgentPasses.cost_usd`) and stamped onto every outcome that
passed through an agent loop or the failure road — filed, refused, parked, and `failed` via the
exception (`at_agent_attempt(n, cost_usd=…)`; a pass that died mid-run carries its own figure as
`run_cost_usd`) — as `report.cost_usd`. `ask`'s token usage (requests / input / cache-read /
output, both runs summed) lands in `audit_log.result.usage` through `audit_summary`: counts only,
per that column's no-transcript contract. Both figures are operator telemetry and are stripped
from BOTH client wires the same way — `usage` from `ask`'s response (`mcp_server.py`) and
`cost_usd` from `brain_submissions`' report shape (`service._without_operator_telemetry`) — so no
MCP surface grows a per-item spend disclosure; the stored rows keep the figures for
`stigmergy-queue show`, the admin console and the audit table. Nothing here decides anything —
it is instrumentation, and it exists so the next model-policy decision starts from recorded
dollars instead of modeled ranges.
