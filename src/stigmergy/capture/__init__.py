"""`stigmergy.capture` — the durable write-path front half.

Sibling of `index`, `server` and `answer`. It owns the capture queue: the durable table a
submission lands in, the exactly-once claim primitive a single-writer librarian drains it with,
the content-addressed evidence store the raw material is archived to, the operational spine
(`job_runs`, `ingest_errors`) and retention.

Layering (enforced by `tests/test_architecture.py`): `capture` must NEVER import
`stigmergy.server` or `stigmergy.answer`. `stigmergy.server` imports `capture`
(the MCP tools ride `BrainService`, which is the only place identity, rate limiting and audit
are resolved). The one outward edge allowed is `capture.cli` → `stigmergy.index.store`, purely
for the connection seam (`store.connect`/`store.dsn`): the queue lives in the SAME Postgres as
the index and re-deriving the autocommit-connection discipline here would fork it. Library code
in this package never opens a connection — every function takes `conn` as an argument.

**The queue is durable; the index is disposable.** `pages_index` is dropped and rebuilt on every
`stigmergy-index --rebuild`; `capture_queue` holds material that exists nowhere else until the
librarian files it. `schema.DURABLE_TABLES` names the tables a rebuild must never take with it.
"""
