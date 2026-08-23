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

**What this file deliberately does NOT check**: that the two frozen copies under
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
# MOVED for issue #77: three entity pages joined the tree (a name written several ways, a false
# friend sharing its prefix, and one nearly always written as an unregistered short form), which is
# what F11-F14 measure resolution against. Growing the yardstick is a deliberate act — see
# `evals/README.md`'s growth protocol and `EXPECTED_DENOMINATORS`, which moved in the same commit.
FIXTURE_PAGES = 8
FIXTURE_ENTITIES = 6

# The `FROZEN.md` beside each frozen copy. Their SHAs are NOT compared with the knowledge repo
# (see the module docstring); what is checked is that each one still records one, and that all of
# them record the SAME one as `PROVENANCE.json`.
#
# There were THREE. The meeting-distiller copy was deleted when the librarian became one pipe:
# `agent.read_meeting_brief` read it on a `kind="meeting"` row, that reader is gone with the flow,
# and a frozen copy nothing reads is a byte pin whose failure would say nothing about any
# measurement. The deletion moves no score — it touched neither the pages, the registry, the
# templates, the linter nor the librarian brief — and `PROVENANCE.json` records it in
# `frozen_copies_removed` rather than letting the list shrink silently.
FROZEN_MARKERS = (
    REPO / ".claude" / "tools" / "FROZEN.md",
    REPO / ".claude" / "skills" / "librarian" / "FROZEN.md",
)
_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")

# ── the one state a frozen copy may be in WITHOUT a sha, and only until its PR lands ───────────
# the structured filing flow rewrote the librarian brief, and the platform PR and the knowledge-repo PR land together:
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
# The librarian brief's pin has moved FIVE times, deliberately, and every move was the same kind
# of event: not an edit to a yardstick but a NEW yardstick, which retires the series with it per
# `evals/README.md`.
#
#   1. the structured filing flow rewrote the brief backend-NEUTRAL (no tool mechanics in it at all).
#   2. The `sdk` retirement closed that rewrite's last debt: the brief's environment paragraph still
#      told its reader that "some runs of this skill hold tools and a checkout, and write the page
#      themselves", which stopped being true of ANY run when the tool-holding backend went. That
#      paragraph became the tool-less statement, at knowledge-repo commit
#      `c1e0996ed497e70a9df82661c367294b48207a16`.
#   3. the agentic pydantic harness gave the ordinary run its tools BACK, so a brief describing one run style stopped
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
#   5. the file-first write path — file first, govern after. The brief stops answering a name the registry does not
#      know with a question to the submitter and answers it with a PROPOSAL: the agent declares the
#      identity it read out of the material and `librarian.identity` creates the page, with
#      `approved_by:` empty, in the same commit as the capture. Knowledge-repo commit
#      `e118c8a38c2bd447f27be5d5e07a7c9b9df57cce`.
#
# All THREE numbers moved together at that last commit, which is the first time that has happened
# and the reason `PROVENANCE.json`'s `stigmergy_sha_note` no longer needs its "not the commit each
# copy was taken at" caveat: the meeting brief lost the meeting park and the linter learned the
# `approved_by:` / `proposed_aliases:` lifecycle a proposal lands in. Before it, one freeze meant one
# commit and not one freeze, every copy edited — read the four moves above with that in mind.
# RE-FROZEN AGAIN when the `edits` declaration was removed. The librarian brief's number moved and
# the linter's did not, and this is a CORRECTION rather than a re-grade for the same reason the one
# pipe's was: the shipped copy told the agent to declare additive `backlink`/`overlap`/
# `contradiction` edits against pages that already exist, and there is no such field any more — a
# brief promising a mechanism the worker does not have is not a yardstick either. The `edits` FACET
# retired with it (evals/README.md, and `run_filing.QUALITY_FACETS`), so no recorded score loses a
# comparison it had: that column simply stops existing. The knowledge-repo commit these bytes will
# carry does not exist yet, which is why both FROZEN.md copies say PENDING-KNOWLEDGE-REPO-SHA.
FROZEN_SHA256 = {
    ".claude/tools/stigmergy_lint.py":
        "679796fa3e87f3ce984869353d3997428b7f879fa5c0e182e803f78011f69736",
    ".claude/skills/librarian/SKILL.md":
        "f1bcdadc53d01d4c0fa3ee657674d85dcc1f8e269fd755f2e7618a655be1e4cc",
}
# The meeting-distiller brief's pin — `76d3967f…` — was REMOVED with the copy, not relaxed.
#
# Both surviving numbers MOVED at the one pipe, and that re-freeze is a correction rather than a
# re-grade: the brief this fixture shipped had stopped being able to file at all — it offered a
# `decision` page type the placement table has no folder for and told the agent "one capture yields
# one page" — so every capture it briefed would have been refused before any behaviour was
# measured. A brief that cannot file is not a yardstick. `evals/README.md` records which facets
# stopped being comparable.

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
    """Every scored moment an entry declares — exactly one since the file-first write path retired the park.

    Kept as a list rather than inlined: every loop below walks it, and the day a capture legitimately
    scores twice again they should all follow without being found one at a time.
    """
    return [entry["expect"]]


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


