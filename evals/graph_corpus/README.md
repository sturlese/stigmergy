# Graph-search corpus

A fictional cold-chain monitoring company, filed source by source into an empty wiki by
`evals/build_graph_corpus.py` with the production planner, the writer's gates, and at most one
bounded correction per source. The tool replicates the worker's filing loop in memory; rejected
sources are dropped rather than archived, and `filing-log.json` records every outcome.

When the corpus was frozen, the writer rejected every finance-restricted source whole, because each
plan also tried to add restricted facts to public pages. The four restricted notes listed in
`PROVENANCE.json` were therefore written by hand in the shape the writer accepts: restricted notes
that link public pages without editing them. They now live in the finance layer
(`layers/finance/`), which also holds finance-only parts of shared pages, and the builder files a
restricted source in its layer's view the way the writer does. `evals/graph_golden.json` asks its
questions against this corpus.

Four long sources were added after freezing, listed under `added` in `PROVENANCE.json` and written
by `evals/make_long_sources.py`: an all-hands transcript, a customer advisory board transcript, a
quarterly operations report with sections, and a due diligence call in the finance layer, together
about 73,000 words that no wiki page cites. Each plants details, some beside a distractor on the
same subject, that `evals/graph_detail_golden.json` asks for.
