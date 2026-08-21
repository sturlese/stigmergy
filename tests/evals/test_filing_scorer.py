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

# The per-facet denominators of the golden set as it stands: 14 captures, 14 scored phases.
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
#
# MOVED for issue #77, and counted by hand off the expectations file again rather than copied from
# the runner: F11-F14 are four ordinary single-phase filings, each naming status/type/folder/anchor
# plus the two cost axes, so exactly those six facets gain four apiece and the meeting, edits and
# reason denominators do not move at all. Scores recorded before and after remain comparable per
# FACET and are not comparable per run.
#
# MOVED again for ADR 041, counted by hand a third time. Four numbers changed and each is a fact
# about the redesign, not an edit made to let something pass:
#
#   * `status` 16 -> 14 — the two ask-back captures stopped being two phases each. There is no
#     park, no reply and no re-file, so a capture is one scored moment.
#   * `anchor` 11 -> 10 — F02's surviving phase asserts no anchor. Its page anchors to an entity
#     PROPOSED in the same commit, whose id is `slugify` of a name the agent chose, so the id would
#     score the spelling. `proposals` scores the judgment instead, and loosely.
#   * `park_question` and `reuse` are GONE with the states they measured, and `proposals` inherits
#     the former's denominator of 2 from the same two captures.
DENOMINATORS = {"status": 14, "reason": 1, "type": 13, "folder": 13, "anchor": 10, "edits": 1,
                "proposals": 2, "decisions": 2, "attempts": 12, "bounces": 12}
CAPTURES, PHASES = 14, 14


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
    if "proposals" in expect:
        # Padded on purpose, the way a real proposal legitimately differs from the expectation:
        # the agent proposes `the Halcyon Grid programme` and the yardstick names `Halcyon Grid`.
        # A scorer that quietly became literal fails right here rather than on a paid run.
        observed["proposals"] = [f"the {name} programme" for name in expect["proposals"]]
    if "decisions" in expect:
        observed["decisions"] = [
            {"path": f"wiki/decisions/{decision['title']} agreed on the call.md",
             "anchor": decision.get("anchor", {"kind": "company", "ids": []})}
            for decision in reversed(expect["decisions"])]
    return observed


def _phases(entries: list) -> list:
    """Every scored phase of the golden set, observed perfectly — built through the runner's own
    `_phase`, the same composer `_drive` uses on a real run. One per entry: a capture never waits
    on a person, so it is never scored twice (ADR 041)."""
    return [run_filing._phase(entry["id"], "only", entry["expect"], _perfect(entry["expect"]))
            for entry in entries]


def _block_naming(entries: list, facet: str) -> tuple:
    """The first expectation block in the golden set that names `facet`, with its capture id."""
    for entry in entries:
        if facet in entry["expect"]:
            return entry["id"], entry["expect"]
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
        block = entry["expect"]
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
    # `triage` until ADR 041 retired it. A capture reaches one of three terminal states now, so the
    # mutation that proves this facet discriminates has to be one of the OTHER two.
    "status": lambda o: dict(o, status=schema.REJECTED),
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
    # The proposal a backend makes when it did not recognise the unregistered name for what it is:
    # a plausible identity, taken from the corpus's own registry, rather than nonsense.
    "proposals": lambda o: dict(o, proposals=["Northwind Freight"]),
    "decisions": lambda o: dict(o, decisions=[*o["decisions"],
                                              {"path": "wiki/decisions/A third decision.md",
                                               "anchor": {"kind": "company", "ids": []}}]),
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


# **DELETED with the phase it guarded (ADR 041):**
# `test_a_backend_that_never_parked_scores_the_after_reply_phase_as_a_miss`. `_drive` used to record
# the second phase of a park as a MISS rather than skipping it, because a vanishing phase would
# shrink its facets' denominators and quietly raise the score of a backend that never asked. There
# is no second phase to vanish: one capture is one phase, and the denominator it fills is pinned in
# `DENOMINATORS` and refused on drift by `_check_set`. The rule the test carried — a phase that
# cannot be measured is a MISS and never an absence — now lives one level up, in
# `_check_set`'s refusal of the retired `reply`/`after_reply` keys.


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


