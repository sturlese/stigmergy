"""`stigmergy.answer` — the answering half: the agent that writes a cited answer and the
deterministic verifier that judges it before it leaves the server (ADR 007).

It is built on `stigmergy.server.service.BrainService`: the agent's tools call that service under
the caller's identity, and the text renderers here turn its structured (JSON) results into the
exact evidence the agent — and the verifier — sees.

Layer position (enforced by tests/test_architecture.py): `stigmergy.answer` sits ABOVE
`stigmergy.server`'s service (it consumes `BrainService` and the ACL primitive) and BELOW the MCP
adapter (`stigmergy.server.mcp_server` mounts `ask` on top of it). The service surface does not
change — the answering loop lives entirely in this package.

The strict verdict gate (ADR 007) lives in `service.py`, AFTER verification, never inside
`verify()` itself: after the single corrective retry, any remaining unverified figure suppresses
the answer and the server returns an honest refusal carrying the findings — no untraced figure
ever leaves.
"""
