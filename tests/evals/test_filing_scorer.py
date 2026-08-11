"""`evals/run_filing.py`'s scorer — the half of the filing instrument with no key, no Postgres,
no git and no model in it.

The runner itself is the test at system level and runs by hand; `score_phase`/`aggregate`/`render`
are a small pure library, and they are where a yardstick can lie. Everything here scores the REAL
expectations out of `evals/filing/expected/expectations.json` through the SAME functions a paid
run calls — the canned half is only the `observed` dict, which
`test_filing_observation_contract.py` builds from real production reports instead.

Two properties carry the whole design and each is tested from both sides:

* **Per facet, never one number.** A capture is scored on exactly the facets its expectation
  NAMES, each keeping its own denominator. A facet that silently leaves a denominator is a score
  that rose because a capture stopped being counted.
* **The instrument has to be able to say NO.** Every facet is mutated one at a time below and must
  report exactly itself as a miss, with its benign twin — the unmutated observation — scoring
  every facet True. A permanently-green instrument is worse than none: it reads as evidence.
"""
import json
import re
from pathlib import Path

import pytest

from evals import bars, eval_history, run_filing
from stigmergy.capture import schema

ROOT = Path(__file__).resolve().parents[2]
EXPECTATIONS = ROOT / "evals" / "filing" / "expected" / "expectations.json"
EVALS_README = ROOT / "evals" / "README.md"
EVALS_INDEX = ROOT / "evals" / "index.md"

# The per-facet denominators of the golden set as it stands: 10 captures, 12 scored phases.
# Pinned rather than derived, so ADDING A CAPTURE fails this test — which is the intent. A new
# capture that names a facet changes that facet's denominator, and scores recorded before and
# after are then comparable per facet but not per run (`evals/README.md`'s growth protocol). That
# is a decision to make consciously, in the same commit, not a number to discover later in a diff.
#
# Written out here rather than imported from `run_filing.EXPECTED_DENOMINATORS` on purpose: this
# copy was counted by hand off the expectations file, so the two pins are INDEPENDENT and the test
# below is a genuine agreement between them. Importing the runner's number would make every
# assertion in this file a tautology the day somebody edits it to match a broken set.
#
# `edits` sits at 1 rather than 2 because F01 stopped naming the facet, and that is a CONTRACT
# CHANGE with evidence behind it rather than a pin relaxed to make something pass: the first
# Sonnet-5 baseline — run before any number was recorded — declared a reciprocal link on the page
# F01's material openly continues, which is correct filing, and the `edits: []` it was scored
# against asserted an assumption about how one backend would file. Under the containment rule that
# replaced list equality, an empty list is true for every backend anyway, so the honest spelling of
# "this capture owes no edit" is silence. `expected/expectations.json`'s F01 `why` note is the
# source of truth; `test_no_expectation_names_an_empty_edits_list` keeps the trap from returning.
DENOMINATORS = {"status": 12, "reason": 1, "type": 9, "folder": 9, "anchor": 7, "edits": 1,
                "park_question": 2, "decisions": 2, "reuse": 1, "attempts": 8, "bounces": 8}
CAPTURES, PHASES = 10, 12


@pytest.fixture(scope="module")
def entries():
    return json.loads(EXPECTATIONS.read_text(encoding="utf-8"))["expectations"]


def _perfect(expect: dict) -> dict:
    """The observation a flawless backend would leave for `expect`.

    A translation into the OBSERVED shape — paths for titles, a `preserved` flag for
    `decisions_preserved` — and deliberately not a copy of `score_phase`'s comparisons: where the
    scorer is allowed to be generous (word order, list order) this builds the awkward spelling on
    purpose, so a scorer that quietly became literal fails here.
    """
    observed = {facet: expect[facet] for facet in
                ("status", "reason", "type", "folder", "anchor", "attempts", "bounces")
                if facet in expect}
    if "edits" in expect:
        observed["edits"] = list(reversed(expect["edits"]))
    if "park_question" in expect:
        observed["park_question"] = list(expect["park_question"])
    if "decisions" in expect:
        observed["decisions"] = [
            {"path": f"wiki/decisions/{decision['title']} agreed on the call.md",
             "anchor": decision.get("anchor", {"kind": "company", "ids": []})}
            for decision in reversed(expect["decisions"])]
    if "reuse" in expect:
        observed["reuse"] = {"preserved": expect["reuse"]["decisions_preserved"],
                             "reused": False, "redistilled": True, "dropped": []}
    return observed


def _phases(entries: list) -> list:
    """Every scored phase of the golden set, observed perfectly — built through the runner's own
    `_phase`, the same composer `_drive` uses on a real run."""
    out = []
    for entry in entries:
        parks = "reply" in entry
        out.append(run_filing._phase(entry["id"], "park" if parks else "only",
                                     entry["expect"], _perfect(entry["expect"])))
        if parks:
            out.append(run_filing._phase(entry["id"], "after_reply", entry["after_reply"],
                                         _perfect(entry["after_reply"])))
    return out


def _block_naming(entries: list, facet: str) -> tuple:
    """The first expectation block in the golden set that names `facet`, with its capture id."""
    for entry in entries:
        for block in [entry["expect"]] + ([entry["after_reply"]] if "after_reply" in entry else []):
            if facet in block:
                return entry["id"], block
    raise AssertionError(f"no expectation in the golden set names {facet!r}")


# ── AC3: the same stored outcomes always score the same ────────────────────────────────────────

def test_scoring_the_same_phases_twice_is_byte_identical(entries):
    """Determinism is what makes two reports diffable — the instrument's own claim that "did this
    change move anything?" is answerable with `diff` rather than by eye."""
    first = json.dumps(_phases(entries), sort_keys=True, default=str)
    second = json.dumps(_phases(entries), sort_keys=True, default=str)
    assert first == second


