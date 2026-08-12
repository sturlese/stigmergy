"""Text hygiene for anything untrusted that a person will read, plus the UNTRUSTED-DATA fence.

**Nothing here imports anything from this project** (`tests/test_architecture.py` enforces it) —
this module is the bottom of the stack, importable from every subsystem that renders text somebody
else wrote. If it ever needs a stigmergy import, the thing it wants belongs somewhere else.
"""

import os
import re


def parse_result_ref(ref: str | None) -> tuple[str, str] | None:
    """`capture_queue.result_ref` (`'<page path>@<sha>'`) -> `(path, sha)`, or `None` when it does
    not parse. Every caller counts a `None` as an explicit exclusion, never a silent skip.

    `rpartition("@")` on the LAST `@`, deliberately not hex-anchored: fixtures legitimately carry
    readable placeholder shas. **An absolute path or a `..` segment is UNPARSEABLE** — every call
    site turns the result into a filesystem read scoped to a repo checkout, and
    `pages/../../etc/passwd@sha` must never resolve outside it."""
    path, sep, sha = (ref or "").rpartition("@")
    if not sep or not path:
        return None
    if os.path.isabs(path) or any(segment == ".." for segment in path.split("/")):
        return None
    return path, sha


# C0/C1 control characters (newline/tab excluded — whitespace collapsing removes them): a hostile
# page title, capture body or steward note must not smuggle ANSI escape sequences into whoever
# renders it.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# The UNTRUSTED-DATA fence. Without neutralization, a hostile body containing the literal closing
# delimiter closes the fence early and everything after it reads as TRUSTED INSTRUCTIONS.
# `librarian.agent.fence` stays a separate implementation on purpose: its word-joiner placement
# differs, and its exact bytes reach a live agent's prompt.
_FENCE_TOKEN = "UNTRUSTED-DATA"
_FENCE_NEUTRALIZED = _FENCE_TOKEN + "⁠"   # U+2060 WORD JOINER: invisible, breaks the token


def neutralize_fence(text: str) -> str:
    """Insert an invisible word joiner into every in-band UNTRUSTED-DATA token so it can never
    act as a fence delimiter. Human-readable, inert as a fence."""
    return (text or "").replace(_FENCE_TOKEN, _FENCE_NEUTRALIZED)


def fence(body: str) -> str:
    """Wrap untrusted text in the UNTRUSTED-DATA fence, first neutralizing any in-band fence token
    so a hostile body cannot close the fence early — unchanged for a human, inert as a fence."""
    return f"<<<{_FENCE_TOKEN}\n{neutralize_fence(body)}\n{_FENCE_TOKEN};end>>>"


def sanitize(text: str) -> str:
    """Strip control characters from text destined for a terminal or log line."""
    return _CONTROL_RE.sub("", text or "")


def clamp(text: str, width: int) -> str:
    """Truncate to `width`, stopping at a WORD boundary when one is in reach — a hard byte slice
    can cut a runnable invocation or the load-bearing word in half. The boundary is only accepted
    inside the last quarter of the budget, so a single long token (a path, a hash) still truncates
    rather than collapsing to nothing. `width` of 0 (or less) means "no clipping".
    """
    out = text or ""
    if width <= 0 or len(out) <= width:
        return out
    cut = out[:width]
    space = cut.rfind(" ")
    if space >= width - max(1, width // 4):
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + "…"


def one_line(text: str, width: int) -> str:
    """`sanitize`, then every whitespace run collapsed to one space, then `clamp`.

    `sanitize` deliberately leaves newlines and tabs, so it alone is no one-line guarantee — on a
    surface built to be one line, a landed newline is an injection seam as well as a display bug.
    Collapse must run BEFORE `clamp`: collapsing shortens, so the other order picks the truncation
    point against text the reader never sees rendered.
    """
    return clamp(" ".join(sanitize(str(text or "")).split()), width)
