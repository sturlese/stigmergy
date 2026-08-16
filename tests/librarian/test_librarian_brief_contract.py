"""The librarian brief and the ordinary flow's code are ONE CONTRACT, edited on both sides.

`test_meeting_brief_contract.py`'s twin, for the brief the ORDINARY flow reads — and it exists for
the reason that file's own history gives: dropping a rule from one side while the other keeps
promising it is invisible to 3,000 green tests. ADR 033 made the exposure worse rather than better.
The brief is backend-NEUTRAL — it documents a worker that hands the agent its context and
writes the page from a structured account — and any backend that departs from it says so in an
ENVIRONMENT preamble on the platform side. One shape answers it today; when a second one existed
the two differed only in that preamble, and nothing but a table like this one notices when the
brief starts describing neither.

**What this test can and cannot prove.** A fully automated two-sided contract check would need to
parse English prose into formal rules, which nothing here attempts. What it DOES check, honestly:
a hand-maintained table of (a rule the brief states, in its own words) <-> (a marker proving the
CODE actually implements that rule — a finding code, a specific field, a specific behaviour),
asserted in BOTH directions:

- the brief phrase must still be PRESENT in the brief text (catches a rule silently dropped from
  the brief while the code that enforces it stays);
- the code marker must still be PRESENT in the live source (catches that failure mode directly: a
  check stops enforcing something, or is renamed, while the brief still promises it).

**A marker that survives only inside a comment or a docstring proves nothing** and is the defect
the meeting version of this file has had twice. Every marker below names live code: a module
constant something reads, a function something calls, or a finding string something raises.

**The skeleton the developer landed has been GROWN, and the two declared gaps are closed.** The
brief's three-outcome anchoring rule now has three rows instead of one — they are three different
decisions with three different code paths, and a single row went green while two of them were
unenforced. An OVERRIDE note was pinned here too, on the same argument the meeting flow's still is:
it is the one place the platform tells an agent that part of the brief below does not describe its
run, and an override that stopped matching the brief it corrects would be worse than no override.
That row retired with the backend that carried the note — see the table's own tombstone, which also
names the paragraph the knowledge repo still owes.

What the table also covers now, in the order the flow meets it: what the worker HANDS the agent
(each of the four gathered fields, against the gatherer that builds it), what the agent may
CREATE, what it may not write, and what happens to each of the three ways a capture can end.
"""
import os
import pathlib

import pytest

from stigmergy.librarian import agent, config, edits, gates, gather, processing, pydantic_backend

# ── this contract runs in CI ────────────────────────────────────────────────────────────────────
# The same two-halves arrangement `test_meeting_brief_contract.py` argues for at length: the RULE
# TABLE reads a frozen copy that is always present, so every code-side marker is checked on every
# push; a DRIFT TEST asserts that copy is byte-identical to the real brief, skipping only when the
# sibling repo is absent. Neither half is sufficient alone.
_BRIEF_RELPATH = agent.SKILL_RELPATH
_FROZEN_BRIEF = (pathlib.Path(__file__).parent / "fixtures" / "repo" / ".claude" / "skills"
                 / "librarian" / "SKILL.md")
_FROZEN_NOTES = _FROZEN_BRIEF.parent / "FROZEN.md"
_PLATFORM_ROOT = pathlib.Path(__file__).resolve().parents[2]

# What `FROZEN.md` carries between the platform PR being written and the knowledge-repo PR that
# lands beside it existing. Named here rather than matched loosely, so the day it is replaced is
# the day the sha assertion below starts running for real.
_PENDING_SHA = "PENDING-KNOWLEDGE-REPO-SHA"


def _knowledge_repo() -> pathlib.Path:
    """The same resolution the librarian itself uses (`config.Settings.repo`). Lifted verbatim from
    `test_meeting_brief_contract.py`, which asks the same question about the same repo."""
    configured = os.environ.get(config.REPO_ENV)
    return (pathlib.Path(configured) if configured
            else (_PLATFORM_ROOT / config.REPO_DEFAULT)).resolve()


