# `evals/corpus/` — the frozen reference corpus (38 pages)

The corpus both golden instruments run on. **Committed, not generated** — see
`PROVENANCE.json` for the origin of every part.

```sh
make db-up
python evals/run_retrieval.py --embedder openai --rebuild --repo evals/corpus
python evals/run_qa.py --embedder openai --llm openai --repo evals/corpus
```

**Why it is committed.** A corpus assembled on demand is a corpus that quietly changes underneath
a score series — and once it has changed, no two entries in that series mean the same thing.
Freezing it is what makes the numbers in `evals/history.ndjson` comparable across time by
construction rather than by hope.

**It is a fixture, so treat it as one.** Do not regenerate it, do not "tidy" it, and do
not add pages to make a question pass. It is the fixed thing the moving parts are
measured against; changing it silently invalidates every entry in
`evals/history.ndjson` recorded before the change.

This directory sits outside `evals/out/`, which the runners treat as disposable report output —
the same reason `history.ndjson` lives where it does.
