"""Issue #32 — `processing._triage` / `_ask_or_park`'s missing plural port, isolated at the
routing layer (`processing.py:691`), independent of whatever `agent.parse_outcome` accepts.

The meeting flow already solved this exact case: `_triage_meeting` reads `outcome.triage["names"]`
and routes through `_ask_or_park_multi`, which keeps two-or-more unresolved names SEPARATE
(`report.needs_input_multi` / `report.triage_entity_multi`, `schema.SITUATION_NAMES_KEY`) rather
than collapsing them into the singular `schema.SITUATION_NAME_KEY` slot. `_triage`/`_ask_or_park`,
the ordinary path's siblings, were never given the same port: they read only `parked.get("name")`.

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
    naming BOTH — `_ask_or_park_multi` already builds this via `report.needs_input_multi` for the
    meeting flow. The ordinary `_triage` never reaches it: it reads only `parked.get("name")`, so
    the second name has nowhere to go and the resulting question is about ONE entity, not two.
    """
    outcome = _outcome({"kind": "unresolved-entity", "names": ["Jack", "Acme Capital"]})

    result = processing._triage(FRESH_ITEM, _deps(), outcome)

    assert result.status == schema.NEEDS_INPUT
    assert result.report.get("unresolved_names") == ["Jack", "Acme Capital"], (
        "the ordinary path's `_triage` does not route a multi-name outcome through "
        "`_ask_or_park_multi`/`report.needs_input_multi` the way `_triage_meeting` already does "
        "for the meeting flow — `unresolved_names` is never written on this path"
    )


