"""The structured backends' OUTPUT SCHEMAS: what the framework refuses before this package sees it.

**This file exists because a paid run measured the cost of getting it wrong.** Every field of both
accounts carried a default, `decision` included, on the reasoning that a provider omitting something
should produce an account the boundary can judge and refuse on its own terms rather than a
validation error inside the framework. That reasoning had the mechanism backwards, and the first
paid structured golden showed how: a default does not make an omission VISIBLE, it makes it
INVISIBLE. The framework's output validation accepted a half-empty account, so its own
`OUTPUT_RETRIES` never fired; `agent.parse_outcome` then refused downstream; and the WORKER's one
corrective retry — the expensive one, a whole second agent pass — was spent re-asking a model to
repair a shape a brief cannot reliably teach. Five of the golden's ten ordinary captures died that
way, two passes each.

So the schema demands what the boundary demands, and the two enforcement points are DECLARED
duplication:

* the SCHEMA is the cheap, early road. pydantic-ai hands a validator's `ValueError` back to the
  model as its retry prompt, so the completeness messages below are not diagnostics for an
  operator — **they are the repair instruction, and the only text that gets a chance to work**.
  That is why each one is asserted for what it NAMES rather than for its existence;
* the BOUNDARY (`agent.parse_*_outcome`) keeps every check regardless, because it also judges the
  FILE channel and because a typed provider response is not a trusted one.

**What is NOT restated here, and the distinction is the whole design.** Bounds belong to the
boundary — identifiers refused over `MAX_IDENTIFIER_LEN`, prose truncated, a page body refused,
lists capped — and a second set of limits in a schema would be a second answer to one question.
Requiredness is the opposite case. `test_pydantic_meeting_pg`'s own refused-shape tests are the
other side of this line: they drive an over-long identifier precisely because that is what the
schema declines to catch.

Keyless and pure apart from the two framework-driven cases at the bottom, which use pydantic-ai's
own offline models — a real `Agent.run`, real usage accounting, no key and no network.
"""
import dataclasses
import re

import pytest
from pydantic import ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import config, pydantic_backend
from stigmergy.librarian.errors import AgentError, OutcomeShapeError
from stigmergy.librarian.pydantic_backend import (
    FilingAccount,
    MeetingAccount,
    MeetingAnchoring,
    MeetingDecision,
    MeetingTriage,
    OrdinaryAnchoring,
    OrdinaryPage,
    OrdinaryTriage,
    PydanticFilingAgent,
)
from tests.librarian import support

PRICED_MODEL = "openai:gpt-5.6-terra"


def _page_body() -> str:
    return "## What the capture said\n\nThe renewal window was confirmed."


def _filing(**over) -> dict:
    """The keyword arguments of a COMPLETE ordinary filing account, so each case below overrides
    exactly the one field it is about and nothing else drifts."""
    base = dict(decision="file",
                page=OrdinaryPage(title="Acme Corp Renewal Window", page_type="note",
                                  body=_page_body()),
                anchoring=OrdinaryAnchoring(kind="entity", entities=["Acme Corp"]),
                summary="filed the renewal note")
    base.update(over)
    return base


def _meeting(**over) -> dict:
    base = dict(decision="file", meeting_title="Q3 sync",
                decisions=[MeetingDecision(
                    title="Q3 sync — the renewal scope", body="## Context\n\nAgreed.",
                    anchoring=MeetingAnchoring(kind="entity", entities=["Acme Corp"]))],
                summary="distilled one decision")
    base.update(over)
    return base


def _message(exc_info) -> str:
    return str(exc_info.value)


# ── LEG 1: `decision` is required and enum-constrained ─────────────────────────────────────────
@pytest.mark.parametrize("account", [FilingAccount, MeetingAccount],
                         ids=["ordinary", "meeting"])
def test_an_account_that_omits_its_decision_cannot_be_built_at_all(account):
    """**The exact shape the paid run died on.** `decision` used to default to `""`, so an account
    with no decision satisfied the schema, spent no framework retry, and was refused downstream
    with `unknown-decision` after the worker had already paid for the pass.

    It is required now, so the framework re-asks — which is free, immediate, and names the field.
    """
    with pytest.raises(ValidationError) as exc_info:
        account()

    assert "decision" in _message(exc_info)
    assert "Field required" in _message(exc_info), (
        "the omission was refused for some other reason — `decision` may have re-acquired a "
        "default and be failing the completeness validator instead")


