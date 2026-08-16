"""Issue #32 and its sequel — `processing._triage`'s park routing, isolated at the routing layer,
independent of whatever `agent.parse_outcome` accepts.

Issue #32 was the ordinary flow having only a SINGULAR slot for an unresolved name while the
meeting flow already had a plural one, so a capture naming two things garbled both into one. The
sequel is the collapse that followed: there is now ONE router, `_ask_or_park`, one pair of report
builders, and ONE written name shape — `schema.SITUATION_NAMES_KEY` / `unresolved_names`, a LIST
even for one name. The singular keys survive as read-only legacy input for rows already in the
queue (`entities.situations.subjects_of`); nothing writes them.

So every assertion here that names a shape asserts BOTH halves — the plural key present AND the
singular key absent. Half a contract is what a dual-writing implementation passes.

What the count still decides is the SENTENCE, never the keys: a one-name park reads as one name.

Duck-typed `outcome`/`item` inputs, mirroring `test_refusal_routing.py`'s convention — this is
`processing`'s own routing contract, exercised with no database, no git worktree and no model.

The second half of this file is about what a name IS. `entities.birth._prepare` already refuses a
whitespace-only name as empty (`tests/entities/test_birth.py::test_an_empty_name_is_refused`), so
the registry's own answer is settled: a blank is not a name. The plural park was not asking the
registry's question — it asked whether the LIST was non-empty — and the blank travelled all the way
into the one question the submitter ever gets. Both flows, both shapes, one normalisation.
"""
from types import SimpleNamespace

import pytest

from stigmergy.capture import schema
from stigmergy.librarian import processing

REGISTRY = SimpleNamespace(entities={})


def _deps() -> processing.Deps:
    return processing.Deps(settings=None, evidence=None, agent=None, registry=REGISTRY)


def _outcome(triage: dict) -> SimpleNamespace:
    return SimpleNamespace(triage=triage, summary="", findings=[])


# ── the ask: a fresh capture, the one question not yet spent ──────────────────────────────────
FRESH_ITEM = {"id": 101, "attempts": 1, "asked_at": None}

# ── the park: the one question already spent, still unresolved ────────────────────────────────
ALREADY_ASKED_ITEM = {"id": 101, "attempts": 2, "asked_at": "2026-08-01T00:00:00Z"}


def test_the_one_ask_names_only_one_of_two_unresolved_entities():
    """Issue #32 repro at the ASK stage. A capture whose outcome already separates two unresolved
    names (`triage.names`, the exact shape `_triage_meeting` reads) should earn ONE question
    naming BOTH — the meeting flow already built exactly that question. The ordinary `_triage`
    never reached it: it read only `parked.get("name")`, so the second name had nowhere to go and
    the resulting question was about ONE entity, not two.
    """
    outcome = _outcome({"kind": "unresolved-entity", "names": ["Jack", "Acme Capital"]})

    result = processing._triage(FRESH_ITEM, _deps(), outcome)

    assert result.status == schema.NEEDS_INPUT
    assert result.report.get("unresolved_names") == ["Jack", "Acme Capital"], (
        "the ordinary path's `_triage` does not route a multi-name outcome through "
        "the ONE park router the way `_triage_meeting` already does "
        "for the meeting flow — `unresolved_names` is never written on this path"
    )


def test_a_second_park_after_the_one_ask_still_tracks_only_one_entity_name():
    """Issue #32's literal reproduction: the submitter already replied once, separating the two new
    entities explicitly, and the capture parks again (still unresolved). The steward-facing report
    should keep BOTH names tracked separately (`schema.SITUATION_NAMES_KEY`), exactly as
    `report.triage_entity` already does for the meeting flow's equivalent park. Instead, the
    ordinary `_triage`/`_ask_or_park` pair has no `names` read at all: `parked.get("name")` is
    `None` for this outcome, so it falls back to `schema.UNNAMED_ENTITY_PLACEHOLDER` and BOTH
    real names are lost, not merely folded together — the single-string compound the issue reports
    ("Jack Acme Capital") is the filing AGENT's downstream workaround for this same missing slot.
    """
    outcome = _outcome({"kind": "unresolved-entity", "names": ["Jack", "Acme Capital"]})

    result = processing._triage(ALREADY_ASKED_ITEM, _deps(), outcome)

    assert result.status == schema.TRIAGE
    assert result.report.get(schema.SITUATION_NAMES_KEY) == ["Jack", "Acme Capital"], (
        "both unresolved names should be tracked separately via `schema.SITUATION_NAMES_KEY`, the "
        "way `report.triage_entity` already tracks them on the meeting path"
    )