def test_aggregating_the_same_phases_twice_is_byte_identical(entries):
    """`wall_s` is an argument rather than a clock read inside `aggregate`, which is what lets the
    whole report be compared byte for byte."""
    def report():
        return json.dumps(run_filing.aggregate(_phases(entries), backend="double", model="m",
                                               wall_s=12.5), sort_keys=True, default=str)
    assert report() == report()


def test_the_score_of_a_facet_does_not_depend_on_the_order_of_its_lists(entries):
    """Anchor ids, declared edits and a meeting's decision pages are SETS in everything but their
    spelling. A scorer that compared them positionally would report misses for correct filings —
    the dangerous direction for a number a release decision reads."""
    assert run_filing.score_phase(
        {"anchor": {"kind": "entity", "ids": ["a", "b"]}, "edits": ["p.md", "q.md"]},
        {"anchor": {"kind": "entity", "ids": ["b", "a"]}, "edits": ["q.md", "p.md"]},
    ) == {"anchor": True, "edits": True}


# ── AC4: every capture names its facets, and each facet keeps its own denominator ──────────────

def test_a_capture_is_scored_on_exactly_the_facets_its_expectation_names(entries):
    for entry in entries:
        for block in [entry["expect"]] + ([entry["after_reply"]] if "after_reply" in entry else []):
            scored = run_filing.score_phase(block, _perfect(block))
            assert set(scored) == set(block) & set(run_filing.FACETS), entry["id"]


def test_a_facet_an_expectation_is_silent_about_is_absent_rather_than_false(entries):
    """Silence is not a miss. F04 (`rejected`, pre-agent) names no anchor, so a run that produced
    a wrong anchor for it must not enter the anchor denominator at all — otherwise every capture
    added to probe one facet dilutes every other one."""
    _, block = _block_naming(entries, "reason")
    observed = dict(_perfect(block), anchor={"kind": "entity", "ids": ["marlowe-publishing"]},
                    decisions=[{"path": "wiki/decisions/Something.md"}], edits=["wiki/notes/X.md"])
    scored = run_filing.score_phase(block, observed)
    assert "anchor" not in scored and "decisions" not in scored and "edits" not in scored
    assert scored["reason"] is True


def test_the_per_facet_denominators_of_the_golden_set_are_the_pinned_ones(entries):
    """Fails when a capture is added or an expectation stops naming a facet. Update it in the
    same commit, on purpose — see `DENOMINATORS`."""
    report = run_filing.aggregate(_phases(entries), backend="double", model="m", wall_s=1.0)
    assert {name: row["of"] for name, row in report["facets"].items()} == DENOMINATORS
    assert report["counts"] == {"captures": CAPTURES, "phases": PHASES}


def test_the_runners_own_denominator_pin_agrees_with_this_files(entries):
    """Two pins, counted independently, of the number a paid run is refused over
    (`_check_set`'s fourth refusal). They agree, or one of them was edited to make something pass
    and the golden set's size is no longer a fact anybody checked."""
    assert run_filing.EXPECTED_DENOMINATORS == DENOMINATORS
    assert run_filing._denominators({"expectations": entries}) == DENOMINATORS


def test_the_report_carries_no_single_quality_number(entries):
    """The whole posture: a backend that starts filing everything as a note must not be able to
    hide that behind a rising anchor score, and the surest way to lose that is one mean over all
    facets appearing 'for convenience'."""
    report = run_filing.aggregate(_phases(entries), backend="double", model="m", wall_s=1.0)
    assert "score" not in report and "pass" not in report
    for row in report["facets"].values():
        assert set(row) == {"hits", "of", "score", "bar", "pass"}


def test_one_facet_collapsing_leaves_every_other_facet_untouched(entries):
    """Two phases, one of them filed as the wrong type: `type` drops to 0.50 and fails ITS OWN
    bar, while every other row — score and verdict alike — is byte-identical to the healthy run.

    Now that the bars are real numbers, this is the property that matters most about them: a
    backend that starts filing everything as a note must fail `type` and nothing else. Asserted as
    a diff against the healthy aggregate rather than facet by facet, so a future change that let
    one facet's collapse bleed into another's verdict cannot pass by being missed in the list.
    """
    _, block = _block_naming(entries, "anchor")
    good = run_filing._phase("A", "only", block, _perfect(block))
    bad = run_filing._phase("B", "only", block, dict(_perfect(block), type="concept"))

    healthy = run_filing.aggregate([good, good], backend="double", model="m",
                                   wall_s=1.0)["facets"]
    collapsed = run_filing.aggregate([good, bad], backend="double", model="m",
                                     wall_s=1.0)["facets"]

    assert healthy["type"]["pass"] is True
    assert collapsed["type"] == {"hits": 1, "of": 2, "score": 0.5,
                                 "bar": bars.FILING_BARS["type"], "pass": False}
    assert {name: row for name, row in collapsed.items() if name != "type"} == \
           {name: row for name, row in healthy.items() if name != "type"}


# ── AC7: sensitivity, and the benign twin that keeps it honest ─────────────────────────────────

MUTATIONS = {
    "status": lambda o: dict(o, status=schema.TRIAGE),
    "reason": lambda o: dict(o, reason=schema.REASON_SECRET),
    "type": lambda o: dict(o, type="concept"),
    "folder": lambda o: dict(o, folder="wiki/concepts"),
    # The set's sharpest probe, mutated the way a real backend gets it wrong: a plausible
    # neighbour from the same registry, not a nonsense id.
    "anchor": lambda o: dict(o, anchor={"kind": "entity", "ids": ["quillon-labs"]}),
    # The owed page untouched and a DIFFERENT one edited instead. Adding a path beside the owed
    # one is forgiven by design now (containment — `_edits_match`), so the mutation that proves
    # this facet still discriminates has to remove the one edit the material actually owed.
    "edits": lambda o: dict(o, edits=["wiki/decisions/Warehouse Slotting Policy.md"]),
    "park_question": lambda o: dict(o, park_question=["Northwind Freight"]),
    "decisions": lambda o: dict(o, decisions=[*o["decisions"],
                                              {"path": "wiki/decisions/A third decision.md",
                                               "anchor": {"kind": "company", "ids": []}}]),
    "reuse": lambda o: dict(o, reuse={"preserved": False, "reused": False, "redistilled": True,
                                      "dropped": ["a decision that went missing"]}),
    "attempts": lambda o: dict(o, attempts=o["attempts"] + 1),
    "bounces": lambda o: dict(o, bounces=o["bounces"] + 1),
}