@pytest.mark.parametrize("key", run_filing.RETIRED_ENTRY_KEYS)
def test_the_consistency_check_refuses_the_retired_ask_back_keys(key, expectations, manifest):
    """**INVERTED by the file-first write path.** This refusal used to demand that `reply` and `after_reply` travel
    TOGETHER — half an ask-back case was measured on its first phase alone and the table said
    nothing. Nothing waits on a person any more: `BrainService.reply` is gone, `_drive` scores one
    phase per capture, and an entry carrying either key would have that half read by nobody. So the
    refusal keeps its job — a silently unmeasured phase — and reverses its condition.

    Parametrized over the runner's own tuple rather than two literals, so a third retired key
    acquires this test in the same commit that names it.
    """
    mutated = json.loads(json.dumps(expectations))
    entry = mutated["expectations"][0]
    entry[key] = "anything at all"

    with pytest.raises(SystemExit) as ex:
        run_filing._check_set(manifest, mutated)

    assert entry["id"] in str(ex.value) and key in str(ex.value)


def test_the_shipped_set_carries_neither_retired_key(entries):
    """The benign twin of the refusal above, asserted over the shipped set rather than a mutation:
    the yardstick itself has to be in the state the refusal demands, or `_check_set` would fail
    every run against it and no test above would say why."""
    carriers = {entry["id"]: sorted(set(entry) & set(run_filing.RETIRED_ENTRY_KEYS))
                for entry in entries if set(entry) & set(run_filing.RETIRED_ENTRY_KEYS)}
    assert not carriers, carriers


# ── the fifth refusal: what a `pages` entry has to assert, and in what order ──────────────────
# Both halves guard one mechanism from opposite ends, and both are SET defects that would otherwise
# read as a backend result — which is the expensive direction: a red cell on a paid run, caused by
# two lines of JSON, pointing at nothing.
#
# **The title-less shape is LIVE in the shipped set now, and that is a change worth reading.** Until
# the one pipe landed, no expectation omitted a `title` and both refusals below were pre-emptive:
# their shipped-set twin proved only that the set passes them, never that the shape is exercised.
# F08's second entry omits its title deliberately — the word `review` was stable across three runs
# of a flow that no longer exists, while its company-wide anchor discriminates it from its sibling
# with no vocabulary in it at all — so the refusals have a live instance and
# `test_the_shipped_set_uses_the_title_less_shape_where_it_says_it_does` is what keeps that honest.
#
# The mutations below stay staged on **F09**, whose two entries are titles with NO anchor: the
# stored reply that used to name a registered entity is gone, and a proposed identity's id is
# slugified from a name the agent chose, so there is no anchor left to assert. That is the older
# "scores the title alone" shape, and it is the only place in the set where a title can be STRIPPED
# to produce the assert-nothing case each mutation needs.
def _mutated_pages(expectations, entry_id: str, block: str) -> tuple:
    """The mutable copy and the `pages` list this refusal is about, for one shipped entry."""
    mutated = json.loads(json.dumps(expectations))
    entry = next(e for e in mutated["expectations"] if e["id"] == entry_id)
    return mutated, entry[block]["pages"]


