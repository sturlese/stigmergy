# Quality evaluations

The keyless test suite proves contracts and integration behavior. These optional evaluations use
real models to measure retrieval and answer quality over the frozen canonical corpus in
[`corpus/`](./corpus/).

## Retrieval

`run_retrieval.py` reports Recall@5 for lexical, vector, reciprocal-rank-fusion, and final ranking.
The golden set includes nine questions filtered by entity page so ACL-safe entity linking remains
part of the measured query.

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
  --live --max-usd 0.05 --report evals/out/qa.json
```

Real runs append one row to `history.ndjson`. Fake backends exercise plumbing and append nothing.
`run_gates.py` combines the two measured bars with the adversarial test suite.

The evaluation runner uses the librarian skill packaged with the platform. Write-path behavior is
covered deterministically by the real Postgres/Git integration suite.

## Graph search

`graph_corpus/` is a second frozen corpus: a fictional cold-chain company filed source by source
through the production librarian with `build_graph_corpus.py`, including a `finance` layer.
`graph_golden.json` asks 238 questions against it (about 50 each for lookup, relational, and
neighbor; 15 to 25 each for global, alias, ACL, absent, and refute), with reader scopes in
`graph_identities.json`. `graph_detail_golden.json` asks 50 questions about details planted in four
long sources that `make_long_sources.py` writes from fixed seeds, one of them in the finance layer.
`graph_qa_subset.json` is the 22-question answer set.

`run_graph_retrieval.py` runs the questions through `BrainService` search with entity resolution
and ACL, and reports recall at each width, reach at each width (an expected source counts when a
hit within the width cites it, one read away for the answering agent), MRR, entity share, leaks,
and search latency per family,
with the corpus's graph measures from `index.measures`.
`--ablate` switches graph features off inside the run to measure each one's share.
`run_qa.py` with a graph golden measures answers, model requests, tokens, and cost on the same
questions. A run with `--rebuild` replaces the evaluation index with its `--repo`, so rebuild the
graph corpus again before graph runs that follow a canonical one.

```bash
python evals/run_graph_retrieval.py --embedder fake --rebuild
python evals/run_graph_retrieval.py --embedder openrouter --rebuild --live --max-usd 0.05
python evals/run_qa.py --golden evals/graph_qa_subset.json --identities evals/graph_identities.json \
  --embedder openrouter --llm openrouter --rebuild --repo evals/graph_corpus --live --max-usd 0.40
```

## Listing

`graph_listing_golden.json` asks eight enumeration and period questions over `graph_corpus/`: every
person, organization, product, and concept page, the notes of one month by the dates of the sources
they cite, and the notes that mention one organization, two of them as the scoped `analyst`.
Expected items were computed from the corpus files, not from `list_pages`. A question passes when
the answer names every expected item and no forbidden one (case, accents, and punctuation folded).

```bash
python evals/run_qa.py --golden evals/graph_listing_golden.json \
  --identities evals/graph_identities.json --embedder openrouter --llm openrouter --rebuild \
  --repo evals/graph_corpus --live --max-usd 0.40
```

## Aliases

`alias_golden.json` holds invented alias candidates: an entity, a variant a page links it by, and
the sentence around the link, for true other names and for roles, descriptions, parts, and products
named after their company. `run_aliases.py` asks the production classifier and reports, for each
threshold, the non-aliases it would accept and the recall over true aliases. It calls the real
classifier only.

```bash
python evals/run_aliases.py --live --max-usd 0.05 --report evals/out/aliases.json
```

## Capture routing

`routing_golden.json` holds invented captures, each with the layers its sender may publish to and
the expected layer or `default`. `run_routing.py` routes them through the production classifier
and reports, against spec `audience-layers.md` §3.6.4, how many unambiguous cases reach their layer
and how many ambiguous ones reach the default. It calls the real classifier only.

```bash
python evals/run_routing.py --live --max-usd 0.05 --report evals/out/routing.json
```

## Spending cap

Every paid runner in this directory except `run_retrieval.py` requires `--live` and `--max-usd`.
`run_qa.py`, `run_graph_retrieval.py`, and `build_graph_corpus.py` price each call through
`spend.py`, stop at their cap, and keep a ledger in `out/spend.ndjson`; no run starts when the
ledger total plus its cap would pass 3 USD, the cap of `specs/retrieval-and-curation.md`; each
specification starts its own ledger. The ledger's baseline row also lets each run compare the
provider's own usage counter with the cap. `run_aliases.py` and `run_routing.py` stop once that
counter has moved by their cap and add nothing to the ledger.

## Knowledge-graph parity

Filing quality has a separate real-model, fail-closed release gate in
[`filing/README.md`](./filing/README.md). It compares three implementations over the same frozen
source corpus and initial graph: Hippocampus as the baseline, the Stigmergy candidate, and the
configuration in production. It covers cold-readable bodies, local citations, meaningful page
connections, entity pages, link coverage and density, anti-fragmentation, writer gates, ACL safety,
and a provenance-bearing blind editorial review. Evidence is per case and per repeat: the candidate
needs exactly three independent production-equivalent repeats of every case, and production as many.
Each repeat records the initial plan, optional bounded correction, request telemetry, elapsed time,
usage metadata, case and fixture hashes, and the canonical initial-graph template manifest; the
artifact binds the candidate's and production's commits and librarian-skill hashes. The gate replays
the recorded plans and effective output, and permits deployment only when every candidate repeat
passes and the blind review finds no more material regressions in the candidate than in production.
Schema 9 is the only accepted artifact format.
