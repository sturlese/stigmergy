"""The proposer's brief, as a FILE in the knowledge repo: where it lives, how it is read, and how a
code-owned frame is composed in front of it.

Shared by every model road in this package — the three the proposer runs overnight and the sweep
writer the server runs in the act (ADR 043 D4) — and by nothing else. A separate module rather than
a corner of `proposer.py` because the server reaches the sweep writer and must not drag the
proposer's orchestration (findings, the gardener store, `job_runs`) in with it; this module loads no
model stack and no store, only the filesystem seam.
"""
import os

from stigmergy.repair.errors import RepairError

# ── the operating procedure, in the knowledge repo ───────────────────────────────────────────
SKILL_RELPATH = ".claude/skills/repair-proposer/SKILL.md"
# The same ceiling `librarian.agent` puts on its own skill, for the same reason: a procedure is a
# page of prose, and anything larger is a mistake or a payload.
MAX_SKILL_BYTES = 256 * 1024

SKILL_SEPARATOR = "── the `repair-proposer` skill, from {relpath} ──\n\n"

# ── the prompt's two halves, and the line that divides them ──────────────────────────────────
# Every brief in this package is in TWO halves and the order is load-bearing: an unfenced INDEX
# first — ids and page paths, which are structure — then everything untrusted, every byte of it
# fenced. `DETAILS_MARKER` separates them and is emitted exactly once, before any fenced content,
# so "the index" is definable as "everything before the FIRST marker" no matter what a page body
# contains. That is what lets every offline double read a prompt's structure without a fence
# parser of its own, and it is why one page path per line: a path may carry spaces and commas.
DETAILS_MARKER = "## the findings' own words, and the pages they name"
PAGE_LINE = "page: "


def skill_path(repo: str) -> str:
    """Where the `repair-proposer` skill lives in a checkout of the knowledge repo."""
    return os.path.join(repo, *SKILL_RELPATH.split("/"))


def read_skill(repo: str) -> str:
    """The skill's text, size-capped BEFORE the bytes are read, from the checkout being repaired.

    A missing or empty skill raises: this is the agent's whole operating procedure, and a
    proposer running without it would be one briefed only by the header above — which says what it
    may not do and nothing at all about what is worth doing.

    The LEAF is judged before anything resolves it, `gather.confined_page`'s own ordering: both
    `getsize` and `open` follow a link, so the size ceiling would measure the target instead of
    guarding it, and whatever the link pointed at would become the system prompt.
    """
    path = skill_path(repo)
    if os.path.islink(path):
        raise RepairError(
            f"the repair-proposer skill at {SKILL_RELPATH} is a symlink — it is read as the "
            f"proposer's entire operating procedure and must be a real file committed in the "
            f"knowledge repo, not a pointer at something else on the host")
    try:
        size = os.path.getsize(path)
        if size > MAX_SKILL_BYTES:
            raise RepairError(f"the repair-proposer skill at {SKILL_RELPATH} is {size} bytes, over "
                              f"the {MAX_SKILL_BYTES}-byte ceiling")
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except RepairError:
        raise
    except (OSError, UnicodeDecodeError) as ex:
        raise RepairError(
            f"the repair-proposer skill is missing or unreadable at {SKILL_RELPATH} in the "
            f"knowledge repo ({ex.__class__.__name__}) — it is the proposer's operating procedure "
            f"and it will not propose without it") from ex
    if not text.strip():
        raise RepairError(f"the repair-proposer skill at {SKILL_RELPATH} is empty")
    return text


def with_skill(header: str, skill_text: str) -> str:
    """The code-owned header plus the skill's body, frontmatter dropped (loader metadata, and an
    `allowed-tools` key would be a second, unenforced tool list). `replace`, not `format`: a
    procedure containing a JSON example would otherwise take the run down at the last moment."""
    body = skill_text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + len("\n---"):]
    return (header.replace("{relpath}", SKILL_RELPATH)
            + SKILL_SEPARATOR.replace("{relpath}", SKILL_RELPATH)
            + body.strip() + "\n")
