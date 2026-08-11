"""`evals/filing/` — the golden filing set and the mini knowledge repo it files into.

The instrument itself needs a Claude credential and a real Postgres, so it runs by hand. This
file is the keyless half CI can run, and it guards the thing a measurement is only as good as:
the two halves of the golden set, the fixture repo and the CODE agreeing about what exists.
Same posture as `test_golden_corpus_fixture.py` next door, with one deliberate difference recorded
below.

**Every value is checked against the real thing that consumes it, never against a second literal.**
Statuses come from `capture.schema`, page types and their folders from `librarian.page`, the
registry from `kernel.registry.load_registry`, the pages from `index.corpus.load_pages`, the
facet names from `run_filing` itself. A yardstick that spells a status, a facet or a page type its
own way does not fail — it scores that facet 0.00 forever and reads as a failing backend, which
is the most expensive failure this set can have (it is discovered after paying for a real run).

**What this file deliberately does NOT check**: that the three frozen copies under
`filing/repo/.claude/` still match the live knowledge repo. That is the opposite of
`tests/librarian/test_frozen_linter.py`'s rule and it is intentional — a drift guard keeps a test
honest about the present, while a yardstick has to stay still or every score already recorded is
silently re-graded. Each copy's `FROZEN.md` says so and names the commit it was taken at.
"""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from evals import bars, run_filing

# Production constants, so a fixture path that moved is a failure here rather than an inert gate
# during a paid run. These pull `psycopg` in transitively and no API-key machinery: keyless.
from stigmergy.capture import schema
from stigmergy.index import corpus
from stigmergy.kernel import registry as registry_module
from stigmergy.librarian import agent as librarian_agent
from stigmergy.librarian import config as librarian_config
from stigmergy.librarian import double, page

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "evals" / "filing"
CAPTURES = FIXTURE / "captures"
REPO = FIXTURE / "repo"
LINTER = REPO / ".claude" / "tools" / "stigmergy_lint.py"

# The mini repo's own count, asserted against `PROVENANCE.json` as well so the two cannot drift.
FIXTURE_PAGES = 5
FIXTURE_ENTITIES = 3

# The `FROZEN.md` beside each frozen copy. Their SHAs are NOT compared with the knowledge repo
# (see the module docstring); what is checked is that each one still records one, and that all of
# them record the SAME one as `PROVENANCE.json`.
FROZEN_MARKERS = (
    REPO / ".claude" / "tools" / "FROZEN.md",
    REPO / ".claude" / "skills" / "librarian" / "FROZEN.md",
    REPO / ".claude" / "skills" / "meeting-distiller" / "FROZEN.md",
)
_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")

# ── the one state a frozen copy may be in WITHOUT a sha, and only until its PR lands ───────────
# ADR 033 rewrote the librarian brief, and the platform PR and the knowledge-repo PR land together:
# at the moment these bytes were copied here, the commit that carries them did not exist yet. A sha
# row naming the PREVIOUS brief would be worse than a placeholder, because it would look answered —
# `eval_history.corpus_provenance` copies `PROVENANCE.json`'s value into every `suite: "filing"`
# row, so a wrong sha makes every future score comparable to the wrong predecessor. That is the one
# failure this pin exists to prevent.
#
# So the placeholder is TOLERATED and BOUNDED, not ignored:
#
#  * the sha-shape assertion accepts 40 hex OR exactly this literal, and goes green either way once
#    the real sha lands — no test edit at landing;
#  * `_PENDING_ALLOWED` names the only files that may carry it, so a placeholder cannot spread to a
#    copy nobody was expecting to see one in;
#  * a file carrying it must also carry the command that replaces it, so the instruction cannot be
#    dropped while the placeholder stays;
#  * and the LANDING TRIPWIRE — the check that turns this from a polite request into a failure —
#    lives in `tests/librarian/test_librarian_brief_contract.py`, because it has to read the live
#    knowledge repo and this file deliberately never does (see the module docstring: a yardstick is
#    not drift-guarded). It fails the moment the knowledge repo's brief matches these frozen bytes
#    while a placeholder is still here.
_PENDING_SHA = "PENDING-KNOWLEDGE-REPO-SHA"
_PENDING_REPLACEMENT_COMMAND = "log -1 --format=%H -- .claude/skills/librarian/SKILL.md"
_PENDING_ALLOWED = (REPO / ".claude" / "skills" / "librarian" / "FROZEN.md",)