# A mutation is only meaningful against the block it was written for. `edits` is the case that
# needs saying: the mutation above names a page that is NOT the one F03's material owes, and a
# capture added later that also names `edits` would silently become `_block_naming`'s first hit —
# mutated with a path its own expectation might legitimately contain, and the test would pass
# while proving nothing.
SENSITIVITY_TARGETS = {"edits": "F03-declared-edit-related-growth"}


@pytest.mark.parametrize("facet", sorted(MUTATIONS))
def test_every_facet_reports_a_deliberate_error_as_its_own_miss(facet, entries):
    """One mutation per facet, each proving that facet is LIVE — and that it does not drag any
    other facet down with it.

    This is the test the instrument is worth nothing without. A facet whose comparison silently
    stopped discriminating (a key renamed in a report, a matcher that became `True`-by-default)
    keeps printing a score, keeps filling a denominator, and reads to every future reader as
    evidence that the thing was measured. A permanently-green instrument is worse than no
    instrument, because nobody goes looking for the measurement it already appears to have.

    It earned its keep on `edits`: when that facet moved from list equality to containment, the
    old mutation (add a path) stopped failing and this test said so, instead of the facet quietly
    becoming unfalsifiable.

    The benign twin is the test below: the same fixtures, unmutated, score every facet True.
    """
    capture_id, block = _block_naming(entries, facet)
    if facet in SENSITIVITY_TARGETS:
        assert capture_id == SENSITIVITY_TARGETS[facet], (
            f"the {facet} mutation was written against {SENSITIVITY_TARGETS[facet]} and is now "
            f"being applied to {capture_id} — check it still proves anything there")
    scored = run_filing.score_phase(block, MUTATIONS[facet](_perfect(block)))
    assert scored[facet] is False, f"{capture_id}: mutating {facet} did not fail it"
    assert [name for name, ok in scored.items() if not ok] == [facet], (
        f"{capture_id}: mutating {facet} also moved another facet")


def test_the_unmutated_observation_scores_every_facet_of_every_phase_true(entries):
    """The benign twin of the whole sensitivity table above. Without it, a scorer stuck on False
    would pass every mutation test in this file — measuring its sensitivity and never its
    specificity, while every real filing it ever judged would read as a miss."""
    report = run_filing.aggregate(_phases(entries), backend="double", model="m", wall_s=1.0)
    misses = [(phase["id"], name) for phase in report["phases"]
              for name, ok in phase["facets"].items() if not ok]
    assert not misses
    assert {name: row["score"] for name, row in report["facets"].items()} == \
           dict.fromkeys(DENOMINATORS, 1.0)


def test_a_reciprocal_link_the_backend_never_declared_is_a_miss(entries):
    """The other direction of the same facet, and the one the golden set exists to catch: the
    material continues an account an existing page already holds, and the backend filed a new page
    without recognising that the old one was owed a link back. Nothing bounces — the capture files
    perfectly well — so only this facet can see it."""
    owed = [(e["id"], e["expect"]) for e in entries if e["expect"].get("edits")]
    assert owed, "the golden set has stopped measuring declared edits"
    for capture_id, block in owed:
        assert run_filing.score_phase(block, dict(_perfect(block), edits=[]))["edits"] is False, \
            capture_id


def test_an_edit_the_expectation_never_named_does_not_fail_the_facet():
    """The containment rule itself, and the reason it is not a relaxation of standards.

    An extra declared edit is an additive `related:` link or a callout on an existing in-lane page
    (`edits.validate` refuses anything else: a path outside the fast lane's folders, a page this
    capture created, a symlink, a dead link), judged by `gate_zone` and `gate_body_rewrite` like
    any other diff. So a backend that cross-links more generously than the yardstick anticipated
    has done something already proved harmless, and list equality was scoring the yardstick's
    imagination — which is exactly what the first Sonnet-5 baseline caught, twice.
    """
    assert run_filing.score_phase({"edits": ["a.md"]}, {"edits": ["a.md", "b.md"]})["edits"] is True


def test_an_owed_edit_missing_from_a_pile_of_others_is_still_a_miss():
    """Containment forgives extras, never a substitution. A backend that edited two OTHER pages and
    left the one its material owed untouched has not been generous — it missed the debt, and a
    facet that counted edits rather than checking for the named one would call that a pass."""
    assert run_filing.score_phase({"edits": ["a.md"]},
                                  {"edits": ["b.md", "c.md"]})["edits"] is False


def test_the_same_page_edited_twice_still_satisfies_the_expectation_once():
    """`report["pages_edited"]` is a list, not a set: two declared edits to one page (a `related:`
    link and an overlap callout) arrive as two entries. The facet asks whether the page was
    touched, so a duplicate must not read as a different page — and must not fail."""
    assert run_filing.score_phase({"edits": ["a.md"]},
                                  {"edits": ["a.md", "a.md"]})["edits"] is True


