"""The meeting system prompt, composed — one shared frame, one per-backend environment paragraph,
and the one place a backend contradicts the brief said out loud.

The brief is the KNOWLEDGE REPO's text, and the platform does not get to edit it. That has a
consequence nobody can wish away: the brief still tells its reader, in its own voice, that it holds
a `Write` tool and returns its account by writing `.librarian-outcome.json`. Inject that under a
preamble saying "you have NO tools" and the model is handed a flat contradiction with nothing to
say which half is operative — and a model that resolves it the other way describes writing a file
it cannot write, so the run comes back with an account about the wrong thing.

So there is an override note, and this file is what keeps it honest in both directions:

* **it must still be needed** — asserted against the frozen fixture brief's OWN sentences, so the
  day the knowledge repo's brief drops the tool and the file, this test fails and says the override
  can retire rather than sitting there forever correcting a contradiction that no longer exists;
* **it must be SCOPED** — it names the tool and the file it is about and sits immediately in front
  of the brief it corrects, so a reader meets the correction before the text being corrected.

A third property lived here — that the override was the ONLY divergence between the two composed
preambles — and it retired with the second backend. See the tombstone at the foot of this file for
what it measured and when it becomes writable again.

Keyless and repo-free: the brief is read from `tests/librarian/fixtures/repo/`, the frozen copy the
whole suite already files against.
"""
import pathlib

import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import pydantic_backend

BRIEF = (pathlib.Path(__file__).parent / "fixtures" / "repo" / ".claude" / "skills"
         / "meeting-distiller" / "SKILL.md")


@pytest.fixture(scope="module")
def brief_text() -> str:
    return BRIEF.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pydantic_prompt(brief_text) -> str:
    """What the structured backend actually sends — its own header, composed from the same pieces."""
    return agent_module.build_meeting_system_prompt(
        brief_text, header=pydantic_backend.MEETING_SYSTEM_PROMPT_HEADER)


# ── the contradiction the override exists for is really in the brief ───────────────────────────
def test_the_frozen_brief_still_tells_its_reader_it_holds_a_write_tool(brief_text):
    """**The override's own justification, checked against the text it overrides.**

    If this fails, the knowledge repo's brief has stopped describing a `Write` tool and an outcome
    file — and then `pydantic_backend.OVERRIDE_NOTE` is correcting a contradiction that no longer
    exists, which is a paragraph of prompt spent on nothing and one more thing for a reader to
    reconcile. The right response to this failure is to RETIRE the override, not to weaken this
    assertion.
    """
    assert "Write" in brief_text
    assert agent_module.OUTCOME_FILENAME in brief_text


def test_the_structured_backends_prompt_carries_both_the_contradiction_and_its_correction(
        pydantic_prompt):
    """The composed prompt is the artifact a model reads, so the property is asserted THERE rather
    than on the two strings that make it. Both halves have to be present: the brief's own sentences
    (this milestone changes not one word of them) and the note that scopes them."""
    assert "Write" in pydantic_prompt
    assert agent_module.OUTCOME_FILENAME in pydantic_prompt
    assert pydantic_backend.OVERRIDE_NOTE.strip() in pydantic_prompt


def test_the_override_names_the_tool_and_the_file_it_is_about(pydantic_prompt):
    """A correction that says "ignore parts of the text below" and names nothing is worse than no
    correction: it invites a model to discount whatever it finds inconvenient. This one is scoped to
    exactly two nouns, and says every other word applies unchanged."""
    note = pydantic_backend.OVERRIDE_NOTE
    assert "Write" in note
    assert agent_module.OUTCOME_FILENAME in note
    assert "unchanged" in note


def test_the_override_is_read_immediately_before_the_brief_it_corrects(pydantic_prompt):
    """Position is the contract. A reader — human or model — must meet the correction BEFORE the
    text being corrected, so the note sits last in the preamble, after the shared points and
    directly above the skill separator. Anywhere earlier and it is a claim the brief then appears to
    refute at length."""
    note_at = pydantic_prompt.index(pydantic_backend.OVERRIDE_NOTE.strip())
    separator_at = pydantic_prompt.index(agent_module.MEETING_SKILL_SEPARATOR.split("{")[0])
    brief_body_at = pydantic_prompt.index("## What you return")

    assert note_at < separator_at < brief_body_at
    # ...and nothing of the shared frame comes after it
    assert pydantic_prompt.index(agent_module.MEETING_SYSTEM_PROMPT_BODY) < note_at


# ── the header is the BACKEND's fact, and it is required ───────────────────────────────────────
def test_the_header_is_REQUIRED_and_no_backends_preamble_can_be_inherited_by_default(brief_text):
    """**The parameter change the retirement forced, on the flow where it would have hurt most.**
    `header` used to have a default, and that default was the tool-holding backend's own preamble —
    the paragraph telling a distiller it holds exactly one `Write` tool. Inherited by a run that
    holds NO tool, in a flow whose brief already claims a `Write` tool loudly enough to need
    `OVERRIDE_NOTE`, it would have made the correction directly above it false.

    A `TypeError` at composition time is the loud version of that mistake, which is the whole point
    of removing the default rather than fixing it.
    """
    with pytest.raises(TypeError):
        agent_module.build_meeting_system_prompt(brief_text)

    assert agent_module.build_meeting_system_prompt(
        brief_text, header=pydantic_backend.MEETING_SYSTEM_PROMPT_HEADER)


