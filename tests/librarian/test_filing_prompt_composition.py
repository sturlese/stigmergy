"""The system prompt and the per-item prompt, composed.

OLD BEHAVIOUR: this file had a twin, `test_meeting_prompt_composition.py`, one entry point over —
`build_meeting_prompt` was a second per-item builder with its own fence discipline and its own
section order. There is one pipe and one builder: `kind` chooses the PROSE a capture is filed with
and never a code path, so a transcript's own metadata rides `build_prompt`'s hint channel like
every other capture's. The preamble in front of the brief is built from a shared frame plus ONE
per-backend ENVIRONMENT paragraph, and the per-item prompt is built by that ONE builder whose two
caller-declared facts (`gathered_block`, `outcome_channel`) are the only things a backend varies.

**A whole section of this file retired with the `sdk` backend, and it is worth saying what it
proved.** the structured filing flow split one preamble string into four pieces so a second backend could vary exactly
one of them, and claimed the split was byte-preserving. A byte-preserving refactor is exactly the
kind of claim that is asserted in a commit message and never checked — so these tests read the
PRE-the structured filing flow source out of git (`git show <pinned sha>:src/.../agent.py`), rebuilt the old constants
and the old `build_prompt` by `ast`, and compared whole strings. What was at stake was the M3
retire-or-keep decision: a preamble that drifted by a sentence during the extraction would have
moved the SDK arm of a comparison between two shapes ON THE SAME MODEL, and the golden would simply
have reported a different number.

That decision has been taken and that arm no longer exists, so the comparison has nothing to be
fair between and the pinned sha has nothing to be extracted for. **The extraction machinery went
with it** — `_source_before`, `_string_constant`, `_function_before` and the `before` fixture — and
the rule it embodied is recorded here rather than in a deleted docstring: *a refactor claimed to be
byte-preserving is checked against the bytes, from the object database, not against a copy pasted
into the test.* The next such claim rebuilds it; nothing here is load-bearing for it.

Keyless and repo-free: the brief is this package's own fixture.
"""
import pathlib

import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import pydantic_backend

BRIEF = (pathlib.Path(__file__).parent / "fixtures" / "repo" / ".claude" / "skills" / "librarian"
         / "SKILL.md")


@pytest.fixture(scope="module")
def brief_text() -> str:
    return BRIEF.read_text(encoding="utf-8")


# ── the frame, and the variation point ─────────────────────────────────────────────────────────
# Two tests here compared the TWO composed preambles against each other — that they shared the
# opening, the body and the separator, that their environment paragraphs genuinely differed, and
# that neither told a backend about a capability it did not have. They needed two backends and
# there is one, so they went with the second. The mechanism they guarded did not: `header` is still
# a required, caller-declared parameter of `build_system_prompt`, and the day a second environment
# paragraph exists those three comparisons are the ones to write.
def test_the_header_is_REQUIRED_and_no_backends_preamble_can_be_inherited_by_default(brief_text):
    """**The parameter change the retirement forced, pinned as a refusal.** `header` used to have a
    default, and the default was one backend's own preamble — so a caller that forgot it was
    silently briefed with another backend's environment: told it holds `Read`/`Glob`/`Grep`/`Write`
    when it holds no tool at all, in the same system prompt as the procedure it must follow.

    `agent.py` drives no model and therefore HAS no environment to default to, which is why the
    fix is a required keyword rather than a better default. A `TypeError` at composition time is
    loud, immediate and impossible to mistake for a working run — the opposite of what a wrong
    default produces.
    """
    with pytest.raises(TypeError):
        agent_module.build_system_prompt(brief_text)

    assert agent_module.build_system_prompt(
        brief_text, header=pydantic_backend.ORDINARY_AGENTIC_SYSTEM_PROMPT_HEADER)


def test_the_brief_arrives_with_its_frontmatter_stripped_and_its_body_whole(brief_text):
    """One strip, one place, every backend — and it is not cosmetic. The skill's frontmatter is
    metadata for a loader this platform deliberately does not use, and `allowed-tools` in
    particular would become a SECOND, unenforced statement of a tool list inside the instructions
    of a run that holds none.

    The body below it is the procedure and must arrive intact: a strip that took one line too many
    would remove the top of the brief, which is where its own framing lives.
    """
    composed = agent_module.build_system_prompt(
        brief_text, header=pydantic_backend.ORDINARY_AGENTIC_SYSTEM_PROMPT_HEADER)

    front, body = brief_text.split("---", 2)[1], brief_text.split("---", 2)[2]
    assert "name:" in front, "the fixture brief carries no frontmatter; this test proves nothing"
    assert "name: librarian" not in composed
    assert "allowed-tools" not in composed
    assert body.strip() in composed, "the brief's body did not arrive whole"
    assert composed.endswith("\n")


