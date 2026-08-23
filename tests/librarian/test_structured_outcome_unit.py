"""The ordinary outcome envelope, which GREW a half — and the rule that both halves stay valid.

the structured filing flow is an expand–contract step, and this file is what keeps the "expand" honest. One parser
(`agent.parse_outcome`) accepts two shapes: the OLD one, where the agent wrote the page itself and
declared `page_path`, and the NEW one, where the account CARRIES the page's own text in `page` for
code to write. Which half is REQUIRED is deliberately not the schema's question — the schema cannot
know which backend ran — so `processing._require_page_content` asks it, keyed on the backend's own
declaration.

**Why the additive claim needs a test rather than a reading.** Both shapes are still produced —
the offline double declares `page_path` and writes its own page, the structured backend carries the
page's text in `page` — and the old shape is what every page this brain filed before the structured filing flow was
filed by. A parser that quietly started requiring `page`, or that let `page.title` override a
top-level one, would not fail loudly on either path: the exploring path would begin failing every
item with a shape finding (visible, at least), and the precedence bug would file pages whose
FILENAME and whose commit subject name two different things — silently, forever, in somebody's
repo.

The one bound that behaves differently from its neighbours is `page.body`: REFUSED over
`MAX_PAGE_BODY_LEN`, never truncated. Prose truncates because nothing downstream re-reads it; a
page body IS the product, and a clipped one is a page that ends mid-sentence in the repo forever
with the only evidence in a log line. Its TWIN is the meeting flow, whose page bodies still
truncate — an asymmetry that is declared rather than accidental, so both sides are pinned here and
a later convergence has to be a decision.

Keyless and pure: `parse_outcome` and `_require_page_content` take plain data and return plain data.
"""
import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import processing
from stigmergy.librarian.errors import AgentError, OutcomeShapeError

# The two shapes, as the two backends actually emit them. Spelled as whole dicts rather than built
# by a helper with switches: the point of this file is that the two are DIFFERENT documents, and a
# builder that produced both from one template would encode the very merge this file exists to
# check.
OLD_SHAPE = {
    "decision": "file",
    "title": "Acme Corp Renewal Window",
    "page_path": "wiki/notes/Acme Corp Renewal Window.md",
    "page_type": "note",
    "summary": "filed the renewal note",
    "anchoring": {"kind": "entity", "entities": ["Acme Corp"]},
    "links_created": ["Acme Corp"],
}

NEW_SHAPE = {
    "decision": "file",
    "page": {"title": "Acme Corp Renewal Window", "page_type": "note",
             "body": "## Context\n\nThe renewal window was confirmed."},
    "summary": "filed the renewal note",
    "anchoring": {"kind": "entity", "entities": ["Acme Corp"]},
    "links_created": ["Acme Corp"],
}


def _codes(exc_info) -> set:
    return {(f.gate, f.code) for f in exc_info.value.findings}


def _messages(exc_info) -> str:
    return "\n".join(f.message for f in exc_info.value.findings)


# ── the OLD shape still parses, unchanged ──────────────────────────────────────────────────────
def test_the_exploring_backends_account_parses_exactly_as_it_did_before(before=None):
    """The expand half's whole promise. `page` absent is `page=None`, and every field the old shape
    declared arrives where every downstream reader already looks for it — `_commit_message`,
    `_stamp`, `gate_zone` and `_cross_check_outcome` were not taught about a second declaration
    site, which is the property that keeps the two shapes from becoming two flows."""
    outcome = agent_module.parse_outcome(OLD_SHAPE)

    assert outcome.page is None
    assert outcome.title == "Acme Corp Renewal Window"
    assert outcome.page_path == "wiki/notes/Acme Corp Renewal Window.md"
    assert outcome.page_type == "note"
    assert outcome.links_created == ("Acme Corp",)


def test_the_old_shape_needs_no_page_body_and_is_not_asked_for_one():
    """`page.body` is required by the CALLER, not by the parser, and only of a backend that
    declared the structured shape. A parser that required it would refuse every filing made in the
    exploring shape — which is what the offline double produces on every run of this suite, and what
    every page filed before the structured filing flow was filed by."""
    outcome = agent_module.parse_outcome(OLD_SHAPE)

    assert processing._require_page_content(outcome) != []
    # ...and that is the CALLER's answer for a structured backend, never something `parse_outcome`
    # raised: the same account parsed cleanly a line above.
    assert outcome.decision == "file"


