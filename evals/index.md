# Evaluation map

## Purpose

Optional, real-model quality measurements over the frozen `corpus/` and `graph_corpus/` fixtures.
The keyless test suite remains the contract and integration check.

## Key entry points

- `run_retrieval.py` measures lexical, vector, RRF, and final hybrid ranking against
  `retrieval_golden.json`.
- `run_qa.py` measures grounded answers, refusals, refutations, and latency against
  `qa_golden.json`, using `qa_identities.json` for scoped ACL probes.
- `run_gates.py` runs the real-model retrieval and QA bars, plus the named adversarial suite.
- `run_graph_retrieval.py` measures graph search per question family over `graph_corpus/`
  against `graph_golden.json`, with reader scopes from `graph_identities.json`;
  `graph_detail_golden.json` asks for details planted in its long sources. `run_qa.py` measures
  answers with `graph_qa_subset.json` and `graph_listing_golden.json`.
- `build_graph_corpus.py` builds `graph_corpus/` by filing fictional sources through the planner
  and writer gates with at most one bounded correction; `make_long_sources.py` writes its four long
  sources from fixed seeds.
- `run_aliases.py` calibrates the filing-time alias decision against `alias_golden.json`;
  `run_routing.py` measures capture-layer routing against `routing_golden.json`.
- `filing/parity.py` is the fail-closed filing release gate over evidence recorded by
  `filing/run_planner.py`; `filing/calibrate_editorial.py` measures case-independent editorial
  checks against the evaluation gates.
- `spend.py` holds the 3 USD total cap for paid runs and keeps the ledger in `out/spend.ndjson`.

## Use these

Start with [`README.md`](./README.md) for runnable commands. Use `bars.py` as the single source
of the armed retrieval, honesty, and groundedness thresholds.

## Avoid / anti-patterns

Do not treat fake-backend results as model-quality evidence. Do not change the frozen corpus
without updating its provenance and goldens.

## Data & contracts

`corpus/` is the committed reference knowledge repository: sources, wiki pages, and entity pages
in `public` and three restricted layers, with frozen provenance. The scoped reader identities are
in `qa_identities.json` and `graph_identities.json`. `eval_history.py` appends
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
