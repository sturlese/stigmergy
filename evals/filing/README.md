# Filing parity release gate

`run_planner.py` records real-model evidence from the same agent-first filing path used in production.
One coherent librarian request returns a complete `FilingPlan`. The writer then applies deterministic
ACL, provenance, identity, link, and atomicity gates. One bounded replacement-plan request is allowed
only when those contracts reject the first plan; there is no staged semantic review pipeline.

`parity.py` binds every recorded result to the candidate commit, librarian skill, case,
fixture, canonical initial-graph template manifest, configured provider route, request budget, writer
result, semantic score, latency, usage, and content-addressed output. It rebuilds a pristine ACL-safe
worktree and replays the recorded plan rather
than trusting an aggregate score. Schema v6 is a clean cut designed from the active release invariants:
candidate and prompt binding, production-equivalent execution, per-case input/plan/result evidence,
bounded correction telemetry, replayed effective output, three configured production repeats, and a verified blind-review
bundle. Older evidence formats, staged planner phases, and legacy compatibility fields are unsupported.

Case contracts also score editorial quality that structural coverage alone cannot prove. They can require
multiple explanatory paragraphs, explicit evidence limits, preservation of named seeded knowledge, useful
contextual connections, and rejection of raw evidence inventories or duplicated entity-name prefixes.
These gates complement rather than replace the blind review: deterministic checks catch known regressions,
while the anonymous comparison remains the release authority for overall cold-read and reuse quality.
Entity editorial gates are deliberately source-neutral: they check substantive descriptions, durable facts,
temporal anchors when the case is time-sensitive, and separation between descriptions and facts, but never
require fixture phrases. The blind reviewer receives the complete recorded fixture alongside the complete
anonymous effective-plan payload for each candidate, and judges factual support, relevance, and whether the
resulting graph would remain useful outside the source.

The configured Stigmergy production level requires exactly three independent, complete,
`production-equivalent` repeats per case. Those runs use `deepseek/deepseek-v4.1-flash` through
throughput-sorted zero-data-retention OpenRouter providers, medium reasoning, and the same three-request
ceiling as the writer: one initial request, at most one schema retry, and at most one bounded
correction. Runs exercise the temporary-worktree
writer and its bounded contract-correction path, and must all pass. Hippocampus and the selected
Stigmergy runs share the same corpus and initial graph. The
blind editorial review binds to every exact selected run ID and declares whether Stigmergy has a
material regression. Hippocampus is comparative baseline evidence, not an admission candidate: its
hard gates may honestly fail under Stigmergy's writer rubric, but every case, provenance field,
canonical payload, semantic score, writer result, and raw gate map is still replay-verified.

Every production repeat records the same corpus, initial graph, case hashes, candidate commit, skill hash,
runtime route, token ceiling, and raw gate map. The librarian sends the completion ceiling to OpenRouter as
`max_completion_tokens`. Nonproduction reasoning evidence and diagnostic `planner-only` evidence are rejected.
Missing, stale, incomplete, or failing evidence rejects the release.

An admission packet is terminal only when `admission_status` is `passed`, no field is pending, and
`blind_review_packet.status` is `completed`. Its `blind_editorial_review.provenance.artifact_ref` is a
`sha256:<digest>` reference to `blind-review-unblind-<digest>.json` in the same external evidence
directory. The validator loads that record and its content-addressed reviewer response, blind packet,
and private mapping; it verifies their raw and canonical hashes, fixture text and hash, complete
effective-plan payloads, candidate labels, case-output coverage, reviewed run IDs, and the mechanically
derived verdict. The blind packet binds initial and optional correction plan hashes without exposing
implementation, model, provider, or run identity. Keep the complete
evidence bundle outside the candidate checkout; a self-reported verdict or mutable path is rejected.

### Trust boundary

This gate verifies the integrity and internal consistency of evidence produced by a trusted release
operator. The operator gives an independent reviewer only the blind packet and response schema, stores
the returned JSON verbatim, and performs the mechanical unblind. Content addressing proves which bytes
were reviewed; it does not authenticate the reviewer against an operator who controls the release host.
Such an operator is outside the current threat model because the same Fly authority can bypass the local
deploy entrypoint entirely. Defending against that actor requires moving deployment credentials and
review generation into a protected external CI environment; a locally managed signing key would not
create a meaningful additional trust boundary.

Run one immutable case-result record with the production-equivalent path:

```bash
.venv/bin/python evals/filing/run_planner.py --live \
  --source evals/filing/fixtures/harness_engineering_synthetic.md \
  --case evals/filing/cases/harness_engineering.json \
  --include-payload
```

`--worktree` is the fixed versioned initial graph at `evals/filing/repo` by default.
Production-equivalent evidence rejects any other worktree and records the materialized initial
worktree manifest. The runner always uses the librarian skill packaged with the candidate platform.

By default the runner prints safe telemetry, gate results, prompt/case/fixture provenance, and the
content hash only. Add `--include-payload` only when recording a local release artifact: it prints the
derived page bodies and an explicit stderr warning, but never the supplied source text. The emitted
`case_result` with that flag is the exact object embedded under its repeat's `case_results` array.
The artifact assembler assigns one run ID to the records in an independent repeat and a new ID to every
subsequent repeat. The top-level repeat records the shared runtime, execution mode, three-request ceiling,
and candidate provenance. The validator requires the nested records to repeat that runtime and budget, so
summaries cannot stand in for individual outcomes.

[`parity-artifact.example.json`](./parity-artifact.example.json) documents the nested shape only;
it is deliberately marked `example_only` and cannot pass the release validator. A real artifact
must contain every current case in each of the three complete production repeats.

Run the gate directly while recording a release artifact:

```bash
.venv/bin/python evals/filing/parity.py --artifact /absolute/path/to/parity-result.json \
  --repo-root /absolute/path/to/stigmergy
```

Deploy only through the guarded entrypoint; it validates the artifact before `fly deploy`:

```bash
make deploy-staging PARITY_ARTIFACT=/absolute/path/to/parity-result.json
```

`scripts/deploy_staging.sh` rejects a missing, stale, mismatched, or failing artifact before it
calls `fly deploy`. After refreshing the brain at its verified commit, it compares the artifact prompt
provenance and the packaged platform skill. It also requires an
exact clean platform checkout, so keep the artifact outside that checkout.