def test_the_consistency_check_refuses_a_page_asserting_neither_title_nor_anchor(expectations,
                                                                                manifest):
    """An entry that names neither matches whatever page is left on the table — a facet that reads
    as measured and measures nothing, arrived at from the other side.

    Staged on F09's first page, which ships a title and no anchor: stripping its title is the
    edit somebody would really make (a title that turned out to be the model's prose), and it
    leaves the entry asserting nothing at all.
    """
    mutated, pages = _mutated_pages(expectations, "F09-meeting-proposes", "expect")
    assert "anchor" not in pages[0], (
        "F09's first page now carries an anchor — dropping its title no longer produces the "
        "assert-nothing shape, so this mutation must move to an entry that has none")
    pages[0].pop("title")

    with pytest.raises(SystemExit) as ex:
        run_filing._check_set(manifest, mutated)

    assert "F09-meeting-proposes" in str(ex.value)
    assert "neither a `title` nor an `anchor`" in str(ex.value)


def test_the_consistency_check_refuses_a_title_less_page_written_before_a_titled_one(
        expectations, manifest):
    """The ordering half. An anchor-only entry is the WEAKEST matcher and greedy pairing walks the
    file's own order, so one written first can take the page a titled sibling needed and score a
    correct page set a miss — which
    `test_filing_scorer.test_a_title_less_entry_written_FIRST_starves_its_titled_sibling…`
    demonstrates against the scorer directly.

    Staged by giving F09's first entry an anchor and dropping its title, so the set carries an
    anchor-only entry BEFORE a titled one — the shape the refusal exists for. The message has to
    name the repair ("write the titled entries first"), because nothing about a red `pages` cell
    would point at line order.
    """
    mutated, pages = _mutated_pages(expectations, "F09-meeting-proposes", "expect")
    pages[0].pop("title")
    pages[0]["anchor"] = {"kind": "company", "ids": []}
    assert "title" in pages[1], "the mutation needs a TITLED entry after the title-less one"

    with pytest.raises(SystemExit) as ex:
        run_filing._check_set(manifest, mutated)

    assert "F09-meeting-proposes" in str(ex.value)
    assert "Write the titled entries first" in str(ex.value)


def test_the_same_two_entries_in_the_SAFE_order_are_accepted(expectations, manifest):
    """The ordering refusal's own benign twin, and the one that decides whether it is safe to have:
    a guard that refused the title-less shape outright would make the whole capability unusable.
    Same mutation, anchor-only entry written LAST — accepted."""
    mutated, pages = _mutated_pages(expectations, "F09-meeting-proposes", "expect")
    pages[0].pop("title")
    pages[0]["anchor"] = {"kind": "company", "ids": []}
    pages.reverse()

    run_filing._check_set(manifest, mutated)              # must not raise


def test_the_shipped_set_uses_the_title_less_shape_where_it_says_it_does(entries):
    """**The scope note for both refusals above, asserted rather than assumed.**

    OLD BEHAVIOUR: this asserted that NO shipped expectation omitted a title, so both refusals were
    pre-emptive and this test's job was to report the day that changed. That day is this one. F08's
    second page omits its title and pairs on its company-wide anchor alone, which is what gives the
    refusals a live instance — and what makes the mutation-driven tests above a second, constructed
    case rather than the only one.

    Pinned to the ONE entry that is allowed to have the shape, not to a count: a title quietly
    dropped somewhere else is an expectation that stopped scoring the model's judgment about what a
    page is called, and it must not be able to arrive unannounced.
    """
    titleless = [(e["id"], n)
                 for e in entries
                 for block in _expect_blocks(e)
                 for n, entry in enumerate(block.get("pages") or [])
                 if "title" not in entry]

    assert titleless == [("F08-meeting-two-decisions", 1)], (
        f"the shipped set's title-less entries are {titleless}, and only F08's SECOND page is "
        f"written that way on purpose — see expected/expectations.json's `why` for F08. A title "
        f"dropped elsewhere stops scoring a judgment; one added back to F08 re-pins the yardstick "
        f"to a word measured under the retired meeting flow.")


