"""The hybrid derived index (ADR 012).

Postgres + pgvector over a checkout of the knowledge repo: lexical arm (FTS), semantic arm
(multilingual embeddings), RRF fusion, then the explainable contract ranking factors. Derived and
disposable: rebuilt from git at will, never a source of truth, never migrated — wipe and rebuild
IS the upgrade path.

Deliberately shares no code with any WRITER: the index carries its own frontmatter parser and
wikilink resolver, so a refactor on the writing side can never silently change what gets indexed.
`tests/index/test_architecture.py` enforces the separation.

This layer knows no identity. `acl` labels are parsed and stored here; access is decided ABOVE, by
`stigmergy.server.acl.visible()`.
"""