@pytest.mark.parametrize("account, complete", [(FilingAccount, _filing), (MeetingAccount, _meeting)],
                         ids=["ordinary", "meeting"])
def test_a_decision_outside_the_vocabulary_is_refused_by_the_enum(account, complete):
    """`Literal[*agent.DECISIONS]`, derived rather than retyped — so the schema and
    `agent.parse_*_outcome`'s `DECISIONS` check cannot come to disagree about the vocabulary.

    `"publish"` is the case this suite used to drive through the schema on purpose, precisely
    because a plain string took it. It does not any more, and that is the milestone.
    """
    with pytest.raises(ValidationError) as exc_info:
        account(**{**complete(), "decision": "publish"})

    message = _message(exc_info)
    assert "decision" in message
    for known in agent_module.DECISIONS:
        assert known in message, (
            f"the enum refusal does not name {known!r} — a model told only 'invalid' has nothing "
            f"to repair towards, and this text IS its retry prompt")


def test_the_schemas_decision_vocabulary_is_the_boundarys_own():
    """One vocabulary, two enforcement points, derived from one tuple. A schema that hardcoded
    `Literal["file", "triage"]` would be a second list to keep in step, and the failure mode is
    silent: a third decision added to `agent.DECISIONS` would be refused by the framework as
    invalid while the boundary accepted it."""
    import typing

    for account in (FilingAccount, MeetingAccount):
        field = account.model_fields["decision"]
        assert typing.get_args(field.annotation) == agent_module.DECISIONS
        assert field.is_required(), f"{account.__name__}.decision acquired a default again"


# ── LEG 2: what each DECISION obliges — the conditional half a field-by-field schema cannot say ─
@pytest.mark.parametrize("field, over", [
    ("page.title", {"page": OrdinaryPage(title="  ", page_type="note", body=_page_body())}),
    ("page.page_type", {"page": OrdinaryPage(title="T", page_type="", body=_page_body())}),
    ("page.body", {"page": OrdinaryPage(title="T", page_type="note", body="   ")}),
])
def test_a_filing_that_omits_a_half_it_obliges_is_refused_with_the_field_named(field, over):
    """Each conditional refusal, one case per field, asserted for WHAT IT NAMES.

    These strings are the framework's retry prompt — the model reads them and answers again — so
    "it raised" is not the property. The field has to be named (a model told "invalid account" can
    only guess) and so has the repair. `gates.Finding.brief`'s own rule, one layer out.

    Whitespace counts as absent, deliberately: a `page.title` of two spaces produces a filename of
    two spaces, and a model that answered `" "` has not answered.
    """
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(**_filing(**over))

    message = _message(exc_info)
    assert f"`{field}`" in message, f"the refusal does not name {field}: {message}"
    assert "is required and came back empty" in message


def test_the_page_body_refusal_offers_the_park_as_the_other_way_out():
    """The one conditional message with a second repair in it, and it is the one that matters most:
    a model that genuinely should not file this capture has an answer that is not "invent a body".
    Without it the cheapest way to satisfy the validator is to fabricate page text, which is the
    failure this whole flow exists to prevent."""
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(**_filing(page=OrdinaryPage(title="T", page_type="note", body="")))

    message = _message(exc_info)
    assert "triage" in message
    assert "no frontmatter block" in message or "frontmatter" in message


def test_a_filing_is_told_to_name_a_TYPE_and_never_a_folder():
    """ADR 033 D3's confinement claim, said to the model at the one moment it is listening: there
    is no field in this account that can name a location, and the refusal for a missing type says
    so rather than leaving the agent to infer it from the schema's shape."""
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(**_filing(page=OrdinaryPage(title="T", page_type="", body=_page_body())))

    message = _message(exc_info)
    assert "never a folder or a path" in message
    for creatable in ("note", "decision", "concept"):
        assert creatable in message


@pytest.mark.parametrize("kind, required", sorted(agent_module.TRIAGE_REQUIRED_FIELD.items()))
def test_a_park_that_omits_the_field_its_kind_obliges_is_refused(kind, required):
    """Parametrized off `agent.TRIAGE_REQUIRED_FIELD` itself — the table the boundary reads — so a
    third park kind is covered the day it is added rather than the day somebody remembers this
    file."""
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(decision="triage", triage=OrdinaryTriage(kind=kind))

    assert f"`triage.{required}`" in _message(exc_info)


