# Filing parity release gate

`parity.py` validates recorded real-model evidence before an image can be published or rolled out.
It independently derives the candidate Git commit, the current librarian-skill hash, the hashes of
every versioned filing case and fixture, and the exact checked-out brain prompt. An artifact cannot
self-attest those inputs. This is a release gate, not a fake-backend test or a score-only benchmark.

The artifact contains one complete result for every current case inside every recorded repeat. It
does not accept a summary score standing in for individual case results. Schema v3 binds every result
to the SHA-256 of its versioned case and readable fixture, plus the commit and SHA-256 of the exact
knowledge-repository librarian prompt that executed it. Each case result records the full raw semantic
gate map: mutation and identity quality, links and reference resolution, readable bodies, local
provenance, entity relationships, anti-fragmentation, writer gates, actual model requests, derived
schema retries, semantic-repair count, elapsed time, and a content-addressed output reference. Its
canonical payload retains the original and effective derived plans, never source text. The gate
recalculates that payload hash, semantic score, writer gate, and raw gate map against the current case
and fixture. Token usage is recorded only when the runner exposes it; provider fields are never inferred.

The selected Stigmergy level requires at least three independent, complete,
`production-equivalent` repeats per case. Those runs use the production Azure route and the fixed
two-request budget, exercise the temporary-worktree writer and bounded semantic-repair path, and must
all pass. Hippocampus and the selected Stigmergy runs share the same corpus and initial graph. The
blind editorial review binds to every exact selected run ID and declares whether Stigmergy has a
material regression. Hippocampus is comparative baseline evidence, not an admission candidate: its
hard gates may honestly fail under Stigmergy's writer rubric, but every case, provenance field,
canonical payload digest, semantic score, writer result, and raw gate map is still replay-verified.

Every Stigmergy reasoning-level candidate records full per-case runs using the same corpus, initial
graph, case hashes, candidate commit, skill hash, runtime route, and complete raw gate map. The
selected production level must be the first passing level in the ordered matrix and equal the one
runtime librarian reasoning setting. The runner accepts a diagnostic `planner-only` mode, but that
evidence can never select a production level. Missing, stale, mismatched, aggregate-only, incomplete,
or failing selected-Stigmergy evidence rejects the release.

An admission packet is terminal only when `admission_status` is `passed`, no field is pending, and
`blind_review_packet.status` is `completed`. Its `blind_editorial_review.provenance.artifact_ref` is a
`sha256:<digest>` reference to `blind-review-unblind-<digest>.json` in the same external evidence
directory. The validator loads that record and its content-addressed reviewer response, blind packet,
and private mapping; it verifies their raw and canonical hashes, packet bindings, candidate labels,
case-output coverage, reviewed run IDs, and the mechanically derived verdict. Keep the complete
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
  --brain-root /absolute/path/to/verified/stigmergy-brain \
  --run-id <independent-case-run-id>
```

`--brain-root` is the verified Git checkout used only to prove the exact librarian prompt. Do not
pass it as `--worktree`: the latter is the fixed versioned initial graph at `evals/filing/repo` by
default. The runner validates the template's librarian skill byte-for-byte against the packaged
skill, but the template itself does not need to be a Git checkout.

By default the runner prints safe telemetry, gate results, prompt/case/fixture provenance, and the
content hash only. Add `--include-payload` only when recording a local release artifact: it prints the
derived page bodies and an explicit stderr warning, but never the supplied source text. The emitted
`case_result` with that flag is the exact object embedded under its repeat's `case_results` array.
Use the same repeat ID for every case in one matrix repeat; record a new ID for each independent
repeat. The top-level repeat records the shared runtime, execution mode, two-request budget, and
candidate provenance. The validator requires the nested records to repeat that runtime and budget,
so summaries cannot stand in for individual outcomes.

[`parity-artifact.example.json`](./parity-artifact.example.json) documents the nested shape only;
it is deliberately marked `example_only` and cannot pass the release validator. A real artifact
must contain every current case and complete run objects at every recorded matrix level.

Run the gate directly while recording a release artifact:

```bash
.venv/bin/python evals/filing/parity.py --artifact /absolute/path/to/parity-result.json \
  --brain-root /absolute/path/to/verified/stigmergy-brain --brain-commit <verified-40-char-commit>
```

Deploy only through the guarded entrypoint; it validates the artifact before `fly deploy`:

```bash
make deploy-staging PARITY_ARTIFACT=/absolute/path/to/parity-result.json
```

`scripts/deploy_staging.sh` rejects a missing, stale, mismatched, or failing artifact before it
calls `fly deploy`. After refreshing the brain at its verified commit, it compares the artifact prompt
provenance, the brain `SKILL.md`, and the packaged platform skill byte for byte. It also requires an
exact clean platform checkout, so keep the artifact outside that checkout.
