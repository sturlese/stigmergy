"""The librarian: the fast lane's back half — the single writer that drains the capture queue.

`stigmergy.capture` makes the brain writable (submit, attribution, the evidence plane) and stops at
the queue. This package is what drains it: claim one capture, let a bounded agent draft a page
inside a throwaway worktree of the knowledge repo, let CODE veto the resulting diff, and commit
exactly one page per capture — or refuse, with a reason the submitter can act on.

**The division of labour.** The agent judges (placement, wikilinks, anchoring, duplication —
identity and meaning problems, where a deterministic resolver fails silently); code vetoes (zone,
binary pages, body rewrites, secrets, PII, frontmatter, the contract linter, anchoring —
properties an independent checker can settle, over the diff, after the fact). Gates check; they
never interpret.

Layering (`tests/test_architecture.py` enforces it): `librarian` may import `capture` (the queue
primitives, the evidence plane, the operational spine) and `kernel` (the ACL resolver, the entity
registry, the page contract). It may **not** import `server` or `answer` — it is a worker beside
the API, never above or below it.
"""