def test_a_header_containing_braces_composes_instead_of_raising(brief_text):
    """**`replace`, not `format`.** `header` is a parameter, so `str.format` would scan
    caller-supplied text for braces and raise on any that are not `{relpath}` — and a backend whose
    whole answer is a JSON-shaped object is exactly the caller likely to put an example in its own
    preamble. The run would die at the last moment before the model call, having built everything.
    """
    braced = agent_module.build_filing_header(
        '1. Return {"decision": "file"} — a set is {1, 2} and this is fine.\n')

    composed = agent_module.build_system_prompt(brief_text, header=braced)

    assert '{"decision": "file"}' in composed and "{1, 2}" in composed
    assert agent_module.SKILL_RELPATH in composed and "{relpath}" not in composed


def test_the_gathered_block_sits_above_the_material_it_is_context_for():
    """A reader meets its context before the thing the context is for. Below the material it
    would read as commentary on a document already read, and a model that had already decided
    placement would have nothing left to use it for.

    Driven through `build_prompt` itself now: the ordinary run composes its per-item message there
    directly, where it used to go through `build_structured_prompt`'s thin wrapper.
    """
    prompt = agent_module.build_prompt(
        material="A renewal note.", hints={}, submitted_by="a@b.test",
        gathered_block="\nGATHERED CONTEXT BLOCK",
        outcome_channel=pydantic_backend.ORDINARY_AGENTIC_OUTCOME_CHANNEL)

    assert prompt.index("GATHERED CONTEXT BLOCK") < prompt.index("The captured material follows")


def test_the_hint_block_sits_above_the_gathered_context_which_sits_above_the_material():
    """BOTH bounds of the same rule, on the one builder there is.

    OLD BEHAVIOUR: this was `build_meeting_prompt`'s own ordering pin — the gathered block between
    the REGISTRY and the transcript, because the two answered different questions and the order
    said which was which. That builder is gone, and the ordering it protected is the same ordering
    `build_prompt` owes: what the SUBMITTER suggested first (it is about this capture), then what
    this brain already wrote, then the material itself. A gathered block above the hints would put
    other people's page titles ahead of the submitter's own words about their own capture.

    The `kind="meeting"` hints are used deliberately: a transcript's metadata is the case that used
    to have a builder of its own, and this is where it lands now.
    """
    prompt = agent_module.build_prompt(
        material="A transcript.", submitted_by="a@b.test",
        hints={"title": "Q3 sync", "meeting_date": "2026-07-29"},
        gathered_block="\nGATHERED CONTEXT BLOCK")

    assert (prompt.index("The submitter's own suggestions")
            < prompt.index("GATHERED CONTEXT BLOCK")
            < prompt.index("The captured material follows"))


def test_the_prompt_is_unchanged_when_the_worker_gathered_nothing():
    """The benign twin for the branch: an empty `gathered_block` adds no section and no stray blank
    heading. A prompt that announced a context it did not carry would tell an agent it had been
    handed something it can neither see nor, on a tool-less backend, go and fetch."""
    without = agent_module.build_prompt(material="A transcript.", hints={"title": "Q3 sync"},
                                        submitted_by="a@b.test")
    with_empty = agent_module.build_prompt(material="A transcript.", hints={"title": "Q3 sync"},
                                           submitted_by="a@b.test", gathered_block="")

    assert without == with_empty
    assert "What this brain already holds" not in without


def test_the_outcome_channel_names_the_tool_the_account_is_written_with():
    """The channel sentence is the one line of the per-item prompt that varies per backend, and it
    has to be positively true of the run it goes into.

    **It changed direction in the agentic pydantic harness and that is exactly why it is pinned.** It used to say "you
    write no file and you have no tool that could"; this run writes its account as a file, with one
    specific tool, and a sentence naming the file but not the ROUTE is how a model reaches for a
    `Write` it does not have and reports having filed nothing. So: the file, the tool, and the
    field a run that writes its own pages owes (each entry's `path` in `pages`) — and NOT `body`,
    which belongs to the shape where code writes the page.

    OLD BEHAVIOUR: it named `page_path`, the singular field, while the brief above it asked for the
    list. A model following the preamble wrote N pages and declared one, and the cross-check
    refused the capture — the preamble and the brief disagreeing is the exact failure this file
    exists to catch.
    """
    channel = pydantic_backend.ORDINARY_AGENTIC_OUTCOME_CHANNEL

    assert agent_module.OUTCOME_FILENAME in channel
    assert "write_page" in channel
    assert "`pages`" in channel and "`path`" in channel
    assert "page.body" not in channel