def _live_brief() -> pathlib.Path:
    return _knowledge_repo() / _BRIEF_RELPATH.replace("/", os.sep)


@pytest.fixture(scope="module")
def brief_text() -> str:
    """The FROZEN copy — always present, so the table below runs in CI."""
    return _FROZEN_BRIEF.read_text(encoding="utf-8")


def test_the_frozen_brief_is_byte_identical_to_the_knowledge_repos_own():
    """The half that keeps the frozen copy honest. Skips — never fails — when the knowledge repo is
    not on this machine."""
    source = _live_brief()
    if not source.exists():
        pytest.skip(
            f"no knowledge repo at {_knowledge_repo()} (set ${config.REPO_ENV}) — the frozen copy "
            f"exists precisely so the suite does not need one. To resync when you do have it:\n"
            f'  cp "$STIGMERGY_REPO/{_BRIEF_RELPATH}" '
            f"{_FROZEN_BRIEF.relative_to(_PLATFORM_ROOT)}")

    assert _FROZEN_BRIEF.read_bytes() == source.read_bytes(), (
        f"the frozen librarian brief has drifted from {source}. The contract table in this file is "
        f"checked against the frozen copy in CI, so a stale copy means CI is enforcing a contract "
        f"the agent is no longer given — the same drift with the sides swapped. Resync and record "
        f"the new sha in {_FROZEN_NOTES.relative_to(_PLATFORM_ROOT)}:\n"
        f'  cp "$STIGMERGY_REPO/{_BRIEF_RELPATH}" {_FROZEN_BRIEF.relative_to(_PLATFORM_ROOT)}\n'
        f'  git -C "$STIGMERGY_REPO" log -1 --format=%H -- {_BRIEF_RELPATH}')


def _recorded_sha(notes_path: pathlib.Path) -> str:
    """The sha one `FROZEN.md` records in its "Copied at commit" row — `_PENDING_SHA` while the
    knowledge-repo commit does not exist yet."""
    notes = notes_path.read_text(encoding="utf-8")
    assert "Copied at commit" in notes, f"{notes_path} records no source commit row at all"
    return notes.split("Copied at commit")[1].split("`")[1]


def test_the_frozen_brief_records_the_commit_it_was_taken_from():
    """A copy with no recorded provenance cannot be resynced with confidence — "is this behind or
    ahead?" has no answer without the sha the copy was taken at.

    **40 hex OR the placeholder, and it goes green either way.** ADR 033's two PRs land together,
    so the brief's own commit is minted after these bytes are copied here; a sha row that named the
    PREVIOUS brief would be worse than a placeholder, because it would look answered. This is
    deliberately NOT a skip: a skipped test proves nothing about the row's SHAPE, and the shape is
    what stops somebody replacing the placeholder with an abbreviated sha or a branch name. What
    makes the placeholder temporary is the tripwire below, not this.
    """
    sha = _recorded_sha(_FROZEN_NOTES)
    assert sha == _PENDING_SHA or (len(sha) == 40
                                   and all(c in "0123456789abcdef" for c in sha)), (
        f"FROZEN.md must record the full 40-character source commit sha (or exactly "
        f"{_PENDING_SHA} until the knowledge-repo PR lands), found {sha!r}")


