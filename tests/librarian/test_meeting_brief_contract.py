"""The meeting-distiller brief and the meeting flow's gates are ONE CONTRACT, edited on both sides.

The failure this file exists to prevent happened once: dropping the wikilink requirement from
`gate_anchoring` left `SKILL.md` telling the agent to write a wikilink the gate no longer read, and
an ordinary capture routed to "the librarian broke".

**What this test can and cannot prove.** A fully automated two-sided contract check would need to
parse English prose into formal rules, which nothing here attempts. What it DOES check, honestly:
a hand-maintained table of (a rule the brief states, in its own words) <-> (a marker proving the
CODE actually implements that rule — a finding code, a specific field, a specific behaviour),
asserted in BOTH directions:

- the brief phrase must still be PRESENT in the brief text (catches a rule silently dropped from
  the brief while the gate that enforces it stays);
- the code marker must still be PRESENT in the gate/processing source (catches that failure mode
  directly: a gate stops enforcing something, or is renamed, while the brief still promises it).

Every entry is a real, load-bearing rule from both `meeting-distiller/SKILL.md` and
`librarian/processing.py`/`librarian/gates.py` — not a paraphrase invented for this test. Adding a
new brief rule without a table entry (or vice versa) is exactly the drift class this test exists
to catch on the NEXT edit to either side.

**The table is deliberately smaller than it once was.** When the page-writing tool was taken away
from the agent — it now returns one JSON object in `.librarian-outcome.json` and the WORKER builds
and writes every page in the set — six entries lost a real side and were REMOVED rather than
re-pointed, each with its removal reason recorded in the table where the entry used to be:

- four (`source-page-count`, `meeting-page-count`, `` no `"anchoring"` key in its declaration ``,
  `_meeting_page_decision_links`) told the AGENT how to write a page, so the brief no longer
  states them at all — nothing on the brief side to pin, and re-pointing them at some other
  sentence would only make a passing test that proves nothing;
- two (`decision-set-mismatch`, `no additive edits to pages that already exist`) had a code marker
  that survived only inside a COMMENT or a DOCSTRING, so `code_marker in code` could never have
  proved a behaviour — a defect this file has already had once and must not re-introduce by
  re-pointing an entry at another comment.

The rejected alternatives are recorded on purpose: re-pointing an entry at a brief sentence that
does not state the rule, or ADDING a sentence to the brief so a marker can read it back, both
produce a green test with one side missing. A shrunken honest table is the correct outcome; the
removed code-side rules are covered behaviourally elsewhere (each removal record names where), and
the rows that remain are exactly the pairs where the brief still tells the agent something AND
the code still enforces it in live behaviour, not in prose about itself.

**It grew again with ADR-038**, by three rows, on the same terms it shrank by: the meeting flow
gained a gathered context and a declared-edit mechanism, so the brief gained rules that a live
piece of code really enforces — the worker's own gather call, this flow's lane, and the line that
puts a bad declaration's findings in front of the vetoes. Growth is not the opposite of the
shrinking above; both are the same rule applied, which is that a row exists when and only when
both of its sides do.
"""
import os
import pathlib

import pytest

from stigmergy.librarian import base_inputs, config, gates, processing

# ── this contract runs in CI ────────────────────────────────────────────────────────────────────
# It used to read the brief straight out of the knowledge repo and SKIP whenever that checkout was
# absent — which in CI is always. So the one test written to catch a gate that stops enforcing
# something while the brief keeps promising it ran only on a machine that happened to have both
# clones, and never on the pushes that gate a branch: the whole class was unguarded exactly where
# guarding matters.
#
# The fix is the arrangement the frozen contract linter one directory over already uses
# (`fixtures/repo/.claude/tools/FROZEN.md`), split into two halves that are each named for what
# they prove — because a vendored copy alone proves "the copy and the code agree", which is weaker
# than what this file claims, and a green test making the weaker claim while looking like the
# stronger one is the *"passes for the reason it does not name"* defect this file has already
# fixed in itself twice:
#
#   * the CONTRACT TABLE reads the frozen copy, so it runs everywhere, always — every code-side
#     marker is checked against the live source on every push;
#   * a DRIFT TEST asserts the frozen copy is byte-identical to the real brief, skipping only when
#     the sibling repo is absent — so on every local run (both clones present) the pin is checked
#     against reality, and editing the brief without resyncing turns it red.
#
# Neither half is sufficient alone; together they say the code is checked against a pinned
# contract on every push, and the pin is checked against reality on every local run.
_FROZEN_BRIEF = (pathlib.Path(__file__).parent / "fixtures" / "repo" / ".claude" / "skills"
                 / "meeting-distiller" / "SKILL.md")