def test_the_consistency_check_refuses_a_set_whose_denominators_moved(expectations, manifest):
    """The check that makes the other three add up to the set the series was calibrated on. A
    capture that quietly stops naming a facet does not fail anything — it raises that facet's
    score by leaving its denominator, and no rendered table shows it."""
    mutated = json.loads(json.dumps(expectations))
    dropped = next(e for e in mutated["expectations"] if "reason" in e["expect"])
    dropped["expect"].pop("reason")
    with pytest.raises(SystemExit) as ex:
        run_filing._check_set(manifest, mutated)
    assert "reason: pinned 1, file has 0" in str(ex.value)


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
    """**NARROWED by the file-first write path.** The reachable set used to include `needs_input` and `triage`, the two
    states a capture waited on a person in; they are `schema.RETIRED_STATUSES` now and the queue's
    own CHECK constraint refuses them by name. `resolved` is deliberately absent too: it survives
    read-only on rows a steward closed by hand and nothing reaches it. So the set a claim can be
    FINISHED into is exactly what an expectation may name, and it is read from the queue rather than
    retyped here.
    """
    reachable = set(schema.FINISHED_STATUSES)
    assert not reachable & set(schema.RETIRED_STATUSES)
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
    correct backend can produce.

    OLD BEHAVIOUR: the folder came from `FOLDER_BY_TYPE.get(page_type, "wiki/meetings")`, because
    `meeting` was a type the placement table gave no folder and the meeting flow wrote there
    anyway. That fallback is gone with the flow, and its absence is what makes this strict: every
    type an expectation may name is one the fast lane can CREATE, so its folder is the table's own
    and there is no second source for one.
    """
    for entry in entries:
        for block in _expect_blocks(entry):
            if "type" not in block:
                continue
            page_type = block["type"]
            assert page_type in page.ALL_PAGE_TYPES, entry["id"]
            if "folder" not in block:
                continue
            assert block["folder"] == page.FOLDER_BY_TYPE[page_type], entry["id"]


def test_no_expectation_names_a_page_type_the_pipe_cannot_create(entries):
    """**The literal that outlived its source, and the migration that closed it.**

    It tied `wiki/meetings` — the one folder `page.FOLDER_BY_TYPE` could not supply — to
    `processing.MEETING_MEETING_PREFIX`, so the fixture's literal could not outlive the code that
    wrote there. The constant went with the meeting flow while the literal stayed, and this ran
    strictly XFAILING for exactly as long as F05, F08 and F09 named `decision` and `meeting`: a
    golden that cannot be met is worse than one nobody runs, because it reports the yardstick's own
    staleness as the backend's score.

    The three expectations were re-aimed at the one pipe (`note` in `wiki/notes/`, with the page SET
    scored by `pages`), so the marker came off. Asserted against the live placement table rather
    than a literal, which is what keeps it from drifting again: `page.PAGE_TYPES` gives `entity`,
    `source` and `view` no folder because their writers are the birth fold, the door and the view
    regenerator, and an expectation naming one of them would be scoring a page the fast lane is
    forbidden to create.
    """
    declared = {block["type"] for entry in entries for block in _expect_blocks(entry)
                if "type" in block}
    assert declared, "no expectation names a page type — this check has lost its subject"
    assert declared <= set(page.FOLDER_BY_TYPE), (
        f"{sorted(declared - set(page.FOLDER_BY_TYPE))} is not a type any flow can create: "
        f"`page.PAGE_TYPES` gives it no folder, so `gate_zone` refuses the write")


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
    return anchors + [p["anchor"] for p in block.get("pages") or [] if "anchor" in p]


# OLD BEHAVIOUR: two tests stood here, both about the retired `edits` facet — one refusing the
# permanently-green spelling `edits: []`, one proving every expected edit path was a page the
# fixture repo already carried. The facet retired with the declaration it scored: a capture no
# longer declares an additive edit to a page that already exists. The rule they encoded — a cell
# that reads as measured and can never fail is worse than no cell — is enforced for the surviving
# facets by `test_the_consistency_check_refuses_a_page_asserting_neither_title_nor_anchor` above.


# ── the proposal the two unregistered names measure ────────────────────────────────────────────

def test_every_proposing_expectation_files_and_names_what_it_must_propose(entries):
    """**INVERTED by the file-first write path.** This used to assert the ask-back loop's two halves: a `needs_input`
    expectation carried a `park_question`, a `reply` and an `after_reply` block, and without all
    three the loop those two captures existed for was never exercised.

    Nothing parks. What the same two captures measure now is the other side of the same event — an
    unregistered name gets an IDENTITY instead of a question — so the pairing that has to hold is
    `proposals` with a terminal `filed`. A `proposals` expectation on a capture that was allowed to
    end anywhere else would be scoring a proposal nobody could read, because the entity page and
    the capture's own page land in ONE commit or neither does.
    """
    proposing = [e for e in entries if "proposals" in e["expect"]]
    assert len(proposing) == 2, "the set measures the proposal path with exactly two captures"
    for entry in proposing:
        assert entry["expect"]["status"] == schema.FILED, entry["id"]
        assert entry["expect"]["proposals"], entry["id"]


def test_a_proposing_capture_asserts_no_anchor_it_would_have_to_invent(entries):
    """The silence in those two expectations, pinned as a decision rather than left as an omission.

    A proposed entity's registry id is `slugify` of the name the AGENT chose, so `Halcyon Grid` and
    `Halcyon Grid pilot` are one judgment and two ids. `proposals` scores that judgment through the
    loose matcher; an `anchor` beside it would score one defensible spelling of it a second time,
    and would go red on a run that proposed the right thing under a name the yardstick's author had
    not predicted. If a future editor adds the anchor back, this is what asks them to write down
    which recorded runs made the id predictable.
    """
    for entry in entries:
        if "proposals" in entry["expect"]:
            assert "anchor" not in entry["expect"], (
                f"{entry['id']} asserts an anchor whose id is derived from a name the agent chose")


def test_every_proposed_name_is_absent_from_the_registry_and_present_in_its_own_material(
        manifest, entries, registry):
    """Register either of these and the capture stops proposing — it files against the registered
    entity, quietly, and both `proposals` cells move without anyone touching the backend.

    Present in its own material is the other half, and it is not a nicety: `librarian.identity`
    refuses a proposed name the material never names, so a yardstick asking for one would demand a
    filing the gates forbid.
    """
    materials = {c["id"]: (CAPTURES / c["material"]).read_text(encoding="utf-8")
                 for c in manifest["captures"]}
    proposed = [(e["id"], name) for e in entries for name in e["expect"].get("proposals") or []]
    assert len(proposed) == 2, "the set measures the proposal path with exactly two captures"
    for capture_id, name in proposed:
        assert registry.canonical_id(name) is None, f"{name!r} is registered — {capture_id} " \
                                                    f"can no longer propose it"
        assert name.lower() in materials[capture_id].lower(), capture_id


def test_the_two_proposing_captures_arrive_through_the_two_DOORS_the_one_pipe_serves(manifest,
                                                                                     entries):
    """One typed note and one transcript, which is the property the one pipe is about: the same
    input used to behave differently per door — a name typed into a capture parked, the same name
    in a transcript parked the whole page SET, and then the two were filed by two different flows.
    There is one flow; `kind` chooses the prose and the `sources/` folder and nothing else. Two
    proposing captures of the same kind would leave the other door unmeasured and the instrument
    would agree that the doors behave alike without having looked.

    OLD NAME: `test_the_two_proposing_captures_reach_the_two_flows_that_can_propose`. The
    ASSERTION is unchanged, because it was always about the kinds on the queue rows rather than
    about which code path drained them — which is exactly why it survived the flow it was named
    after.
    """
    kinds = {c["id"]: c["kind"] for c in manifest["captures"]}
    proposing = sorted(kinds[e["id"]] for e in entries if "proposals" in e["expect"])
    assert proposing == ["meeting", "raw"]


def test_no_expected_page_title_can_swallow_a_later_ones_page(entries):
    """A trap the loose titles introduced, and one that fails a CORRECT backend — the direction
    that costs the most to diagnose.

    `_pages_match` walks the expected list in order and takes the FIRST page each title
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
            titles = [p["title"] for p in block.get("pages") or [] if "title" in p]
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


