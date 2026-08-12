"""`stigmergy.capture` — the durable write-path front half: the capture queue, the exactly-once
claim primitive, the content-addressed evidence store, the operational spine and retention.

Layering (enforced by `tests/test_architecture.py`): `capture` never imports `stigmergy.server`
or `stigmergy.answer`; the one outward edge is the CLIs → `stigmergy.index.store`, for the
connection seam only. Library code never opens a connection — every function takes `conn`.

The queue is durable; the index is disposable: `capture_queue` holds material that exists
nowhere else until the librarian files it, and `schema.DURABLE_TABLES` names what a rebuild
must never take with it.
"""