# ── the far more common single-name park, in the SAME shape ───────────────────────────────────
def test_a_single_unresolved_name_lands_in_the_plural_key_as_a_one_element_list():
    """INVERTED by the plural collapse. This test used to assert the opposite — that one name kept
    landing in the singular `SITUATION_NAME_KEY` with no plural key at all — which was the shape
    the collapse retired. The singular key is now read-only legacy: nothing writes it, and a
    steward's tooling reads one list whatever the count.

    Both halves asserted. A test that only checked the plural key would stay green against an
    implementation writing both, which is exactly the duplication being removed.
    """
    outcome = _outcome({"kind": "unresolved-entity", "name": "Halcyon Grid"})

    result = processing._triage(ALREADY_ASKED_ITEM, _deps(), outcome)

    assert result.status == schema.TRIAGE
    assert result.report.get(schema.SITUATION_NAMES_KEY) == ["Halcyon Grid"]
    assert schema.SITUATION_NAME_KEY not in result.report
    # ...and the SENTENCE is still the one-name one. The count chose the keys before; now it
    # chooses only the prose, and this is the assertion that keeps that true.
    assert 'seems to be about "Halcyon Grid"' in result.report["summary"]
    assert "named 1 things" not in result.report["summary"]


# ── a BLANK name in the list is not a name, and must never reach a human ──────────────────────
# `report.needs_input` and `report.triage_entity` were both WRITTEN to drop blanks — each filters
# its list on `if _clean(n, 120)` / `if _clean_identity(n, 120)`. Neither filter fired, because
# `_clean` is `sanitize` + `clamp` and NEITHER strips surrounding whitespace, so `_clean("   ")` is
# `"   "` — truthy. `processing._triage`'s own `[n for n in names if n]` filter had the same blind
# spot. The intent was already in the code three times over; nothing implemented it, and nothing
# tested it.
def test_a_blank_name_beside_a_real_one_never_reaches_the_submitters_question():
    """The one with real user-visible harm, reproduced live: a park naming "Jack" and a blank asks
    the submitter to place TWO things, and prints the blank as item 2 of a numbered list of things
    "the entity registry doesn't recognize" — a question about a name that was never in his
    material, inside the ONE question the capture ever gets. He cannot answer it, and the capture
    burns its budget on it.

    OLD BEHAVIOUR: on `main` the ordinary flow had no `names` at all, so no ordinary capture could
    reach this text. The meeting flow could, and could before this change too — see the
    pre-existing note in `test_agent_pure.py`. Contract pinned here: a blank is DROPPED, not
    refused (one real name still makes a well-formed park; refusing would spend the corrective
    retry on a formatting artefact), so what is left is ONE name — and after the plural collapse
    that means a ONE-ELEMENT `unresolved_names` list carrying the one-name SENTENCE. The key
    assertion was inverted; the harm this test exists for (the blank reaching a human) is
    unchanged.
    """
    outcome = _outcome({"kind": "unresolved-entity", "names": ["Jack", "   "]})

    result = processing._triage(FRESH_ITEM, _deps(), outcome)

    assert result.status == schema.NEEDS_INPUT
    assert result.report.get("unresolved_names") == ["Jack"]
    assert "unresolved_name" not in result.report
    assert '"   "' not in result.report["summary"]
    assert "2 things" not in result.report["summary"]
    assert 'seems to be about "Jack"' in result.report["summary"]


def test_a_blank_name_beside_a_real_one_never_reaches_the_stewards_park():
    """The same drop on the far side of the one-ask budget. `schema.SITUATION_NAMES_KEY` is what
    `entities.cli` selects approvable subjects from, and a blank subject there is a steward handed
    an inert command block for a name nobody wrote — `_suggestable` refuses it (it strips, then
    sees nothing), so the blank costs a real reviewer a real line of attention and can never
    resolve."""
    outcome = _outcome({"kind": "unresolved-entity", "names": ["Jack", "   "]})

    result = processing._triage(ALREADY_ASKED_ITEM, _deps(), outcome)

    assert result.status == schema.TRIAGE
    assert result.report.get(schema.SITUATION_NAMES_KEY) == ["Jack"]
    assert schema.SITUATION_NAME_KEY not in result.report


