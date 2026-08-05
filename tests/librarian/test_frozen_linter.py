"""The frozen contract linter must not drift from the knowledge repo's own.

`tests/librarian/fixtures/repo/.claude/tools/stigmergy_lint.py` is a byte-for-byte copy of the
knowledge repo's linter (see `FROZEN.md` beside it for why copying was the right call). Copying is
fine; copying with nothing watching it is not, and this is the thing that watches it.

**Why this one matters more than an ordinary duplication guard.** `gates.gate_contract` is the
ONLY contract check the librarian's commits ever receive — it pushes direct to `main` on the fast
lane, so no PR and no CI run sits between a filed page and the graph. A frozen copy that falls
behind means the librarian is quietly held to a standard the repo has moved on from.

Skips, with the resync command, when the knowledge repo is not on this machine — that is the whole
point of the copy existing, so a laptop or a CI runner without it must not fail here.
"""
import os
import pathlib

import pytest

from stigmergy.librarian import config

FROZEN = (pathlib.Path(__file__).parent / "fixtures" / "repo" / ".claude" / "tools"
          / "stigmergy_lint.py")
NOTES = FROZEN.parent / "FROZEN.md"

# The same resolution the librarian itself uses (`config.Settings.repo`): `$STIGMERGY_REPO`, else
# `../knowledge-repo` beside this checkout. No absolute path is hardcoded — one machine's layout is not a
# test's business.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _knowledge_repo() -> pathlib.Path:
    configured = os.environ.get(config.REPO_ENV)
    return (pathlib.Path(configured) if configured
            else (REPO_ROOT / config.REPO_DEFAULT)).resolve()


def test_the_frozen_linter_is_byte_identical_to_the_knowledge_repos_own():
    source = _knowledge_repo() / ".claude" / "tools" / "stigmergy_lint.py"
    if not source.exists():
        pytest.skip(
            f"no knowledge repo at {_knowledge_repo()} (set ${config.REPO_ENV}) — the frozen copy "
            f"exists precisely so the suite does not need one. To resync when you do have it:\n"
            f"  cp \"$STIGMERGY_REPO/.claude/tools/stigmergy_lint.py\" {FROZEN.relative_to(REPO_ROOT)}")

    assert FROZEN.read_bytes() == source.read_bytes(), (
        f"the frozen contract linter has drifted from {source}. `gate_contract` is the only "
        f"contract check the librarian's direct-to-main commits get, so a stale copy means the "
        f"librarian is held to an outdated standard. Resync and record the new sha in "
        f"{NOTES.relative_to(REPO_ROOT)}:\n"
        f"  cp \"$STIGMERGY_REPO/.claude/tools/stigmergy_lint.py\" {FROZEN.relative_to(REPO_ROOT)}\n"
        f"  git -C \"$STIGMERGY_REPO\" log -1 --format=%H -- .claude/tools/stigmergy_lint.py")


def test_the_frozen_copy_records_the_commit_it_was_taken_from():
    """A copy with no recorded provenance cannot be resynced with confidence — "is this behind or
    ahead?" has no answer without the sha the copy was taken at."""
    notes = NOTES.read_text(encoding="utf-8")
    assert "Copied at commit" in notes
    sha = notes.split("Copied at commit")[1].split("`")[1]
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), (
        f"FROZEN.md must record the full 40-character source commit sha, found {sha!r}")