def test_the_registry_sits_where_the_librarian_reads_it(registry):
    """It is read by relative path out of the commit being filed against. In the wrong place the
    registry is EMPTY, which does not fail — every capture births a new identity for a name that
    was registered all along, the anchor facet reports a backend that cannot anchor, and
    `proposals` scores 1.00 for a run in which nothing was recognised at all.

    `ops/acl.json` used to be asserted beside it and is gone from both the fixture and the real
    knowledge repo: a capture's audience is the door's decision on its own queue row,
    so no repo file decides a label and there is nothing here for the librarian to read."""
    assert (REPO / librarian_config.REGISTRY_RELPATH).is_file()
    assert not (REPO / "ops" / "acl.json").exists()
    assert len(registry.entities) == FIXTURE_ENTITIES


def test_the_fixture_repo_carries_the_brief_the_measured_backend_reads(entries):
    """`agent.read_skill` reads the librarian brief out of the item's own worktree and
    `worker.startup_checks` refuses a brief-reading run without one. A mini repo missing it scores
    config failures as filing quality.

    OLD BEHAVIOUR: it asserted BOTH briefs, because `agent.read_meeting_brief` read a second one on
    the first `kind="meeting"` row. There is one brief and one reader: every capture is filed
    against the librarian brief, whatever its kind. The frozen meeting-distiller copy was deleted
    with the expectations migration, so the second half of this assertion did not become a weaker
    check — it lost its subject, and `FROZEN_MARKERS`/`FROZEN_SHA256` lost the copy in the same
    edit rather than keeping a pin over bytes nothing reads.
    """
    assert (REPO / librarian_agent.SKILL_RELPATH).is_file()
    assert not (REPO / ".claude" / "skills" / "meeting-distiller").exists(), (
        "a meeting-distiller brief is back in the fixture. Nothing reads one — every capture is "
        "filed against the librarian brief whatever its kind — so a copy here is bytes the freeze "
        "protocol has to carry for no measurement")