# ── the LANDING TRIPWIRE ────────────────────────────────────────────────────────────────────────
# Both frozen copies of this brief carry `PENDING-KNOWLEDGE-REPO-SHA` — this suite's drift-guard
# copy and `evals/filing/repo/`'s yardstick one — and both sha-shape assertions tolerate it so the
# real sha can be filled in without touching a test. Tolerance with nothing behind it is how a
# placeholder becomes permanent, and a permanently-green test is worse than no test.
#
# This is what is behind it. The placeholder is honest for exactly as long as the knowledge repo's
# own commit does not exist; the moment that repo's brief IS these bytes, the commit exists, the sha
# is knowable, and a copy still saying PENDING is simply unfinished. So: when the live brief matches
# the frozen bytes, a surviving placeholder FAILS, and the failure carries the command that fixes
# it.
#
# It skips when there is no knowledge repo on the machine (CI) or when the brief there is still the
# old one — the two states where the placeholder is the truth. The skip says which of the two it is,
# because "skipped" without that is indistinguishable from "passed" in a summary line.
_PENDING_COPIES = (
    _FROZEN_NOTES,
    _PLATFORM_ROOT / "evals" / "filing" / "repo" / ".claude" / "skills" / "librarian" / "FROZEN.md",
)


def test_the_pending_sha_cannot_survive_the_knowledge_repo_landing():
    source = _live_brief()
    still_pending = [path for path in _PENDING_COPIES
                     if _recorded_sha(path) == _PENDING_SHA]
    if not still_pending:
        return                                    # landed and filled in — nothing to trip
    if not source.exists():
        pytest.skip(
            f"no knowledge repo at {_knowledge_repo()} (set ${config.REPO_ENV}), so whether the "
            f"commit behind {_PENDING_SHA} exists cannot be answered here. This tripwire is a "
            f"maintainer-machine check by construction; the sha-SHAPE assertions above run in CI.")
    if source.read_bytes() != _FROZEN_BRIEF.read_bytes():
        pytest.skip(
            f"the knowledge repo's brief is not yet these bytes, so {_PENDING_SHA} is still the "
            f"honest answer: the PR carrying ADR 033's brief has not landed. (If it landed with "
            f"DIFFERENT bytes, the drift test above is the one that says so.)")

    pending_names = ", ".join(str(p.relative_to(_PLATFORM_ROOT)) for p in still_pending)
    pytest.fail(
        f"the knowledge repo's {_BRIEF_RELPATH} is byte-identical to the frozen copy, so the "
        f"commit that carries it EXISTS — and {pending_names} still says {_PENDING_SHA}. Fill in "
        f"the real sha in every copy, and in evals/filing/repo/PROVENANCE.json's `stigmergy_sha` "
        f"and the other two FROZEN.md files with it (one freeze, one commit — "
        f"tests/evals/test_filing_golden_fixture.py pins that):\n"
        f'  git -C "$STIGMERGY_REPO" log -1 --format=%H -- {_BRIEF_RELPATH}')


def test_the_contract_table_is_not_vacuous():
    """The anti-vacuity guard: a truncated or emptied copy would turn every phrase check into a
    silent no-op against `""`."""
    text = _FROZEN_BRIEF.read_text(encoding="utf-8")
    assert len(text) > 5_000 and "capture" in text.lower()
    # The floor is what makes shrinking the table a deliberate act, and it is RAISED with the
    # table rather than left at the skeleton's number: a floor that lags the table lets rows be
    # dropped silently down to it, which is the exact failure the floor exists to prevent.
    assert len(RULE_TABLE) >= 28, "the brief<->code contract table has gone thin"
    # ...and no row may be a duplicate of another, which is the cheap way a table grows without
    # covering anything new.
    assert len({phrase for phrase, _ in RULE_TABLE}) == len(RULE_TABLE)
    assert len({marker for _, marker in RULE_TABLE}) == len(RULE_TABLE)


# ── the code side: read once, so every table entry's "marker in code" check is grep-cheap ───────
def _code_text() -> str:
    """The six modules the ordinary flow's contract actually lives in.

    Wider than the meeting version's two, because ADR 033 spread the flow: `gather` builds the
    context the brief promises, `edits` validates the declarations it documents, and `agent` owns
    the outcome boundary that decides which half of the account is well-formed.

    `pydantic_backend` joined them for issue #53. The brief documents an inbound spelling
    (`triage.name`, folded into `names`) that BOTH outcome boundaries have to honour — the file
    channel through `agent.parse_outcome` and the structured road through `OrdinaryTriage`. A
    contract that could only see one of them would go green on the day the other stopped agreeing,
    which is the whole shape of what #53 was filed about.
    """
    import inspect
    return "".join(inspect.getsource(module)
                   for module in (gates, processing, agent, edits, gather, pydantic_backend))