# ── the number fold: exactly one suffix, and its narrowness is the property ────────────────────
# `_same_word` exists because grammatical number was measured to be run-to-run NOISE: three runs of
# one model on one capture titled F08's review decision two ways, and the plural scored FAIL while
# the singular scored PASS with the anchors right every time. A 2-denominator facet flipping on
# whether a model wrote "review" or "reviews" is the instrument measuring the model's grammar.
#
# The fold is one trailing `s`, in either direction, and the NARROWNESS is what makes it safe to
# have at all — so it is pinned from both sides. A stemmer here would quietly start matching
# `tracked`/`tracking`, and the yardstick's obligation to name uninflected content words would be
# gone with nothing to show it.
@pytest.mark.parametrize("want, got", [
    ("review", "reviews"),          # the measured case, in the direction the run produced
    ("reviews", "review"),          # ...and the other, because the fold is symmetric
    ("review", "review"),           # plain equality still holds
])
def test_the_number_fold_folds_a_trailing_s_in_either_direction(want, got):
    assert run_filing._same_word(want, got) is True


@pytest.mark.parametrize("want, got", [
    ("process", "processes"),       # `es` is NOT folded — the documented edge of the rule
    ("processes", "process"),
    ("summary", "summaries"),       # nor `ies`
    ("track", "tracked"),           # nor any other inflection
    ("track", "tracking"),
    ("mouse", "mice"),              # nor an irregular
])
def test_the_number_fold_reaches_no_further_than_one_trailing_s(want, got):
    """**The half that keeps this from becoming a stemmer.** Each row is an inflection a table
    WOULD fold and this rule does not, so a future edit that reached for one has to break a test
    with the reason written beside it. `process`/`processes` in particular is named in
    `_same_word`'s own docstring as a miss it accepts — the first draft of that comment overclaimed,
    and this is the assertion that keeps the corrected claim true."""
    assert run_filing._same_word(want, got) is False


def test_the_bus_residual_is_ACCEPTED_rather_than_fixed():
    """**A declared residual, pinned as declared.** `a == b + "s"` also fires when one token happens
    to be another plus `s`: `bus` and `bu` are "the same word" to this rule.

    That is only a false match if BOTH strings are words a title would carry, and a two-letter
    fragment is not — every expectation in this set is written in proper nouns and stable nouns. It
    is a residual, not an impossibility, and `_same_word`'s docstring says the fix would be the
    EXPECTATION rather than a longer rule.

    Pinned rather than "fixed" for that reason: a length guard or a vowel check here would be the
    first step of the stemmer the tests above exist to prevent, bought against a case nothing in
    the set can produce. If this ever goes red because somebody added such a guard, the question to
    ask is which real expectation needed it.
    """
    assert run_filing._same_word("bus", "bu") is True
    assert run_filing._same_word("bu", "bus") is True


def test_the_number_fold_is_reachable_through_the_matcher_that_uses_it():
    """The fold is only worth anything if `title_matches` actually consults it — a unit-green
    `_same_word` beside a matcher that compares tokens with `==` would be exactly the shape of a
    fix that never shipped. Driven through the real scorer, on the real F08 shape."""
    assert run_filing.title_matches(
        "review", "wiki/decisions/A shared checklist of what every review covers.md") is True
    assert run_filing.title_matches(
        "review", "wiki/decisions/A shared checklist of what the reviews cover.md") is True


def test_a_proposal_is_matched_on_the_name_however_the_agent_qualified_it():
    """The whole reason this facet scores NAMES and the expectation asserts no anchor beside it: an
    identity proposed as `the Halcyon Grid pilot` is the same judgment as one proposed as `Halcyon
    Grid`, and their registry ids — `slugify(name)` — are two different strings. Scoring the id
    would mark a correct proposal down for the qualifier the agent read off the material."""
    assert run_filing.score_phase(
        {"proposals": ["Halcyon Grid"]},
        {"proposals": ["the Halcyon Grid pilot"]})["proposals"] is True


