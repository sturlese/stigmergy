"""`stigmergy.repair` — the repair loop: a finding's path to zero, without anybody being asked.

The gardener DETECTS and fixes nothing. This package gives a finding somewhere to go without
handing a model a write path: an agent DECLARES a concrete change; CODE validates it twice — when
it is derived and again in the tree it will be committed from, through the SAME gates the
librarian's own declared edits pass; and code applies EXACTLY those ops as one App-authored commit.
Nobody approves it. What stands in the way of a bad repair is the validator, the nine
gates, the ceilings and the ledger's permanent memory — not a person reading a queue.

What replaces the reading is that the diff is STORED: every applied repair carries the unified diff
it landed, because nobody saw it beforehand.

The agent's judgment lives in a skill in the KNOWLEDGE repo, never in Python here, and the op
vocabulary is the librarian's declared-edit kinds only (`page.EDIT_KINDS`) — three strictly
additive shapes every gate already knows how to judge — plus the three non-additive kinds each
guarded by its own validator and its own told-permission.

No module here reads the environment except `settings.py`, and none opens a connection: `conn` and
settings arrive as plain arguments, from `librarian.worker`'s idle pass. Per-module import edges
are pinned by `tests/test_architecture.py`. See `index.md` for the code map.
"""
