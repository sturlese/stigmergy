# Filing parity release gate

`parity.py` validates recorded real-model evidence before an image can be published or rolled out.
It independently derives the candidate Git commit, the current librarian-skill hash, and the hashes
of every versioned filing case. An artifact cannot self-attest those inputs. This is a release gate,
not a fake-backend test or a score-only benchmark.

The artifact must contain independent Hippocampus and Stigmergy runs over the same corpus and initial
graph. Each run records the full raw semantic gate map: mutation and identity quality, links and
reference resolution, readable bodies, local provenance, entity relationships, anti-fragmentation,
and writer gates. The blind editorial review binds to the two exact run IDs and declares whether
Stigmergy has a material regression.

Every Stigmergy reasoning-level result repeats the same corpus, initial graph, case hashes, candidate
commit, skill hash, runtime route, and complete raw gate map. The selected production level must be
the first passing level in the ordered matrix. Missing, stale, mismatched, incomplete, or failing
evidence rejects the release.

Run the gate directly while recording a release artifact:

```bash
.venv/bin/python evals/filing/parity.py --artifact /absolute/path/to/parity-result.json
```

Deploy only through the guarded entrypoint; it validates the artifact before `fly deploy`:

```bash
make deploy-staging PARITY_ARTIFACT=/absolute/path/to/parity-result.json
```

`scripts/deploy_staging.sh` rejects a missing, stale, mismatched, or failing artifact before it
calls `fly deploy`. It also requires an exact clean platform checkout, so keep the artifact outside
that checkout.
