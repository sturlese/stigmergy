"""The ORDINARY system prompt, composed — and the claim ADR 033 makes about the `sdk` path being
untouched, checked against the code that path actually had before the milestone.

`test_meeting_prompt_composition.py`'s twin, one entry point over, with one addition that file does
not need: **the extraction that produced these constants is claimed to be byte-preserving**, and a
byte-preserving refactor is exactly the kind of claim that is asserted in a commit message and
never checked. So the pre-ADR-033 strings are read out of GIT — the real previous source, not a
copy pasted into this file, which would only prove this file agrees with itself.

What is at stake if the claim is false: the `sdk` ordinary path is the flow that has actually filed
this brain's pages, and M3's retire-or-keep decision is a comparison of the two shapes ON THE SAME
MODEL. A preamble that drifted by a sentence during the extraction would move the SDK arm of that
comparison and nothing would say so — the golden would simply report a different number and the
decision would read it as the structured flow being better or worse than it is.

Three properties, in the order they matter:

* `build_filing_header(ORDINARY_SDK_ENVIRONMENT)` reproduces the OLD `SYSTEM_PROMPT_HEADER` byte
  for byte — the extraction is a refactor;
* the SHIPPED header adds exactly one thing to it, the named override note, and nothing else;
* `build_prompt` with no `gathered_block` and no `outcome_channel` produces the OLD per-item
  prompt byte for byte, over every combination of its optional arguments.

Keyless, repo-free apart from `git show` against this checkout's own history, and it skips loudly
rather than failing when that history is unavailable (a source tarball, a shallow clone) — a check
that cannot run must say which check it is.
"""
import ast
import json
import pathlib
import subprocess

import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import pydantic_backend

ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIEF = (pathlib.Path(__file__).parent / "fixtures" / "repo" / ".claude" / "skills" / "librarian"
         / "SKILL.md")

# The module the milestone changed, and the ref that predates it. `HEAD` rather than a pinned sha:
# this file is checked out with the working tree that carries the M2 diff, so `HEAD` IS "before
# this change" for as long as the change is unlanded — and once it lands, the assertion has to be
# re-anchored deliberately rather than silently comparing a commit with itself.
_AGENT_RELPATH = "src/stigmergy/librarian/agent.py"
_BEFORE_REF = "HEAD"