def test_a_backend_that_never_parked_scores_the_after_reply_phase_as_a_miss(entries):
    """`_drive` records the second phase of a park as a MISS rather than skipping it. A vanishing
    phase would shrink its facets' denominators and quietly raise the score of a backend that
    never asked — the failure mode that turns 'refuses to ask' into 'files everything well'."""
    parking = [e for e in entries if "after_reply" in e]
    assert parking, "the golden set has stopped measuring the ask-back loop"
    for entry in parking:
        block = entry["after_reply"]
        scored = run_filing.score_phase(block, {"status": "",
                                                "note": "never parked, so no reply was possible"})
        assert set(scored) == set(block) & set(run_filing.FACETS), entry["id"]
        assert not any(scored.values()), entry["id"]


# ── the anchor, which is four different answers and not two ───────────────────────────────────

ANCHORS = [
    {"kind": "entity", "ids": ["northwind-freight"]},
    {"kind": "entity", "ids": ["quillon-labs"]},
    {"kind": "company", "ids": []},           # a checked, explicit company-wide claim
    {"kind": "none", "ids": []},              # nothing resolved: the same `entity: []` on disk
]


@pytest.mark.parametrize("expected", ANCHORS)
@pytest.mark.parametrize("observed", ANCHORS)
def test_an_anchor_matches_itself_and_nothing_else(expected, observed):
    """`kind` alone would fold a wrong entity into 'anchored'; `ids` alone would call a
    company-wide page and an unresolved one the same thing. Both halves, always."""
    scored = run_filing.score_phase({"anchor": expected}, {"anchor": observed})
    assert scored["anchor"] is (expected == observed)


def test_a_missing_anchor_is_a_miss_rather_than_a_crash():
    """A refusal carries no anchor at all. The scorer has to answer False, not raise — a run that
    died on phase three would lose the nine captures behind it."""
    assert run_filing.score_phase({"anchor": ANCHORS[0]}, {})["anchor"] is False


# ── the two loose matchers, loose in ONE direction only ───────────────────────────────────────

@pytest.mark.parametrize("expected, path", [
    ("Northwind second wave", "wiki/decisions/Second wave sequencing for Northwind.md"),
    ("Northwind second wave", "wiki/decisions/Northwind second wave goes depot by depot.md"),
    ("review checklist", "wiki/decisions/A shared checklist of what every review covers.md"),
])
def test_a_title_matches_however_the_backend_ordered_or_padded_its_words(expected, path):
    """`evals/README.md` records what a literal expectation cost the QA golden
    (`globex-meeting-budget`, a question demanding a word an answer was free to paraphrase
    around). A decision titled "Second wave sequencing for Northwind" is the same decision as
    "Northwind second wave", and an instrument that scored the first a miss would be measuring
    word order."""
    assert run_filing.title_matches(expected, path) is True


@pytest.mark.parametrize("expected, path", [
    ("Northwind second wave", "wiki/decisions/Northwind rollout continues.md"),
    ("Northwind second wave", "wiki/decisions/Second wave for Quillon.md"),
    ("Northwind second wave depots", "wiki/decisions/Northwind second wave.md"),
    ("", "wiki/decisions/Anything at all.md"),
    ("the decisions of the meeting", "wiki/decisions/Something unrelated entirely.md"),
])
def test_a_title_that_is_missing_a_significant_word_is_still_a_miss(expected, path):
    """Generous one way only: a page may say MORE than the expectation and never less. The last
    two rows are the ones that keep the matcher from degenerating — an empty expectation and one
    made only of stopwords match nothing, rather than matching everything."""
    assert run_filing.title_matches(expected, path) is False


def test_a_park_question_is_matched_against_the_whole_question_the_report_asked():
    """The report renders one sentence naming every unresolved name; scoring the list shape would
    measure `report.py`'s wording instead of whether the right thing was unresolved."""
    assert run_filing.score_phase(
        {"park_question": ["Halcyon Grid"]},
        {"park_question": ["the Halcyon Grid pilot"]})["park_question"] is True


@pytest.mark.parametrize("observed", [[], ["Northwind Freight"], ["Halcyon"]])
def test_a_park_that_asked_about_the_wrong_thing_or_nothing_is_a_miss(observed):
    assert run_filing.score_phase({"park_question": ["Halcyon Grid"]},
                                  {"park_question": observed})["park_question"] is False


def test_a_park_expectation_naming_every_unresolved_name_needs_all_of_them_asked():
    """A meeting parks the WHOLE capture in ONE ask naming every unresolved name — a partial page
    set is worse than an honest park, so a question that dropped one of two names is a miss."""
    expect = {"park_question": ["Project Wren", "Halcyon Grid"]}
    assert run_filing.score_phase(expect, {"park_question": ["Project Wren"]})["park_question"] \
        is False
    assert run_filing.score_phase(
        expect, {"park_question": ["Project Wren", "Halcyon Grid"]})["park_question"] is True


# ── a meeting's decision pages: count, anchors, and one-to-one ────────────────────────────────

TWO_DECISIONS = [{"title": "Northwind second wave",
                  "anchor": {"kind": "entity", "ids": ["northwind-freight"]}},
                 {"title": "review checklist", "anchor": {"kind": "company", "ids": []}}]


def _page(title, anchor):
    return {"path": f"wiki/decisions/{title}.md", "anchor": anchor}


def test_a_meeting_that_split_two_decisions_into_three_pages_is_a_miss():
    """Five pages where two were expected has not over-delivered — it has fragmented one decision
    into pieces that each anchor separately, which is the granularity failure the meeting brief
    spends a section on."""
    observed = [_page("Northwind second wave", {"kind": "entity", "ids": ["northwind-freight"]}),
                _page("review checklist", {"kind": "company", "ids": []}),
                _page("Northwind second wave timing", {"kind": "company", "ids": []})]
    assert run_filing.score_phase({"decisions": TWO_DECISIONS},
                                  {"decisions": observed})["decisions"] is False


