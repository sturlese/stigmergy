"""stigmergy — a team's knowledge, filed by an agent and answered with citations.

A markdown wiki in git — the knowledge repo, which this package never contains — written by one
bounded librarian model, judged by code, navigated through a curated entity spine. This package is
everything that is not the wiki itself.

Subpackages, roughly bottom-up:

- ``text`` · ``kernel``      the bottom of the stack: the untrusted-data fence; and the shared
                             dependency-free library (LLM dispatch, page contract, ACL resolver,
                             entity registry, document converters)
- ``capture``                the durable capture queue, attribution, the evidence plane, retention
- ``index``                  the hybrid derived index over the repo (Postgres + pgvector)
- ``librarian``              the ONE machine writer: worktree, injected brief, eight gates, push
- ``entities``               governed entity birth — the only path-scoped writer of ``ops/``
- ``views``               the per-entity rollup: a deterministic skeleton + a bounded synthesis
- ``server``                 one ``BrainService`` behind three transports; ACL decided at one point
- ``answer``                 the answering agent + the answer verifier (cites or refuses)
- ``slack``                  the third transport: 🧠 capture, Q&A, the steward doorbell
- ``gardener`` · ``digest``  corpus health as findings; the week's learning as one post
- ``admin``                  the operator console over the running deployment
"""
