"""The hybrid derived index: Postgres + pgvector over a knowledge-repo checkout — an FTS arm and a
vector arm fused with RRF, then explainable contract ranking. Derived and disposable: rebuilt from
git at will, never a source of truth, never migrated — wipe and rebuild IS the upgrade path.

Shares no code with any WRITER: it carries its own frontmatter parser and wikilink resolver, so a
refactor on the writing side can never silently change what gets indexed (architecture-tested).
This layer knows no identity: `acl` is parsed and stored here; access is decided ABOVE, by
`stigmergy.server.acl.visible()`.
"""