def test_two_expected_decisions_cannot_both_be_satisfied_by_one_page():
    """Matching is greedy and one-to-one. Without that, a single page titled to cover both
    expectations would score two hits — and a backend that merged two decisions into one page
    would read as the correct answer."""
    both = _page("Northwind second wave and the review checklist",
                 {"kind": "entity", "ids": ["northwind-freight"]})
    observed = [both, _page("Something else entirely", {"kind": "company", "ids": []})]
    assert run_filing.score_phase({"decisions": TWO_DECISIONS},
                                  {"decisions": observed})["decisions"] is False


def test_each_decision_page_is_matched_against_its_OWN_anchor():
    """One of these two belongs to an entity and the other to nobody. A single-anchor
    implementation cannot express that, and a scorer that checked only titles could not see it."""
    swapped = [_page("Northwind second wave", {"kind": "company", "ids": []}),
               _page("review checklist", {"kind": "entity", "ids": ["northwind-freight"]})]
    assert run_filing.score_phase({"decisions": TWO_DECISIONS},
                                  {"decisions": swapped})["decisions"] is False


def test_the_order_the_decision_pages_were_written_in_does_not_matter():
    right = [_page("review checklist", {"kind": "company", "ids": []}),
             _page("Northwind second wave", {"kind": "entity", "ids": ["northwind-freight"]})]
    assert run_filing.score_phase({"decisions": TWO_DECISIONS},
                                  {"decisions": right})["decisions"] is True


def test_a_decision_expectation_that_names_no_anchor_scores_the_title_alone():
    """The re-file after a park is scored on whether the decisions came back at all; pinning
    their anchors there would score the steward's mint a second time."""
    loose = [{"title": "Wren tracked formally"}, {"title": "summary joins the shared"}]
    observed = [_page("Wren tracked formally in the registry", {"kind": "company", "ids": []}),
                _page("Wren summary joins the shared digest",
                      {"kind": "entity", "ids": ["quillon-labs"]})]
    assert run_filing.score_phase({"decisions": loose},
                                  {"decisions": observed})["decisions"] is True


# ── reuse: what a park cost the capture, not whether the model ran ────────────────────────────

def test_the_reuse_facet_scores_whether_a_decision_was_LOST_and_not_whether_a_pass_was_saved():
    """The deliberate weakening, pinned so it cannot be silently re-tightened. Whether a park
    leaves a reusable distillation is decided by HOW it parked, and an agent following the meeting
    brief parks with `decision: "triage"`, storing nothing — so scoring `reused` would mark a
    brief-following backend down for following the brief. Losing a decision is the failure on both
    roads, so that is the scored half; `reused`/`redistilled` ride along unscored."""
    expect = {"reuse": {"decisions_preserved": True}}
    redistilled = {"reuse": {"preserved": True, "reused": False, "redistilled": True,
                             "dropped": []}}
    reused = {"reuse": {"preserved": True, "reused": True, "redistilled": False, "dropped": []}}
    assert run_filing.score_phase(expect, redistilled)["reuse"] is True
    assert run_filing.score_phase(expect, reused)["reuse"] is True
    assert run_filing.score_phase(expect, {"reuse": {"preserved": False, "reused": True,
                                                     "dropped": ["one that vanished"]}})["reuse"] \
        is False


def test_a_re_file_that_reported_no_reuse_block_at_all_is_a_miss_when_preservation_was_expected():
    """A phase with nothing observable about the distillation cannot be credited with having
    preserved it — the missing observation is the failure, not the absence of one."""
    assert run_filing.score_phase({"reuse": {"decisions_preserved": True}}, {})["reuse"] is False


# ── the bars: None means REPORT, DO NOT JUDGE ─────────────────────────────────────────────────
# The shipped bars are now the Sonnet-5 baseline's own scores, so `None` is no longer the state
# the table ships in — but it is still the state every NEW facet is born in: `aggregate` looks a
# facet up with `FILING_BARS.get(name)`, and a facet added before its baseline exists gets `None`
# from that lookup, exactly as all nine did before 2026-08-10. That is what these two tests drive
# now, by injection rather than by shipping uncalibrated.

def test_an_uncalibrated_facet_reports_a_verdict_that_is_None_and_never_truthy(entries,
                                                                              monkeypatch):
    """A bar of `None` means REPORT, DO NOT JUDGE. An instrument that answered "fine" about a
    facet whose baseline nobody has measured would be worse than one that says nothing — so `pass`
    must be None, not True, and not merely 'not False'."""
    for facet in run_filing.QUALITY_FACETS:
        monkeypatch.setitem(bars.FILING_BARS, facet, None)
    report = run_filing.aggregate(_phases(entries), backend="double", model="m", wall_s=1.0)
    assert report["facets"], "the aggregate scored nothing — this check lost its subject"
    for name, row in report["facets"].items():
        assert row["bar"] is None and row["pass"] is None, name
        assert row["pass"] is not True and not bool(row["pass"]), name


def test_the_two_cost_facets_carry_no_bar_even_once_the_quality_bars_are_calibrated(
        entries, monkeypatch):
    """A backend reaching the same page in two agent passes is more expensive at filing, not worse
    at it. Folding that into a quality bar would measure two things through one number."""
    for facet in run_filing.QUALITY_FACETS:
        monkeypatch.setitem(bars.FILING_BARS, facet, 0.90)
    report = run_filing.aggregate(_phases(entries), backend="double", model="m", wall_s=1.0)
    for facet in run_filing.COST_FACETS:
        assert report["facets"][facet]["bar"] is None
        assert report["facets"][facet]["pass"] is None


def test_a_calibrated_bar_judges_in_both_directions(entries, monkeypatch):
    """The benign twin of the `None` test: proof that `pass` is None because the bar is unset and
    not because the comparison stopped working. When the baseline lands, this is the behaviour the
    numbers inherit."""
    _, block = _block_naming(entries, "type")
    good = run_filing._phase("A", "only", block, _perfect(block))
    bad = run_filing._phase("B", "only", block, dict(_perfect(block), type="concept"))
    monkeypatch.setitem(bars.FILING_BARS, "type", 0.90)

    assert run_filing.aggregate([good], backend="b", model="m",
                                wall_s=1.0)["facets"]["type"]["pass"] is True
    assert run_filing.aggregate([good, bad], backend="b", model="m",
                                wall_s=1.0)["facets"]["type"]["pass"] is False


