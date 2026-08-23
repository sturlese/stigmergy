"""The structured backend's OUTPUT SCHEMA: what a framework would refuse before this package sees it.

**This file exists because a paid run measured the cost of getting it wrong.** Every field of the
account carried a default, `decision` included, on the reasoning that a provider omitting something
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
* the BOUNDARY (`agent.parse_outcome`) keeps every check regardless, because it also judges the
  FILE channel and because a typed provider response is not a trusted one.

**What is NOT restated here, and the distinction is the whole design.** Bounds belong to the
boundary — identifiers refused over `MAX_IDENTIFIER_LEN`, prose truncated, a page body refused,
lists capped — and a second set of limits in a schema would be a second answer to one question.
Requiredness is the opposite case.

**OLD BEHAVIOUR: this file covered TWO accounts, and drove one of them through the framework.**
`MeetingAccount` was `run_meeting`'s wired `output_type`, so the completeness validators really
were handed to pydantic-ai and re-asked a model for free (LEG 4) — and that flow's fault-wrapping
arms were exercised through the same rig (LEG 5). There is one pipe now: `run` is the only call,
it wires NO `output_type` (its account comes home as `.librarian-outcome.json`), and
`MeetingAccount` is gone with the flow. What is left here is the schema's own rules, which is what
this repository owns; the framework's re-ask is pydantic-ai's behaviour and no shipped call reaches
it any more. The fault-wrapping properties LEG 5 held moved to
`test_agentic_processing_pg.py`, onto `run`'s own arms, rather than being dropped.

Keyless and pure: nothing here builds an agent or a model.
"""
import dataclasses

import pytest
from pydantic import ValidationError

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import config, pydantic_backend
from stigmergy.librarian.errors import OutcomeShapeError
from stigmergy.librarian.pydantic_backend import (
    FilingAccount,
    NewEntity,
    OrdinaryAnchoring,
    OrdinaryPage,
)


def _page_body() -> str:
    return "## What the capture said\n\nThe renewal window was confirmed."


def _filing(**over) -> dict:
    """The keyword arguments of a COMPLETE ordinary filing account, so each case below overrides
    exactly the one field it is about and nothing else drifts."""
    base = dict(decision="file",
                pages=[OrdinaryPage(title="Acme Corp Renewal Window", page_type="note",
                                    body=_page_body())],
                anchoring=OrdinaryAnchoring(kind="entity", entities=["Acme Corp"]),
                summary="filed the renewal note")
    base.update(over)
    return base


def _message(exc_info) -> str:
    return str(exc_info.value)


# ── LEG 1: `decision` is required and enum-constrained ─────────────────────────────────────────
def test_an_account_that_omits_its_decision_cannot_be_built_at_all():
    """**The exact shape the paid run died on.** `decision` used to default to `""`, so an account
    with no decision satisfied the schema, spent no framework retry, and was refused downstream
    with `unknown-decision` after the worker had already paid for the pass.

    It is required now, so the framework re-asks — which is free, immediate, and names the field.
    """
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount()

    assert "decision" in _message(exc_info)
    assert "Field required" in _message(exc_info), (
        "the omission was refused for some other reason — `decision` may have re-acquired a "
        "default and be failing the completeness validator instead")


def test_a_decision_outside_the_vocabulary_is_refused_by_the_enum():
    """`Literal[*agent.DECISIONS]`, derived rather than retyped — so the schema and
    `agent.parse_outcome`'s `DECISIONS` check cannot come to disagree about the vocabulary.

    `"publish"` is the case this suite used to drive through the schema on purpose, precisely
    because a plain string took it. It does not any more, and that is the milestone.
    """
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(**{**_filing(), "decision": "publish"})

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

    field = FilingAccount.model_fields["decision"]
    assert typing.get_args(field.annotation) == agent_module.DECISIONS
    assert field.is_required(), "FilingAccount.decision acquired a default again"