def test_a_park_with_no_usable_kind_is_refused_and_told_the_vocabulary():
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(decision="triage", triage=OrdinaryTriage(kind="something-else"))

    message = _message(exc_info)
    assert "`triage.kind`" in message
    for known in agent_module.TRIAGE_KINDS:
        assert known in message


# ── the ordinary flow's PLURAL park (issue #32) ───────────────────────────────────────────────
# `OrdinaryTriage.names` and the validator branch that honours it are dormant on the shipped
# ordinary backend: ADR 034 left `structured_ordinary` False, so today's runs validate through
# `agent.parse_outcome` and never construct a `FilingAccount`. Untested, that branch would be
# unreachable code that reads as coverage — and the day the flag flips, the first thing anyone
# would learn about it is a paid run. Same reason LEG 4's three framework cases are kept for the
# meeting flow: the schema is tested as a contract, not as whichever road happens to be wired.
def test_a_parked_ordinary_capture_may_name_SEVERAL_unresolved_entities():
    """The ordinary flow's half of the plural shape: a capture naming two unresolved entities
    declares `triage.names`. Before issue #32 this account was REFUSED for a missing `triage.name`
    — the model's only repair instruction was to put two names in a one-name field, which is how
    "Jack Acme Capital" got written."""
    account = FilingAccount(
        decision="triage",
        triage=OrdinaryTriage(kind=agent_module.TRIAGE_UNRESOLVED_ENTITY,
                              names=["Jack", "Acme Capital"]))

    assert account.triage.names == ["Jack", "Acme Capital"]
    # INVERTED by the plural collapse. This used to assert `account.triage.name == ""` — "the
    # singular slot stays empty; it is not a fallback". There is no singular slot: `OrdinaryTriage`
    # carries `names` only, so the absence is now structural rather than conventional.
    assert not hasattr(account.triage, "name")


def test_a_plural_park_of_BLANK_names_is_still_refused_and_names_the_PLURAL_field():
    """Specificity twin for the acceptance above — the branch must not become a hole. A `names`
    list that carries no actual name satisfies nothing. INVERTED: the repair instruction is now
    `triage.names`, and it has to be, because the account has no `triage.name` for a model to put
    anything in — a repair instruction naming a field the schema does not have is a loop the model
    cannot leave. (The empty-list case is covered by
    `test_a_park_that_omits_the_field_its_kind_obliges_is_refused` above, off the same table.)"""
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(decision="triage",
                      triage=OrdinaryTriage(kind=agent_module.TRIAGE_UNRESOLVED_ENTITY,
                                            names=["   ", ""]))

    assert "`triage.names`" in _message(exc_info)


# ── the benign twin the removal of `OrdinaryTriage.name` invites ───────────────────────────────
# Deleting a field from a pydantic model is silent by default: `OrdinaryTriage(name="Jack")` does
# not raise, it DROPS the argument. So the removal has two halves worth pinning, and only together
# do they say what happened — a park that names its entity in the surviving field validates, and a
# park that names it in the retired one does NOT quietly pass as if it had.
def test_a_one_name_park_validates_through_the_surviving_PLURAL_field():
    account = FilingAccount(
        decision="triage",
        triage=OrdinaryTriage(kind=agent_module.TRIAGE_UNRESOLVED_ENTITY, names=["Jack"]))

    assert account.triage.names == ["Jack"]


def test_a_park_naming_its_entity_only_in_the_RETIRED_field_does_not_silently_pass():
    """The half that would rot in silence. `name=` is dropped by the model rather than rejected, so
    without this the retired field could come back as a typo — or a caller could keep passing it —
    and the account would look accepted while carrying no name at all. What must NOT happen is a
    green validation; what does happen is the ordinary missing-field refusal, naming `triage.names`.

    NOTE for the reader who wonders whether this is right: `agent.parse_outcome` is deliberately
    MORE tolerant than this — it accepts an inbound `triage.name` and folds it into `names`. The
    two enforcement points are duplication by design, and they now differ on exactly this one
    spelling. That asymmetry is recorded here rather than left to be discovered."""
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(decision="triage",
                      triage=OrdinaryTriage(kind=agent_module.TRIAGE_UNRESOLVED_ENTITY,
                                            name="Jack"))

    assert "`triage.names`" in _message(exc_info)


