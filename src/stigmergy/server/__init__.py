"""`stigmergy.server` — the single MCP server over the hybrid index and the capture queue, with ACL
enforcement wired from day one. One MCP server is the only API.

It carries three surfaces: the read tools over `stigmergy.index` (Postgres hybrid); the answering
loop (`ask`), served here but living in `stigmergy.answer`, which sits ABOVE this package; and the
write path (`brain_submit`, `brain_submissions`), whose queue lives in `stigmergy.capture`. The HTTP
transport adds per-user token auth, rate limiting and auditing over exactly the same tools.

This package is not read-only, but the boundary that made it read-only still holds: it queues a
capture and attributes it, and files nothing. Turning a capture into a committed page is the
librarian's job — see `docs/reference/capture.md`.
"""