# ── LEG 2: what each DECISION obliges — the conditional half a field-by-field schema cannot say ─
@pytest.mark.parametrize("field, over", [
    ("pages[1].title", {"pages": [OrdinaryPage(title="  ", page_type="note", body=_page_body())]}),
    ("pages[1].page_type", {"pages": [OrdinaryPage(title="T", page_type="", body=_page_body())]}),
    ("pages[1].body", {"pages": [OrdinaryPage(title="T", page_type="note", body="   ")]}),
    ("pages", {"pages": []}),
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


def test_a_filing_is_told_to_name_a_TYPE_and_never_a_folder():
    """the structured filing flow's confinement claim, said to the model at the one moment it is listening: there
    is no field in this account that can name a location, and the refusal for a missing type says
    so rather than leaving the agent to infer it from the schema's shape."""
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(**_filing(pages=[OrdinaryPage(title="T", page_type="",
                                                    body=_page_body())]))

    message = _message(exc_info)
    assert "never a folder or a path" in message
    for creatable in ("note", "decision", "concept"):
        assert creatable in message


# ── LEG 2b: the same obligation over MANY pages, which is where a transcript lands now ─────────
# OLD BEHAVIOUR: this leg was the MEETING twins — `MeetingAccount.meeting_title`, and one title per
# `decisions[n]`, refused by position so a model handed "a title is missing" over four decisions
# did not have to guess. `MeetingAccount` retired with the flow, and the property it protected did
# not: a transcript declares one page per conclusion in `pages`, so the by-position refusal is
# owed by exactly the same schema every other capture is judged by.
def test_the_refusal_names_WHICH_page_of_a_multi_page_filing_is_incomplete():
    """A capture writes as many pages as its material establishes, and this text IS the retry
    prompt: a model told "a title is missing" over four declared pages has to guess, and a wrong
    guess costs the whole account another round."""
    pages = [
        OrdinaryPage(title="Renewal scope agreed", page_type="decision", body=_page_body()),
        OrdinaryPage(title="  ", page_type="decision", body=_page_body()),
    ]

    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(**_filing(pages=pages))

    message = _message(exc_info)
    assert "`pages[2].title`" in message, message
    assert "is required and came back empty" in message


def test_a_single_page_filing_is_named_without_a_position_it_would_have_to_count():
    """The twin of the sentence above, and the reason `_complete_for_its_decision` counts at all:
    with ONE page there is no position to disambiguate, so the refusal reads `pages[1]` rather
    than making a model count a list of one."""
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(**_filing(pages=[OrdinaryPage(title="  ", page_type="note",
                                                    body=_page_body())]))

    assert "`pages[1].title`" in _message(exc_info)


# ── LEG 3: the BENIGN twins — every complete account still validates ───────────────────────────
def test_a_complete_filing_account_validates():
    """**The half that decides whether this schema is safe to ship.** A validator that also
    refused correct accounts would turn every structured filing into a framework retry loop and
    then a `failed` row — strictly worse than the defect it was written for."""
    account = FilingAccount(**_filing())

    assert account.decision == "file"
    assert account.pages[0].body == _page_body()


def test_a_complete_account_parses_through_the_BOUNDARY_unchanged():
    """The two enforcement points agree on a good account, which is the property that makes them
    duplication rather than two different contracts: what the schema accepts, `parse_outcome`
    accepts, and the fields land where every downstream reader looks for them."""
    outcome = agent_module.parse_outcome(FilingAccount(**_filing()).model_dump())

    assert outcome.decision == "file"
    assert outcome.title == "Acme Corp Renewal Window"      # mirrored up from `page`
    assert outcome.page_type == "note"
    assert outcome.pages[0].body == _page_body()


# ── LEGS 4 and 5 retired with the flow that wired an output schema ────────────────────────────
# OLD BEHAVIOUR: LEG 4 drove `MeetingAccount` through a real `Agent.run` against pydantic-ai's own
# offline models and pinned three cases — a model that never completes its account exhausts the
# framework's re-ask budget and is PRICED, an incomplete-then-good answer is repaired inside ONE
# worker pass, and a complete first answer costs no extra request. LEG 5 pinned what
# `_run_meeting` did with an exception's own TEXT.
#
# Both needed a call that wires an `output_type`, and there is none: `run` is the only call the
# port has, its account comes home as `.librarian-outcome.json`, and `FilingAccount` is declared
# but unwired (`pydantic_backend`'s own section comment says so). Re-aiming LEG 4 at a hand-built
# `Agent(output_type=FilingAccount)` would measure pydantic-ai's retry behaviour against a flow
# nothing ships — the definition of a test that is green about nothing.
#
# **Neither property was dropped, and neither is silently uncovered:**
#
#   * the SCHEMA's own completeness rules — the part this repository owns and the part the paid
#     run was lost to — are LEGS 1-3 above, as pure pydantic, where they need no flow to be true;
#   * the FAULT-TEXT contract (`_fault_message`/`_log_fault`: sanitized, one-lined,
#     fence-neutralized, clamped, and a blanket arm that wraps by class name only) now lives on
#     `run`'s own arms in `test_agentic_processing_pg.py`, driven by a model function that raises
#     directly — those helpers have exactly one caller left and that is where it is;
#   * the worker-side protection for an ordinary account that arrives wrong is `parse_outcome`'s
#     findings reaching the corrective retry (`test_structured_outcome_unit.py`, and
#     `test_filing_port_conformance.py`'s fault cases).


def test_the_settings_default_backend_is_still_the_offline_double():
    """A guard rail for this whole file: it constructs a `PydanticFilingAgent` several times, and
    the suite stays keyless only because nothing here is the shipped default. If the default ever
    moves, every test in this repository starts needing a provider key."""
    assert config.Settings().backend == "double"
    assert dataclasses.replace(config.Settings(), backend="pydantic").backend == "pydantic"


# ── stringified nested structures: a provider quirk the schema must absorb ────────────────────
def test_a_provider_that_stringifies_nested_structures_is_decoded_at_the_boundary():
    """OLD BEHAVIOUR: a ValidationError, and the framework's single output retry burned on a
    SERIALIZATION quirk rather than a content problem. Measured directly: GLM-5.2 through
    OpenRouter's tool-calling returned `triage='{}'` — the JSON *string*, not the object — and
    the filing golden's two meeting captures then validated with every nested list empty
    (decisions 0/2), because the model's retry flattened the account into something that passed.
    The producer is a MODEL behind a provider's tool-calling; decoding a bracketed string on a
    field DECLARED as a structure is the same inbound tolerance `_fold_a_singular_name_into_the_
    list` already extends."""
    account = pydantic_backend.FilingAccount.model_validate({
        "decision": "file",
        "links_created": '["Acme Corp"]',
        "pages": ('[{"title": "Expand to Madrid", "page_type": "decision", '
                  '"body": "September."}]'),
        "anchoring": '{"kind": "company", "reason": "org"}',
        "new_entities": "[]",
    })
    assert account.links_created == ["Acme Corp"]                  # a stringified list
    assert [page.title for page in account.pages] == ["Expand to Madrid"]   # a list of models
    assert account.anchoring.kind == "company"                     # a stringified nested model


def test_a_stringified_anchoring_inside_a_declared_page_is_decoded_too():
    """**The red proof of a defect the multi-page account introduced, now fixed.**

    OLD BEHAVIOUR: `_wants_structure` answered `False` for an OPTIONAL nested model
    (`OrdinaryAnchoring | None`) — `get_origin` of a union is neither `list` nor `dict`, and a
    union is not a `type` — so the per-page anchor was the one nested structure the shield did not
    cover. The fix unwraps the union and asks the question of each member.

    The shield exists for a provider that serializes a nested structure as its JSON string, and it
    is applied per FIELD by annotation. When the account carried `decisions[n].anchoring` — a
    required nested model — the inner decode worked, and this file pinned "stringified INSIDE
    stringified" on it. A capture now declares `pages[n].anchoring`, which is OPTIONAL
    (`OrdinaryAnchoring | None`, so a page with no anchor inherits the capture's), and an optional
    nested model does not satisfy `_wants_structure`. So the per-page anchor — the field a
    multi-page transcript filing depends on most — is the one nested structure the shield no longer
    covers, and a provider that stringifies it burns the framework's single output retry exactly
    as the measured quirk did.

    """
    account = pydantic_backend.FilingAccount.model_validate({
        "decision": "file",
        "pages": [{"title": "Expand to Madrid", "page_type": "decision", "body": "September.",
                   "anchoring": '{"kind": "company", "reason": "org"}'}],
    })

    assert account.pages[0].anchoring.kind == "company"


def test_prose_that_merely_looks_like_json_stays_prose():
    """The benign twin: content fields are declared `str`, so a body or the notes OPENING with a
    bracket are never decoded — the shield is the field's ANNOTATION, not a guess about text."""
    account = pydantic_backend.FilingAccount.model_validate({
        "decision": "file",
        "summary": '{"looks": "like json"} but is prose',
        "pages": [{"title": "T", "page_type": "note",
                   "body": '["still prose: the field is a str"]'}],
    })
    assert account.summary.startswith('{"looks"')
    assert account.pages[0].body == '["still prose: the field is a str"]'


# ── proposals: the schema asks for the three fields without which there is no page ───────────
# Mirrored from `agent._parse_new_entities` (the file channel's rule) through
# `pydantic_backend._complete_proposals`, so both readers refuse the same half-proposal — the same
# declared duplication the schema and the boundary have always had.
@pytest.mark.parametrize("missing", ["name", "entity_type", "summary"])
def test_a_proposed_entity_missing_a_page_making_field_is_refused_and_the_field_is_named(missing):
    fields = {"name": "Scircle", "entity_type": "organization", "summary": "a perfume startup"}
    fields[missing] = "  "
    with pytest.raises(ValidationError) as exc_info:
        FilingAccount(**_filing(new_entities=[NewEntity(**fields)]))
    assert f"`new_entities[0].{missing}`" in _message(exc_info)


def test_a_complete_proposal_validates_and_parses_through_the_boundary():
    """The benign twin, and the agreement between the schema and the boundary stated as a round
    trip: what the schema accepts, `parse_outcome` accepts, and the proposal lands where
    `librarian.identity` reads it."""
    proposed = NewEntity(name="Scircle", entity_type="organization", role="a perfume startup",
                         aliases=["S-Circle"], summary="Scircle sells personalised perfume.",
                         facts=["Seed round in 2026"], connections=["[[Scircle analysis]] — why"])
    account = FilingAccount(**_filing(new_entities=[proposed]))
    outcome = agent_module.parse_outcome(account.model_dump())
    assert outcome.new_entities[0]["name"] == "Scircle"
    assert outcome.new_entities[0]["aliases"] == ("S-Circle",)


def test_both_readers_refuse_the_same_incomplete_proposal():
    """The agreement itself, exercised rather than inferred from a shared import."""
    with pytest.raises(ValidationError):
        FilingAccount(**_filing(new_entities=[NewEntity(name="Scircle", entity_type="organization")]))
    with pytest.raises(OutcomeShapeError) as boundary:
        agent_module.parse_outcome({"decision": "file", "title": "T",
                                    "new_entities": [{"name": "Scircle",
                                                      "entity_type": "organization"}]})
    assert any("new_entities[0].summary" in f.message for f in boundary.value.findings)