def _source_before() -> str:
    """`agent.py` as it was before this milestone, from the object database.

    Skips rather than fails when git cannot answer: a check that cannot run must say so in its own
    words, because "skipped" and "passed" look the same in a summary line and this is the only
    check standing between a claimed refactor and an unnoticed prompt change.
    """
    try:
        result = subprocess.run(["git", "show", f"{_BEFORE_REF}:{_AGENT_RELPATH}"],
                                cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as ex:      # pragma: no cover — no git at all
        pytest.skip(f"git is unavailable, so the pre-ADR-033 prompt cannot be extracted: {ex}")
    if result.returncode != 0:                                # pragma: no cover — shallow/tarball
        pytest.skip(f"`git show {_BEFORE_REF}:{_AGENT_RELPATH}` failed, so the pre-ADR-033 prompt "
                    f"cannot be extracted (a shallow clone or a source tarball): "
                    f"{result.stderr.strip()}")
    return result.stdout


def _module_before() -> ast.Module:
    return ast.parse(_source_before())


def _string_constant(tree: ast.Module, name: str) -> str:
    """One module-level string constant out of the parsed OLD source.

    Read through `ast` rather than by importing the old module: importing a second copy of
    `stigmergy.librarian.agent` under any name would run its imports, register a second set of
    classes and give two `AgentRun` types in one process — a cure considerably worse than the
    disease. Implicit string concatenation folds into a single `Constant` node, so the value here
    is exactly the bytes that module defined.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets):
            value = ast.literal_eval(node.value)
            assert isinstance(value, str), f"{name} was not a string constant before the change"
            return value
    raise AssertionError(f"{name} is not a module-level constant in {_AGENT_RELPATH} at "
                         f"{_BEFORE_REF} — re-anchor this test deliberately")


def _function_before(tree: ast.Module, source: str, name: str):
    """One module-level function out of the OLD source, compiled in a namespace carrying exactly
    the module globals it reads.

    The namespace is spelled out rather than handed the live module's `__dict__`: `fence`,
    `json` and `OUTCOME_FILENAME` are the only three globals the old `build_prompt` touches, and
    naming them is what makes "the old function, run for real" a true statement instead of "the
    old function, run against whatever the new module happens to expose".
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            namespace = {"json": json, "fence": agent_module.fence,
                         "OUTCOME_FILENAME": agent_module.OUTCOME_FILENAME}
            exec(ast.get_source_segment(source, node), namespace)   # noqa: S102 — our own history
            return namespace[name]
    raise AssertionError(f"{name} is not a module-level function in {_AGENT_RELPATH} at "
                         f"{_BEFORE_REF}")


@pytest.fixture(scope="module")
def before():
    source = _source_before()
    return source, ast.parse(source)


@pytest.fixture(scope="module")
def brief_text() -> str:
    return BRIEF.read_text(encoding="utf-8")


# ── the extraction was a refactor ──────────────────────────────────────────────────────────────
def test_the_sdk_header_composes_back_to_the_exact_string_it_was_before(before):
    """**The byte-preserving claim, checked against git rather than against a comment.**

    ADR 033 broke one string into four — an opening, a per-backend environment paragraph, a shared
    point and a separator — so a second backend could vary exactly one of them. Composition is only
    a refactor if it composes back; if it does not, the SDK arm of the M3 comparison has quietly
    moved and every filing this brain has done since would be judged against a different preamble.
    """
    _, tree = before

    assert agent_module.build_filing_header(agent_module.ORDINARY_SDK_ENVIRONMENT) == (
        _string_constant(tree, "SYSTEM_PROMPT_HEADER")), (
        "build_filing_header(ORDINARY_SDK_ENVIRONMENT) no longer reproduces the pre-ADR-033 "
        "SYSTEM_PROMPT_HEADER — the extraction stopped being byte-preserving")


def test_the_shipped_header_adds_the_override_note_and_nothing_else(before):
    """The ONE delta in what the SDK backend actually sends, isolated as a delta rather than
    described as one: the shipped header minus the override note is the old header exactly. A
    second sentence smuggled into the preamble alongside the note would pass a "the note is
    present" check and fail this one."""
    _, tree = before
    old = _string_constant(tree, "SYSTEM_PROMPT_HEADER")
    note = agent_module.ORDINARY_SDK_OVERRIDE_NOTE

    shipped = agent_module.SYSTEM_PROMPT_HEADER

    assert note in shipped
    assert shipped.replace(note + "\n", "") == old, (
        "the shipped SDK header differs from the pre-ADR-033 one by more than the override note")


def test_the_override_note_sits_immediately_before_the_brief_it_corrects():
    """Position is the contract, and it is the same argument the meeting flow's override makes: a
    reader — human or model — meets the correction BEFORE the text being corrected. Anywhere
    earlier and the brief appears to refute it at length."""
    header = agent_module.SYSTEM_PROMPT_HEADER
    note_at = header.index(agent_module.ORDINARY_SDK_OVERRIDE_NOTE)
    separator_at = header.index(agent_module.ORDINARY_SKILL_SEPARATOR.split("{")[0])

    assert note_at < separator_at
    assert header.index(agent_module.ORDINARY_SYSTEM_PROMPT_BODY) < note_at


def test_the_override_names_every_mechanic_it_is_overriding():
    """A correction that says "parts of the text below do not apply" and names nothing invites a
    model to discount whatever it finds inconvenient. This one names the three mechanics the SDK
    run genuinely differs on — the handed context, the tools it holds, the field it declares — and
    says every judgment is unchanged.

    The direction is the milestone (ADR 033 D4): the brief is now written for the STRUCTURED flow
    and the SDK backend carries the correction, which is the inverse of ADR 032's arrangement.
    """
    note = agent_module.ORDINARY_SDK_OVERRIDE_NOTE

    assert "Glob" in note and "Read" in note and "Grep" in note
    assert "`page_path`" in note and "`page`" in note
    assert "ops/templates/" in note, (
        "a tool-less run is told the templates are summarised in the brief; the SDK run must be "
        "sent to read them, or it drafts frontmatter from a summary when it could read the source")
    assert "unchanged" in note


def test_the_structured_backend_carries_no_override_because_it_contradicts_nothing():
    """The specificity half. The brief describes the structured run's own environment, so a note
    telling that backend to read parts of it as "shape only" would be the same class of defect in
    the other direction — a correction that makes a true instruction sound optional."""
    structured = pydantic_backend.ORDINARY_SYSTEM_PROMPT_HEADER

    assert agent_module.ORDINARY_SDK_OVERRIDE_NOTE not in structured
    assert "One override" not in structured
    assert structured == agent_module.build_filing_header(pydantic_backend.ORDINARY_ENVIRONMENT)


# ── one frame, one variation point ─────────────────────────────────────────────────────────────
def test_both_ordinary_backends_share_the_opening_the_body_and_the_separator(brief_text):
    """The identity that makes drift impossible rather than merely unlikely. These paragraphs are
    facts about the WORKER — where the skill came from, that nothing in the repo configures the
    agent — and they are true of every backend, so they are written once and asserted present in
    both composed prompts."""
    sdk = agent_module.build_system_prompt(brief_text)
    structured = agent_module.build_system_prompt(
        brief_text, header=pydantic_backend.ORDINARY_SYSTEM_PROMPT_HEADER)

    for shared in (agent_module.ORDINARY_SYSTEM_PROMPT_OPENING,
                   agent_module.ORDINARY_SYSTEM_PROMPT_BODY,
                   agent_module.ORDINARY_SKILL_SEPARATOR):
        rendered = shared.replace("{relpath}", agent_module.SKILL_RELPATH)
        assert rendered in sdk, f"the SDK prompt lost a shared paragraph: {rendered[:60]!r}"
        assert rendered in structured, (
            f"the structured prompt lost a shared paragraph: {rendered[:60]!r}")


def test_the_two_environment_paragraphs_describe_genuinely_different_environments():
    """The variation point earns its existence: one backend holds five tools and explores, the
    other holds none and is handed its context. If these two ever converge, the parameter should go
    and the header should be one string again."""
    assert "exactly these tools: Read, Glob, Grep, Write, Edit" in (
        agent_module.ORDINARY_SDK_ENVIRONMENT)
    assert "NO tools" in pydantic_backend.ORDINARY_ENVIRONMENT
    assert agent_module.ORDINARY_SDK_ENVIRONMENT != pydantic_backend.ORDINARY_ENVIRONMENT


def test_neither_preamble_tells_a_backend_about_a_capability_it_does_not_have(brief_text):
    """The defect the whole composition exists to prevent, stated as the property. Asserted over
    the PREAMBLE only — the words still appear further down, because that is the brief, and the
    override is what scopes them."""
    structured = agent_module.build_system_prompt(
        brief_text, header=pydantic_backend.ORDINARY_SYSTEM_PROMPT_HEADER)
    separator = agent_module.ORDINARY_SKILL_SEPARATOR.split("{")[0]

    structured_preamble = structured.split(separator)[0]
    assert "NO tools" in structured_preamble
    assert "Read, Glob, Grep, Write, Edit" not in structured_preamble

    sdk_preamble = agent_module.build_system_prompt(brief_text).split(separator)[0]
    assert "Read, Glob, Grep, Write, Edit" in sdk_preamble


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


def test_the_brief_arrives_with_its_frontmatter_stripped_and_its_body_whole(brief_text):
    """One strip, one place, both backends. The skill's frontmatter is metadata for a loader this
    platform deliberately does not use; the body below it is the procedure and must arrive
    intact."""
    sdk = agent_module.build_system_prompt(brief_text)

    assert "name: librarian" not in sdk
    assert brief_text.split("---", 2)[2].strip()[:80] in sdk
    assert sdk.endswith("\n")


# ── the per-item prompt: byte-identical for the backend that did not change ────────────────────
_PROMPT_CASES = [
    ("plain", {}),
    ("with hints", {"hints": {"title": "Renewal", "source_participants": "Ada"}}),
    ("with a reply", {"reply": "It is about Acme Corp."}),
    ("with a corrective brief", {"corrective": "Repair this: the page_type was not creatable."}),
    ("with a flow note", {"flow_note": "The verbatim source page is code's; yours is the "
                                       "synthesis."}),
    ("everything at once", {"hints": {"title": "Renewal"}, "reply": "Acme Corp.",
                            "corrective": "Repair this.", "flow_note": "Attachment note."}),
]


@pytest.mark.parametrize("label, extra", _PROMPT_CASES, ids=[c[0] for c in _PROMPT_CASES])
def test_an_sdk_item_prompt_is_byte_identical_to_the_one_this_flow_produced_before(before, label,
                                                                                   extra):
    """**The claim that `gathered_block` and `outcome_channel` are CALLER-DECLARED facts defaulting
    to what this function always produced**, checked by running the old function.

    Not "the new prompt still contains the old sentences" — a containment check passes when a
    paragraph is added, and an added paragraph is exactly the drift that would move the SDK arm of
    the M3 comparison. The old `build_prompt` is compiled out of the old source and called with the
    same arguments, and the two strings are compared whole.

    Every optional argument is exercised, because each one is a branch: a default that changed for
    `reply` alone would be invisible on the plain case, which is the case everybody eyeballs.
    """
    source, tree = before
    old_build_prompt = _function_before(tree, source, "build_prompt")
    call = {"material": "A renewal note about Acme Corp.\nSecond line.", "hints": {},
            "submitted_by": "tester@stigmergy.test", **extra}

    assert agent_module.build_prompt(**call) == old_build_prompt(**call), (
        f"the {label} SDK prompt changed — `gathered_block`/`outcome_channel` are supposed to "
        f"default to exactly what this function produced before ADR 033")


def test_a_structured_prompt_differs_from_the_sdk_one_in_exactly_two_places():
    """The other side of the same defaulting rule, and the reason `build_structured_prompt` is a
    thin wrapper rather than a second builder: every fence and hint mechanic is a property of the
    ITEM, not of the backend, so the structured prompt must be the same prompt plus a gathered
    block and minus the file-channel sentence.

    A forked builder is how one of those mechanics silently stops holding on one path — which is
    why this asserts the DIFFERENCE is bounded rather than asserting the structured prompt looks
    right on its own.
    """
    call = dict(material="A renewal note about Acme Corp.", hints={"title": "Renewal"},
                submitted_by="tester@stigmergy.test")
    sdk = agent_module.build_prompt(**call)

    structured = agent_module.build_structured_prompt(
        **call, gathered_block="\nGATHERED CONTEXT BLOCK",
        outcome_channel=pydantic_backend.ORDINARY_OUTCOME_CHANNEL)

    assert agent_module.OUTCOME_CHANNEL_FILE in sdk
    assert agent_module.OUTCOME_CHANNEL_FILE not in structured, (
        "the structured prompt still tells a tool-less agent to write a file")
    assert pydantic_backend.ORDINARY_OUTCOME_CHANNEL in structured
    # ...and putting the two differences back reproduces the SDK prompt exactly
    rebuilt = structured.replace("\nGATHERED CONTEXT BLOCK\n", "").replace(
        pydantic_backend.ORDINARY_OUTCOME_CHANNEL, agent_module.OUTCOME_CHANNEL_FILE)
    assert rebuilt == sdk


def test_the_gathered_block_sits_above_the_material_it_is_context_for():
    """A reader meets its context before the thing the context is for — the same position
    `build_meeting_prompt` gives the registry and the source page's path. Below the material it
    would read as commentary on a document already read, and a model that had already decided
    placement would have nothing left to use it for."""
    prompt = agent_module.build_structured_prompt(
        material="A renewal note.", hints={}, submitted_by="a@b.test",
        gathered_block="\nGATHERED CONTEXT BLOCK",
        outcome_channel=pydantic_backend.ORDINARY_OUTCOME_CHANNEL)

    assert prompt.index("GATHERED CONTEXT BLOCK") < prompt.index("The captured material follows")


def test_the_reply_still_sits_below_the_material_on_the_structured_path():
    """The one ordering rule in this prompt that is a security property rather than a readability
    one: the submitter's reply is the newest attacker-reachable text in the system, and placing it
    beside the corrective brief — the one genuinely instruction-shaped thing here, written by code
    — would let it borrow that authority. It is a property of the ITEM, so it holds on both
    shapes."""
    prompt = agent_module.build_structured_prompt(
        material="A renewal note.", hints={}, submitted_by="a@b.test",
        gathered_block="\nGATHERED CONTEXT BLOCK", corrective="Repair this.",
        reply="It is about Acme Corp.",
        outcome_channel=pydantic_backend.ORDINARY_OUTCOME_CHANNEL)

    assert prompt.index("The captured material follows") < prompt.index("submitter's reply")
    assert prompt.index("submitter's reply") < prompt.index("Repair this.")


def test_the_structured_outcome_channel_tells_the_agent_it_holds_no_file_tool():
    """The channel sentence is the one line of the per-item prompt that differs between the two,
    and it has to be positively true of the run it goes into: this backend returns a typed object
    and has no tool that could write a file. A channel sentence that merely omitted the file would
    leave the brief's own mention of it uncorrected in the per-item message too."""
    channel = pydantic_backend.ORDINARY_OUTCOME_CHANNEL

    assert "page.body" in channel
    assert "no tool" in channel
    assert agent_module.OUTCOME_FILENAME not in channel