# ── the table a human reads ───────────────────────────────────────────────────────────────────

def test_the_table_prints_every_scored_facet_with_its_own_denominator(entries):
    report = run_filing.aggregate(_phases(entries), backend="double", model="m", wall_s=3.0)
    rows = {}
    for line in run_filing.render(report).splitlines():
        words = line.strip().removeprefix("cost ").split()
        if len(words) > 2 and words[0] in DENOMINATORS:
            rows[words[0]] = line
    for name, of in DENOMINATORS.items():
        assert name in rows, f"{name} is scored but never printed"
        assert f"[{of}/{of}]" in rows[name], rows[name]


def test_an_uncalibrated_table_claims_neither_PASS_nor_FAIL(entries, monkeypatch):
    """The render side of REPORT, DO NOT JUDGE: a facet with no bar prints its score, its
    denominator and no verdict — never a blank that reads as a pass."""
    for facet in run_filing.QUALITY_FACETS:
        monkeypatch.setitem(bars.FILING_BARS, facet, None)
    rendered = run_filing.render(run_filing.aggregate(_phases(entries), backend="double",
                                                      model="m", wall_s=3.0))
    assert "PASS" not in rendered and "FAIL" not in rendered
    assert "no bar — baseline not yet fixed" in rendered


def test_the_calibrated_table_prints_PASS_at_the_baseline_and_FAIL_below_it():
    """The twin of the test above, and now the SHIPPED state: with the real `FILING_BARS` the
    table judges, and it judges at exactly the numbers the Sonnet-5 baseline set.

    `type`/`folder` sit at 0.88 because the baseline scored 8/9 and 8/9 must satisfy its own bar —
    a two-decimal 0.89 would refuse the very run that fixed it, which is why the pair is floored.
    That is asserted here rather than left to the comment in `bars.py`: the day somebody tidies
    0.88 up to 0.89, this is what says the baseline no longer passes.

    The other seven bars are 1.00, so a single miss fails them. That is the tightness a future red
    run will be read against, and it should be visible in a test rather than discovered in anger.
    """
    def _typed(hits, of):
        return [run_filing._phase(f"T{i}", "only", {"type": "note"},
                                  {"type": "note" if i < hits else "concept"})
                for i in range(of)]

    assert 8 / 9 >= bars.FILING_BARS["type"] == bars.FILING_BARS["folder"] == 0.88, (
        "the baseline's own 8/9 must satisfy the bar it set")

    at_the_baseline = run_filing.render(run_filing.aggregate(_typed(8, 9), backend="sdk",
                                                             model="m", wall_s=1.0))
    below_it = run_filing.render(run_filing.aggregate(_typed(7, 9), backend="sdk", model="m",
                                                      wall_s=1.0))
    assert "(PASS vs 0.88 bar)" in at_the_baseline and "FAIL" not in at_the_baseline
    assert "(FAIL vs 0.88 bar)" in below_it and "PASS" not in below_it

    one_miss = [run_filing._phase(f"S{i}", "only", {"status": "filed"},
                                 {"status": "filed" if i else "triage"}) for i in range(12)]
    assert bars.FILING_BARS["status"] == 1.00
    assert "(FAIL vs 1.00 bar)" in run_filing.render(
        run_filing.aggregate(one_miss, backend="sdk", model="m", wall_s=1.0))


def test_every_miss_is_named_with_the_capture_and_facet_that_produced_it(entries):
    """A score with no misses listed is a number nobody can act on: the whole point of the run is
    to go read the page that went wrong."""
    _, block = _block_naming(entries, "anchor")
    bad = run_filing._phase("F10-anchoring-choice", "only", block,
                            dict(_perfect(block), anchor={"kind": "company", "ids": []}))
    rendered = run_filing.render(run_filing.aggregate([bad], backend="double", model="m",
                                                      wall_s=1.0))
    assert "misses:" in rendered
    assert "F10-anchoring-choice [only] anchor:" in rendered


# ── the cost axes the instrument prices itself with ───────────────────────────────────────────

def test_the_report_prices_the_run_it_describes():
    """A filing row carries `total_cost_usd` and `agent_passes` so a model-policy argument starts
    from recorded dollars rather than an estimate. Neither is a quality number and neither has a
    bar; both have to be arithmetic over the phases that actually ran."""
    phases = [run_filing._phase("A", "only", {"status": "filed"},
                                {"status": "filed", "cost_usd": 0.0123456, "attempts": 2}),
              run_filing._phase("B", "only", {"status": "filed"},
                                {"status": "filed", "cost_usd": 0.02, "attempts": 1})]
    report = run_filing.aggregate(phases, backend="sdk", model="m", wall_s=9.876)
    assert report["total_cost_usd"] == 0.032346
    assert report["agent_passes"] == 3
    assert report["wall_s"] == 9.88


# ── what may become a durable row in the series, and what may not ─────────────────────────────

def _observed(status=schema.FILED, attempts=1):
    return {"status": status, "attempts": attempts}


def test_a_healthy_run_is_allowed_to_append_its_row(entries):
    """The benign twin, first and deliberately: a withholding rule that never lets anything
    through stops the series growing and nobody notices, because the absence of a row looks
    exactly like nobody having run the instrument."""
    phases = [run_filing._phase("F01", "only", {"status": schema.FILED}, _observed())]
    assert run_filing._withheld_reason(phases, failed_status=schema.FAILED) == ""