# The BYTES of the three frozen copies, pinned. This is what turns the freeze from a request into
# an enforced fact — `FROZEN.md` asks a human not to resync, and asking is what failed everywhere
# this repo has needed a guard. It is deliberately NOT a drift guard against the live knowledge
# repo (that comparison is what `FROZEN.md` refuses on purpose: a yardstick has to stay still), so
# the pin lives here, where the value on the left is what every score in the series was measured
# under. The linter copy in particular is EXECUTED as a subprocess on the maintainer's machine by
# `gate_contract` and by this file's own linter test, so an unreviewed edit to it must be
# impossible to miss.
#
# A deliberate re-freeze updates these three numbers IN THE SAME COMMIT as the bytes, which is the
# review moment this pin exists to force — and, per `evals/README.md`, retires the series with it.
#
# The librarian brief's pin has moved FOUR times, deliberately, and every move was the same kind
# of event: not an edit to a yardstick but a NEW yardstick, which retires the series with it per
# `evals/README.md`.
#
#   1. ADR 033 rewrote the brief backend-NEUTRAL (no tool mechanics in it at all).
#   2. The `sdk` retirement closed that rewrite's last debt: the brief's environment paragraph still
#      told its reader that "some runs of this skill hold tools and a checkout, and write the page
#      themselves", which stopped being true of ANY run when the tool-holding backend went. That
#      paragraph became the tool-less statement, at knowledge-repo commit
#      `c1e0996ed497e70a9df82661c367294b48207a16`.
#   3. ADR 034 gave the ordinary run its tools BACK, so a brief describing one run style stopped
#      being true again — in the other direction. The preamble is now environment-CONDITIONAL
#      ("your run is described in the preamble above this skill"; a short tools paragraph that
#      applies when the run holds them), which is the only shape that stays true of both a
#      handed-context run and a searching one. Knowledge-repo commit
#      `0bf3c5462d50e72f5435ce61d61ba5f023e60388` — the sha all five provenance records carry.
#   4. The "Writing the page" section's opening became a symmetric two-branch statement (`**Your
#      preamble decides who writes the file, and the two ways are not alike.**`), folding in what
#      used to be a standalone "write no frontmatter block at all" sub-bullet — a phrasing a staging
#      measurement found 8 of 13 first-pass drafts dropped the frontmatter block over, against 0 of
#      12 after the rewrite. The same edit added a `**A wikilink stays on one line.**` sub-bullet
#      ahead of the existing "claim that a page exists" one. Knowledge-repo commit
#      `03aab8799f9778087ab78cc23fbbf9a809d52d5b`.
#
# The other two numbers did NOT move with it, and that is the fact worth reading off this block: one
# freeze, one commit does not mean one freeze, every copy edited. The linter and the meeting brief
# are byte-identical at that commit and at its predecessor, which is exactly what
# `PROVENANCE.json`'s `stigmergy_sha_note` already claims about the tree.
FROZEN_SHA256 = {
    ".claude/tools/stigmergy_lint.py":
        "5c914e43a33e05a276142b26cd6ebc3ff84479b43703c783b9959e6a28948f28",
    ".claude/skills/librarian/SKILL.md":
        "9376e8f1863ade51f89fcb239a0842cb33454baee7255a54d7f15eced3759645",
    ".claude/skills/meeting-distiller/SKILL.md":
        "b3686f91666c5fa6f9f9a2aa602230db716abbf503792c60d64f7d0e2300476a",
}

# Vocabulary that belongs to the MEASUREMENT and never to a page inside the fixture repo. The
# librarian's agent reads this repo while it files, so a page describing the eval it takes part in
# briefs the very backend under measurement — the instrument would be scoring its own fixture's
# prose. Every one of these words is absent from `wiki/` today and none of them has a plausible
# reading in synthetic freight, publishing or laboratory prose, which is what keeps this a
# property and not a false-positive generator. If a page needs one of them, that page is writing
# about the measurement.
MEASUREMENT_VOCABULARY = ("facet", "denominator", "golden", "yardstick", "backend", "eval",
                          "evals", "fixture")