def test_a_second_park_after_the_one_ask_still_tracks_only_one_entity_name():
    """Issue #32's literal reproduction: the submitter already replied once, separating the two new
    entities explicitly, and the capture parks again (still unresolved). The steward-facing report
    should keep BOTH names tracked separately (`schema.SITUATION_NAMES_KEY`), exactly as
    `report.triage_entity_multi` already does for the meeting flow's equivalent park. Instead, the
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
        "way `report.triage_entity_multi` already tracks them on the meeting path"
    )


# ── benign twin: the far more common single-name park is unaffected ───────────────────────────
def test_a_single_unresolved_name_still_uses_only_the_singular_field():
    """Benign twin for the two tests above: the fix must not force every ordinary park through the
    plural machinery. One unresolved name keeps landing in the singular `SITUATION_NAME_KEY` slot,
    with no `SITUATION_NAMES_KEY` at all — unchanged before and after a correct fix, exactly as
    `_ask_or_park_multi` itself already promises for its own single-name case ("a single name
    still goes through the SINGULAR builders").
    """
    outcome = _outcome({"kind": "unresolved-entity", "name": "Halcyon Grid"})

    result = processing._triage(ALREADY_ASKED_ITEM, _deps(), outcome)

    assert result.status == schema.TRIAGE
    assert result.report.get(schema.SITUATION_NAME_KEY) == "Halcyon Grid"
    assert schema.SITUATION_NAMES_KEY not in result.report


# ── a BLANK name in the list is not a name, and must never reach a human ──────────────────────
# `report.needs_input_multi` and `report.triage_entity_multi` were both WRITTEN to drop blanks —
# each filters its list on `if _clean(n, 120)` / `if _clean_identity(n, 120)`. Neither filter
# fires, because `_clean` is `sanitize` + `clamp` and NEITHER strips surrounding whitespace, so
# `_clean("   ")` is `"   "` — truthy. `processing._triage`'s own `[n for n in names if n]` filter
# has the same blind spot. The intent is already in the code three times over; nothing implements
# it, and nothing tested it.
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
    retry on a formatting artefact), so what is left is ONE name and it lands in the singular
    report shape.
    """
    outcome = _outcome({"kind": "unresolved-entity", "names": ["Jack", "   "]})

    result = processing._triage(FRESH_ITEM, _deps(), outcome)

    assert result.status == schema.NEEDS_INPUT
    assert result.report.get("unresolved_name") == "Jack"
    assert "unresolved_names" not in result.report
    assert '"   "' not in result.report["summary"]
    assert "2 things" not in result.report["summary"]


def test_a_blank_name_beside_a_real_one_never_reaches_the_stewards_park():
    """The same drop on the far side of the one-ask budget. `schema.SITUATION_NAMES_KEY` is what
    `entities.cli` selects approvable subjects from, and a blank subject there is a steward handed
    an inert command block for a name nobody wrote — `_suggestable` refuses it (it strips, then
    sees nothing), so the blank costs a real reviewer a real line of attention and can never
    resolve."""
    outcome = _outcome({"kind": "unresolved-entity", "names": ["Jack", "   "]})

    result = processing._triage(ALREADY_ASKED_ITEM, _deps(), outcome)

    assert result.status == schema.TRIAGE
    assert result.report.get(schema.SITUATION_NAME_KEY) == "Jack"
    assert schema.SITUATION_NAMES_KEY not in result.report


def test_the_meeting_flow_drops_a_blank_name_the_same_way():
    """PRE-EXISTING on the meeting flow, landed with its ordinary twin on purpose: `_triage_meeting`
    filters on `if n`, so the blank survives there too and produces the identical two-item question.
    A fix applied only to `_triage` would leave the meeting flow — the flow where a plural park is
    the NORMAL case, not the exception — still shipping it."""
    outcome = _outcome({"kind": "unresolved-entity", "names": ["Jack", "   "]})

    result = processing._triage_meeting(FRESH_ITEM, _deps(), outcome)

    assert result.status == schema.NEEDS_INPUT
    assert result.report.get("unresolved_name") == "Jack"
    assert "unresolved_names" not in result.report


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


def test_the_singular_shape_strips_the_same_padding_the_plural_one_does():
    """The normalisation above, asked of the OTHER shape. A per-shape strip would recreate exactly
    the asymmetry between a singular and a plural name that issue #32 exists to close — one padded
    name would render differently depending on which field carried it. One seam, both shapes."""
    outcome = _outcome({"kind": "unresolved-entity", "name": " Jack "})

    result = processing._triage(FRESH_ITEM, _deps(), outcome)

    assert result.report.get("unresolved_name") == "Jack"


# ── G1: making the docs' "a single name, however it arrived" claim checkable ──────────────────
# `docs/reference/librarian.md` claims a single name "still goes through the unchanged singular
# `_ask_or_park`, `report.needs_input` and `schema.SITUATION_NAME_KEY`". The FIRST of those three
# is not observable and is not the contract: `_ask_or_park_multi` delegates to the same singular
# builders for one name (its own docstring says so), so which helper ran leaves no trace anywhere
# a reader, a steward or `entities.cli` can see. Asserting it would need a spy on a private
# helper — an implementation detail frozen as a contract. The other two ARE the contract, and are
# what steward tooling actually reads, so they are what this pins.
@pytest.mark.parametrize("triage", [
    pytest.param({"kind": "unresolved-entity", "name": "Halcyon Grid"}, id="via triage.name"),
    pytest.param({"kind": "unresolved-entity", "names": ["Halcyon Grid"]},
                 id="via a one-element triage.names"),
])
def test_one_name_lands_in_the_singular_report_shape_whichever_field_carried_it(triage):
    """The falsifiable half of the docs' sentence: ONE unresolved name produces the SINGULAR report
    shape — `unresolved_name` / `schema.SITUATION_NAME_KEY`, and no plural key — no matter which
    field it arrived in. That is the part `entities.cli` and the eval instrument depend on; a park
    that wrote the plural key for one name would be scored as a miss by the instrument and would
    offer the steward a differently-shaped subject list for no reason."""
    asked = processing._triage(FRESH_ITEM, _deps(), _outcome(triage))
    parked = processing._triage(ALREADY_ASKED_ITEM, _deps(), _outcome(triage))

    assert asked.report.get("unresolved_name") == "Halcyon Grid"
    assert "unresolved_names" not in asked.report
    assert parked.report.get(schema.SITUATION_NAME_KEY) == "Halcyon Grid"
    assert schema.SITUATION_NAMES_KEY not in parked.report


# ── which flow the submitter is told he is in — the flag, where it is THREADED ─────────────────
# `report.needs_input_multi`/`triage_entity_multi` now take `meeting=`, and `tests/librarian/
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
