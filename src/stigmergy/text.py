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
# page title, capture body or model-written report line must not smuggle ANSI escape sequences into
# whoever renders it.
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


# `sanitize` deliberately does NOT strip these: at the bottom of the stack a U+2028 in a search hit
# is inert, so the extra step lives in the one function that renders a scalar into a PROMPT.
_LINE_SEPARATORS = str.maketrans({"\u2028": " ", "\u2029": " "})


def prompt_scalar(value: str) -> str:
    """One untrusted scalar rendered into a prompt OUTSIDE the fence: `text.sanitize` plus
    U+2028/U+2029, which survive it and which `json.dumps` emits RAW, splitting the structural
    block. A REPLACEMENT, never a whitespace collapse, or a filename carrying two spaces is
    rewritten into one that names no file. PUBLIC: every unfenced scalar comes through here."""
    return sanitize(str(value or "")).translate(_LINE_SEPARATORS)


# The `###` that opens every unfenced prompt-section header, neutralized the way the fence token
# is and for the identical reason: a scalar that can spell the delimiter can forge the structure.
_SECTION_TOKEN = "###"
_SECTION_NEUTRALIZED = "#⁠##"   # U+2060 WORD JOINER: invisible, breaks the token


def prompt_header_scalar(value: str) -> str:
    """One untrusted scalar rendered into an unfenced prompt HEADER that is not a path: collapsed
    onto one line, and unable to spell a header of its own.

    Two steps, each closing half of the same hole. **Collapse** folds `\\n`, `\\r`, U+2028/U+2029
    and every other Unicode whitespace to a single space — `prompt_scalar` alone does not, because
    `sanitize` defends terminals rather than line structure and deliberately keeps `\\n`.
    **Neutralize** breaks any in-band `###`, because the readers of these headers are not
    line-anchored: a model reads structure loosely, and the offline doubles parse with a regex
    whose section pattern can match mid-line. Collapsing alone would have left a forged header
    sitting on the real one's line, still parseable as a second page.

    NOT interchangeable with `prompt_scalar`, and the split is the point: a PATH is dropped rather
    than rewritten (`is_one_line`), because a filename carrying two spaces collapsed into one names
    no file. Everything else in a header — an entity id, a page id — is context the model reads and
    never resolves back to a file, so rewriting it costs nothing and loses no page.
    """
    return " ".join(prompt_scalar(value).split()).replace(_SECTION_TOKEN, _SECTION_NEUTRALIZED)


def is_one_line(path: str) -> bool:
    """A path that cannot be named on ONE line is not named at all.

    `text.sanitize` strips control characters and deliberately keeps `\\n` — it defends terminals,
    not line structure — so a page whose FILENAME carries one would emit a second line inside the
    unfenced index, and a second line there is a forged `### finding` header the model reads as a
    real finding. Filenames may contain newlines on every filesystem this runs on.
    """
    text = str(path or "")
    return "\n" not in text and "\r" not in text