_FROZEN_NOTES = _FROZEN_BRIEF.parent / "FROZEN.md"
_PLATFORM_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _knowledge_repo() -> pathlib.Path:
    """The same resolution the librarian itself uses (`config.Settings.repo`): `$STIGMERGY_REPO`,
    else `config.REPO_DEFAULT` beside this checkout. No absolute path is hardcoded — one machine's
    layout is not a test's business. Lifted verbatim from `test_frozen_linter.py`, which asks the
    same question about the same repo."""
    configured = os.environ.get(config.REPO_ENV)
    return (pathlib.Path(configured) if configured
            else (_PLATFORM_ROOT / config.REPO_DEFAULT)).resolve()


def _live_brief() -> pathlib.Path:
    return _knowledge_repo() / base_inputs.MEETING_BRIEF_RELPATH.replace("/", os.sep)


@pytest.fixture(scope="module")
def brief_text() -> str:
    """The FROZEN copy — always present, so the table below runs in CI. Its fidelity to the real
    brief is the drift test's job, not this fixture's."""
    return _FROZEN_BRIEF.read_text(encoding="utf-8")


def test_the_frozen_brief_is_byte_identical_to_the_knowledge_repos_own():
    """The half that keeps the frozen copy honest. Skips — never fails — when the knowledge repo
    is not on this machine: the copy exists precisely so the suite does not need one, and a CI
    runner without it must not go red here. On a machine with both clones this runs on every
    local pass, which is where a brief edit is actually made."""
    source = _live_brief()
    if not source.exists():
        pytest.skip(
            f"no knowledge repo at {_knowledge_repo()} (set ${config.REPO_ENV}) — the frozen copy "
            f"exists precisely so the suite does not need one. To resync when you do have it:\n"
            f"  cp \"$STIGMERGY_REPO/{base_inputs.MEETING_BRIEF_RELPATH}\" "
            f"{_FROZEN_BRIEF.relative_to(_PLATFORM_ROOT)}")

    assert _FROZEN_BRIEF.read_bytes() == source.read_bytes(), (
        f"the frozen meeting brief has drifted from {source}. The contract table in this file is "
        f"checked against the frozen copy in CI, so a stale copy means CI is enforcing a contract "
        f"the agent is no longer given — the same drift with the sides swapped. Resync and "
        f"record the new sha in {_FROZEN_NOTES.relative_to(_PLATFORM_ROOT)}:\n"
        f"  cp \"$STIGMERGY_REPO/{base_inputs.MEETING_BRIEF_RELPATH}\" "
        f"{_FROZEN_BRIEF.relative_to(_PLATFORM_ROOT)}\n"
        f"  git -C \"$STIGMERGY_REPO\" log -1 --format=%H -- {base_inputs.MEETING_BRIEF_RELPATH}")


def test_the_frozen_brief_records_the_commit_it_was_taken_from():
    """A copy with no recorded provenance cannot be resynced with confidence — "is this behind or
    ahead?" has no answer without the sha the copy was taken at. Same assertion, same reason, as
    `test_frozen_linter.py`'s."""
    notes = _FROZEN_NOTES.read_text(encoding="utf-8")
    assert "Copied at commit" in notes
    sha = notes.split("Copied at commit")[1].split("`")[1]
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), (
        f"FROZEN.md must record the full 40-character source commit sha, found {sha!r}")


