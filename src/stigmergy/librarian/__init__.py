"""The librarian: the fast lane's back half — the single writer that drains the capture queue.

Claim one capture, let a bounded agent draft a page inside a throwaway worktree of the
knowledge repo, let CODE veto the resulting diff, and commit exactly one page per capture — or
refuse, with a reason the submitter can act on. The agent judges (placement, wikilinks,
anchoring, duplication); code vetoes over the diff, after the fact. Gates check; they never
interpret.

Layering (`tests/test_architecture.py` enforces it): `librarian` may import `capture` and
`kernel`; it may **not** import `server` or `answer` — a worker beside the API, talking to it
only through the durable queue row, so a slow agent run can never happen inside an HTTP request.
"""
