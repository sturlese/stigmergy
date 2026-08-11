"""The meeting system prompt, composed — one shared frame, one per-backend environment paragraph,
and the one place a backend contradicts the brief said out loud.

The brief is the KNOWLEDGE REPO's text, and this milestone changes not one word of it. That is the
right call and it has a consequence nobody can wish away: the brief still tells its reader, in its
own voice, that it holds a `Write` tool and returns its account by writing
`.librarian-outcome.json`. Inject that under a preamble saying "you have NO tools" and the model is
handed a flat contradiction with nothing to say which half is operative — and a model that resolves
it the other way describes writing a file it cannot write, which is noise on the exact measurement
M3's retire-or-keep decision reads.

So there is an override note, and this file is what keeps it honest in both directions:

* **it must still be needed** — asserted against the frozen fixture brief's OWN sentences, so the
  day the knowledge repo's brief drops the tool and the file, this test fails and says the override
  can retire rather than sitting there forever correcting a contradiction that no longer exists;
* **it must be the ONLY divergence** — the opening, the shared points and the separator are one
  string each, reused by both backends, so the two preambles cannot quietly start saying different
  things about a repo's own configuration rules. That was how they were written the first time (one
  copied whole into the other), and a composition test is the only version of that check which
  cannot go stale.

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
def sdk_prompt(brief_text) -> str:
    """What the SDK backend actually sends — the default header, unchanged."""
    return agent_module.build_meeting_system_prompt(brief_text)


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


def test_the_sdk_prompt_carries_no_override_because_it_contradicts_nothing(sdk_prompt):
    """The specificity half. The SDK backend really does hold `Write` and really does write the
    outcome file, so a note telling it to read those as "shape only" would be the same class of
    defect in the opposite direction — a correction that makes a true instruction sound optional."""
    assert pydantic_backend.OVERRIDE_NOTE.strip() not in sdk_prompt
    assert "One override" not in sdk_prompt


# ── one frame, one variation point ─────────────────────────────────────────────────────────────
def test_both_backends_share_the_opening_the_body_and_the_separator(sdk_prompt, pydantic_prompt):
    """**The identity that makes drift impossible rather than merely unlikely.** These three
    paragraphs are facts about the WORKER — who writes the pages, what configures the agent, where
    the skill came from — and they are true of every backend. Written once, asserted present in
    both composed prompts, so a change to one is a change to both by construction."""
    relpath = agent_module.MEETING_BRIEF_RELPATH
    for shared in (agent_module.MEETING_SYSTEM_PROMPT_OPENING,
                   agent_module.MEETING_SYSTEM_PROMPT_BODY,
                   agent_module.MEETING_SKILL_SEPARATOR):
        rendered = shared.replace("{relpath}", relpath)
        assert rendered in sdk_prompt, f"the SDK prompt lost a shared paragraph: {rendered[:60]!r}"
        assert rendered in pydantic_prompt, (
            f"the structured prompt lost a shared paragraph: {rendered[:60]!r}")


def test_the_environment_paragraph_is_the_only_thing_the_two_headers_disagree_about():
    """Composition, asserted as an equality rather than by reading the two results: each header IS
    `build_meeting_header(<its own environment>)`, so there is no third place a paragraph could be
    added to one and not the other."""
    sdk_composed = agent_module.build_meeting_header(agent_module.MEETING_SDK_ENVIRONMENT)
    structured_composed = agent_module.build_meeting_header(
        pydantic_backend.MEETING_ENVIRONMENT, override_note=pydantic_backend.OVERRIDE_NOTE)

    assert sdk_composed == agent_module.MEETING_SYSTEM_PROMPT_HEADER
    assert structured_composed == pydantic_backend.MEETING_SYSTEM_PROMPT_HEADER


def test_the_two_environment_paragraphs_describe_genuinely_different_environments():
    """The variation point earns its existence: one backend holds a tool and the other holds none,
    which is exactly the fact a preamble is for. If these two ever converge, the parameter should go
    and the header should be one string again."""
    assert "ONE tool, Write" in agent_module.MEETING_SDK_ENVIRONMENT
    assert "NO tools" in pydantic_backend.MEETING_ENVIRONMENT
    assert agent_module.MEETING_SDK_ENVIRONMENT != pydantic_backend.MEETING_ENVIRONMENT


def test_neither_prompt_tells_a_backend_about_a_capability_it_does_not_have(sdk_prompt,
                                                                            pydantic_prompt):
    """The defect this whole composition exists to prevent, stated as the property rather than as
    the mechanism: the structured backend's own preamble must never claim a `Write` tool. The word
    still appears further down — that is the brief, and the override is what scopes it — so the
    assertion is over the PREAMBLE, up to the separator."""
    preamble = pydantic_prompt.split(
        agent_module.MEETING_SKILL_SEPARATOR.split("{")[0])[0]
    assert "ONE tool" not in preamble
    assert "NO tools" in preamble

    sdk_preamble = sdk_prompt.split(agent_module.MEETING_SKILL_SEPARATOR.split("{")[0])[0]
    assert "ONE tool, Write" in sdk_preamble


# ── the substitution, and the reason it is `replace` ───────────────────────────────────────────
def test_the_brief_path_is_substituted_and_no_placeholder_survives(sdk_prompt, pydantic_prompt):
    """A `{relpath}` reaching a model is a prompt that names its own template. Both composed
    prompts carry the real path and neither carries the placeholder."""
    for prompt in (sdk_prompt, pydantic_prompt):
        assert agent_module.MEETING_BRIEF_RELPATH in prompt
        assert "{relpath}" not in prompt


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


def test_the_brief_arrives_with_its_frontmatter_stripped_and_its_body_whole(sdk_prompt,
                                                                            brief_text):
    """The skill's frontmatter is metadata for the knowledge repo's own tooling, not instructions —
    and the body below it is the procedure, which must arrive intact. One strip, one place, both
    backends."""
    assert "allowed-tools: Write" not in sdk_prompt      # the frontmatter line
    assert "## What you return" in sdk_prompt            # ...and the body it precedes
    assert sdk_prompt.endswith("\n")