def test_the_contract_table_is_not_vacuous():
    """The anti-vacuity guard the switch to a frozen copy makes necessary. Reading the brief out of
    the knowledge repo SKIPPED when it was missing; this reads a file that is always there, so a
    truncated or emptied copy would turn every phrase check into a silent no-op against `""` — a
    check that stops running has to be impossible to miss, and that applies to the mechanism that
    replaced a skip as much as to the skip."""
    text = _FROZEN_BRIEF.read_text(encoding="utf-8")
    assert len(text) > 5_000 and "meeting" in text.lower()
    # Ten pairs today — seven, plus the three ADR-038 added. The floor is what makes shrinking the
    # table a deliberate act.
    assert len(RULE_TABLE) >= 10, "the brief<->gate contract table has gone thin"


# ── the code side: read once, so every table entry's "marker in code" check is grep-cheap ───────
def _code_text() -> str:
    import inspect
    return inspect.getsource(gates) + inspect.getsource(processing)


# (brief phrase, code marker) — see the module docstring for what each direction proves. Every
# phrase is a VERBATIM, single-line substring of SKILL.md (markdown emphasis and all — a
# substring check cannot cross the line breaks between them, so each entry was picked to sit
# inside one line of the real file).
#
# **The AGENT no longer writes any page.** It returns one JSON object in `.librarian-outcome.json`
# and the WORKER builds and writes every page in the set. Entries marked `re-pinned` point at the
# brief sentence that states the SAME rule in the current brief's own words. Six entries were
# REMOVED instead — see the module docstring for the two reasons, and the `REMOVED` records below
# for the per-entry reason and where the code-side rule is still covered. The records are kept in
# place, at the position the entry occupied, so the next person to widen this table sees which
# pairings were tried and refused.
RULE_TABLE = [
    # REMOVED: ("`sources/meetings/` | **exactly one**", "source-page-count").
    # BRIEF SIDE GONE, and the rule itself changed: the worker writes the source page verbatim from
    # the archived bytes and splits it into N >= 1 cross-linked parts, so "exactly one" is no
    # longer true of the code either. `source-page-count` survives only as a `< 1` veto whose own
    # docstring calls it unreachable by construction ("kept as a self-check, not a live path").
    # CODE-SIDE COVERAGE: none, and that is acceptable — a veto that cannot fire cannot be pinned by
    # a behavioural test without faking the impossible state it guards. The reachable half of the
    # arity contract (at least one source-page part, split when long) IS covered behaviourally by
    # `test_meeting_processing_pg.py::test_a_long_transcript_source_page_splits_into_cross_linked_`
    # `parts_and_files`.
    #
    # REMOVED: ("`wiki/meetings/` | **exactly one**", "meeting-page-count").
    # BRIEF SIDE GONE. The code veto (`!= 1`) is unchanged and live, but the brief's only singular
    # mention of the meeting page is the `description:` blurb ("a page SET — a source page, a
    # meeting page, and any"), whose companion singular "a source page" is already false now that
    # a long transcript splits; loose prose cannot honestly carry an arity rule. Adding an arity
    # sentence to the brief so this marker could read it back was proposed and REFUSED: writing a
    # rule into the brief for the sake of a test is the same defect as re-pointing at a sentence
    # that does not state it — the brief must be what the agent needs, not what this table wants.
    # CODE-SIDE COVERAGE: `test_meeting_processing_pg.py::test_two_resolvable_entities_file_`
    # `atomically_with_the_meeting_1to1_link_contract` asserts `len(meeting) == 1` over the paths of
    # the meeting's own commit, so the arity is pinned behaviourally on the filing road — AND the
    # veto BRANCH is pinned too, by `test_meeting_processing_pg.py::test_sabotage_proof_a_second_`
    # `meeting_page_is_refused_by_the_arity_veto`: it patches `_write_meeting_pages` into writing a
    # second `wiki/meetings/` page — the shape of the worker-construction bug this veto exists for,
    # since nothing else can reach it — and asserts the preserved refused diff names
    # `outcome/meeting-page-count` and NOTHING else, with no commit anywhere in the bare remote's
    # history. **This record said "the veto BRANCH stays unpinned" until that test existed; it was
    # corrected in the same pass that closed the gap, because a coverage note that has gone stale
    # is the same defect class this whole file exists to catch — a green side that describes
    # something which is no longer true.**
    #
    # REMOVED: ("carries **no anchoring outcome of its own**",
    #                 'no `"anchoring"` key in its declaration').
    # BOTH SIDES INVALID. The agent no longer declares anything about the source/meeting pages, so
    # the brief has no side; and the marker was DOCSTRING-only (gates.py, `_per_page_anchoring`'s own
    # docstring), so it never proved the behaviour even when it passed.
    # CODE-SIDE COVERAGE: the real behaviour (`if "anchoring" not in declared_for_page: continue`,
    # gates.py) is covered by EVERY happy-path filing test in `test_meeting_processing_pg.py` —
    # `processing._stamp_meeting` gives source and meeting pages a `page_declared`
    # entry with no `"anchoring"` key, so if that exemption broke, both would take an
    # `anchoring/undeclared` veto and no meeting could ever file at all.
    #
    # Re-pinned. The rule survived the restructure verbatim in spirit — each decision the agent
    # DESCRIBES still carries its own anchoring outcome, and `_per_page_anchoring` is still the
    # one-veto-per-page loop that asks that question of each decision page independently.
    ("decision you describe anchors on its OWN, independently of every other decision in the same",
     "_per_page_anchoring"),
    # **A figure-tracing row used to sit here and its code side is gone.** It was pinned to
    # `gates.prose_written`, the collector ingest-time figure verification read; that verification
    # was removed and so was the function. The row is removed rather than re-pointed at something
    # adjacent, because a contract test whose two sides no longer describe the same mechanism is
    # worse than no row. **The BRIEF, in the knowledge repo, still carries the sentence** telling a
    # live agent that a deterministic verifier will check its figures — which is no longer true,
    # and is an edit to that repo rather than to this one.
    #
    # Re-pinned to the brief's own server-owned FIELD LIST (the heir of "Do not write
    # `content_hash`, `tier`, `status`, `as_of`,"). `stamp_source_fields` is the worker function
    # that writes those fields onto the source page, which is why the pairing is the same rule.
    ("`owner`, `submitted_by`, `verification`, `acl`, `as_of`, `content_hash`, `id`, `entity`, "
     "`status`", "stamp_source_fields"),
    # Re-pinned. The marker is the sha256 over `ctx.material` — the archived material — which is
    # precisely what "computed by the worker from the archived material" names.
    ("computed by the worker from the archived material and this run's own facts.",
     'digest = hashlib.sha256((ctx.material or "").encode("utf-8")).hexdigest()'),
    # **The park row that stood here is gone with the parks.** A meeting no longer parks on an
    # unregistered name: the brief's third anchoring outcome INTRODUCES the entity, and the marker
    # is the meeting flow's own call into `identity.write_births` — the one line that makes it a
    # MEETING birth rather than a capture's (`related=` names the decision pages, or the meeting
    # page when there are none, so the newborn entity page links back to what introduced it). A
    # bare `write_births` marker would be satisfied by the ordinary flow's call; this keyword line
    # occurs in the meeting pass only.
    ("3. **INTRODUCE the entity it is about** — the decision is about a specific thing that is "
     "**not in",
     'related=decision_stems or [written["meeting_stem"]])'),
    # REMOVED: ("there is no edit mechanism in this flow.",
    #           "no additive edits to pages that already exist").
    # BOTH SIDES INVALID. The marker was a COMMENT in `processing.py`, so this entry never proved a
    # code behaviour even while green; and the brief phrase is gone. Worse, checking the comment
    # HID the real shape of the guarantee: the property is enforced purely by ABSENCE —
    # `edits.apply_declared` is simply never invoked in the meeting flow, and NO gate refuses a
    # modified page here, so wiring the edit path in would break the contract while this table
    # stayed green.
    # CODE-SIDE COVERAGE: `test_meeting_processing_pg.py::test_the_meeting_flow_never_reaches_the_`
    # `edit_path_a_raising_apply_declared_does_not_disturb_a_filing` — a behavioural lock, not a
    # text grep: it makes `edits.apply_declared` raise and asserts a meeting still files, so the
    # day that call becomes reachable from a meeting filing, that test fails.
    ("`declare-canonical` · `write-outside-lane` · `reveal-credentials`", "INJECTION_CATEGORIES"),
    # Repointed away from `decision-set-mismatch`. That marker checks the OUTCOME's own declared
    # decision list against the diff — a real rule — but this brief phrase ("links every decision
    # page you file... the worker checks both directions of this exactly") is a claim about the
    # MEETING PAGE'S OWN BODY, which `decision-set-mismatch` never reads at all. The old pairing
    # made this parametrized case pass for the wrong reason: `decision-set-mismatch` is real code
    # that is genuinely in the module, so `code_marker in code` was true, but it does not implement
    # the brief phrase it was paired with.
    # The rule itself is unchanged — the meeting page's "## Decisions" section and the decision
    # pages this capture filed correspond 1:1 — but it is no longer a VETO, so pairing it with a
    # veto marker was the defect this file's own docstring warns about: `meeting-links-mismatch`
    # survives in the sources only inside `_cross_check_meeting_outcome`'s docstring, in the list
    # of checks that are ABSENT. `code_marker in code` was true because a docstring named the
    # check's own death.
    # Repointed at the live mechanism instead. The brief states this as the worker's construction
    # ("the worker builds that section from exactly this list"), and `_build_meeting_page` builds
    # the section by iterating `decision_stems` — the link list IS the decision list it just
    # wrote, which is why a mismatch is structurally unrepresentable rather than checked. Sabotage
    # cover: `test_meeting_processing_pg.py::test_sabotage_proof_a_builder_bug_that_undercounts_`
    # `decision_pages_is_still_caught`.
    ("the worker builds that section from exactly this list.", "decision_stems"),
    # REMOVED: ("the worker checks both directions of this exactly",
    #           "_meeting_page_decision_links").
    # BRIEF SIDE GONE, AND DUPLICATED. That phrase was a promise to the AGENT about the links IT
    # wrote into the meeting page; the agent writes no page now, and the brief's single remaining
    # statement of the 1:1 rule is the sentence the `meeting-links-mismatch` entry ABOVE already
    # pins. Re-pointing this entry at the same sentence would spread one brief sentence over two
    # markers instead of pinning a second rule.
    # CODE-SIDE COVERAGE: `test_meeting_processing_pg.py::test_sabotage_proof_a_meeting_page_`
    # `missing_a_decision_link_is_still_caught` — it breaks the builder so the meeting page omits one
    # decision link and asserts the veto still fires, which exercises
    # `_meeting_page_decision_links` for real.
    #
    # REMOVED: ("`decisions` is a list, possibly empty.", "decision-set-mismatch").
    # CODE MARKER IS A DOCSTRING ABOUT ITS OWN DEATH. `decision-set-mismatch` no longer exists as a
    # check; its ONLY occurrence in the sources is `_cross_check_meeting_outcome`'s docstring, which
    # lists it among the checks that "are REMOVED because the disagreement they checked for is no
    # longer reachable" — so `code_marker in code` was true purely because a docstring names the
    # removal. Re-pointing it at its successor `decision-count-mismatch` using the brief's "The
    # worker turns each title into a filename itself" was proposed and REFUSED: that sentence is
    # about CONSTRUCTING FILENAMES, not about the count correspondence the marker enforces, so the
    # pairing would pass for the wrong reason — the same pass-for-the-wrong-reason defect one more
    # time. The 1:1 rule the brief does state is already pinned by `meeting-links-mismatch`.
    # CODE-SIDE COVERAGE: `test_meeting_processing_pg.py::test_sabotage_proof_a_builder_bug_that_`
    # `undercounts_decision_pages_is_still_caught` covers the live successor
    # `decision-count-mismatch`.
    # The date-bearing-link rule is mechanical — see `gardener.checks.check_date_bearing_body_links` —
    # and explicitly still live for the agent (that function's own docstring and
    # `_cross_check_meeting_outcome`'s: the agent still drafts the meeting page's "## Notes" prose
    # and each decision body, and either could link a date-bearing stem). The phrase stops at
    # "entirely" because the `sources:`/`related:` repair sits on the NEXT line of the real file
    # (the module docstring's own warning about line breaks).
    ("keep it out of body prose entirely", "date-bearing-body-link"),
    # ── the three rules ADR-038 added, and the reason each marker is the one it is ──────────────
    # The brief's whole framing of the gathered context rests on one claim: the worker looked, and
    # you cannot look again. The code side is `_one_meeting_pass`' OWN gather call, and the marker
    # is its closing `))` — the ordinary flow's identical-looking call ends `),` because it passes
    # `**_SEEDED_GATHERED_SENTENCES` on the next line, the EXPLORING wording that tells a reader to
    # search further. So this marker is not merely "the meeting flow gathers something": the two
    # characters that make it unique are exactly the ones that mean "rendered with
    # `render_gathered`'s no-tools defaults", which is what the brief phrase promises a tool-less
    # agent. Since ADR 045 D3 the closing characters are the capture's own audience, which is the
    # other half of what a tool-less agent is promised: the block it is handed is everything, AND
    # it is scoped to what this capture may cite. Verified against the concatenated source before
    # use: it occurs exactly once.
    ("tool for looking past it: judge overlap from what is in it, and never assert something "
     "about this",
     "acl=_capture_acl(item)))"),
    # The editable folder. `edits.validate` admits all three fast-lane folders, so what actually
    # narrows a MEETING's edits to decision pages is this flow's own lane, declared at its
    # `GateContext` and enforced by `gate_zone`'s `outside-lane` check — the keyword is the
    # declaration itself and occurs nowhere else.
    ("**`path` must be a decision page — `wiki/decisions/`, and nothing else.**",
     "write_prefixes=MEETING_WRITE_PREFIXES"),
    # All-or-nothing, and it costs the retry. `edits.apply_declared` returns `([], findings)` when
    # ANY declaration is bad, and this is the line that puts those findings in front of
    # `gates.vetoes` — without it a bad declaration would simply be skipped in silence. Pinned as
    # the two-line join because `+ edit_findings` alone also occurs in the ordinary flow;
    # `_cross_check_meeting_outcome` is what makes this the meeting road's own.
    ("guessed path or a name that resolves to nothing refuses the WHOLE set — every page of it — "
     "and",
     "+ edit_findings\n                + _cross_check_meeting_outcome(ctx, outcome))"),
]