# (brief phrase, code marker) — see the module docstring for what each direction proves. Every
# phrase is a VERBATIM, single-line substring of SKILL.md (markdown emphasis and all — a substring
# check cannot cross the line breaks between them, so each entry sits inside one line of the real
# file).
RULE_TABLE = [
    # The injection vocabulary, unchanged from the pre-ADR-033 brief and still the fixed set
    # `processing._injection_categories` filters an account's findings against.
    ("`declare-canonical` · `write-outside-lane` · `reveal-credentials`", "INJECTION_CATEGORIES"),
    # The confinement-by-construction claim, on both sides. The brief tells the agent it names a
    # TYPE and never a location; the marker is the line where code derives the location from that
    # type and from `page.FOLDER_BY_TYPE` — which is why there is no field an account could steer.
    ("**Never a folder and never a path**", 'path = f"{policy.folder}/{stem}.md"'),
    # Placement goes through the ONE table (`page.classify_page_type`), never a folder list.
    ("| `concept` | `wiki/concepts/` |", "classify_page_type"),
    # The page body is REFUSED over its ceiling rather than truncated — the one bound in the
    # outcome boundary that does not truncate, and the brief tells the agent so in its own words.
    ("refused (not shortened) if it is enormous", "_page_body"),
    # ── the THREE anchoring outcomes, one row each ────────────────────────────────────────────
    # They are three different decisions with three different code paths, and the skeleton pinned
    # them with one row — which went green while two of them were unenforced.
    #
    # 1. ANCHOR: the anchor is the DECLARED list against the registry, and no wikilink is read.
    #    Both halves of that sentence are `gates.resolve_entity_ids`, which the gate and the stamp
    #    share — so a page could not establish its own anchor by linking even if it tried.
    ("The anchor is this declared `anchoring.entities` value and", "resolve_entity_ids"),
    #    ...and the gate that judges it. A brief promising "an entity in the list resolves" is only
    #    true because this is what runs.
    ("**No wikilink is", "def gate_anchoring"),
    # 2. COMPANY-WIDE: legal only WITH a written reason, and "a shrug is not one" is enforced
    #    rather than requested — `gate_anchoring` refuses a company scope with no reason, which is
    #    the difference between an ownerless page and a declared company-wide one.
    ("2. **COMPANY-WIDE, with a written reason** — when the material genuinely is about the "
     "company as", "declared company-wide scope with no written reason"),
    # 3. PARK: a correct outcome, not a failure — and the one road that spends the submitter's
    #    single question (`_ask_or_park`, pinned again below for the budget itself).
    ("**This is a correct outcome, not a failure**", "TRIAGE_UNRESOLVED_ENTITY"),
    # The server-owned field list the agent must never write, and the function that writes them.
    ("`owner`, `submitted_by`, `verification`, `acl`, `status`, `as_of`, `content_hash`, `id`, "
     "`entity`.", "stamp_server_fields"),
    # `related:` is built by the worker from `links_created` — the field is the graph edge, not
    # bookkeeping, and `_build_ordinary_page` is where that is true.
    ("- **`links_created`** — the bare page names you linked from this page. The worker builds the",
     "_build_ordinary_page"),
    # The wikilink vocabulary the brief hands over is the SAME set `edits.validate` later answers
    # "does this link resolve" with — one reading, so the gatherer cannot offer a name the edit
    # validator refuses. The marker carries `confined=True` because that is the whole call: the
    # gatherer asks the shared reader for the CONTAINED answer, and a marker pinned to the bare
    # `edits.page_names(worktree)` would keep passing if that argument were ever dropped — which is
    # the day the gatherer starts offering the model a page it read through a symlink.
    ("- **`link_names`** — every page name in this repo, which is the whole wikilink vocabulary. "
     "It is", "edits.page_names(worktree, confined=True)"),
    # Exactly one page per ordinary capture, unchanged by the restructure.
    ("One capture yields **one** page.", "multiple-pages"),
    # Edits are DECLARED and performed by code — the split ADR 015 §3 made and this flow kept.
    ("You never write to them. You declare the edit and the worker performs it.", "apply_declared"),
    # ...and an entity page is not editable, whatever it was anchored to.
    ("So do not declare an edit on the", "outside-lane"),
    # The one-ask budget: the brief promises at most one question ever, and `_ask_or_park` is the
    # single place that decides whether this park spends it. The marker is the DEFINITION, not the
    # bare name — a bare-name marker is satisfied by any mention, including a comment or a call
    # that no longer decides anything. (It used to be stated as "`_ask_or_park` is a prefix of
    # `_ask_or_park_multi`"; the plural sibling was deleted by the one-shape collapse, and the
    # reason for pinning the definition survives it unchanged.)
    ("**The submitter is asked at most once, ever.**", "def _ask_or_park("),
    # The two park kinds and the field each one's report cannot be written without — now TWO rows,
    # because issue #32 rewrote the sentence that used to state both on one line. The
    # `unresolved-entity` half grew `names` beside `name` and the `unsupported-type` half moved onto
    # the next line of the real file; a phrase cannot cross that break (see this table's header), so
    # re-aiming the surviving row at only the first line would have DROPPED the second kind from the
    # contract while the row went green. Each half keeps its own live marker: the fixed set of kinds
    # this flow accepts, and the map that says which field each kind's report cannot be written
    # without (`TRIAGE_REQUIRED_FIELD[triage["kind"]]`, the lookup `parse_outcome` performs).
    # RE-POINTED by issue #53, which made `names` the primary spelling and rewrapped this sentence:
    # both halves now sit on ONE line where they used to sit on two. They stay TWO rows anyway, for
    # the reason above — a single row aimed at the joint phrase would go green while saying nothing
    # about whichever kind fell outside its substring.
    ('`kind` is `"unresolved-entity"` (with `names`, a list even for one) or `"unsupported-type"` '
     "(with", "TRIAGE_KINDS"),
    ("`judged_type`) — both the kind and its field are required, because",
     "TRIAGE_REQUIRED_FIELD"),
    # ── issue #32: EVERY unresolved name, and never joined into one ───────────────────────────
    # The rule the brief change exists for, pinned on both of the places the brief now states it.
    #
    # 1. The plural park itself. `_unresolved_names` is the ONE reader of a park's declaration, and
    #    it returns every name the account declared from EITHER shape — which is precisely "name
    #    every one of them in `names`" on the code side. A brief that kept promising the plural
    #    field while that reader went back to `parked.get("name")` is issue #32 reopening.
    ("`names` is a LIST, always — one entry when one thing is unregistered, and when the material "
     "leaves", "_unresolved_names"),
    # ── issue #53: the legacy singular spelling, tolerated on BOTH outcome boundaries ──────────
    # The brief now promises that `"name": "Acme Corp"` is still read as a one-entry list. Pinned
    # to the LINE THAT PERFORMS the fold, never to the validator's name: a marker satisfied by a
    # method merely existing is the failure mode this table has already hit twice, and a validator
    # that had stopped folding would still be a validator. `agent.parse_outcome`'s own half of the
    # same tolerance is covered by the `_unresolved_names` row above, which reads either shape.
    ("accepted and read as a one-entry list, so an older account is never refused over the "
     "spelling —", 'return {**data, "names": [single]}'),
    # 2. The `## Never` bullet, which is the same rule stated as a prohibition ("park only SOME, or
    #    join several into one"). Its marker is the ORDINARY flow's own call into the park router,
    #    as a FULL CALL LINE rather than a bare function name.
    #
    #    **Re-pointed by the one-shape collapse, and the re-point is the row's own subject.** The
    #    marker used to be `_ask_or_park_multi(item, deps, names=names, ...)` — the plural sibling
    #    the ordinary flow had to reach to name a second entity. There is no sibling now: one
    #    router serves both counts and both flows, so what this row pins is `_triage` handing it
    #    the NAMES LIST, which is "one ask names every one of them" on the code side. Parking on
    #    only some of them would mean not passing the list.
    #
    #    Why the full call line and not `def _ask_or_park(`: the definition already has its own row
    #    above (the one-ask budget), and a marker satisfied by the function merely EXISTING would
    #    have been green throughout issue #32 — the plural machinery existed the whole time; what
    #    was missing was the ordinary flow routing into it. Verified against the concatenated
    #    source before use: this string occurs exactly once, it is not a substring of the
    #    definition line (`def _ask_or_park(item: dict, ...` — `item:` not `item,`), and it does
    #    not match `_triage_meeting`'s call, which wraps after `notes=notes,` and so ends in a
    #    comma where this one ends in a paren.
    ("- Park only SOME of a capture's unresolved names, or join several into one — one ask names "
     "every",
     "_ask_or_park(item, deps, names=names, agent_rationale=rationale, notes=notes)"),

    # ── what the worker HANDS the agent: four fields, four producers ──────────────────────────
    # The brief promises a context the agent no longer has a tool to go and get. Each promise is
    # matched to the gatherer field that keeps it, because a promise the gatherer stopped keeping
    # is not a thinner prompt — it is an anchor the agent cannot declare or a link it cannot make,
    # with nothing anywhere saying why.
    # **Re-aimed by ADR 034**, and the re-aim is the row's own subject. The brief used to open "You
    # do not go looking for anything", which was true of every run while one shipped shape existed
    # and false the moment the ordinary run got its tools back. What survives — and what the
    # gatherer still keeps — is the promise that a context is ASSEMBLED BEFORE THE CALL, whether or
    # not the reader can go further from it.
    ("**What every run is handed** is one capture and everything this brain already holds that is",
     "def gather("),
    ("  registry `id`, its canonical `name`, its aliases, and the path of its own page when this "
     "brain", "structural_payload"),
    ("- **`candidates`** — the existing pages this material most overlaps with, ranked, each with "
     "its", "def _candidates("),
    ("- **`neighbourhood`** — the pages one link out from those candidates and from the entity "
     "pages,", "def _neighbours("),
    # ...and the bound that keeps "not in the list" from reading as proof a name does not exist.
    ("  bounded: `link_names_total` says how many pages exist, and when it is larger than the "
     "list you", "MAX_LINK_NAMES"),

    # ── what CODE writes, which is everything about the container ─────────────────────────────
    # The brief tells the agent it writes text and nothing else. `_write_ordinary_page` is the
    # function that makes that true, and the title-is-the-filename rule is the one an agent can
    # break by choosing a name that already exists.
    ("  globally unique across the whole repo. Check it against `link_names` before you choose "
     "it: a", "existing-page-collision"),
    ("    letters belong in a title, and dropping or approximating them (\"Reuni n\", \"Reunion\") "
     "writes a", "unnameable_reason"),
    # Re-aimed again: the standalone "write no frontmatter block at all" sub-bullet this row used
    # to pin is GONE — the section's opening no longer treats "the worker builds the container" as
    # the default and the other shape as a footnote bullet under the server-owned-fields item. It
    # now states both branches up front, symmetrically, in the same sentence: a run that writes its
    # own file authors the whole container (frontmatter included); a run that returns text writes
    # NONE of it. The marker is unchanged and still the right one — `_build_ordinary_page` is the
    # function that makes the rule true on the shape the new sentence is about, where code owns the
    # container.
    ("**Your preamble decides who writes the file, and the two ways are not alike.**",
     "def _build_ordinary_page("),

    # ── RETIRED: the ordinary flow's override row ────────────────────────────────────────────
    # The pair was:
    #
    #   ("**One thing about your environment, and it is above this skill rather than in it.** Some "
    #    "runs of", "ORDINARY_SDK_OVERRIDE_NOTE")
    #
    # It paired the brief's "some runs of this skill hold tools and a checkout, and write the page
    # themselves" against the platform constant that told such a run exactly what changed. The
    # constant retired with the only backend that held tools, so the CODE side of this row is gone.
    #
    # **The BRIEF side landed too, in the knowledge repo, where it had to.** The paragraph said
    # "some runs of this skill hold tools and a checkout" — true while two backends existed, false
    # of every run once one did — and the platform reads that file but may not reword it. Commit
    # `c1e0996ed497e70a9df82661c367294b48207a16`, "chore(skills): the brief describes one run
    # style", rewrote the environment note to describe ONE run style.
    #
    # **And then ADR 034 made even THAT too specific, which is the lesson this tombstone is now
    # worth keeping for.** The ordinary run got its tools back, so a brief describing one run style
    # was false again — in the opposite direction, and for the same structural reason both times: a
    # brief that names mechanics is a brief that goes stale whenever a backend changes shape. The
    # merged answer (`0bf3c5462d50e72f5435ce61d61ba5f023e60388`) does not name them at all. It
    # defers to the preamble and states the tools CONDITIONALLY, which is what the environment row
    # at the end of this table now pins — one row, aimed at the deferral rather than at whichever
    # mechanic is currently true.
    #
    # So there is still no pending half and no override row to re-add: the platform constant is
    # gone, and where a backend genuinely departs from the brief it says so in its own ENVIRONMENT
    # paragraph (`pydantic_backend.ORDINARY_AGENTIC_ENVIRONMENT`, and the meeting flow's
    # `OVERRIDE_NOTE`), which is the state this table's own doctrine says an override should reach.
    #
    # The row is DELETED rather than kept pointing at something, and deliberately not re-aimed at a
    # nearby marker: `ORDINARY_SDK_OVERRIDE_NOTE` still appears in `agent.py` as the name inside its
    # own retirement comment, so a row left in place would have gone green on a tombstone — a
    # contract check passing because somebody wrote the constant's name in prose is worse than no
    # row at all.
    # The environment row, re-aimed and now stronger than what it replaced. The brief used to
    # assert one mechanic in its own voice ("And you write no file"); it now DEFERS the mechanics to
    # the preamble the platform composes, which is exactly what `build_filing_header` builds. A
    # brief that stopped deferring — that went back to describing one run style — would be the drift
    # this row exists to catch, and it is the drift that actually happened once, in both directions.
    ("**Your run is described in the preamble above this skill.** It says what you hold, what you "
     "were", "build_filing_header"),
]


@pytest.mark.parametrize("brief_phrase,code_marker", RULE_TABLE,
                         ids=[m for _, m in RULE_TABLE])
def test_brief_and_code_agree_on_every_tabled_rule(brief_text, brief_phrase, code_marker):
    code = _code_text()
    in_brief = brief_phrase in brief_text
    in_code = code_marker in code
    assert in_brief and in_code, (
        f"brief<->code contract drift: phrase {brief_phrase!r} "
        f"{'is' if in_brief else 'is NOT'} in the brief, marker {code_marker!r} "
        f"{'is' if in_code else 'is NOT'} in the librarian's sources — the two sides of this "
        f"contract disagree")


def test_the_contract_check_can_actually_fail(brief_text):
    """Before trusting a check, ask whether it can go red, and prove it. Sabotages one direction
    with a phrase/marker pair that provably do not exist on either side."""
    code = _code_text()
    with pytest.raises(AssertionError):
        brief_phrase, code_marker = "a rule the brief never states", "a_marker_no_module_has"
        assert (brief_phrase in brief_text) and (code_marker in code)