# ── the NEW shape, and how its fields reach the single readers ─────────────────────────────────
def test_the_structured_account_mirrors_its_title_and_type_up_to_the_single_fields():
    """`title` and `page_type` stay SINGLE fields whichever half declared them. Everything
    downstream reads one field, so the reconciliation happens once, at the boundary — teaching
    `_commit_message`, `_stamp`, `gate_zone` and `_cross_check_outcome` about both sites would be
    four places that can come to disagree about which is authoritative."""
    outcome = agent_module.parse_outcome(NEW_SHAPE)

    assert outcome.page is not None
    assert outcome.title == "Acme Corp Renewal Window"
    assert outcome.page_type == "note"
    assert outcome.page.title == "Acme Corp Renewal Window"
    assert outcome.page.body.startswith("## Context")


def test_there_is_no_path_in_the_new_half_and_a_structured_account_declares_none():
    """**The confinement claim, at the schema.** the structured filing flow's "there is no write to stop" rests on
    the account having no field that could name a location: the folder is derived from `page_type`
    through the one placement table and the filename from the title. `page_path` stays empty for a
    structured account, which is what `_cross_check_outcome` then compares the DIFF against."""
    outcome = agent_module.parse_outcome(NEW_SHAPE)

    assert outcome.page_path == ""
    assert not hasattr(outcome.page, "page_path")
    assert not hasattr(outcome.page, "path")
    assert not hasattr(outcome.page, "folder")


def test_the_top_level_wins_when_both_halves_declare_a_title_or_a_type():
    """**Strictly additive, and the direction matters.** The sub-object FILLS IN what the top level
    left silent and never overrides it, so a new field can add information to an outcome and never
    change what the old shape already meant.

    The failure this prevents is not abstract: `_write_ordinary_page` derives the FILENAME from
    `outcome.title` and `_commit_message` derives the commit subject from the same field. If the
    two sites could disagree about which wins, an account declaring both would file a page whose
    name and whose `git log` entry are two different strings.
    """
    both = {**NEW_SHAPE, "title": "The Top Level Title", "page_type": "decision"}

    outcome = agent_module.parse_outcome(both)

    assert outcome.title == "The Top Level Title"
    assert outcome.page_type == "decision"
    assert outcome.page.title == "Acme Corp Renewal Window", (
        "the sub-object's own value must survive verbatim — it is evidence about what the agent "
        "said, and only the RESOLVED field is the one readers use")


def test_a_filing_with_a_title_only_in_the_page_half_is_not_reported_as_titleless():
    """The presence check reads EITHER declaration site. Asked of the raw values, so a title that
    failed its own bound earns one finding rather than two — the corrective brief gets exactly one
    pass to be right, and two findings for one defect is how it gets crowded."""
    agent_module.parse_outcome(NEW_SHAPE)          # must not raise

    with pytest.raises(OutcomeShapeError) as exc_info:
        agent_module.parse_outcome({"decision": "file",
                                    "page": {"page_type": "note", "body": "text"}})

    assert ("outcome", "missing-field") in _codes(exc_info)
    assert "`title`" in _messages(exc_info)


# ── `page.body`: REFUSED over the ceiling, never truncated ─────────────────────────────────────
def test_a_body_over_the_ceiling_is_refused_correctably_rather_than_clipped():
    """**The one bound in this boundary that does not truncate, and the reason is the product.**

    A clipped page body would commit a page whose last section stops mid-sentence, pass every gate
    (a truncated page is still well-formed) and stay in the knowledge repo permanently, with the
    only evidence of the mutilation in a log line. So it refuses — and refuses CORRECTABLY, as an
    `OutcomeShapeError` carrying findings, because the agent's one corrective pass can actually
    perform this repair.
    """
    over = {**NEW_SHAPE, "page": {**NEW_SHAPE["page"],
                                  "body": "x" * (agent_module.MAX_PAGE_BODY_LEN + 1)}}

    with pytest.raises(OutcomeShapeError) as exc_info:
        agent_module.parse_outcome(over)

    assert ("outcome", "too-long") in _codes(exc_info)
    message = _messages(exc_info)
    assert "page.body" in message
    assert "REFUSED rather than" in message, (
        "the finding must say it refused rather than shortened — an agent told only 'too long' "
        "repeats itself, which is how both attempts get spent on one defect")
    assert "file the part worth keeping" in message, "the finding names no repair the agent can do"