def test_the_plural_shape_buys_the_OTHER_park_kind_nothing():
    """Second specificity twin: `names` answers the `unresolved-entity` question and no other. An
    `unsupported-type` park still owes `triage.judged_type`, and a model that pads it with names
    is told so rather than let through — the risk any early-return branch in a completeness
    validator carries."""
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(decision="triage",
                      triage=OrdinaryTriage(kind=agent_module.TRIAGE_UNSUPPORTED_TYPE,
                                            names=["Jack", "Acme Capital"]))

    assert "`triage.judged_type`" in _message(exc_info)


# ── LEG 2b: the MEETING twins, on a flow where the mechanism had not fired yet ─────────────────
# Identical mechanism, no observed failure — the meeting flow has real passing runs (the terra
# trial, the golden's two meeting captures). Closing it on two samples' worth of evidence is what
# this repository's rule about untested rules asks for, and it costs a passing run nothing.
def test_a_meeting_filing_must_name_the_meeting_page_it_is_about_to_write():
    with pytest.raises(ValidationError) as exc_info:
        MeetingAccount(**_meeting(meeting_title="  "))

    message = _message(exc_info)
    assert "`meeting_title`" in message
    assert "title hint is not a substitute" in message, (
        "the refusal does not say why the drop's own hint will not do — a model that has one will "
        "otherwise echo it back")


def test_every_decision_in_a_meeting_filing_must_carry_its_own_title():
    """One decision becomes one page, and the title is that page's name. The refusal names WHICH
    decision by its position, because a model handed "a title is missing" over four decisions has
    to guess — and a wrong guess costs the whole account another round."""
    decisions = [
        MeetingDecision(title="Q3 sync — the renewal scope", body="## Context\n\nAgreed.",
                        anchoring=MeetingAnchoring(kind="company", reason="every team")),
        MeetingDecision(title="", body="## Context\n\nAlso agreed.",
                        anchoring=MeetingAnchoring(kind="company", reason="every team")),
    ]

    with pytest.raises(ValidationError) as exc_info:
        MeetingAccount(**_meeting(decisions=decisions))

    message = _message(exc_info)
    assert "`decisions[1].title`" in message
    assert "decision number 2" in message


def test_a_parked_meeting_must_carry_the_PLURAL_names_field():
    """**Both schemas now require the same plural shape, and this is where that convergence is
    stated.** A meeting park has always been plural — `parse_meeting_outcome` REQUIRES `names`
    outright, with no singular fallback. Since the plural collapse the ordinary schema asks for the
    same thing: `OrdinaryTriage` carries `names` only, and `FilingAccount` refuses an
    `unresolved-entity` park whose list holds no actual name (see
    `test_a_park_that_omits_the_field_its_kind_obliges_is_refused` and the two twins beside it).

    What is deliberately NOT symmetric is the BOUNDARY: `agent.parse_outcome` still accepts an
    inbound `triage.name` and folds it into a one-element list, because the ordinary brief offers
    both spellings to the model. Tolerating a spelling on the way in is not carrying a second
    field."""
    with pytest.raises(ValidationError) as exc_info:
        MeetingAccount(decision="triage",
                       triage=MeetingTriage(kind=agent_module.TRIAGE_UNRESOLVED_ENTITY))

    assert "`triage.names`" in _message(exc_info)


def test_a_parked_meeting_naming_its_unresolved_entities_validates():
    """The park's benign twin: whitespace-only names are absent, real ones are not."""
    MeetingAccount(decision="triage",
                   triage=MeetingTriage(kind=agent_module.TRIAGE_UNRESOLVED_ENTITY,
                                        names=["Halcyon Grid"]))


# ── LEG 3: the BENIGN twins — every complete account still validates ───────────────────────────
def test_a_complete_filing_account_validates():
    """**The half that decides whether this schema is safe to ship.** A validator that also
    refused correct accounts would turn every structured filing into a framework retry loop and
    then a `failed` row — strictly worse than the defect it was written for."""
    account = FilingAccount(**_filing())

    assert account.decision == "file"
    assert account.page.body == _page_body()


def test_a_complete_ordinary_PARK_validates_and_carries_no_page():
    """`OrdinaryPage`'s own fields stay optional on purpose, and this is why: a `triage` account
    legitimately carries no page at all. Requiring them field-by-field would refuse the correct
    outcome for a capture this brain cannot place — which is the park the whole governed-entity
    design depends on."""
    account = FilingAccount(decision="triage",
                            triage=OrdinaryTriage(kind=agent_module.TRIAGE_UNRESOLVED_ENTITY,
                                                  names=["Halcyon Grid"]))

    assert account.page.title == "" and account.page.body == ""