@pytest.mark.parametrize("brief_phrase,code_marker", RULE_TABLE,
                        ids=[m for _, m in RULE_TABLE])
def test_brief_and_gates_agree_on_every_tabled_rule(brief_text, brief_phrase, code_marker):
    code = _code_text()
    in_brief = brief_phrase in brief_text
    in_code = code_marker in code
    assert in_brief and in_code, (
        f"brief<->gate contract drift: phrase {brief_phrase!r} "
        f"{'is' if in_brief else 'is NOT'} in the brief, marker {code_marker!r} "
        f"{'is' if in_code else 'is NOT'} in gates.py/processing.py — the two sides of this "
        f"contract disagree")


def test_the_contract_check_can_actually_fail(brief_text):
    """Before trusting a check, ask whether it can go red, and prove it: a demonstration that this
    check catches drift, not just that it currently passes. Sabotages ONE direction of the check
    with a phrase/marker pair that provably do not exist on either side, and asserts the SAME
    assertion `RULE_TABLE`'s parametrized test runs would refuse to pass silently."""
    code = _code_text()
    with pytest.raises(AssertionError):
        brief_phrase, code_marker = "a rule the brief never states", "a_marker_no_gate_has"
        assert (brief_phrase in brief_text) and (code_marker in code)


# MOVED OUT, and named here rather than dropped in silence: the check that the meeting flow's
# three folders are the ones `gate_zone` widens to now lives in `test_gates_unit.py`, rebuilt as a
# CODE <-> CODE check — `test_the_meeting_lane_is_exactly_the_range_of_paths_the_meeting_builder_
# can_write` plus its per-folder sabotage twin. Its brief side died with the restructure: the three
# folders were an instruction to an agent that could write pages, and the agent has no page-writing
# tool at all, so they are CODE's own placement contract now (`_write_meeting_pages`' range, judged
# by `gate_zone`). Keeping it here would have meant asking the brief to name folders the agent
# never uses; the anti-tautology property is preserved and strengthened there — the second side is
# the BUILDER's reachable paths, so a leaked folder is caught structurally.