def test_one_phase_that_ended_failed_withholds_the_whole_row():
    """`failed` is `worker.process_next` saying processing RAISED — a config, git or fixture
    fault. Recorded as a score it reads as a backend that cannot file, and it drags every trend
    line it sits in. The reason has to name the captures, or the operator is told only that
    something was withheld."""
    phases = [run_filing._phase("F01", "only", {"status": schema.FILED}, _observed()),
              run_filing._phase("F07-figure-dense", "only", {"status": schema.FILED},
                                _observed(status=schema.FAILED))]
    withheld = run_filing._withheld_reason(phases, failed_status=schema.FAILED)
    assert "F07-figure-dense" in withheld and schema.FAILED in withheld


def test_a_run_in_which_the_agent_was_never_called_withholds_the_row():
    """Every capture refused before the model ran — a credential accepted and then rejected, a
    fixture the gates bounce deterministically — leaves a full table of facets scored against
    whatever the deterministic path produced. F04 legitimately spends zero passes; a whole RUN at
    zero measured no backend at all."""
    phases = [run_filing._phase("F04", "only", {"status": schema.REJECTED},
                                _observed(status=schema.REJECTED, attempts=0)),
              run_filing._phase("F01", "only", {"status": schema.FILED}, _observed(attempts=0))]
    assert "never called" in run_filing._withheld_reason(phases, failed_status=schema.FAILED)


def test_a_single_agent_pass_anywhere_is_enough_to_keep_the_row():
    """The twin of the zero-attempts rule: F04's own zero must not withhold a run that otherwise
    measured nine captures."""
    phases = [run_filing._phase("F04", "only", {"status": schema.REJECTED},
                                _observed(status=schema.REJECTED, attempts=0)),
              run_filing._phase("F01", "only", {"status": schema.FILED}, _observed(attempts=1))]
    assert run_filing._withheld_reason(phases, failed_status=schema.FAILED) == ""


def test_the_row_records_what_the_phases_actually_ended_as(entries):
    """`statuses` is what makes a row whose facets look ordinary but whose phases all ended
    `triage` readable six months later, without still having the report. Sorted, so two reports of
    the same run diff to nothing."""
    phases = [run_filing._phase("A", "only", {}, _observed(status=schema.TRIAGE)),
              run_filing._phase("B", "only", {}, _observed(status=schema.FILED)),
              run_filing._phase("C", "only", {}, _observed(status=schema.FILED))]
    counts = run_filing._status_counts(phases)
    assert counts == {schema.FILED: 2, schema.TRIAGE: 1}
    assert list(counts) == sorted(counts)


# ── the row itself: the one artifact of a paid run that outlives the terminal ──────────────────
# `_history_metrics` is the dict `eval_history.append_run` writes into `evals/history.ndjson`, and
# the branch that calls it needs a real credential — so before this seam existed, a typo in a key
# name could only be found by spending a full SDK run and then reading the row it produced. These
# tests are the whole reason the function was extracted.

# The field names `evals/README.md` spells out for a filing row. Named here so a rename has to
# break BOTH the code and the sentence somebody reads instead of the code — the doc-claim posture
# this file already applies to the facet table. `facets`, `counts` and `agent_passes` are described
# in prose there rather than backticked, so they are pinned by the equality test below instead.
DOCUMENTED_ROW_FIELDS = ("backend", "model", "kinds", "statuses", "total_cost_usd", "wall_s")


def test_the_history_row_is_exactly_the_fields_the_series_promises():
    """The typo regression test, stated as one equality rather than a list of `in` checks: a key
    that is renamed, dropped or quietly added fails here, and an extra key nobody meant is as much
    of a defect as a missing one in a file whose readers are years away.

    The provenance half is the REAL `corpus_provenance` over the real fixture — the two halves that
    have to agree, agreeing — rather than a hand-typed dict that would pass while the caller's own
    contract had moved.
    """
    report = run_filing.aggregate(
        [run_filing._phase("F01", "only", {"status": schema.FILED},
                           {"status": schema.FILED, "attempts": 1, "cost_usd": 0.25}),
         run_filing._phase("F04", "only", {"reason": "duplicate"},
                           {"status": schema.REJECTED, "reason": "duplicate", "attempts": 0})],
        backend="sdk", model="claude-sonnet-5", wall_s=8.5)
    provenance = eval_history.corpus_provenance(str(ROOT / "evals" / "filing" / "repo"))

    metrics = run_filing._history_metrics(report, report["phases"], provenance)

    assert metrics == {
        "backend": "sdk",
        "model": "claude-sonnet-5",
        # Which capture kinds the run measured (ADR 032's `--kinds`). Empty here because this
        # `aggregate` call names none, exactly as every pre-`--kinds` caller does — the key is
        # always present so a subset row can never be read as a full-set one, and this equality is
        # what made adding it a decision rather than a quiet growth.
        "kinds": [],
        "facets": {"status": 1.0, "reason": 1.0},
        # F04's block names `reason` and not `status`, so `status` has a denominator of one here —
        # the per-facet rule the row inherits unchanged from `aggregate`.
        "counts": {"status": {"hits": 1, "of": 1}, "reason": {"hits": 1, "of": 1}},
        "statuses": {schema.FILED: 1, schema.REJECTED: 1},
        "total_cost_usd": 0.25,
        "agent_passes": 1,
        "wall_s": 8.5,
        "corpus": provenance["corpus"],
        "stigmergy_sha": provenance["stigmergy_sha"],
        "corpus_frozen_at": provenance["corpus_frozen_at"],
    }
    # It is written as one line of NDJSON. A Counter, a Path or a set in here would raise at the
    # append site, on the run that could least afford it.
    assert json.loads(json.dumps(metrics, sort_keys=True)) == metrics


