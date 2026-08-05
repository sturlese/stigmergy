"""`processing._reusable_outcome`: the re-file reuse's own gate, unit-tested directly — pure, no
DB, no git, no agent double.

`tests/librarian/test_meeting_processing_pg.py` proves the mechanism works end to end over a real
processing pass: park, mint, requeue, re-file, with a `_LossyMeetingAgent` that makes
"the decisions survived" a claim about the reuse rather than about the double happening to be
deterministic. This module is for the branches that suite cannot cheaply reach — same split
`test_refusal_routing.py`'s own docstring states for `_refuse`'s routing: what a real processing
pass can stage belongs in the PG suite; what it cannot (or can only awkwardly, by hand-corrupting
a JSONB column mid-flight) belongs here, proven directly against the real function.

**Why this matters for THIS function specifically.** `_reusable_outcome`'s own docstring: "the row
is a mutable surface — an operator can edit it, a migration can touch it, and a future version of
this code will read rows an older one wrote... A shape problem means 'no reusable outcome', not a
refusal." Every negative branch below is a way that mutable surface can stop agreeing with what
`_reusable_outcome` expects, and each one has to degrade to "read the material again" rather than
raise or silently reuse something it should not.
"""
import pytest

from stigmergy.librarian import processing

MATERIAL = "the archived transcript, byte for byte, exactly as it was when it was parked"
REPLY = "the submitter's reply on file, if any"

VALID_RAW = {
    "decision": "file",
    "meeting_title": "Q3 Sync",
    "decisions": [{"title": "keep the fast lane running as it is", "body": "some body text",
                   "anchoring": {}}],
}


def _stored(raw=None, *, version=None, material=MATERIAL, reply=REPLY) -> dict:
    """The exact shape `_with_park_outcome` writes onto `capture_queue.outcome` — built the same
    way the production code builds it (`processing._sha`), never a hand-typed hash."""
    return {
        "version": processing.OUTCOME_REUSE_VERSION if version is None else version,
        "material_sha256": processing._sha(material),
        "reply_sha256": processing._sha(reply),
        "raw": VALID_RAW if raw is None else raw,
    }


def _item(outcome=None, reply=REPLY) -> dict:
    return {"id": 1, "outcome": outcome, "reply": reply}


# ── the benign twin every negative case below needs ────────────────────────────────────────────
def test_a_matching_stored_outcome_is_reused():
    """Material and reply both unchanged, a well-formed `file` outcome with a real decision —
    reuse must fire, and fire with the PARSED outcome, not the raw dict."""
    outcome, why_not = processing._reusable_outcome(_item(_stored()), MATERIAL)
    assert why_not == ""
    assert outcome is not None and outcome.decision == "file"
    assert [d["title"] for d in outcome.decisions] == ["keep the fast lane running as it is"]


def test_no_stored_outcome_at_all_is_not_a_refusal_just_nothing_to_reuse():
    """Every capture ever written before this column existed, and every capture that has never
    been parked: `outcome is None` with no explanation, not a refusal — the honest fallback IS the
    model, which is what would have run anyway."""
    outcome, why_not = processing._reusable_outcome(_item(None), MATERIAL)
    assert outcome is None and why_not == ""


@pytest.mark.parametrize("bad", [None, "", 0, [], "a string, not a mapping"])
def test_a_non_dict_outcome_column_degrades_to_nothing_stored(bad):
    """The column is JSONB and nothing at the database level stops a future write (an operator, a
    migration, a bug) from putting something other than the reuse shape there. A non-mapping must
    be read as "nothing to reuse", never raise into the caller."""
    outcome, why_not = processing._reusable_outcome(_item(bad), MATERIAL)
    assert outcome is None and why_not == ""


def test_a_stored_outcome_from_an_older_schema_version_is_not_reused():
    """`OUTCOME_REUSE_VERSION` exists precisely so a future shape change can refuse to reuse what
    an older version wrote rather than silently misreading it — the version check has to actually
    gate, not just exist as a documented intention."""
    outcome, why_not = processing._reusable_outcome(_item(_stored(version=0)), MATERIAL)
    assert outcome is None
    assert "version" in why_not


def test_a_stored_outcome_whose_material_no_longer_matches_is_not_reused():
    """The reuse PRECONDITION, not provenance decoration (`_with_park_outcome`'s own docstring): a
    distillation is a function of the material it was produced from, so a change to the archived
    bytes invalidates it — proven here by handing `_reusable_outcome` different material than the
    stored digest was computed over."""
    outcome, why_not = processing._reusable_outcome(_item(_stored()), "a different transcript entirely")
    assert outcome is None
    assert "material" in why_not


def test_a_stored_outcome_whose_reply_no_longer_matches_is_not_reused():
    """The other reuse precondition — a submitter's reply is INPUT to the distillation
    (`agent.build_prompt` hands it to the model), and on a real walk the second pass comes AFTER
    the submitter's `brain_reply`, so this is the live case the PG suite's own
    `test_a_changed_reply_re_runs_the_model_rather_than_reusing` exercises end to end; this is the
    same gate isolated to the one function that decides it."""
    stored = _stored(reply="the reply that was on file when this outcome was parked")
    outcome, why_not = processing._reusable_outcome(
        _item(stored, reply="a NEW reply the submitter just sent"), MATERIAL)
    assert outcome is None
    assert "reply" in why_not


def test_a_stored_outcome_that_no_longer_shape_validates_is_not_reused():
    """"A shape problem means 'no reusable outcome', not a refusal" — the row's own docstring
    promise, exercised with a `raw` that is missing `meeting_title`, which `parse_meeting_outcome`
    demands for a `file` decision. Must degrade cleanly, never raise `OutcomeShapeError` into the
    caller (`_run_meeting_in_worktree` has no handler for it at that call site — it is caught
    HERE, precisely so the caller does not need one)."""
    unparseable = _stored(raw={"decision": "file", "decisions": [{"title": "x", "body": "y"}]})
    outcome, why_not = processing._reusable_outcome(_item(unparseable), MATERIAL)
    assert outcome is None
    assert "no longer validates" in why_not


def test_a_stored_triage_outcome_is_not_reused():
    """`_with_park_outcome` never stores one of these in production (it only keeps `decision ==
    "file"` outcomes) — this is the defensive read-back half of that same rule, for a row a
    migration or a hand-edit could still present to a future pass."""
    triage_raw = {"decision": "triage", "triage": {"kind": "unresolved-entity", "names": ["X"]}}
    outcome, why_not = processing._reusable_outcome(_item(_stored(raw=triage_raw)), MATERIAL)
    assert outcome is None
    assert "no decisions to file" in why_not


def test_a_stored_file_outcome_with_no_decisions_is_not_reused():
    """A DIFFERENT guard than the shape-validation test above: `{"decision": "file", "decisions":
    []}` shape-VALIDATES cleanly (`parse_meeting_outcome` never requires a non-empty list), so this
    exercises `_reusable_outcome`'s own separate `_decision_titles(outcome)` check — an empty
    distillation is not worth a reuse, matching `_with_park_outcome`'s own exclusion (a `file`
    outcome with no decisions is never stored in the first place)."""
    empty_raw = {"decision": "file", "meeting_title": "T", "decisions": []}
    outcome, why_not = processing._reusable_outcome(_item(_stored(raw=empty_raw)), MATERIAL)
    assert outcome is None
    assert "no decisions to file" in why_not
