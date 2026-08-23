"""The librarian brief and the code are ONE CONTRACT, edited on both sides.

**This is the only brief there is, and this file is the only contract table.** OLD BEHAVIOUR: it
had a twin, `test_meeting_brief_contract.py`, for the meeting-distiller brief a `kind="meeting"`
capture was filed against; both the second brief and the second flow are gone, and every capture —
a note, a document, a transcript — is filed against this one. The reason the table exists is that
file's own history: dropping a rule from one side while the other keeps promising it is invisible
to 3,000 green tests.

The brief is backend-NEUTRAL — it documents a worker that hands the agent its context and
writes the pages from a structured account — and any backend that departs from it says so in an
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
the retired meeting version of this file had twice. Every marker below names live code: a module
constant something reads, a function something calls, or a finding string something raises.

**The skeleton the developer landed has been GROWN, and the two declared gaps are closed.** The
brief's three-outcome anchoring rule now has three rows instead of one — they are three different
decisions with three different code paths, and a single row went green while two of them were
unenforced. An OVERRIDE note was pinned here too, on the same argument: it is the one place the
platform tells an agent that part of the brief below does not describe its
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

from stigmergy.librarian import (
    agent,
    config,
    gates,
    gather,
    processing,
    pydantic_backend,
    report,
)

# ── this contract runs in CI ────────────────────────────────────────────────────────────────────
# Two halves, and neither is sufficient alone: the RULE TABLE reads a frozen copy that is always
# present, so every code-side marker is checked on every push; a DRIFT TEST asserts that copy is
# byte-identical to the real brief, skipping only when the sibling repo is absent. Reading the
# brief out of the knowledge repo instead would make the whole table SKIP on every CI push —
# exactly where the drift it catches actually lands.
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
    """The same resolution the librarian itself uses (`config.Settings.repo`)."""
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

    **40 hex OR the placeholder, and it goes green either way.** the structured filing flow's two PRs land together,
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
            f"honest answer: the PR carrying the structured filing flow's brief has not landed. (If it landed with "
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

    Wider than the two the retired meeting table read, because the structured filing flow spread
    the flow: `gather` builds the context the brief promises, `processing` performs the
    declarations it documents, and `agent` owns the outcome boundary that decides which half of the
    account is well-formed.

    `report` joined them for issue #77. The brief now makes a promise about what the SUBMITTER
    SEES — an entity anchor states WHY it resolved, and that sentence is printed back beside the
    anchor — and `report` is the only module where that is true or false. A contract that could not
    see it would let the brief keep asking for a reason after the report stopped showing one, which
    is precisely how an automatic decision becomes an invisible one.

    `pydantic_backend` joined them for issue #53, when the brief documented an inbound spelling
    BOTH outcome boundaries had to honour — the file channel through `agent.parse_outcome` and
    the structured road through the pydantic models. That spelling retired with the parks, but
    the reason stands: a contract that could only see one boundary would go green on the day the
    other stopped agreeing.
    """
    import inspect
    return "".join(inspect.getsource(module)
                   for module in (gates, processing, agent, gather, pydantic_backend, report))