def test_a_run_against_a_corpus_with_no_provenance_simply_carries_no_sha():
    """The live-checkout road: `--repo` pointed at a real knowledge repo has no `PROVENANCE.json`,
    so `corpus_provenance` answers with the path alone. The row must then be a row with no sha —
    not a row with an empty one, and never an error, because bookkeeping that can fail a run is
    worse than a row that says less (`eval_history.py`'s own rule)."""
    report = run_filing.aggregate(
        [run_filing._phase("F01", "only", {"status": schema.FILED},
                           {"status": schema.FILED, "attempts": 1})],
        backend="sdk", model="m", wall_s=1.0)

    metrics = run_filing._history_metrics(report, report["phases"], {"corpus": "/some/checkout"})

    assert metrics["corpus"] == "/some/checkout"
    assert "stigmergy_sha" not in metrics and "corpus_frozen_at" not in metrics
    assert set(DOCUMENTED_ROW_FIELDS) <= set(metrics)


def test_the_row_reports_every_facet_the_table_did_and_no_other(entries):
    """Shape agreement with `aggregate` over the WHOLE golden set: the row carries a score and a
    hit/denominator pair for exactly the facets the run scored. A row that dropped a facet — or
    kept one the set stopped scoring — would read as a different instrument six months later, and
    the numbers beside it would not be comparable."""
    report = run_filing.aggregate(_phases(entries), backend="sdk", model="m", wall_s=2.0)

    metrics = run_filing._history_metrics(report, report["phases"], {"corpus": "x"})

    assert set(metrics["facets"]) == set(metrics["counts"]) == set(DENOMINATORS)
    assert {name: row["of"] for name, row in metrics["counts"].items()} == DENOMINATORS
    assert metrics["facets"] == {name: report["facets"][name]["score"] for name in DENOMINATORS}
    assert sum(metrics["statuses"].values()) == PHASES


def test_the_series_documentation_names_the_fields_the_row_actually_carries():
    """The other half of the doc-claim tie: every field `evals/README.md` promises a filing row
    carries is a key this function writes, and is still spelled that way in the document. A row and
    a sentence that disagree leave a reader of `history.ndjson` grepping for a field that never
    existed."""
    series = EVALS_README.read_text(encoding="utf-8").split("## The series", 1)[1]
    metrics = run_filing._history_metrics(
        run_filing.aggregate([], backend="sdk", model="m", wall_s=0.0), [], {"corpus": "x"})
    for field in DOCUMENTED_ROW_FIELDS:
        assert field in metrics, f"the README promises `{field}` and the row does not carry it"
        assert f"`{field}`" in series, f"`{field}` is written into the row and documented nowhere"


# ── the table's caption ───────────────────────────────────────────────────────────────────────

def test_the_double_prints_no_model_name_on_a_table_no_model_touched(entries):
    """A table is the thing somebody screenshots and quotes later as "what Sonnet scored". The
    double calls no model at all, so naming one there attributes a plumbing check to a backend
    that never ran."""
    report = run_filing.aggregate(_phases(entries), backend="double", model="claude-sonnet-5",
                                  wall_s=1.0)
    caption = run_filing.render(report).splitlines()[0]
    assert "claude-sonnet-5" not in caption
    assert "no model" in caption
    assert report["model"] == "claude-sonnet-5", (
        "only the human-facing line changes — the JSON report keeps the setting it ran with")


def test_a_real_run_still_names_the_model_it_measured(entries):
    """Its benign twin: the caption that matters most is the one on a REAL run, and a fix that
    silenced both would leave every recorded table unable to say what it measured."""
    caption = run_filing.render(run_filing.aggregate(_phases(entries), backend="sdk",
                                                     model="claude-sonnet-5",
                                                     wall_s=1.0)).splitlines()[0]
    assert "claude-sonnet-5" in caption and "no model" not in caption


# ── the documentation that is read instead of this file ───────────────────────────────────────

def test_the_facet_table_in_the_evals_readme_documents_the_set_that_exists(entries):
    """`evals/README.md` prints one row per quality facet with its denominator. Whoever adds the
    next capture reads that table and not this file, so a stale one is worse than none: it states
    a contract the instrument no longer keeps, in the document that says how the set may grow."""
    body = EVALS_README.read_text(encoding="utf-8")
    table = body.split("### The nine facets", 1)[1].split("\n**", 1)[0]
    documented = {name: int(of) for name, of in
                  re.findall(r"^\| `(\w+)` \|.*\| (\d+) \|$", table, re.MULTILINE)}
    assert documented == {facet: DENOMINATORS[facet] for facet in run_filing.QUALITY_FACETS}


def test_both_eval_docs_state_the_size_of_the_set_they_describe(entries):
    """10 captures and 12 scored phases are countable claims in two documents. They move together
    with `DENOMINATORS` or not at all."""
    report = run_filing.aggregate(_phases(entries), backend="double", model="m", wall_s=1.0)
    assert report["counts"] == {"captures": CAPTURES, "phases": PHASES}
    assert f"{CAPTURES} golden captures, {PHASES} scored phases" in \
           EVALS_README.read_text(encoding="utf-8")
    assert f"{CAPTURES} captures, {PHASES} scored phases" in \
           EVALS_INDEX.read_text(encoding="utf-8")


# ── the seam the whole keyless half rests on ──────────────────────────────────────────────────

def test_importing_the_runner_costs_nothing_but_the_standard_library():
    """`score_phase`/`aggregate`/`render` are importable with no Postgres driver, no pytest and no
    agent SDK loaded — which is what lets this file score canned outcomes through exactly the code
    a paid run uses. Checked in a FRESH interpreter: in this one, another test has already
    imported half the project.

    If a future edit lifts one of `_run`'s deferred imports to module scope, every test here still
    passes and the property quietly dies — so the check has to be the import itself.
    """
    import subprocess
    import sys
    probe = ("import sys; import evals.run_filing as r;"
             "heavy=[m for m in ('psycopg','pytest','claude_agent_sdk') if m in sys.modules];"
             "assert not heavy, heavy;"
             "assert r.score_phase({'status':'filed'}, {'status':'filed'}) == {'status': True}")
    proc = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
