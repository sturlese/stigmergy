"""`stigmergy.repair` — the governed repair loop: a finding's path to zero.

The gardener DETECTS and fixes nothing. This package gives a finding somewhere to go without
handing a model a write path: an agent PROPOSES a concrete, additive change; CODE validates it
twice — at propose time and again at apply time, through the SAME gates the librarian's own
declared edits pass; a steward approves one proposal at a time; and code applies EXACTLY the
approved ops as one App-authored commit in a throwaway clone.

The agent's judgment lives in a skill in the KNOWLEDGE repo, never in Python here, and the op
vocabulary is the librarian's declared-edit kinds only (`page.EDIT_KINDS`) — three strictly
additive shapes every gate already knows how to judge.

`cli.py` is the only module that reads the environment or opens a connection; every other module
takes `conn` and settings as plain arguments. Per-module import edges are pinned by
`tests/test_architecture.py`. See `index.md` in this directory for the code map.
"""
