"""`stigmergy.slack` — the Slack transport: resolve WHO IS ASKING from a Slack event, then
call the same `BrainService`/`AnswerService` every other transport calls. It enforces
nothing: `stigmergy.server.acl.visible()` stays the ONE enforcement point.

See `index.md` in this directory for the code map and the pinned layering.
"""