@pytest.fixture(scope="module")
def manifest():
    return json.loads((CAPTURES / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def expectations():
    return json.loads((FIXTURE / "expected" / "expectations.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def entries(expectations):
    return expectations["expectations"]


@pytest.fixture(scope="module")
def registry():
    return registry_module.load_registry(str(REPO / librarian_config.REGISTRY_RELPATH))


def _expect_blocks(entry: dict) -> list:
    """Every scored moment an entry declares: the first pass, and the re-file after a reply."""
    return [entry["expect"]] + ([entry["after_reply"]] if "after_reply" in entry else [])


# ── the two halves of the golden set ───────────────────────────────────────────────────────────

def test_both_halves_parse_and_name_exactly_the_same_captures(manifest, entries):
    assert [c["id"] for c in manifest["captures"]] == [e["id"] for e in entries], (
        "captures/manifest.json and expected/expectations.json are kept apart on purpose; they "
        "must still describe the same set, in the same order — F04 can only be refused as a "
        "duplicate after F01 has actually filed")


def test_the_runners_own_consistency_check_accepts_the_shipped_set(manifest, expectations):
    """`_check_set` is what stops a drifted set from spending real money before it fails."""
    run_filing._check_set(manifest, expectations)


def test_the_consistency_check_refuses_a_set_whose_halves_have_drifted(manifest, expectations):
    """The benign twin of the test above: a guard nothing has tried to break is not a guard.

    Half of the golden set is deleted here in memory only — the check has to name the orphan and
    exit, not shrug and score a smaller set than the one it claims to.
    """
    thinner = {"expectations": [e for e in expectations["expectations"]
                                if e["id"] != "F10-anchoring-choice"]}
    with pytest.raises(SystemExit) as ex:
        run_filing._check_set(manifest, thinner)
    assert "F10-anchoring-choice" in str(ex.value)


def test_the_consistency_check_counts_ids_rather_than_comparing_sets(manifest, expectations):
    """A capture listed TWICE in one half and missing from the other cancels out under `set()` —
    the halves compare equal, the run scores that capture twice and the missing one not at all.
    Counting is what makes the check see it, so the counting has to be exercised."""
    doubled = {"captures": [manifest["captures"][0]] + manifest["captures"]}
    with pytest.raises(SystemExit) as ex:
        run_filing._check_set(doubled, expectations)
    assert manifest["captures"][0]["id"] in str(ex.value)


def test_the_consistency_check_refuses_a_key_the_scorer_would_silently_ignore(expectations,
                                                                              manifest):
    """`score_phase` ignores what it does not recognize, so `achor:` does not fail a run — it
    removes that capture from the anchor denominator in silence and raises the facet's score. The
    refusal has to name the capture AND the key, or the operator is left grepping."""
    mutated = json.loads(json.dumps(expectations))
    mutated["expectations"][0]["expect"]["achor"] = {"kind": "entity", "ids": ["x"]}
    with pytest.raises(SystemExit) as ex:
        run_filing._check_set(manifest, mutated)
    assert "achor" in str(ex.value) and mutated["expectations"][0]["id"] in str(ex.value)


@pytest.mark.parametrize("drop", ["reply", "after_reply"])
def test_the_consistency_check_refuses_half_an_ask_back_case(drop, expectations, manifest):
    """Both directions: a `reply` with nothing to score after it, and an `after_reply` phase the
    runner will never reach because no reply is ever sent. Either way the ask-back loop is not
    measured and the table does not say so."""
    mutated = json.loads(json.dumps(expectations))
    parking = next(e for e in mutated["expectations"] if "reply" in e)
    parking.pop(drop)
    with pytest.raises(SystemExit) as ex:
        run_filing._check_set(manifest, mutated)
    assert parking["id"] in str(ex.value)


# ── the fifth refusal: what a DECISION entry has to assert, and in what order ──────────────────
# Both halves guard one mechanism from opposite ends, and both are SET defects that would otherwise
# read as a backend result — which is the expensive direction: a red cell on a paid run, caused by
# two lines of JSON, pointing at nothing.
#
# **Both refusals are PRE-EMPTIVE today, and that is recorded rather than glossed.** The title-less
# capability is live in `_decisions_match`, but no shipped expectation uses it — every decision
# entry in the set names a `title` (F09's `after_reply[0]` is `{"title": "summary"}`: a title with
# no anchor, which is the older "scores the title alone" shape and a different thing). So the
# shipped set is a benign twin in the sense that matters — it PASSES the refusal — and not in the
# sense of exercising the shape. `test_whether_the_shipped_set_uses_the_title_less_shape_at_all`
# below is what keeps that distinction honest, and it will say so the day it changes.
#
# Guards written WITH the capability rather than after it bit something are the cheap direction,
# so this is a note about scope, not an objection.
def _mutated_decisions(expectations, entry_id: str, block: str) -> tuple:
    """The mutable copy and the decisions list this refusal is about, for one shipped entry."""
    mutated = json.loads(json.dumps(expectations))
    entry = next(e for e in mutated["expectations"] if e["id"] == entry_id)
    return mutated, entry[block]["decisions"]


def test_the_consistency_check_refuses_a_decision_asserting_neither_title_nor_anchor(expectations,
                                                                                     manifest):
    """An entry that names neither matches whatever page is left on the table — a facet that reads
    as measured and measures nothing. Exactly the defect `edits: []` has, recorded in
    `_edits_match`, arrived at from the other side.

    Staged on F09's `after_reply[0]`, which ships a title and no anchor: stripping its title is the
    edit somebody would really make (a title that turned out to be the distiller's prose), and it
    lands on the one entry in the set for which that leaves nothing behind.
    """
    mutated, decisions = _mutated_decisions(expectations, "F09-meeting-parks", "after_reply")
    assert "anchor" not in decisions[0], (
        "F09's first after_reply decision now carries an anchor — dropping its title no longer "
        "produces the assert-nothing shape, so this mutation must move to an entry that has none")
    decisions[0].pop("title")

    with pytest.raises(SystemExit) as ex:
        run_filing._check_set(manifest, mutated)

    assert "F09-meeting-parks" in str(ex.value)
    assert "neither a `title` nor an `anchor`" in str(ex.value)


def test_the_consistency_check_refuses_a_title_less_decision_written_before_a_titled_one(
        expectations, manifest):
    """The ordering half. An anchor-only entry is the WEAKEST matcher and greedy pairing walks the
    file's own order, so one written first can take the page a titled sibling needed and score a
    correct page set a miss — which
    `test_filing_scorer.test_a_title_less_entry_written_FIRST_starves_its_titled_sibling…`
    demonstrates against the scorer directly.

    Staged by giving F09's first entry an anchor and dropping its title, so the set carries an
    anchor-only entry BEFORE a titled one — the shape the refusal exists for. The message has to
    name the repair ("write the titled entries first"), because nothing about a red decisions cell
    would point at line order.
    """
    mutated, decisions = _mutated_decisions(expectations, "F09-meeting-parks", "after_reply")
    decisions[0].pop("title")
    decisions[0]["anchor"] = {"kind": "company", "ids": []}
    assert "title" in decisions[1], "the mutation needs a TITLED entry after the title-less one"

    with pytest.raises(SystemExit) as ex:
        run_filing._check_set(manifest, mutated)

    assert "F09-meeting-parks" in str(ex.value)
    assert "Write the titled entries first" in str(ex.value)


def test_the_same_two_entries_in_the_SAFE_order_are_accepted(expectations, manifest):
    """The ordering refusal's own benign twin, and the one that decides whether it is safe to have:
    a guard that refused the title-less shape outright would make the whole capability unusable.
    Same mutation, anchor-only entry written LAST — accepted."""
    mutated, decisions = _mutated_decisions(expectations, "F09-meeting-parks", "after_reply")
    decisions[0].pop("title")
    decisions[0]["anchor"] = {"kind": "company", "ids": []}
    decisions.reverse()

    run_filing._check_set(manifest, mutated)              # must not raise


def test_whether_the_shipped_set_uses_the_title_less_shape_at_all(entries):
    """**The scope note for both refusals above, asserted rather than assumed.**

    `_decisions_match` lets an entry omit `title` and pair on its anchor alone, and `_check_set`
    bounds where such an entry may sit. Today NO shipped expectation uses it — every decision entry
    names a title — so both refusals are pre-emptive and their shipped-set twin proves only that
    the set passes them, never that the shape is exercised.

    Pinned in the direction that is true now, so the day an expectation drops a title this test
    reports it: at that point the refusals acquire a live instance, and the mutation-driven tests
    above should be re-pointed at it rather than constructing the shape by hand.
    """
    titleless = [f"{e['id']}.{name}"
                 for e in entries
                 for name, block in (("expect", e.get("expect")),
                                     ("after_reply", e.get("after_reply")))
                 if block
                 for d in block.get("decisions") or []
                 if "title" not in d]

    assert titleless == [], (
        f"the shipped set now omits a decision title in {titleless} — the `_check_set` refusals "
        f"above are no longer pre-emptive, and this test's own docstring is the thing to update")


def test_the_consistency_check_refuses_a_set_whose_denominators_moved(expectations, manifest):
    """The check that makes the other three add up to the set the series was calibrated on. A
    capture that quietly stops naming a facet does not fail anything — it raises that facet's
    score by leaving its denominator, and no rendered table shows it."""
    mutated = json.loads(json.dumps(expectations))
    dropped = next(e for e in mutated["expectations"] if "edits" in e["expect"])
    dropped["expect"].pop("edits")
    with pytest.raises(SystemExit) as ex:
        run_filing._check_set(manifest, mutated)
    assert "edits: pinned 1, file has 0" in str(ex.value)


def test_every_capture_material_exists_and_the_duplicate_case_reuses_F01s_bytes(manifest):
    """F04 pointing at F01's file is load-bearing: the dedup levels key on the payload's own
    sha256, so a second file kept in sync by hand would stop being a duplicate the first time
    somebody fixed a typo in one of them."""
    by_id = {c["id"]: c for c in manifest["captures"]}
    for capture in manifest["captures"]:
        assert (CAPTURES / capture["material"]).is_file(), capture["id"]
    assert (by_id["F04-duplicate-rejection"]["material"]
            == by_id["F01-plain-note-known-entity"]["material"])


def test_the_duplicate_case_is_submitted_by_someone_else_than_the_page_it_duplicates(manifest):
    """Same submitter inside the dedup window is level 1 — retry collapse — and answers `filed`,
    not `rejected`. Fold these two onto one submitter and F04 silently stops measuring the
    duplicate refusal while still looking like a capture that passes."""
    by_id = {c["id"]: c for c in manifest["captures"]}
    assert (by_id["F04-duplicate-rejection"]["submitted_by"]
            != by_id["F01-plain-note-known-entity"]["submitted_by"])


def test_every_capture_declares_a_kind_the_queue_accepts_and_meetings_carry_their_hints(manifest):
    """A meeting submitted without title/date/attendees is a different capture from the one the
    expectation was written for."""
    for capture in manifest["captures"]:
        assert capture["kind"] in schema.KINDS, capture["id"]
        if capture["kind"] != schema.MEETING:
            continue
        hints = capture.get("hints") or {}
        assert all(hints.get(k) for k in ("title", "meeting_date", "attendees")), capture["id"]


def test_no_capture_material_steers_the_offline_double(manifest):
    """`DOUBLE:` directives drive `librarian/double.py`. One in this set would make the golden
    measure the fixture instead of the backend, and would do it silently — the double would file
    exactly what the directive says and the table would read as a strong score.

    Detection uses the double's OWN regex, so this cannot pass because the directive spelling
    moved; the twin below proves the regex still matches something.
    """
    for capture in manifest["captures"]:
        material = (CAPTURES / capture["material"]).read_text(encoding="utf-8")
        assert not double.DIRECTIVE_RE.search(material), capture["id"]


def test_the_directive_detector_used_above_really_detects_a_directive():
    """The benign twin: without it, a regex that stopped matching anything would turn the test
    above into a permanently-green line that reads as coverage."""
    assert double.DIRECTIVE_RE.search("some prose\nDOUBLE:company\nmore prose")


# ── the expectations, checked against the vocabulary the code actually uses ────────────────────

def test_every_facet_an_expectation_names_is_a_facet_the_scorer_knows(entries):
    """`score_phase` ignores a key it does not recognize. So a typo (`achor:`) does not fail —
    it removes that capture from that facet's denominator in silence, which is the one failure
    mode the per-facet design cannot survive."""
    for entry in entries:
        for block in _expect_blocks(entry):
            unknown = sorted(set(block) - set(run_filing.FACETS))
            assert not unknown, f"{entry['id']} names facets the scorer does not score: {unknown}"


def test_the_facet_names_the_bars_file_carries_are_the_scorers_own(entries):
    """A bar keyed under a name no facet has is a bar that can never fire: `aggregate` looks the
    facet up in `FILING_BARS` and a miss reads as `None` — REPORT, DO NOT JUDGE — forever."""
    assert set(bars.FILING_BARS) == set(run_filing.QUALITY_FACETS)
    assert not set(bars.FILING_BARS) & set(run_filing.COST_FACETS), (
        "attempts/bounces are cost axes and must stay unbarred")


def test_every_expected_status_is_a_terminal_status_the_queue_can_actually_reach(entries):
    reachable = {schema.FILED, schema.NEEDS_INPUT, schema.TRIAGE, schema.REJECTED, schema.FAILED}
    for entry in entries:
        for block in _expect_blocks(entry):
            if "status" in block:
                assert block["status"] in reachable, entry["id"]


def test_every_expected_reason_is_a_real_rejection_reason_code(entries):
    for entry in entries:
        for block in _expect_blocks(entry):
            if "reason" in block:
                assert block["reason"] in schema.REJECTION_REASONS, entry["id"]


def test_every_expected_type_is_a_real_page_type_landing_in_that_types_own_folder(entries):
    """Type and folder are two spellings of one decision, and `librarian/page.py` owns the map.
    An expectation pairing `concept` with `wiki/notes` would score both facets against a page no
    correct backend can produce."""
    meeting_folder = "wiki/meetings"          # provenance pages have no `PageType.folder`
    for entry in entries:
        for block in _expect_blocks(entry):
            if "type" not in block:
                continue
            page_type = block["type"]
            assert page_type in page.ALL_PAGE_TYPES, entry["id"]
            if "folder" not in block:
                continue
            expected_folder = page.FOLDER_BY_TYPE.get(page_type, meeting_folder)
            assert block["folder"] == expected_folder, entry["id"]


def test_the_meeting_folder_this_file_pins_is_the_one_processing_writes_into():
    """The one folder above that `page.FOLDER_BY_TYPE` cannot supply, tied to its real source so
    the literal cannot outlive it."""
    from stigmergy.librarian import processing
    assert processing.MEETING_MEETING_PREFIX == "wiki/meetings/"


def test_every_expected_anchor_resolves_in_the_fixture_registry(entries, registry):
    """The `foxglove-health` lesson from the retrieval golden, applied before this set is ever
    run: an expected id that resolves to nothing caps its facet at a miss forever, and nothing in
    a real run says so — the page files, the score drops, and the backend takes the blame."""
    for entry in entries:
        for block in _expect_blocks(entry):
            for anchor in _anchors_of(block):
                assert anchor["kind"] in ("entity", "company"), entry["id"]
                for entity_id in anchor.get("ids") or []:
                    assert registry.canonical_id(entity_id) == entity_id, (
                        f"{entry['id']} expects the anchor {entity_id!r}, which the fixture "
                        f"registry does not resolve to itself")


def test_a_company_wide_expectation_names_no_entity(entries):
    """`{kind: company, ids: []}` and `{kind: entity, ids: [x]}` are different outcomes and the
    scorer compares both halves; a `company` anchor carrying ids could never be matched."""
    for entry in entries:
        for block in _expect_blocks(entry):
            for anchor in _anchors_of(block):
                if anchor["kind"] == "company":
                    assert not anchor.get("ids"), entry["id"]


def _anchors_of(block: dict) -> list:
    anchors = [block["anchor"]] if "anchor" in block else []
    return anchors + [d["anchor"] for d in block.get("decisions") or [] if "anchor" in d]


def test_no_expectation_names_an_empty_edits_list(entries):
    """`edits: []` is the one spelling this facet must never carry again.

    It is scored by containment (`_edits_match`: expected ⊆ observed), so an empty list is
    vacuously TRUE for every backend — and still fills the denominator. That is a cell which reads
    as measured, prints a score, and can never fail: the permanently-green instrument this suite
    exists to prevent, one facet wide. "This capture owes no edit" is said by naming NO `edits`
    key, because silence is not scored.

    F01 carried the empty list until the first Sonnet-5 baseline scored it a miss for correct
    filing. The fix removed the key; this is what stops the next editor from putting it back —
    the runner has no refusal for it yet.
    """
    for entry in entries:
        for block in _expect_blocks(entry):
            assert block.get("edits") != [], (
                f"{entry['id']} names an empty `edits` list: under containment that is true for "
                f"every backend. Say 'owes no edit' by naming no `edits` key at all.")


def test_every_expected_edit_path_is_a_page_that_already_exists_in_the_fixture_repo(entries):
    """`edits` scores whether the backend recognised that an EXISTING page was owed a reciprocal
    link. A page invented in the same commit could not prove that, and a path that does not exist
    at all cannot be edited by anything."""
    for entry in entries:
        for block in _expect_blocks(entry):
            for path in block.get("edits") or []:
                assert (REPO / path).is_file(), f"{entry['id']} expects an edit to {path}"


# ── the ask-back loop the two parking captures measure ─────────────────────────────────────────

def test_every_parking_expectation_carries_a_reply_and_an_after_reply_block(entries):
    """The park is only half the case. Without a reply and an `after_reply` block the runner
    scores one phase and the ask-back loop — the thing these two captures exist for — is never
    exercised at all."""
    for entry in entries:
        parks = entry["expect"].get("status") == schema.NEEDS_INPUT
        assert parks == ("park_question" in entry["expect"]), entry["id"]
        assert parks == ("reply" in entry), entry["id"]
        assert parks == ("after_reply" in entry), entry["id"]
        if parks:
            assert entry["reply"].strip(), entry["id"]
            assert entry["after_reply"].get("status") == schema.FILED, entry["id"]


def test_every_parked_name_is_absent_from_the_registry_and_present_in_its_own_material(
        manifest, entries, registry):
    """Register either of these and the capture stops parking — it files, quietly, and two
    `status` cells plus both `park_question` cells move without anyone touching the backend."""
    materials = {c["id"]: (CAPTURES / c["material"]).read_text(encoding="utf-8")
                 for c in manifest["captures"]}
    parked = [(e["id"], name) for e in entries for name in e["expect"].get("park_question") or []]
    assert len(parked) == 2, "the set measures the ask-back loop with exactly two captures"
    for capture_id, name in parked:
        assert registry.canonical_id(name) is None, f"{name!r} is registered — {capture_id} " \
                                                    f"can no longer park on it"
        assert name.lower() in materials[capture_id].lower(), capture_id


def test_a_reply_that_resolves_an_anchor_names_an_entity_the_registry_knows(entries, registry):
    """The reply is the input the re-file is scored on. One naming a company the registry cannot
    resolve would park a second time, and the `after_reply` anchor could never be reached."""
    for entry in entries:
        anchor = entry.get("after_reply", {}).get("anchor") or {}
        for entity_id in anchor.get("ids") or []:
            title = registry.title(entity_id)
            assert title and title.lower() in entry["reply"].lower(), (
                f"{entry['id']}'s reply never names {title!r}, the entity its after_reply "
                f"expects the capture to anchor to")


def test_no_expected_decision_title_can_swallow_a_later_ones_page(entries):
    """A trap the loose titles introduced, and one that fails a CORRECT backend — the direction
    that costs the most to diagnose.

    `_decisions_match` walks the expected list in order and takes the FIRST page each title
    matches, greedily. So a title whose words are a subset of a later title's ("Wren" before
    "Wren summary") can consume the later one's page, leaving it nothing to match and scoring a
    miss for a page set that was exactly right. The shipped order puts the more specific title
    first; this keeps it that way, and is checked here rather than left to whoever edits the file
    next, because nothing about the failure would point at the ordering.

    **This test is about TITLES and is structurally blind to the title-LESS case — deliberately,
    and the case is owned elsewhere.** An entry that omits `title` is the broadest matcher there
    is, and it can starve a titled sibling exactly the way a subset title can. It cannot be seen
    from here: `d.get("title", "")` renders an absent title as `""`, and `title_matches("")` is
    False by design, so every comparison against it is vacuously satisfied. Widening this loop to
    cover it would mean re-implementing the ordering rule in a second place that could drift from
    the first.

    So it is skipped explicitly rather than silently, and the owner is named:
    `_check_set`'s title-less-before-titled refusal, exercised by
    `test_the_consistency_check_refuses_a_title_less_decision_written_before_a_titled_one` above,
    with the scorer-level hazard it protects against demonstrated in
    `test_filing_scorer.test_a_title_less_entry_written_FIRST_starves_its_titled_sibling…`.
    """
    for entry in entries:
        for block in _expect_blocks(entry):
            titles = [d["title"] for d in block.get("decisions") or [] if "title" in d]
            for i, earlier in enumerate(titles):
                for later in titles[i + 1:]:
                    assert not run_filing.title_matches(earlier, later), (
                        f"{entry['id']}: the expected title {earlier!r} is a word-subset of the "
                        f"later {later!r}, so greedy matching can hand it {later!r}'s page")


# ── the mini knowledge repo it files into ──────────────────────────────────────────────────────

def test_the_fixture_repo_holds_the_pages_provenance_claims():
    """Read through `corpus.load_pages`, the same walk the index builder runs, so a page the
    walker would not see is not counted here either."""
    pages = corpus.load_pages(str(REPO))
    assert len(pages) == FIXTURE_PAGES
    data = json.loads((REPO / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert data["pages"] == FIXTURE_PAGES
    assert data["entities"] == FIXTURE_ENTITIES
    assert _SHA_RE.fullmatch(data["stigmergy_sha"] or "")


def test_the_registry_and_the_acl_sit_where_the_librarian_reads_them(registry):
    """Both are read by relative path out of the commit being filed against. In the wrong place
    the registry is EMPTY, which does not fail — every capture parks on an unknown entity and the
    anchor facet reports a backend that cannot anchor."""
    assert (REPO / librarian_config.REGISTRY_RELPATH).is_file()
    assert (REPO / librarian_config.ACL_RELPATH).is_file()
    assert len(registry.entities) == FIXTURE_ENTITIES


def test_the_fixture_repo_carries_both_briefs_the_measured_backend_reads(entries):
    """`agent.read_skill` reads the librarian brief out of the item's own worktree and
    `worker.startup_checks` refuses an `sdk` run without one; the meeting brief is read on the
    first meeting row. A mini repo missing either scores config failures as filing quality."""
    assert (REPO / librarian_agent.SKILL_RELPATH).is_file()
    assert (REPO / librarian_agent.MEETING_BRIEF_RELPATH).is_file()


def test_every_fast_lane_type_has_a_template_to_draft_from():
    """The agent's system prompt sends it to `ops/templates/`. A creatable type with no template
    there is briefed differently in the eval than in production, which is a difference the score
    would carry silently."""
    stems = {p.stem for p in (REPO / "ops" / "templates").glob("*.md")}
    assert set(page.FOLDER_BY_TYPE) <= stems


def test_each_frozen_copy_exists_and_records_the_commit_it_was_taken_at():
    """Not compared with the live knowledge repo, on purpose (see the module docstring). What
    must hold is that each copy is present and still says WHICH version it is — without that sha,
    "which brief scored this run?" has no answer and the series stops being comparable.

    A copy whose knowledge-repo commit does not exist yet may say `PENDING-KNOWLEDGE-REPO-SHA`
    instead, and only that: see `_PENDING_SHA` above for why a placeholder beats a stale sha here,
    and for the three things that keep it from becoming permanent.
    """
    data = json.loads((REPO / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert len(data["frozen_copies"]) == len(FROZEN_MARKERS)
    for relpath in data["frozen_copies"]:
        assert (REPO / relpath).is_file(), relpath
    for marker in FROZEN_MARKERS:
        text = marker.read_text(encoding="utf-8")
        assert _SHA_RE.search(text) or _PENDING_SHA in text, (
            f"{marker.parent.name}/FROZEN.md records neither a 40-character source commit sha nor "
            f"{_PENDING_SHA}")


def test_the_knowledge_repo_landing_is_DONE_and_no_copy_is_pending_any_more():
    """**The landing, asserted as an accomplished fact rather than tolerated as a state.**

    While the knowledge-repo PR was unlanded these four files carried `PENDING-KNOWLEDGE-REPO-SHA`,
    and the assertions around them were written to tolerate it and to go green without an edit the
    day it was replaced. It has been replaced. Tolerance nobody can exercise is a permanently-green
    check reading as coverage, so what the tolerance becomes is this: the pending state is now
    ABSENT, and the day somebody re-freezes the brief and re-enters that state deliberately, this
    is the test that says the landing is unfinished.

    The bounded-allowance machinery below stays, and is what makes re-entering it survivable
    rather than silent.
    """
    pending = {marker.parent.name for marker in FROZEN_MARKERS
               if _PENDING_SHA in marker.read_text(encoding="utf-8")}

    assert not pending, (
        f"{sorted(pending)} still carries {_PENDING_SHA}. If a re-freeze put it back deliberately, "
        f"the knowledge-repo commit has to land and every copy plus PROVENANCE.json takes its sha "
        f"together — see tests/librarian/test_librarian_brief_contract.py's landing tripwire")


def test_a_copy_that_is_still_pending_says_so_in_the_one_place_it_is_allowed_to():
    """The placeholder, BOUNDED — the machinery that makes RE-ENTERING the pending state
    survivable, now that the state itself is behind us.

    Two properties, and each closes a way the tolerance could rot into a permanent hole:

    * only an enumerated copy may carry it, so a second file cannot quietly acquire one and inherit
      the exemption;
    * a file carrying it must also carry the command that replaces it, so whoever lands the
      knowledge-repo PR reads the fix in the file rather than having to reconstruct it.

    A SUBSET rather than an equality, which is what let it go green when the sha landed without an
    edit here — and what lets it keep working for the next re-freeze.
    """
    pending = {marker for marker in FROZEN_MARKERS
               if _PENDING_SHA in marker.read_text(encoding="utf-8")}

    assert pending <= set(_PENDING_ALLOWED), (
        f"{sorted(p.parent.name for p in pending - set(_PENDING_ALLOWED))} carries "
        f"{_PENDING_SHA}, and only the librarian brief's copy is allowed to be pending")
    for marker in pending:
        assert _PENDING_REPLACEMENT_COMMAND in marker.read_text(encoding="utf-8"), (
            f"{marker.parent.name}/FROZEN.md carries {_PENDING_SHA} without the command that "
            f"replaces it — a placeholder whose fix is not written beside it is how a placeholder "
            f"becomes permanent")


def test_provenance_and_every_frozen_copy_name_the_same_source_commit():
    """One freeze, ONE commit — the fact four separate files each claim.

    They drifted apart once already: `PROVENANCE.json` named a commit whose librarian brief was
    not the brief in this tree, and `eval_history.corpus_provenance` copies that value into every
    `suite: "filing"` row. A score naming the wrong brief version is the one thing the series
    exists to answer, answered wrongly, and nothing about a run would show it.

    **A pending copy is the one exemption, and it is a narrow one**: a placeholder makes no claim
    about a commit, so it cannot disagree with `PROVENANCE.json`. A copy carrying a REAL sha that
    differs still fails — which is exactly what catches the half-finished landing where the
    librarian's `FROZEN.md` is filled in and `PROVENANCE.json` is left naming the old brief.
    """
    sha = json.loads((REPO / "PROVENANCE.json").read_text(encoding="utf-8"))["stigmergy_sha"]
    assert _SHA_RE.fullmatch(sha)
    for marker in FROZEN_MARKERS:
        text = marker.read_text(encoding="utf-8")
        assert sha in text or _PENDING_SHA in text, (
            f"{marker.parent.name}/FROZEN.md does not record PROVENANCE.json's {sha}. If the "
            f"knowledge-repo PR has just landed, PROVENANCE.json's stigmergy_sha and ALL THREE "
            f"FROZEN.md files take the new commit together — that is what 'one freeze, one commit' "
            f"means, and a partial update is the drift this test exists for")


def test_the_frozen_copies_are_byte_for_byte_the_bytes_the_series_was_measured_under():
    """The freeze, ENFORCED rather than requested.

    `FROZEN.md` asks a human not to resync these three files, and this is what makes the request
    checkable: their content is the largest single input to filing quality (the librarian brief),
    the contract every filing is judged against (the linter), and the meeting contract's own half
    (the distiller brief). A silent edit to any of them re-grades every score already recorded
    while the table keeps printing the same shape.

    Deliberately NOT a comparison with the live knowledge repo — that is the drift guard
    `FROZEN.md` refuses on purpose, and it is what `tests/librarian/test_frozen_linter.py` does for
    the OTHER copy, which has the opposite rule. A deliberate re-freeze updates these pins in the
    same commit as the bytes; that is the review moment this test exists to force.
    """
    for relpath, expected in FROZEN_SHA256.items():
        digest = hashlib.sha256((REPO / relpath).read_bytes()).hexdigest()
        assert digest == expected, (
            f"{relpath} is not the copy this series was calibrated on. If this was a deliberate "
            f"re-freeze, update FROZEN_SHA256 and the FROZEN.md/PROVENANCE shas in this same "
            f"commit and retire the baseline (evals/README.md); otherwise revert the edit.")


def test_the_byte_pins_cover_every_frozen_copy_provenance_declares():
    """The benign twin of the pins above: a fourth frozen copy added to the tree without a pin
    would sit unguarded behind a test that keeps passing for the other three."""
    declared = set(json.loads((REPO / "PROVENANCE.json").read_text(encoding="utf-8"))
                   ["frozen_copies"])
    assert declared == set(FROZEN_SHA256)


def test_no_page_in_the_fixture_repo_writes_about_the_measurement_it_serves():
    """The pages here are INPUT to the thing being measured — the agent reads this repo while it
    files — so a page that describes the eval briefs the backend under measurement about how it is
    being scored. That is not a documentation nicety: it is the fixture telling the model the
    answer, and a score collected that way measures the fixture's prose.

    Checked over the whole page text rather than the body alone: a title or a tag saying it is as
    loud as a paragraph.
    """
    offenders = []
    for path in sorted((REPO / "wiki").rglob("*.md")):
        words = set(re.findall(r"[a-z]+", path.read_text(encoding="utf-8").lower()))
        for term in MEASUREMENT_VOCABULARY:
            if term in words:
                offenders.append(f"{path.relative_to(REPO)}: {term!r}")
    assert not offenders, ("fixture pages describing the measurement they take part in:\n  "
                           + "\n  ".join(offenders))


def test_the_measurement_vocabulary_check_can_actually_fire(tmp_path):
    """Its benign twin, and the reason it is a word-set match rather than a substring one:
    `substring` would flag "several" for `eval` and the check would be quietly disabled the first
    time somebody added an exception for it. So: whole words only, and proof that a whole word is
    still caught."""
    words = set(re.findall(r"[a-z]+", "This page explains how the golden set is scored.".lower()))
    assert any(term in words for term in MEASUREMENT_VOCABULARY)
    innocent = set(re.findall(r"[a-z]+", "Several depots evaluated the backhaul.".lower()))
    assert not any(term in innocent for term in MEASUREMENT_VOCABULARY)


def test_the_mini_repo_passes_its_own_frozen_contract_linter():
    """`gate_contract` materializes the linter from the base commit and runs it on every filing.
    A fixture that does not pass its own linter would bounce correct filings, and the table would
    read as a backend that cannot write a legal page."""
    proc = subprocess.run([sys.executable, str(LINTER), "--repo", str(REPO), "--json", "--strict"],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["summary"] == {"errors": 0, "warnings": 0}