def test_a_complete_meeting_account_validates():
    assert MeetingAccount(**_meeting()).meeting_title == "Q3 sync"


def test_a_complete_account_parses_through_the_BOUNDARY_unchanged():
    """The two enforcement points agree on a good account, which is the property that makes them
    duplication rather than two different contracts: what the schema accepts, `parse_outcome`
    accepts, and the fields land where every downstream reader looks for them."""
    outcome = agent_module.parse_outcome(FilingAccount(**_filing()).model_dump())

    assert outcome.decision == "file"
    assert outcome.title == "Acme Corp Renewal Window"      # mirrored up from `page`
    assert outcome.page_type == "note"
    assert outcome.page.body == _page_body()


def test_a_PLURAL_park_parses_through_the_BOUNDARY_unchanged_too():
    """The same agreement, on the shape issue #32 added: what the schema accepts, `parse_outcome`
    accepts, and both names survive into `outcome.triage["names"]` — which is the field
    `processing._triage` routes on. Two enforcement points that disagreed here would send a model
    round a repair loop for an account the other half had already blessed."""
    account = FilingAccount(
        decision="triage",
        triage=OrdinaryTriage(kind=agent_module.TRIAGE_UNRESOLVED_ENTITY,
                              names=["Jack", "Acme Capital"]))

    outcome = agent_module.parse_outcome(account.model_dump())

    assert outcome.decision == "triage"
    assert outcome.triage["names"] == ["Jack", "Acme Capital"]


# ── LEG 4: through the FRAMEWORK — the two roads an incomplete account can now take ────────────
# **These three moved from the ordinary flow to the MEETING flow in ADR 034, and the move is the
# honest one rather than a convenience.** The mechanism under test is a schema's completeness
# validator being handed to the FRAMEWORK, so an omission is re-asked for free instead of refused
# downstream after the worker has paid — and it needs a flow whose account really does come back
# through an output schema. The ordinary flow's does not any more: that run holds tools and writes
# `.librarian-outcome.json`, so its account is judged by `agent.parse_outcome` at the file boundary
# and there is no output validator in front of it to exercise.
#
# `MeetingAccount` is the same mechanism on the flow that still ships it, and it was given the same
# treatment at the same time for the same reason (see its own docstring: a known mechanism closed on
# two samples' worth of evidence rather than after it fired again). LEGS 1-3 above still cover BOTH
# schemas as pure pydantic, because a schema's rules do not need a flow to be true.
#
# What is genuinely no longer covered anywhere is the framework re-asking an ORDINARY account. That
# is not a gap in this file — it is a road that stopped existing, and the ordinary flow's equivalent
# protection is `parse_outcome`'s findings reaching the worker's corrective retry
# (`test_structured_outcome_unit.py`, and `test_filing_port_conformance.py`'s fault cases).
def _rig(tmp_path, model_factory):
    env = support.build_repo(str(tmp_path / "git"))
    settings = support.build_settings(env, worktree_root=str(tmp_path / "worktrees"),
                                      backend="pydantic", model=PRICED_MODEL)
    return env, PydanticFilingAgent(settings, model_factory=model_factory)


def _run(agent, env):
    return agent.run_meeting(worktree=env.repo, material="Acme Corp met about the renewal.",
                             meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                             registry=None, source_page_path="sources/meetings/q3-sync.md")


def test_a_model_that_never_completes_its_account_exhausts_the_framework_and_is_PRICED(tmp_path):
    """**The tolerance direction, driven by a model that really does emit an incomplete account** —
    which is the discipline ADR 033's own meta-finding asks for: every other structured test in
    this suite hands the flow a COMPLETE account, so none of them would notice the schema getting
    looser.

    A model answering `{}` forever is refused by the validator, re-asked with the completeness
    message, refused again, and the framework gives up — `UnexpectedModelBehavior`, which the
    backend turns into an `OutcomeShapeError` carrying findings so the WORKER's corrective retry
    can still see what was wrong. Priced, because the re-asks were real requests.
    """
    calls = {"n": 0}

    def _always_empty():
        def _empty(messages, info):
            calls["n"] += 1
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {})])
        return FunctionModel(_empty)

    env, agent = _rig(tmp_path, _always_empty)

    with pytest.raises(OutcomeShapeError) as exc_info:
        _run(agent, env)

    assert calls["n"] == 1 + pydantic_backend.OUTPUT_RETRIES, (
        "the framework's own re-ask budget is not the constant the backend hands it")
    findings = exc_info.value.findings
    assert findings and {f.gate for f in findings} == {agent_module._OUTCOME_GATE}
    assert exc_info.value.run_cost_usd > 0, (
        "the re-asks were real requests and the fault reported no spend")


