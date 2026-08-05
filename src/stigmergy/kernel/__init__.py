"""stigmergy.kernel — the shared bottom of the stack.

Small, dependency-free primitives every other package needs and none of them should own:

- `llm` · `result` — the PydanticAI dispatch (`build_processor`) + the offline result envelope.
  Consumed by `views.synthesis`, `gardener.sweep`.
- `settings` — `resolve_backend()`, the CLEAN_LLM fake/real switch. Consumed by `llm`.
- `page` — the page-as-chunk cap + the frontmatter scalar emitter. Consumed by
  `librarian.processing`, `views.render`.
- `frontmatter` — `split_frontmatter`. Consumed by `entities.generator`.
- `acl` — the ACL resolver + the view audience intersection. Consumed by
  `librarian.acl_rules`, `views`.
- `registry` — `ops/entity-registry.json` reader/writer. Consumed by `entities`, `gardener`,
  `librarian`, `views`.
- `normalize` — entity-name canonicalization + slug. Consumed by `entities`,
  `librarian.processing`, `registry`.
- `fsutil` — atomic text write. Consumed by `views.regenerate`.
- `converters` — the document hands (pdf/docx/sheet/office) + `vision_extract`. Consumed by
  `librarian.processing`'s drive flow (ADR 028). Extraction is deliberately NOT agent-orchestrated
  (ADR 028 D5), so there is no agent-facing tool shape over these.

This package is a LIBRARY, not a layer: like `stigmergy.text` it may be imported from anywhere and
must import nothing from this project except itself. That rule is pinned in
`tests/test_architecture.py`.
"""
