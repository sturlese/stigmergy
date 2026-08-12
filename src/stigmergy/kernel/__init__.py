"""stigmergy.kernel — the shared bottom of the stack: dependency-light primitives (model
dispatch, page contract, frontmatter, ACL, registry, normalization, document converters).

A LIBRARY, not a layer: it may be imported from anywhere and imports nothing from this project
except itself (pinned in `tests/test_architecture.py`).
"""