def test_the_brief_arrives_with_its_frontmatter_stripped_and_its_body_whole(pydantic_prompt,
                                                                            brief_text):
    """One strip, one place — and on THIS flow it is load-bearing rather than tidy: the meeting
    brief's frontmatter carries `allowed-tools: Write`, so a composition that stopped stripping it
    would inject a tool declaration into the instructions of a run that holds no tool, a few lines
    above the override note saying exactly the opposite.

    The body must arrive whole for the other half of the same reason: it is the procedure, and the
    override note is written to correct two nouns in it, not to stand in for a missing paragraph.
    """
    assert "allowed-tools: Write" in brief_text, (
        "the fixture brief no longer declares a tool in its frontmatter; this test proves nothing")

    assert "allowed-tools" not in pydantic_prompt
    assert "name: meeting-distiller" not in pydantic_prompt
    assert brief_text.split("---", 2)[2].strip() in pydantic_prompt
    assert pydantic_prompt.endswith("\n")


# ── the substitution, and the reason it is `replace` ───────────────────────────────────────────
def test_a_header_containing_braces_composes_instead_of_raising(brief_text):
    """**`replace`, not `format`, and this is the failure that forced it.** `header` is a parameter
    now, so `str.format` would scan caller-supplied text for braces and raise on any that are not
    `{relpath}` — a preamble carrying a JSON example, a set literal or a schema fragment would take
    the run down at the last moment before the model call, having already built everything.

    A backend whose whole answer is a JSON-shaped object is exactly the caller likely to put braces
    in its own preamble, which is why this is a real risk rather than a hypothetical one.
    """
    braced = agent_module.build_meeting_header(
        'Your environment:\n\n1. Return {"decision": "file"} — a set is {1, 2} and this is fine.\n')

    composed = agent_module.build_meeting_system_prompt(brief_text, header=braced)

    assert '{"decision": "file"}' in composed
    assert "{1, 2}" in composed
    assert agent_module.MEETING_BRIEF_RELPATH in composed
    assert "{relpath}" not in composed


# ── RETIRED with the `sdk` backend ─────────────────────────────────────────────────────────────
# The `sdk_prompt` fixture and six tests went with it. **Five of the six were genuinely COMPARISONS
# between two composed preambles** — that they shared the opening, the body and the separator; that
# the environment paragraph was the only thing they disagreed about; that neither told a backend
# about a capability it did not hold. With one backend there is nothing to compare, and a comparison
# of a thing with itself is the tautology this repo treats as worse than no test:
#
#   `test_the_sdk_prompt_carries_no_override_because_it_contradicts_nothing`
#   `test_both_backends_share_the_opening_the_body_and_the_separator`
#   `test_the_environment_paragraph_is_the_only_thing_the_two_headers_disagree_about`
#   `test_the_two_environment_paragraphs_describe_genuinely_different_environments`
#   `test_neither_prompt_tells_a_backend_about_a_capability_it_does_not_have`
#
# **The sixth was not a comparison and this note first claimed it was — the correction matters more
# than the line it replaces.** `test_the_brief_path_is_substituted_and_no_placeholder_survives` and
# `test_the_brief_arrives_with_its_frontmatter_stripped_and_its_body_whole` each merely LOOPED over
# both prompts; the property was per-prompt, so losing one backend removed an iteration and not the
# claim. Both were deleted whole, which was wrong. Their current state:
#
#   * `..._frontmatter_stripped_and_its_body_whole` — RESTORED, above this line, on the surviving
#     backend, where it is load-bearing rather than tidy: the brief's frontmatter declares a tool.
#   * `..._brief_path_is_substituted_and_no_placeholder_survives` — deliberately NOT restored. Its
#     two assertions are already the last two of
#     `test_a_header_containing_braces_composes_instead_of_raising` below (`MEETING_BRIEF_RELPATH`
#     present, `{relpath}` absent), on a header built for that test. A second, narrower copy of a
#     covered property is the kind of duplicate that goes stale first.
#
# The lesson, since this file is where somebody will look for it: **"it took both fixtures" is not
# the same as "it compared them".** A loop over two subjects loses an iteration when one retires; a
# comparison loses its subject. Only the second kind dies with the backend.
#
# **The mechanism they guarded is untouched and still has a variation point.**
# `agent.build_meeting_header(environment, *, override_note="")` still composes the shared frame
# around ONE per-backend paragraph, `build_meeting_system_prompt`'s `header` is now REQUIRED rather
# than defaulted, and `pydantic_backend.OVERRIDE_NOTE` still corrects the frozen brief — which is
# what the tests above this line still assert, on the one backend that exists.
