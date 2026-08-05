"""The admin console (ADR 029): a web operations surface mounted as an ASGI branch in front of
the MCP transport, inside the `app` process group.

The package is a SKIN, not a subsystem: every operational act it exposes lands on a seam another
package already owns and tests — `capture.dispositions` for the steward drain, `retention.purge`
for retention, `gardener.store` for findings, `digest.run` for the digest, `index.check` for the
substrate lint. The only state of its own is the `admin_actions` bookkeeping table. What it may
import is pinned by `tests/test_architecture.py`'s admin section; what it must never become is a
read surface over the corpus — no search, no page bodies, aggregates only.
"""
