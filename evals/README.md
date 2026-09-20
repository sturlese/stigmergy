# Quality evaluations

The keyless test suite proves contracts and integration behavior. These optional evaluations use
real models to measure retrieval and answer quality over the frozen canonical corpus in
[`corpus/`](./corpus/).

## Retrieval

`run_retrieval.py` reports Recall@5 for lexical, vector, reciprocal-rank-fusion, and final ranking.
The golden set includes nine entity-filtered questions so ACL-safe entity membership remains part
of the measured query.

```bash
python evals/run_retrieval.py --embedder fake --rebuild --repo evals/corpus
python evals/run_retrieval.py --embedder openrouter --rebuild --repo evals/corpus \
  --report evals/out/retrieval.json
```

## Answers

`run_qa.py` drives the complete answering and verification path. It reports groundedness, honest
refusal, corrective handling of false premises, retry rate, and latency. ACL probes run as the
scoped `analyst` identity from `qa_identities.json`.

```bash
python evals/run_qa.py --embedder fake --llm fake --rebuild --repo evals/corpus
python evals/run_qa.py --embedder openrouter --llm openrouter --rebuild --repo evals/corpus \
  --report evals/out/qa.json
```

Real runs append one row to `history.ndjson`. Fake backends exercise plumbing and append nothing.
`run_gates.py` combines the two measured bars with the adversarial test suite.

The evaluation librarian skill remains at
`filing/repo/.claude/skills/librarian/SKILL.md` as the required contract copy; write-path behavior
is covered deterministically by the real Postgres/Git integration suite.

## Knowledge-graph parity

Filing quality has a separate real-model, fail-closed release gate in
[`filing/README.md`](./filing/README.md). It compares Hippocampus and Stigmergy over the same frozen
source corpus and initial graph, including cold-readable bodies, local citations, meaningful page
connections, entity precision and relationships, anti-fragmentation, writer gates, ACL safety, and
a provenance-bearing blind editorial review. Evidence is per case and per repeat: the selected
Stigmergy level needs three independent production-equivalent repeats of every case. Each repeat records
the initial plan, optional bounded correction, request telemetry, elapsed time, usage metadata, case and
fixture hashes, canonical initial-graph template manifest, and the exact brain-prompt commit/hash. The
release gate replays the recorded plan and effective output before permitting deployment only at the
lowest passing level. Schema v6 is an
invariant-based release record, not a reduced staged-planner artifact: it binds the candidate and prompt,
production-equivalent execution, per-case evidence, bounded correction, replayed gates, reasoning
selection, and the blind-review bundle. Earlier schemas and legacy phase fields are unsupported.