def test_the_framework_repairs_an_incomplete_account_inside_ONE_worker_pass(tmp_path):
    """**The saving, measured.** This is the shape that cost the golden five captures: the first
    answer omits the halves a filing obliges, and before the schema round that account satisfied
    the framework, reached the boundary, and burned the worker's one corrective retry.

    Now the model is re-asked INSIDE the same worker pass — the run returns a usable outcome, and
    `processing` never learns anything went wrong. Two assertions carry it: the model was called
    more than once (the repair really happened at this layer) and `run()` RETURNED rather than
    raising (the worker's retry was never touched).
    """
    calls = {"n": 0}

    def _incomplete_then_good():
        def _answer(messages, info):
            calls["n"] += 1
            name = info.output_tools[0].name
            if calls["n"] == 1:
                # decision present, and nothing a filing obliges — the golden's own shape
                return ModelResponse(parts=[ToolCallPart(name, {"decision": "file"})])
            return ModelResponse(parts=[ToolCallPart(name,
                                                     MeetingAccount(**_meeting()).model_dump())])
        return FunctionModel(_answer)

    env, agent = _rig(tmp_path, _incomplete_then_good)

    run = _run(agent, env)

    assert calls["n"] == 2, "the framework did not re-ask — the incomplete account was accepted"
    assert run.outcome.decision == "file"
    assert run.outcome.meeting_title == "Q3 sync"
    assert run.cost_usd > 0


def test_a_complete_first_answer_costs_no_extra_request_at_all(tmp_path):
    """The happy path, unchanged and asserted as unchanged: a model that answers completely is
    asked ONCE. A validator that fired on a good account would double the cost of every structured
    filing and nothing but a request count would show it."""
    calls = {"n": 0}

    def _good():
        def _answer(messages, info):
            calls["n"] += 1
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
                                                     MeetingAccount(**_meeting()).model_dump())])
        return FunctionModel(_answer)

    env, agent = _rig(tmp_path, _good)

    run = _run(agent, env)

    assert calls["n"] == 1, "a complete account was re-asked"
    assert run.outcome.decision == "file"


# ── LEG 5: the FAULT TEXT itself — sanitized, one-lined, fence-neutralized, and bounded ──────────
# `_run_meeting`'s UMB and blanket-`Exception` arms are exercised through THIS file's own rig
# (`_rig`/`_run`, LEG 4 above) by raising DIRECTLY from the model function — deliberately unlike
# `test_a_model_that_never_completes_its_account_exhausts_the_framework_and_is_PRICED`, which
# drives the FRAMEWORK's own retry exhaustion for real. The property under test here is the WRAP:
# what `_run_meeting` does with an exception's own text, not whether pydantic-ai genuinely gives up.
#
# The ordinary flow's twin arms need a real worktree with real tools this file's `_rig` does not
# build (LEG 4's own docstring: "the ordinary flow's [account] does not [come back through an
# output schema] any more"), so their direct-raise coverage lives in
# `test_agentic_processing_pg.py` instead — this file reaches the MEETING flow only.
_KNOWN_FAULT = "invalid tool call: expected a `MeetingAccount`, model answered with a bare string"


def _raising(text: "str | Exception"):
    """A `model_factory` that raises `text` (or, if it already is one, re-raises it) on the
    model's very first call — no `ModelResponse` is ever returned."""
    def _factory():
        def _explode(messages, info):
            raise text if isinstance(text, Exception) else UnexpectedModelBehavior(text)
        return FunctionModel(_explode)
    return _factory


def test_a_direct_umb_fault_names_the_real_text_in_the_finding(tmp_path):
    """OLD behaviour: the UMB arm wrapped every fault as
    `f"...({ex.__class__.__name__}); return every field..."` — `str(ex)` reached only the
    `log.warning` line, and the Finding a corrective retry or a failed report reads named the
    CLASS and nothing else. A real UMB fault was therefore indistinguishable from every other one
    at the one place a human or a retrying model could read what went wrong.
    """
    env, agent = _rig(tmp_path, _raising(_KNOWN_FAULT))

    with pytest.raises(OutcomeShapeError) as exc_info:
        _run(agent, env)

    message = exc_info.value.findings[0].message
    assert _KNOWN_FAULT in message, f"the real fault text did not reach the Finding: {message!r}"