def test_the_frozen_brief_offers_only_page_types_the_pipe_can_create():
    """**The other half of the yardstick, and the half that cannot be edited to agree.**

    `test_no_expectation_names_a_page_type_the_pipe_cannot_create` above holds the EXPECTATIONS to
    the live placement table. This holds the frozen BRIEF to it, and the two together are what make
    a run a measurement: an expectation naming a type no flow can create scores a backend for the
    yardstick's staleness, and a brief offering one makes a correct backend declare it.

    Read out of the brief's own placement table — the rows it renders as `| `type` | `folder/` |` —
    rather than by grepping for the word, so a brief that stops naming a type in prose while still
    listing it does not read as fixed.

    Left strictly XFAILING rather than deleted or softened, for the reason the module docstring
    gives: this file is deliberately NOT a drift guard against the live knowledge repo, because a
    yardstick has to stay still. So the disagreement is RECORDED here and the repair is a re-freeze,
    which retires the series with it. The day the fixture is re-frozen this goes green and the
    marker has to come off in the same commit.
    """
    brief = (REPO / librarian_agent.SKILL_RELPATH).read_text(encoding="utf-8")
    offered = set(re.findall(r"^\| `(\w+)` \| `(?:wiki/\w+)/` \|$", brief, re.MULTILINE))
    assert offered, "the frozen brief no longer renders a placement table this can read"
    assert offered <= set(page.FOLDER_BY_TYPE), (
        f"the frozen librarian brief offers {sorted(offered - set(page.FOLDER_BY_TYPE))}, which "
        f"`page.PAGE_TYPES` gives no folder — a backend that believes it will declare a type "
        f"`classify_page_type` refuses, and the table will read as a model that cannot file")


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
