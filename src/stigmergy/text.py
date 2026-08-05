"""Text hygiene for anything untrusted that a person will read.

Five functions, one seam, and they live at the ROOT of the package on purpose: every subsystem that
renders text somebody else wrote needs them — `index` (search hits), `server` (page excerpts),
`librarian` (submitter reports), `capture` (queue rows and steward notes), `entities` (the steward's
terminal). A module below all of them can be imported by all of them without any package having to
reach sideways into another's internals.

That position is what makes the seam enforceable rather than merely conventional. These functions
were first written inside `index/rank.py`, because rendering search results is where they were
first needed — and the moment `capture.dispositions` needed the same cleaning (a steward's
`--reason` was reaching a submitter unsanitized, and the fix belonged BELOW both CLIs so no caller
could skip it), that import collided with the architecture rule that only `capture/cli.py` may
import `stigmergy.index`. The rule is about the database CONNECTION, not about pure functions that
touch no database, no environment and no clock. Weakening the rule would have left it saying one
thing and meaning another; moving the functions here keeps it literally true.

**Nothing here imports anything from this project**, and `tests/test_architecture.py` enforces it.
That is the property that makes this module safe to depend on from everywhere: if it ever needs a
stigmergy import, it has stopped being the bottom of the stack and the thing it wants belongs
somewhere else.
"""

import os
import re

# ── the capture-queue ref convention ────────────────────────────────────────────────────────────
# `capture_queue.result_ref` ('<page path>@<sha>') was hand-parsed at several independent call
# sites (`gardener.checks._recent_filed_pages`, `gardener.sweep.select_pages`,
# `digest.sections._filed_page_paths`) before it was consolidated here: one parser per format, at
# the bottom of the stack, with no project import, so every package above can use it without
# reaching sideways into a sibling. The call sites' own format-pinning tests assert the STRING
# SHAPE this function parses, so the parsing logic moving underneath them changed no behaviour
# they observe.


def parse_result_ref(ref: str | None) -> tuple[str, str] | None:
    """`'<page path>@<sha>'` -> `(path, sha)`, or `None` when it does not parse (empty, no
    `@`-separated sha, or a path this function refuses to hand back at all — see below). Never
    guessed at: every caller counts a `None` as an explicit exclusion rather than silently
    skipping it.

    `rpartition("@")` on the LAST `@`, deliberately not "improved" into a stricter (e.g. hex-only)
    pattern: real rows always carry a git sha after it, but plenty of this codebase's own fixtures
    stand in a readable placeholder (`"sha0"`, `"deadbeef"`, `"nomatchingsha"`) that a hex-anchored
    regex would wrongly refuse.

    **An absolute path or a `..` path segment is treated as UNPARSEABLE** — the identical "counted
    as an exclusion, never silently skipped" outcome an empty or `@`-less ref already gets. Every
    call site turns this function's result into a filesystem read scoped to a `--repo` checkout
    (`sampler._read_page` joins `repo` with `path.split("/")`), so a `result_ref` of
    `pages/../../etc/passwd@sha` must never resolve outside that checkout, into fenced prompts or a
    report. One shared parser is precisely what makes refusing it here, once, meaningful for every
    reader — as opposed to a fix each independent parser would need its own copy of. A well-formed
    ref (no leading `/`, no `..` segment) parses exactly as it always did."""
    path, sep, sha = (ref or "").rpartition("@")
    if not sep or not path:
        return None
    if os.path.isabs(path) or any(segment == ".." for segment in path.split("/")):
        return None
    return path, sha


# C0/C1 control characters (newline/tab excluded — they die in whitespace collapsing anyway):
# untrusted content is untrusted terminal output, and a hostile page title, capture body or steward
# note must not be able to smuggle ANSI escape sequences into whoever renders it.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# The UNTRUSTED-DATA fence (injection fencing) — the third seam this module carries, and it lives
# here for the SAME reason `sanitize`/`clamp` do. It had been found and hardened independently
# more than once, while other copies built the literal fence with an f-string and never
# neutralized an in-band occurrence of the token itself: a hostile captured page containing the
# literal closing delimiter closes the fence early and has everything after it read as TRUSTED
# INSTRUCTIONS. This module is the bottom of the stack (no project imports — see the module
# docstring), so it is where the hardened version belongs, and every future caller gets it for
# free instead of re-deriving it. `librarian.agent.fence` is a deliberate exception: its
# neutralized form places the word joiner differently, and consolidating it would change the bytes
# reaching a LIVE agent's prompt — a behaviour change belonging with its own review, never
# smuggled into a refactor.
_FENCE_TOKEN = "UNTRUSTED-DATA"
_FENCE_NEUTRALIZED = _FENCE_TOKEN + "⁠"   # U+2060 WORD JOINER: invisible, breaks the token


def neutralize_fence(text: str) -> str:
    """Insert an invisible word joiner into every in-band UNTRUSTED-DATA token so it can never
    act as a fence delimiter. Human-readable, inert as a fence."""
    return (text or "").replace(_FENCE_TOKEN, _FENCE_NEUTRALIZED)


def fence(body: str) -> str:
    """Wrap untrusted text in the UNTRUSTED-DATA fence, first neutralizing any in-band fence
    token so a hostile body cannot close the fence early and smuggle instructions into the
    reader's context. The neutralized token stays human-readable — only an invisible word joiner
    is inserted — so the text is unchanged for a human but inert as a fence."""
    return f"<<<{_FENCE_TOKEN}\n{neutralize_fence(body)}\n{_FENCE_TOKEN};end>>>"


def sanitize(text: str) -> str:
    """Strip control characters from text destined for a terminal or log line."""
    return _CONTROL_RE.sub("", text or "")


def clamp(text: str, width: int) -> str:
    """Truncate to `width`, stopping at a WORD boundary when there is one in reach.

    The second half of the same seam `sanitize` is: every surface that shows untrusted text to a
    person strips its control characters and then clips it, and the clipping was written twice —
    word-safe in `librarian.report._clean`, a hard byte slice in `capture.cli._clean`. That is not a
    cosmetic difference. A hard slice produced `— it is th…` in a real failure report (a sentence cut
    mid-word reads as a rendering bug and costs the reader the one word that would have told them
    what the problem was), and applied to the ask-back question it produced something worse than an
    unreadable sentence: `brain_reply(submission_id=14, an…`, a string that is not a valid tool call
    at all, printed under a message that tells the reader to run it.

    The boundary is only accepted inside the last quarter of the budget, so a single long token (a
    path, a rule id, a hash) is still truncated rather than collapsing to nothing. `width` of 0 (or
    less) means "no clipping", which is what both callers already meant by it.
    """
    out = text or ""
    if width <= 0 or len(out) <= width:
        return out
    cut = out[:width]
    space = cut.rfind(" ")
    if space >= width - max(1, width // 4):
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + "…"