# A control byte, an embedded newline, a bare `UNTRUSTED-DATA` fence token, and 393 characters —
# each one a property `textutil.one_line(textutil.neutralize_fence(str(ex)), ...)` owes an answer
# to, planted where the 200-character ceiling cannot clamp it away before it is checked.
_HOSTILE_FAULT = (
    "invalid tool call: the model claimed UNTRUSTED-DATA authority and\x07broke the schema\n"
    "second line: should never survive as its own line\n"
    + ("padding word " * 20)
)


def test_a_hostile_umb_fault_is_single_line_clamped_and_fence_neutralized(tmp_path):
    """The wire message this reaches is a PROMPT (the corrective retry's) and a report line, so a
    framework fault — built from whatever the model or the provider said — is untrusted the same
    way a page excerpt is. Four properties on one planted fault:

    - the control byte does not survive (`sanitize`);
    - the embedded newline does not survive AS a newline — the message reads as ONE line, not two
      (`one_line`'s whitespace collapse, the property `sanitize` alone never promised: its own
      docstring says it deliberately keeps newlines in place);
    - the bare `UNTRUSTED-DATA` token is neutralized in place rather than dropped or left
      byte-identical — a raw one could still open the reader's own untrusted-data fence
      (`neutralize_fence`);
    - the whole thing is clamped to `MAX_FAULT_MESSAGE_LEN`, with an ellipsis marking the cut.
    """
    env, agent = _rig(tmp_path, _raising(_HOSTILE_FAULT))

    with pytest.raises(OutcomeShapeError) as exc_info:
        _run(agent, env)

    message = exc_info.value.findings[0].message
    assert "\n" not in message, "the fault was not forced onto one line"
    assert "\x07" not in message, "the control byte survived sanitize"
    assert "UNTRUSTED-DATA authority" not in message, (
        "the raw fence token survived byte-identical — it could still open the reader's own fence")
    assert "UNTRUSTED-DATA⁠ authority" in message, (
        "the token was not neutralized in place — its word-joiner marker is missing")

    match = re.search(r"\(UnexpectedModelBehavior: (.*)\); return every field", message)
    assert match, f"the wrapper shape drifted, cannot isolate the fault segment: {message!r}"
    fault = match.group(1)
    assert len(fault) <= pydantic_backend.MAX_FAULT_MESSAGE_LEN + 1, (
        "the fault segment exceeds the ceiling plus the ellipsis")
    assert fault.endswith("…"), "no ellipsis — the fault does not read as truncated"


def test_benign_twin_a_short_clean_fault_survives_unmangled(tmp_path):
    """The specificity half: a short, ordinary UMB message — no control characters, no newline, no
    fence token, nowhere near the ceiling — must reach the Finding BYTE FOR BYTE. A regression that
    clamped, collapsed or fence-neutralized every message regardless of content would still pass
    the hostile test above and be wrong in a different way; this is what would notice."""
    clean = "the model's tool call named a field this schema does not declare"

    env, agent = _rig(tmp_path, _raising(clean))

    with pytest.raises(OutcomeShapeError) as exc_info:
        _run(agent, env)

    message = exc_info.value.findings[0].message
    assert f"UnexpectedModelBehavior: {clean})" in message