def test_a_body_exactly_at_the_ceiling_is_accepted(tmp_path=None):
    """The boundary's benign twin. A bound that refused its own limit would make the number in the
    finding a lie, and an agent that shortened a page to exactly the stated ceiling would be
    refused a second time with the same sentence — the one thing a single corrective pass cannot
    survive."""
    exact = {**NEW_SHAPE, "page": {**NEW_SHAPE["page"],
                                   "body": "y" * agent_module.MAX_PAGE_BODY_LEN}}

    outcome = agent_module.parse_outcome(exact)

    assert len(outcome.page.body) == agent_module.MAX_PAGE_BODY_LEN


def test_the_meeting_flows_page_bodies_still_truncate_and_that_asymmetry_is_deliberate():
    """**The twin that makes the asymmetry a decision instead of an accident.**

    The meeting flow's own page bodies pass `MAX_PAGE_BODY_LEN` through `_prose`, which TRUNCATES.
    Changing that would be a behaviour change to a shipped flow with no measurement behind it, so
    the structured filing flow declared the difference rather than unifying it. Pinned on both sides here: if somebody
    later unifies them, this test is the thing that makes it a deliberate act.
    """
    long_notes = "z" * (agent_module.MAX_PAGE_BODY_LEN + 500)

    meeting = agent_module.parse_meeting_outcome({
        "decision": "file", "meeting_title": "Q3 sync", "meeting_notes": long_notes,
        "decisions": [], "summary": "distilled"})

    assert len(meeting.meeting_notes) == agent_module.MAX_PAGE_BODY_LEN, (
        "the meeting flow's notes stopped truncating — if that was deliberate, the structured filing flow's declared "
        "asymmetry has changed and this test moves with it")


def test_a_container_where_the_body_belongs_is_a_wrong_type_rather_than_a_stringified_dict():
    """`str({'a': 1})` on a page body is a bug wearing a page's clothes — it would file, pass the
    gates, and read as prose nobody wrote. Refused at the boundary like every other container in a
    scalar's place."""
    with pytest.raises(OutcomeShapeError) as exc_info:
        agent_module.parse_outcome({**NEW_SHAPE,
                                    "page": {**NEW_SHAPE["page"], "body": {"a": 1}}})

    assert ("outcome", "wrong-type") in _codes(exc_info)
    assert "page.body" in _messages(exc_info)


# ── `page` that is not a page ──────────────────────────────────────────────────────────────────
def test_a_page_that_is_not_a_mapping_is_a_shape_finding_and_not_a_crash():
    """A model that answered `"page": "the whole page as a string"` must earn a correctable finding,
    not an `AttributeError` three functions downstream. `_mapping` refuses it at the boundary, which
    is where every other wrong type is refused."""
    with pytest.raises(OutcomeShapeError) as exc_info:
        agent_module.parse_outcome({**NEW_SHAPE, "page": "the whole page as a string"})

    assert ("outcome", "wrong-type") in _codes(exc_info)
    assert "page" in _messages(exc_info)


def test_a_page_declared_as_null_is_simply_the_old_shape():
    """`null` is how a structured schema with a defaulted sub-object says "I am not using this
    half", and it must be indistinguishable from omitting it — otherwise a backend whose framework
    serializes defaults would take a different road from one that omits them."""
    assert agent_module.parse_outcome({**OLD_SHAPE, "page": None}).page is None


def test_a_page_on_a_TRIAGE_decision_is_refused_as_no_decision_at_all():
    """OLD BEHAVIOUR: a `triage` account carrying page content parsed, and the flow returned at
    the triage branch so the content stayed inert. There is no triage branch: the only decision is
    `file`, a name the registry lacks is PROPOSED, and an account still parking is a shape fault
    the corrective retry repairs."""
    parked = {"decision": "triage",
              "triage": {"kind": "unresolved-entity", "name": "Halcyon Grid"},
              "page": {"title": "Halcyon Grid Renewal", "page_type": "note", "body": "text"},
              "summary": "parked on an unregistered name"}

    with pytest.raises(OutcomeShapeError) as raised:
        agent_module.parse_outcome(parked)

    assert [f.code for f in raised.value.findings] == ["unknown-decision"]