@pytest.mark.parametrize("observed", [[], ["Northwind Freight"], ["Halcyon"]])
def test_a_filing_that_proposed_the_wrong_identity_or_none_at_all_is_a_miss(observed):
    """The three ways this facet has to be able to say no, and the first is the one the redesign
    exists to catch: `[]` is a filing that anchored the capture somewhere and gave the unregistered
    name no identity at all — the shortcut that used to be a park and is now a silent mis-anchor."""
    assert run_filing.score_phase({"proposals": ["Halcyon Grid"]},
                                  {"proposals": observed})["proposals"] is False


def test_an_expectation_naming_two_identities_needs_BOTH_proposed():
    """A capture naming two unregistered things files ONE commit that proposes both or neither —
    a page anchored to one of them and silent about the other is a half-filed identity zone."""
    expect = {"proposals": ["Project Wren", "Halcyon Grid"]}
    assert run_filing.score_phase(expect, {"proposals": ["Project Wren"]})["proposals"] is False
    assert run_filing.score_phase(
        expect, {"proposals": ["Project Wren", "Halcyon Grid"]})["proposals"] is True


def test_a_second_proposal_beside_the_expected_one_does_not_fail_the_facet():
    """Generous in one direction, like `edits` and for the same kind of reason: an extra proposal is
    already fenced by `librarian.identity`'s three honesty checks (named in the material, no
    collision with a registered spelling, not a name a steward declined), so a filing that gave two
    unregistered names identities still recognised the one the expectation asks about. The
    yardstick's imagination is not the thing being measured."""
    assert run_filing.score_phase(
        {"proposals": ["Halcyon Grid"]},
        {"proposals": ["Halcyon Grid", "Halcyon Grid Scheduling"]})["proposals"] is True


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
    """The shape F09 ships since ADR 041: its decisions anchor either to an identity PROPOSED in the
    same commit — whose id is slugified from a name the agent chose — or company-wide, so pinning
    an anchor there would pin the yardstick to one sample of the agent's own vocabulary."""
    loose = [{"title": "Wren tracked formally"}, {"title": "summary joins the shared"}]
    observed = [_page("Wren tracked formally in the registry", {"kind": "company", "ids": []}),
                _page("Wren summary joins the shared digest",
                      {"kind": "entity", "ids": ["quillon-labs"]})]
    assert run_filing.score_phase({"decisions": loose},
                                  {"decisions": observed})["decisions"] is True


# ── an entry that omits `title` entirely: paired on its ANCHOR alone ───────────────────────────
# The third shape of one lesson. F08's review decision flipped on grammatical number (closed by
# `_same_word`); F09's re-filed phase put the word "summary" on the OTHER decision's title than the
# yardstick assumed — and a SEVEN-RUN re-score settled which dimension to keep: summary-title was
# 7/7 stable, summary-anchor 6/7 UNstable. So the entry that keeps a title keeps it, and the one
# whose title was the model's prose drops to its anchor.
#
# The CAPABILITY is what is tested below; no shipped expectation uses it. F09 was its natural home
# and stopped being one under ADR 041: with the stored reply gone, neither of its decisions has an
# anchor a yardstick can name, so both assert titles alone. See
# `test_whether_the_shipped_set_uses_the_title_less_shape_at_all` in the fixture suite.
#
# An omitted `title` is NOT an empty one, and the two must not be confusable: `title_matches("")`
# is False by design, so an entry that NAMED a title and got it wrong still misses. That asymmetry
# is the first thing pinned below.
_ANCHOR_ONLY = [{"title": "Wren", "anchor": {"kind": "entity", "ids": ["quillon-labs"]}},
                {"anchor": {"kind": "company", "ids": []}}]


