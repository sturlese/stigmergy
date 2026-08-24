# Evaluation map

## Purpose

Optional, real-model quality measurements over the frozen `corpus/` fixture. The keyless test
suite remains the contract and integration check.

## Key entry points

- `run_retrieval.py` measures lexical, vector, RRF, and final hybrid ranking against
  `retrieval_golden.json`.
- `run_qa.py` measures grounded answers, refusals, refutations, and latency against
  `qa_golden.json`, using `qa_identities.json` for scoped ACL probes.
- `run_gates.py` runs the real-model retrieval and QA bars, plus the named adversarial suite.

## Use these

Start with [`README.md`](./README.md) for runnable commands. Use `bars.py` as the single source
of the armed retrieval, honesty, and groundedness thresholds.

## Avoid / anti-patterns

Do not treat fake-backend results as model-quality evidence. Do not change the frozen corpus
without updating its provenance and goldens.

## Data & contracts

`corpus/` is the committed reference knowledge repository: sources, wiki pages, entity records,
ACL identities, Slack channel mappings, and frozen provenance. `eval_history.py` appends
real-instrument results to tracked `history.ndjson`; fake backends do not add a row. `out/` is
generated report output, not durable evaluation history.

## Tests

The normal keyless suite validates evaluation code. `run_gates.py` adds the named adversarial test
selection to its real-model measurements.

## Common tasks

Run a fake self-check before an expensive real evaluation; run the gated evaluation before release.
The exact commands and required services are in [`README.md`](./README.md).

## Notes

The retrieval runner measures ranking arms without entity resolution. The QA runner measures the
served, identity-scoped answer path.