def test_the_meeting_flow_drops_a_blank_name_the_same_way():
    """PRE-EXISTING on the meeting flow, landed with its ordinary twin on purpose: `_triage_meeting`
    filters on `if n`, so the blank survives there too and produces the identical two-item question.
    A fix applied only to `_triage` would leave the meeting flow — the flow where a plural park is
    the NORMAL case, not the exception — still shipping it."""
    outcome = _outcome({"kind": "unresolved-entity", "names": ["Jack", "   "]})

    result = processing._triage_meeting(FRESH_ITEM, _deps(), outcome)

    assert result.status == schema.NEEDS_INPUT
    assert result.report.get("unresolved_names") == ["Jack"]
    assert "unresolved_name" not in result.report


# ── benign twins: what must still be ACCEPTED, and how it is normalised ───────────────────────
def test_a_name_that_merely_CONTAINS_whitespace_is_kept_and_its_padding_stripped():
    """Specificity twin for the three drops above — the fix must not over-refuse. Two real names,
    one with a double space inside it and one that arrived padded, both survive and the park is
    still PLURAL.

    This test also DECIDES the normalisation, which nothing pins today: surrounding whitespace is
    stripped, internal whitespace is part of the name. Stripping rather than keeping verbatim,
    because the same value is quoted back at the submitter (`  1. " Jack "`) and offered to a
    steward as `birth.prepare --name`, where `" Jack "` and `"Jack"` mint two different registry
    entities that will never match each other. If the human wants padded names preserved verbatim
    instead, this assertion is the one line to change — say so and it changes.
    """
    outcome = _outcome({"kind": "unresolved-entity", "names": ["  Acme  Capital  ", " Jack "]})

    result = processing._triage(FRESH_ITEM, _deps(), outcome)

    assert result.status == schema.NEEDS_INPUT
    assert result.report.get("unresolved_names") == ["Acme  Capital", "Jack"]


def test_an_inbound_singular_name_strips_the_same_padding_the_plural_one_does():
    """The normalisation above, asked of the OTHER INBOUND shape. A per-shape strip would recreate
    exactly the asymmetry between a singular and a plural name that issue #32 exists to close — one
    padded name would render differently depending on which field carried it. One seam, both
    shapes.

    INVERTED only in its OUTPUT half: `triage.name` is still an accepted INBOUND spelling (this
    function is handed raw park dicts by callers that never crossed `agent.parse_outcome`), but
    what comes out is the one written shape, a list."""
    outcome = _outcome({"kind": "unresolved-entity", "name": " Jack "})

    result = processing._triage(FRESH_ITEM, _deps(), outcome)

    assert result.report.get("unresolved_names") == ["Jack"]
    assert "unresolved_name" not in result.report


# ── one written shape, whichever field the name ARRIVED in ────────────────────────────────────
# INVERTED. This pair used to assert that one name produced the SINGULAR report shape whichever
# field carried it; the collapse reversed the direction, and the property it protects is the same
# one — the INBOUND shape must not be observable in the OUTPUT. Two spellings in, one document
# out. A park whose written shape depended on which field the agent happened to use is precisely
# the asymmetry issue #32 opened and this change closed.
#
# Which private helper ran is deliberately still not asserted: there is one router now, so there
# is nothing to distinguish, and a spy on a private helper freezes an implementation detail as a
# contract. What steward tooling and the eval instrument actually read is the KEY, and that is
# what this pins.
@pytest.mark.parametrize("triage", [
    pytest.param({"kind": "unresolved-entity", "name": "Halcyon Grid"}, id="via triage.name"),
    pytest.param({"kind": "unresolved-entity", "names": ["Halcyon Grid"]},
                 id="via a one-element triage.names"),
])
def test_one_name_lands_in_the_one_written_shape_whichever_field_carried_it(triage):
    asked = processing._triage(FRESH_ITEM, _deps(), _outcome(triage))
    parked = processing._triage(ALREADY_ASKED_ITEM, _deps(), _outcome(triage))

    assert asked.report.get("unresolved_names") == ["Halcyon Grid"]
    assert "unresolved_name" not in asked.report
    assert parked.report.get(schema.SITUATION_NAMES_KEY) == ["Halcyon Grid"]
    assert schema.SITUATION_NAME_KEY not in parked.report