# ── what the CALLER requires, keyed on the backend that ran ────────────────────────────────────
def test_the_structured_requirement_names_the_field_and_carries_a_repair_brief():
    """`_require_page_content` is the caller's question, and its answer is what the agent's one
    corrective pass reads. A finding that only said "invalid outcome" would spend that pass on a
    guess — so it names `page.body`, says the worker writes the page from the account, and shows
    the shape to return."""
    outcome = agent_module.parse_outcome({"decision": "file", "title": "A Title"})

    findings = processing._require_page_content(outcome)

    assert len(findings) == 1
    assert (findings[0].gate, findings[0].code) == ("outcome", "missing-field")
    assert "`page.body`" in findings[0].message
    assert findings[0].brief and "page_type" in findings[0].brief
    assert "you never name a path" in findings[0].brief, (
        "the repair brief must not invite the agent to declare a path — there is no field for one")


@pytest.mark.parametrize("body", ["", "   \n\t  "])
def test_a_blank_body_is_as_missing_as_no_body_at_all(body):
    """Whitespace is not content. A `page` object present with an empty body would otherwise pass
    the presence check and produce a page that is a frontmatter block and an H1 — which files, and
    which nobody can tell from a page whose content was lost."""
    outcome = agent_module.parse_outcome({**NEW_SHAPE,
                                          "page": {**NEW_SHAPE["page"], "body": body}})

    findings = processing._require_page_content(outcome)

    assert [(f.gate, f.code) for f in findings] == [("outcome", "missing-field")]


def test_a_well_formed_structured_account_is_required_of_nothing_further():
    """The benign twin, and the one that decides whether the whole structured path can file at all:
    a complete account returns `[]`, which is what lets `_one_pass` go on to write the page."""
    assert processing._require_page_content(agent_module.parse_outcome(NEW_SHAPE)) == []


def test_the_requirement_is_asked_of_the_OUTCOME_and_not_of_the_backend_class():
    """`_require_page_content` takes an outcome and nothing else — no settings, no agent, no
    `isinstance`. That is what makes the branch above it a DECLARATION (`structured_ordinary`)
    rather than a type test, which is the thing the structured filing flow explicitly refused: a fourth backend, or
    a double standing in for one, must be able to take the right branch by declaring the right
    thing."""
    import inspect

    parameters = list(inspect.signature(processing._require_page_content).parameters)

    assert parameters == ["outcome"]
    assert "isinstance" not in inspect.getsource(processing._require_page_content)


# ── the structural/shape split survives the new field ──────────────────────────────────────────
def test_a_page_nested_past_the_depth_ceiling_is_STRUCTURAL_and_not_a_correctable_shape():
    """The split the agent/gate split made and this milestone had to keep: a resource bound is not something a
    brief can talk an agent out of, so it raises a bare `AgentError` and never reaches the
    corrective retry. `page` is a new place to nest, and the ceiling has to still apply through
    it."""
    node = {}
    deep = {"decision": "file", "page": {"title": "T", "page_type": "note", "body": "b",
                                         "nest": node}}
    for _ in range(agent_module.MAX_OUTCOME_DEPTH + 2):
        node["nest"] = {}
        node = node["nest"]

    with pytest.raises(AgentError) as exc_info:
        agent_module.parse_outcome(deep)

    assert type(exc_info.value) is AgentError, (
        "a nesting ceiling routed to the corrective retry spends a second pass on the same answer")


def test_every_problem_in_one_account_is_collected_rather_than_raised_one_at_a_time():
    """There is exactly ONE corrective pass. A parse that reported the first problem and stopped
    would spend it on a fraction of the fixes — and the new half is a new source of problems that
    has to join the same collection rather than short-circuit it."""
    broken = {"decision": "publish",                      # unknown decision
              "page": {"title": "T", "page_type": "note",
                       "body": "x" * (agent_module.MAX_PAGE_BODY_LEN + 1)},   # over the ceiling
              "edits": [{"kind": "rewrite", "path": "wiki/notes/X.md"}]}      # unknown edit kind

    with pytest.raises(OutcomeShapeError) as exc_info:
        agent_module.parse_outcome(broken)

    assert {"unknown-decision", "too-long", "unknown-edit-kind"} <= {
        f.code for f in exc_info.value.findings}