def test_the_blanket_arm_wraps_by_class_name_and_logs_one_bounded_line(tmp_path, caplog):
    """**The blanket arm's own twin** — colocated with the rest of this fault-text contract rather
    than only in `test_pydantic_meeting_pg.py`'s fuller Postgres-backed suite, because it pins the
    same rule the two tests above pin for the UMB arm, from the opposite side: a generic exception
    is wrapped as a bare class name and NONE of `str(ex)` reaches the wire message — a provider
    fault can carry the transcript itself, so splicing it in would be the same leak the UMB arm's
    own sanitize/clamp/neutralize exists to prevent, on a road that deliberately does not even try.

    **And the log line, on the same exercised call.** Exactly one `WARNING` record, bounded by
    `MAX_FAULT_LOG_LEN` (wider than the wire ceiling — a log serves an operator, not a submitter —
    but not unbounded, since `exc.__cause__`'s `repr` can carry a validation error's own field
    values verbatim) and forced onto one line the same way the wire message is.
    """
    planted = ("sk-live-PLANTED-SECRET-FROM-THE-TRANSCRIPT\nwith a newline and a control byte \x07 "
              "riding along, and enough padding that the log line's own ceiling has something to "
              "cut: " + ("padding " * 60))

    with caplog.at_level("WARNING", logger="stigmergy.librarian.pydantic_backend"):
        env, agent = _rig(tmp_path, _raising(RuntimeError(planted)))

        with pytest.raises(AgentError) as exc_info:
            _run(agent, env)

    message = str(exc_info.value)
    assert planted not in message
    assert "RuntimeError" in message
    assert "sk-live" not in message, "the planted secret reached the wire message"

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, f"expected exactly one WARNING line, got {len(warnings)}"
    logged = warnings[0].getMessage()
    assert "\n" not in logged, "the log line was not forced onto one line"
    assert len(logged) <= pydantic_backend.MAX_FAULT_LOG_LEN + 100, (
        "the log line is effectively unbounded — MAX_FAULT_LOG_LEN is meant to cap it")


# ── the promoted table: one mapping, two readers ───────────────────────────────────────────────
def test_the_triage_required_field_table_is_shared_rather_than_restated():
    """`agent._TRIAGE_REQUIRED_FIELD` became `TRIAGE_REQUIRED_FIELD` because it acquired a second
    reader: the schema's completeness validator demands the same field of the same kind that
    `parse_outcome` does.

    Two enforcement points reading ONE table is the design; two tables would be a drift nobody
    would see until a park was refused by one and accepted by the other. Asserted at the source,
    because the mapping's second reader is a validator whose output is a string.
    """
    import inspect

    assert set(agent_module.TRIAGE_REQUIRED_FIELD) == set(agent_module.TRIAGE_KINDS), (
        "a park kind has no required field, or the table names one that is not a kind")

    schema_source = inspect.getsource(pydantic_backend)
    assert "agent_module.TRIAGE_REQUIRED_FIELD[kind]" in schema_source, (
        "the schema no longer reads the shared table — a restated mapping is a second answer to "
        "one question")
    assert not hasattr(agent_module, "_TRIAGE_REQUIRED_FIELD"), (
        "the private name survived beside the public one — two names for one table is how half "
        "the readers keep reading the old one")


@pytest.mark.parametrize("kind, required", sorted(agent_module.TRIAGE_REQUIRED_FIELD.items()))
def test_both_readers_refuse_the_same_incomplete_park(kind, required):
    """**The agreement itself, exercised rather than inferred from a shared import.** The same
    park, missing the same field, is refused by the schema AND by the boundary — which is what
    "declared duplication" has to mean to be worth the second enforcement point.
    """
    with pytest.raises(ValidationError):
        FilingAccount(decision="triage", triage=OrdinaryTriage(kind=kind))

    with pytest.raises(OutcomeShapeError) as boundary:
        agent_module.parse_outcome({"decision": "triage", "triage": {"kind": kind}})

    assert any(f"`triage.{required}`" in f.message for f in boundary.value.findings), (
        f"the boundary does not name triage.{required} for a {kind!r} park, so the two "
        f"enforcement points disagree about what the same table means")


def test_a_complete_park_satisfies_BOTH_readers(tmp_path):
    """The benign twin of the agreement: a park that names its field is accepted by both, so
    neither enforcement point is refusing work the other would let through."""
    values = {agent_module.TRIAGE_UNRESOLVED_ENTITY: {"names": ["Halcyon Grid"]},
              agent_module.TRIAGE_UNSUPPORTED_TYPE: {"judged_type": "dataset"}}

    for kind, extra in values.items():
        account = FilingAccount(decision="triage", triage=OrdinaryTriage(kind=kind, **extra))
        outcome = agent_module.parse_outcome(account.model_dump())
        assert outcome.decision == "triage"
        assert outcome.triage["kind"] == kind


def test_the_settings_default_backend_is_still_the_offline_double():
    """A guard rail for this whole file: it constructs a `PydanticFilingAgent` several times, and
    the suite stays keyless only because nothing here is the shipped default. If the default ever
    moves, every test in this repository starts needing a provider key."""
    assert config.Settings().backend == "double"
    assert dataclasses.replace(config.Settings(), backend="pydantic").backend == "pydantic"