def test_an_entry_that_omits_its_title_pairs_on_the_anchor_alone():
    """The anchor is the load-bearing pair-er: a decision's aboutness is a fact with one spelling
    (a resolved registry id, or the company-wide empty list), where its title is a sentence
    somebody wrote. The second page's title here is nothing the expectation ever mentions, and the
    set still scores."""
    observed = [_page("Project Wren tracked formally under Quillon Labs",
                      {"kind": "entity", "ids": ["quillon-labs"]}),
                _page("weekly summaries consolidated into the shared summary",
                      {"kind": "company", "ids": []})]
    assert run_filing.score_phase({"decisions": _ANCHOR_ONLY},
                                  {"decisions": observed})["decisions"] is True


def test_a_title_less_entry_still_holds_its_anchor_to_account():
    """**Omitting the title is not weakening the facet**, which is the whole claim — it moves the
    assertion off the dimension that was measuring vocabulary and onto the one that was right in
    every recorded run. So the anchor still has to be right: same two pages, anchors swapped."""
    swapped = [_page("Project Wren tracked formally under Quillon Labs",
                     {"kind": "company", "ids": []}),
               _page("weekly summaries consolidated into the shared summary",
                     {"kind": "entity", "ids": ["quillon-labs"]})]
    assert run_filing.score_phase({"decisions": _ANCHOR_ONLY},
                                  {"decisions": swapped})["decisions"] is False


def test_an_EMPTY_title_is_not_an_absent_one_and_matches_nothing():
    """The two states cannot be confused, and this is the direction that would be silent: an entry
    whose `title` is `""` would, if empty meant absent, become an anchor-only matcher and quietly
    stop asserting the title it was written to assert."""
    empty_titled = [{"title": "", "anchor": {"kind": "entity", "ids": ["quillon-labs"]}}]
    observed = [_page("Anything at all", {"kind": "entity", "ids": ["quillon-labs"]})]
    assert run_filing.score_phase({"decisions": empty_titled},
                                  {"decisions": observed})["decisions"] is False


# The greedy interaction, staged as a clean A/B: ONE page set, two expectation ORDERS, and the
# ordering is the only variable between the two tests below.
#
# The page the titled entry needs comes FIRST in the observed set, which is what makes the hazard
# reachable at all — an anchor-only entry takes the first remaining page whose anchor matches, so
# it starves its sibling only when that sibling's page is the one it reaches first. A fixture whose
# page order happened to be favourable would show the safe result in BOTH orders and prove nothing;
# that is the first shape this pair was written in, and it passed the wrong way round.
_COMPANY = {"kind": "company", "ids": []}
_BOTH_COMPANY_PAGES = [_page("Project Wren tracked formally", _COMPANY),
                       _page("weekly summaries consolidated", _COMPANY)]


def test_a_title_less_entry_written_LAST_leaves_its_titled_sibling_the_page_it_needs():
    """**The greedy interaction in the safe order** — the order `_check_set` enforces.

    Both pages anchor company-wide, so the anchor-only entry matches EITHER. Written last it takes
    what the titled entry left, and the correct page set scores.
    """
    titled_first = [{"title": "Wren", "anchor": _COMPANY}, {"anchor": _COMPANY}]

    assert run_filing.score_phase({"decisions": titled_first},
                                  {"decisions": _BOTH_COMPANY_PAGES})["decisions"] is True


def test_a_title_less_entry_written_FIRST_starves_its_titled_sibling_and_fails_a_correct_set():
    """**The same two entries and the same two pages, in the other order — and it MISSES.**

    Greedy pairing walks the expectations in the order the FILE lists them. The anchor-only entry
    placed first takes "Project Wren tracked formally" (the first page whose anchor matches, and
    the one its titled sibling needed); the titled entry is then left with "weekly summaries
    consolidated", which it cannot match. A page set that was exactly right scores a MISS.

    That failure points at nothing: the table shows a decisions cell gone red and the cause is the
    order of two lines in a JSON file. Which is why the ordering is a REFUSAL in `_check_set`
    rather than a rule in a comment — and this pair is what shows the refusal protects something
    reachable rather than a hypothesis.
    """
    titleless_first = [{"anchor": _COMPANY}, {"title": "Wren", "anchor": _COMPANY}]

    assert run_filing.score_phase({"decisions": titleless_first},
                                  {"decisions": _BOTH_COMPANY_PAGES})["decisions"] is False, (
        "the hazard `_check_set`'s ordering refusal exists for has stopped being reachable — if "
        "greedy pairing became order-independent, that refusal can retire with this test")


