"""`stigmergy.answer` — the answering half: an agent writes a cited answer over `BrainService`,
and a deterministic verifier judges it before it leaves the server. Sits above `stigmergy.server`'s
service and below the MCP adapter; the strict verdict gate lives in `service.py`, outside `verify()`.
"""