# (brief phrase, code marker) — see the module docstring for what each direction proves. Every
# phrase is a VERBATIM, single-line substring of SKILL.md (markdown emphasis and all — a substring
# check cannot cross the line breaks between them, so each entry sits inside one line of the real
# file).
RULE_TABLE = [
    # The injection vocabulary, unchanged from the pre-the structured filing flow brief and still the fixed set
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
    #    Since #77 the candidate list carries WHY an entry is in it, and the two kinds mean
    #    different things to the agent: `named` is a spelling the material carries, `near` is one it
    #    only partly carries. A brief that did not explain the field would leave the agent guessing
    #    at the one input it needs to judge a near miss at all.
    ('- `match: "near"` — the material carries only a distinctive PART of one.', "MATCH_NEAR"),
    #    ...and resolution stopped being something code does silently, so the agent's stated reason
    #    is printed back beside the anchor. A brief that asked for no reason would make every
    #    resolution invisible — the thing this repo does not allow of an automatic decision.
    ("printed back to the person who submitted the capture, beside the anchor",
     "RESOLUTION_PREFIX"),
    # 2. COMPANY-WIDE: legal only WITH a written reason, and "a shrug is not one" is enforced
    #    rather than requested — `gate_anchoring` refuses a company scope with no reason, which is
    #    the difference between an ownerless page and a declared company-wide one.
    ("2. **COMPANY-WIDE, with a written reason** — when the material genuinely is about the "
     "company as", "declared company-wide scope with no written reason"),
    # 3. INTRODUCE: the outcome that replaced the park, and then the proposal. The
    #    brief tells the agent an unknown name is introduced and FILED; the marker is the ordinary
    #    flow's call into the one module that writes those pages before the diff is judged. A brief
    #    promising the identity lands in the same commit is only true because this call precedes
    #    the gates.
    ("3. **INTRODUCE the entity** — when the material really is about a specific thing that is "
     "**not in the", "identity.write_births("),
    #    ...and it is a correct outcome the SUBMITTER is told about: `births_clause` is the
    #    sentence in the filed report naming what their capture introduced. A brief that called
    #    introducing correct while the report hid it would make the registry grow invisibly.
    ("**Introducing is a correct", "births_clause"),
    #    And it is born confirmed by them: the brief says so, and the marker is the line where the
    #    flow hands the birth writer WHO that is — the capture's own `submitted_by`, resolved by
    #    the server, never anything the account said (`gate_identity` proves the page names them).
    ("registers it CONFIRMED by the person whose capture this is",
     'approver=str(item.get("submitted_by")'),
    # The server-owned field list the agent must never write, and the function that writes them.
    ("`owner`, `submitted_by`, `verification`, `acl`, `status`, `as_of`, `content_hash`, `id`, "
     "`entity`.", "stamp_server_fields"),
    # `related:` is built by the worker from `links_created` — the field is the graph edge, not
    # bookkeeping, and `_build_ordinary_page` is where that is true.
    ("- **`links_created`** — the bare page names you linked from this page. The worker builds the",
     "_build_ordinary_page"),
    # The wikilink vocabulary the brief hands over is every name that really resolves to a page in
    # this checkout — one reading, so the gatherer cannot offer a name that would be a dead link.
    # Since the audience-from-the-door change the names come off the SCOPED corpus rather than a
    # filesystem walk, which is a strictly smaller set (still contained, and now also within this
    # capture's audience) — so the promise the brief makes holds a fortiori. The marker is the
    # derivation itself: pinned to `parsed.link_names`, it goes red the day the vocabulary stops
    # being derived from the rows the model is actually allowed to see.
    ("- **`link_names`** — every page name in this repo, which is the whole wikilink vocabulary. "
     "It is", "names = list(parsed.link_names)"),
    # As many pages as the material establishes, and the DECLARATION is what the diff answers to.
    ("One capture yields **as many pages as its material establishes**", "undeclared-page"),
    # ...and the OTHER direction of that same diff, which is a separate finding and was a separate
    # defect: the brief promises the check runs both ways, so a declaration the worker never wrote
    # is refused too. One row per direction, because a single row went green while `pages` was a
    # count rather than a list and only the surplus half was enforced.
    ("  and so is a page declared and not written. The FIRST one is the page every surface names",
     "declared-page-missing"),
    # A page that is about something of its own carries its OWN anchor. The marker is the per-page
    # map the gates read; without it every page of a multi-page filing is judged against the
    # capture's anchor, and a second customer's conclusion lands against the first one's entity.
    ("    own carries its own anchor, in the same shape as the capture-level one below. Leave it "
     "out and", "def _declared_anchorings("),
    # The EXPLORING shape's own plural declaration — the field a run that writes its own pages
    # answers with. The marker is the entry field the fold produces, which is what makes a path a
    # spelling of the same declaration rather than a second contract.
    ("each entry in `pages` carries the `path` you wrote it to instead of a `body`",
     'raw_pages = [{"path": path} for path in declared_paths]'),
    # A page that stopped being true is brought UP TO DATE, and the reason travels to its author.
    ("**A page that has stopped being true is brought up to date, not annotated.**",
     "rewrites_allowed"),
    # ...and the sentence the page's own submitter reads is required, not decoration.
    ("**one sentence for the person who FILED that page.**", "declares a rewrite with no `why`"),
    # A page that already exists is changed by CODE, from the account's declaration, and by nothing
    # else. The two rows that stood here pinned the retired `edits` vocabulary — an additive
    # backlink or callout the agent declared and `edits.apply_declared` performed. That declaration
    # is gone; the property it stood for is not, so each was REPLACED by the row that says what
    # took its place rather than deleted into silence.
    ("**You never write to them.**", "_apply_declared_rewrites"),
    # ...and an entity page is not one of them, whatever this capture anchored to.
    ("`wiki/entities/` is written by code and by nothing else", "ENTITY_ZONE_PREFIX"),
    # ── the ONE decision, and the shape of a proposal ─────────────────────────────────────────
    # The park rows that stood here (`TRIAGE_UNRESOLVED_ENTITY`, `def _ask_or_park(`, the two
    # park kinds, `_unresolved_names`, the legacy `name` fold) are GONE with the parks: the brief
    # no longer states those rules and the code no longer has the markers. Each was replaced by
    # the row pinning what took its place, not deleted into silence.
    #
    # `file` is the only decision. A brief that still offered a second value would be promising
    # an outcome the boundary refuses by shape — the exact drift that lost captures to "resolve by
    # hand" — and the marker is the closed tuple the boundary validates against.
    ('- **`decision`** — `"file"`, always.', 'DECISIONS = ("file",)'),
    # A proposal is a complete identity, and three of its fields cannot be empty. The marker is
    # the shape code the boundary adds when one is missing — the refusal, not the field list,
    # because a list can exist while nothing reads it.
    ("Three of them are required — `name`, `entity_type`, `summary` — and an account missing one "
     "is", "missing-field"),
    # The second thing an account can propose: a spelling for a REGISTERED entity. Pinned to the
    # field tuple the boundary parses, which is the only place that shape is defined.
    ("- **`new_aliases`** — spellings the material uses for REGISTERED entities that the registry "
     "does not", "NEW_ALIAS_FIELDS"),
    # The bound the brief states in words ("more than ten new things is several captures") and the
    # boundary enforces as a refusal rather than a silent truncation.
    ("things is several captures, and code refuses the account rather than registering a list.",
     "MAX_NEW_ENTITIES"),
    # The `## Never` bullet — every unregistered name, one entry each — pinned to the parser that
    # reads the list: a brief promising every name while the parser read one would be the old
    # issue #32 under a new field name.
    ("- Introduce only SOME of a capture's unregistered names, or fold several into one entity — "
     "one", "_parse_new_entities"),

    # ── what the worker HANDS the agent: four fields, four producers ──────────────────────────
    # The brief promises a context the agent no longer has a tool to go and get. Each promise is
    # matched to the gatherer field that keeps it, because a promise the gatherer stopped keeping
    # is not a thinner prompt — it is an anchor the agent cannot declare or a link it cannot make,
    # with nothing anywhere saying why.
    # **Re-aimed by the agentic pydantic harness**, and the re-aim is the row's own subject. The brief used to open "You
    # do not go looking for anything", which was true of every run while one shipped shape existed
    # and false the moment the ordinary run got its tools back. What survives — and what the
    # gatherer still keeps — is the promise that a context is ASSEMBLED BEFORE THE CALL, whether or
    # not the reader can go further from it.
    ("**What every run is handed** is one capture and everything this brain already holds that is",
     "def gather("),
    ("  canonical `name`, its aliases, and the path of its own page when this brain has one",
     "structural_payload"),
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
    # **And then the agentic pydantic harness made even THAT too specific, which is the lesson this tombstone is now
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