# **DELETED with the `reuse` facet (ADR 041):**
# `test_the_reuse_facet_scores_whether_a_decision_was_LOST_and_not_whether_a_pass_was_saved` and
# `test_a_re_file_that_reported_no_reuse_block_at_all_is_a_miss_when_preservation_was_expected`.
# The facet asked whether a meeting re-filed after a park had LOST a decision on the way back; a
# meeting is never re-filed, so nothing makes the round trip and there is nothing to lose. The
# granularity question that facet shared with `decisions` — did the right number of decision pages
# land, each anchoring on its own — is unchanged and is still measured there.


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


def test_the_facet_ADR_041_created_ships_with_no_bar_and_the_table_says_so(entries):
    """`proposals` is the one shipped facet whose bar is `None`, and that is a claim about the
    series rather than an oversight: no recorded run has ever scored it, and `evals/README.md`'s own
    rule is that a bar is a recorded baseline's own score — never a number invented to be met.

    Pinned in both directions. The verdict is `None` (REPORT, DO NOT JUDGE) and the rendered row
    says why, while every other quality facet still carries a real number — so a future editor who
    fills this in from the first run under the re-frozen brief has to come here and delete the
    exception rather than quietly widen it, and one who guesses a bar breaks this test.
    """
    assert bars.FILING_BARS["proposals"] is None
    assert [name for name in run_filing.QUALITY_FACETS
            if bars.FILING_BARS[name] is None] == ["proposals"]

    report = run_filing.aggregate(_phases(entries), backend="double", model="m", wall_s=1.0)
    assert report["facets"]["proposals"]["score"] == 1.0
    assert report["facets"]["proposals"]["pass"] is None
    rendered = run_filing.render(report)
    assert "proposals  1.00  [2/2]  (no bar — baseline not yet fixed)" in rendered


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

    one_miss = [run_filing._phase(f"S{i}", "only", {"status": schema.FILED},
                                 {"status": schema.FILED if i else schema.REJECTED})
                for i in range(12)]
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
    """`statuses` is what makes a row whose facets look ordinary but whose phases mostly ended
    `rejected` readable six months later, without still having the report. Sorted, so two reports of
    the same run diff to nothing.

    It used to be spelled with `triage`, the state a steward drained by hand; ADR 041 retired it, and
    the counter is unchanged because it counts whatever string a phase ended on rather than a
    vocabulary of its own.
    """
    phases = [run_filing._phase("A", "only", {}, _observed(status=schema.REJECTED)),
              run_filing._phase("B", "only", {}, _observed(status=schema.FILED)),
              run_filing._phase("C", "only", {}, _observed(status=schema.FILED))]
    counts = run_filing._status_counts(phases)
    assert counts == {schema.FILED: 2, schema.REJECTED: 1}
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
    table = body.split("### The eight facets", 1)[1].split("\n**", 1)[0]
    documented = {name: int(of) for name, of in
                  re.findall(r"^\| `(\w+)` \|.*\| (\d+) \|$", table, re.MULTILINE)}
    assert documented == {facet: DENOMINATORS[facet] for facet in run_filing.QUALITY_FACETS}


def test_both_eval_docs_state_the_size_of_the_set_they_describe(entries):
    """The capture and phase counts are countable claims in two documents. They move together with
    `DENOMINATORS` or not at all — and they moved for ADR 041, which made every capture a single
    scored phase, so the two numbers are equal for the first time."""
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