# ── which flow the submitter is told he is in — the flag, where it is THREADED ─────────────────
# `report.needs_input`/`triage_entity` take `meeting=`, and `tests/librarian/
# test_report.py` pins what each value renders. That is the builders' half. THIS is the half that
# breaks silently: the flag is a keyword argument with a default, so a call site that stops passing
# it keeps compiling, keeps passing every builder test, and quietly tells one flow's submitters the
# other flow's story. Both directions are asserted, because only the pair can distinguish "threaded
# correctly" from "hardcoded".
TWO_NAMES = {"kind": "unresolved-entity", "names": ["Jack", "Acme Capital"]}


def test_the_ordinary_ask_never_tells_a_submitter_his_capture_is_a_meeting():
    """An ordinary capture is not a transcript. "The whole meeting parks for a steward" and "a
    meeting page can never link a decision that was never filed" describe consequences that do not
    exist for a note somebody dropped — and a reader who can see the instruction is not about him
    is a reader who stops reading the one question his capture ever gets."""
    result = processing._triage(FRESH_ITEM, _deps(), _outcome(TWO_NAMES))

    assert result.status == schema.NEEDS_INPUT
    assert "meeting" not in result.report["summary"]
    assert "the whole capture parks for a steward" in result.report["summary"]


def test_the_meeting_ask_still_names_the_meeting_and_what_parking_costs():
    """The specificity twin, and the actual regression this pair catches: `_triage_meeting` passes
    `meeting=True` explicitly. Drop that one keyword and this test goes red while every other
    meeting test stays green, because nothing else reads the sentence."""
    result = processing._triage_meeting(FRESH_ITEM, _deps(), _outcome(TWO_NAMES))

    assert result.status == schema.NEEDS_INPUT
    assert "the whole meeting parks for a steward" in result.report["summary"]
    assert "decision that names it" in result.report["summary"]


def test_the_ordinary_steward_park_says_capture_and_the_meeting_one_says_meeting():
    """The same split on the far side of the one-ask budget — the park report, which is what the
    submitter reads when his answer did not resolve either. One assertion per flow, from the two
    routers themselves, so a flag threaded on the ask path but not the park path is visible."""
    ordinary = processing._triage(ALREADY_ASKED_ITEM, _deps(), _outcome(TWO_NAMES))
    meeting = processing._triage_meeting(ALREADY_ASKED_ITEM, _deps(), _outcome(TWO_NAMES))

    assert "place this capture where it actually belongs" in ordinary.report["summary"]
    assert "place this meeting where it actually belongs" in meeting.report["summary"]
    # The FACTS are the same either way — the flag is copy, never structure (see
    # `test_report.py::test_the_flow_flag_changes_the_prose_and_nothing_else`).
    assert ordinary.report[schema.SITUATION_NAMES_KEY] == \
           meeting.report[schema.SITUATION_NAMES_KEY] == ["Jack", "Acme Capital"]


# ── the flag has NO slot in the ONE-name sentence, from the two routers themselves ─────────────
# The three cases above are all two-name parks, which is where the flag has a sentence to change.
# One name is the common shape and the flag has nowhere to land in it — the one-name wording never
# named a flow. That is a property worth pinning from the ROUTERS rather than only from the
# builders (`test_report.py`), because it is the routers that thread the flag: it says the two
# flows hand a one-name submitter the same document, so nobody can "fix" the one-name prose for
# one flow and silently fork it for the other.
ONE_NAME = {"kind": "unresolved-entity", "names": ["Jack"]}


def test_a_one_name_park_reads_identically_from_both_routers():
    ordinary_ask = processing._triage(FRESH_ITEM, _deps(), _outcome(ONE_NAME))
    meeting_ask = processing._triage_meeting(FRESH_ITEM, _deps(), _outcome(ONE_NAME))
    ordinary_park = processing._triage(ALREADY_ASKED_ITEM, _deps(), _outcome(ONE_NAME))
    meeting_park = processing._triage_meeting(ALREADY_ASKED_ITEM, _deps(), _outcome(ONE_NAME))

    assert ordinary_ask.report == meeting_ask.report
    assert ordinary_park.report == meeting_park.report
    # ...and it really is the ONE-name document, not two flows agreeing on the plural one.
    assert 'seems to be about "Jack"' in ordinary_ask.report["summary"]
    assert ordinary_park.report[schema.SITUATION_NAMES_KEY] == ["Jack"]
    assert schema.SITUATION_NAME_KEY not in ordinary_park.report
    assert "meeting" not in meeting_ask.report["summary"]
