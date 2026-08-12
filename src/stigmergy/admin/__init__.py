"""The admin console: a web operations surface mounted as an ASGI branch in front of the MCP
transport, inside the `app` process group.

A SKIN, not a subsystem: every operational act lands on a seam another package already owns and
tests; the only state of its own is the `admin_actions` bookkeeping table. Imports are pinned by
`tests/test_architecture.py`'s admin section. It must never become a read surface over the corpus
— no search, no page bodies, aggregates only.
"""
