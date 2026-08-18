"""Direct, white-box tests of individual `librarian.gates` functions.

`test_processing_pg.py` and `test_adversarial.py` prove the gates end to end, through the real
worker over a real Postgres queue, a real git repo and the offline double. That is the right
place for most of the contract — but the offline double is deliberately WELL-BEHAVED apart from
the specific misbehaviors its directives stage (module docstring: "behaves perfectly on ordinary
material"), so several refusal paths are never reached by anything the double can be driven to do:

- it never writes a NUL byte or invalid UTF-8 (`gate_binary_page`'s own trigger — the fix for a
  chain that one byte set off: a NUL byte made `git diff` treat a page as binary, and the secrets,
  PII and body-rewrite gates each read "no added lines" as "nothing to object to");
- it never produces a copy, a typechange or an executable/symlink mode bit (`gate_zone`'s
  `unsupported-change` / `not-a-regular-file`);
- it never omits `page_type` or declares one that disagrees with the folder it wrote to
  (`gate_zone`'s `undeclared-type` / `type-folder-mismatch` — the double always keeps the two in
  agreement by construction);
- it never declares an anchor it did not also link, or a company scope with no reason
  (`gate_anchoring`'s `unresolved` / `undeclared` / `no-reason` — the double's own anchor is always
  real, read straight from the registry);
- and `_related_growth_ok`'s superset proof is only ever driven by CODE's own correct edits
  (`edits.py`), never by an adversarial "before" value.

Each of those is a real gate with a real message that crosses to an operator or a submitter, and a
defense with no test is a defense nobody has proven still works. Real git is used wherever a gate
reads a real diff (`gate_body_rewrite`) — a faked git proves nothing about the property being
claimed; `gitcmd.DiffEntry` is built by hand where only the diff's SHAPE
(status, mode) matters and no worktree content does, the same posture `test_edits_unit.py` and
`test_page.py` already take for the modules they cover directly.
"""
import os
import pathlib
from types import SimpleNamespace

import pytest

from stigmergy.kernel import registry as registry_module
from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import gates, gitcmd
from stigmergy.librarian import page as page_policy
from stigmergy.librarian import processing as processing_module
from stigmergy.librarian import report as report_module
from stigmergy.librarian.errors import LibrarianConfigError
from tests import adversarial_payloads as payloads

_COMMIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.test",
              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.test"}


def _ctx(worktree, entries, **over):
    base = dict(worktree=str(worktree), entries=entries, added=[], material="", outcome=None,
               registry=None)
    base.update(over)
    return gates.GateContext(**base)


# ── two clones do not race each other on anything under `ops/` ─────────────────────────────────
def test_the_write_lane_never_includes_ops_or_entities_however_many_workers_run(tmp_path):
    """The mechanical guarantee behind scaling to a second worker: the fast lane's write lane is
    refused-by-ABSENCE (`ALLOWED_WRITE_PREFIXES`'s own comment: "a new machine zone added tomorrow
    is out of bounds by default"), so neither worker — one or a hundred — can ever write `ops/` or
    `wiki/entities/` regardless of how many run concurrently. Two clones racing on a zone NEITHER
    may ever write to cannot corrupt it; this is what makes `test_adversarial_cat1_steering_that_
    also_trips_a_veto_is_rejected_never_obeyed`'s single-worker proof (an attempt to write
    `ops/acl.json` is refused) hold for N workers too, without needing a second, N-worker version
    of that same test."""
    assert not any(prefix.startswith("ops/") for prefix in gates.ALLOWED_WRITE_PREFIXES)
    assert not any(prefix.startswith("wiki/entities/")
                  for prefix in gates.ALLOWED_WRITE_PREFIXES)
    assert not any(prefix.startswith("meta/") for prefix in gates.ALLOWED_WRITE_PREFIXES)
    # The fast lane's prohibition on `views/` is untouched by the meeting flow — that flow is a
    # distinct writer, not a widened librarian. `views/` is a machine zone too (a governed
    # writer's own commits, App-bot-authored, exactly like `ops/`/`wiki/entities/` above) and must
    # be absent-by-construction from BOTH write lanes this package builds: the ordinary fast lane
    # and the meeting flow, which widens `ALLOWED_WRITE_PREFIXES` to its own three folders and
    # could in principle have widened it to four.
    assert not any(prefix.startswith("views/") for prefix in gates.ALLOWED_WRITE_PREFIXES)
    assert not any(prefix.startswith("views/") for prefix in processing_module.MEETING_WRITE_PREFIXES)
    # positive control: the fast lane's own folders ARE in the lane, so the assertions above are
    # proving an absence, not a broken/empty constant.
    assert gates.ALLOWED_WRITE_PREFIXES, "ALLOWED_WRITE_PREFIXES is empty — this test proves nothing"
    assert processing_module.MEETING_WRITE_PREFIXES, "MEETING_WRITE_PREFIXES is empty — this test proves nothing"


# ── the meeting lane is EXACTLY the meeting builder's own range (code <-> code) ──────────────────
# The agent has no page-writing tool at all (`agent.confine_outcome_write` — its one allowed write
# is its own outcome file), so the meeting flow's three folders are not an instruction to the agent
# but CODE's own placement contract: where `_write_meeting_pages` may create a page, judged by
# `gate_zone`/`in_lane_new_pages` through `GateContext.write_prefixes`. Pinning that against the
# distiller brief would mean asking the brief to name folders the agent has no use for; the honest
# second side is the builder itself, which is what these two tests use. They live in a unit file
# and not in `test_meeting_processing_pg.py` because they need NO Postgres and no queue: one
# `git init` for `gitcmd.tracked_paths` is the builder's whole environment, so putting them in a
# `_pg` file would have made a database the price of running a check that never touches one (and
# would have skipped them, silently, on any machine without one).
_ORDINARY_LANE_LEAKS = tuple(p for p in gates.ALLOWED_WRITE_PREFIXES
                             if p not in processing_module.MEETING_WRITE_PREFIXES)


def _meeting_builder_paths(tmp_path, name: str, *, decision_titles: tuple, material: str) -> list:
    """Every path the REAL `_write_meeting_pages` creates on disk for one outcome, read back off
    the filesystem rather than from the plan it returns — the plan reports stems, while `gate_zone`
    judges paths, and the paths are what this contract is about.

    A real `git init` (no commits, nothing tracked) is the builder's only environment need:
    `gitcmd.tracked_paths` is how it detects a collision with a page that already exists, and an
    empty checkout is the "no collision" case every one of these outcomes is meant to take.
    """
    repo = str(tmp_path / name)
    os.makedirs(repo)
    gitcmd.run("init", "--quiet", "-b", "main", repo)
    meeting_meta = {"title": "acme q3 renewal", "meeting_date": "2026-07-29"}
    # The real exported outcome type, not a stand-in: `MeetingOutcome` is what
    # `agent.parse_meeting_outcome` hands `_one_meeting_pass`, so a field this builder starts
    # reading tomorrow cannot silently be absent here.
    outcome = agent_module.MeetingOutcome(
        decision="file", meeting_title="Acme Q3 renewal", attendees=("Alice", "Bob"),
        meeting_notes="Renewal scope agreed.",
        action_items=({"owner": "Alice", "action": "send the quote", "done": False},),
        decisions=tuple({"title": title, "body": f"{title} — agreed in the meeting.",
                         "anchoring": {"kind": "company", "reason": "no single entity owns it"}}
                        for title in decision_titles))
    plan = processing_module._write_meeting_pages(
        repo, outcome, meeting_meta, material,
        source_stem=processing_module._source_stem(meeting_meta),
        created=meeting_meta["meeting_date"])
    assert not isinstance(plan, list), (
        f"the builder refused to write instead of writing (findings: {plan}) — this helper's "
        f"checkout is empty, so there is nothing for a computed path to collide with")
    created = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d != ".git"]
        created += [os.path.relpath(os.path.join(root, f), repo).replace(os.sep, "/")
                    for f in files]
    return sorted(created)


def _lane_complaints(paths: list, prefixes: tuple) -> list:
    """The check itself, with the lane as a PARAMETER so the sabotage twin below can run the exact
    same code against a widened lane. Both directions:

    (i) every path the builder computes is inside the lane (a builder that starts writing outside
        the folders `gate_zone` widens to would be refused at the gate, or worse, filed if the
        lane were widened to match by reflex);
    (ii) every prefix in the lane is REACHED by the builder — the anti-tautology half: a prefix no
        meeting page can ever occupy is a silent widening of what `gate_zone` permits, bought for
        nothing.
    """
    out = [f"{p!r} is written by the meeting builder but is outside the lane {prefixes}"
           for p in paths if not p.startswith(prefixes)]
    out += [f"{prefix!r} is in the lane but no path the meeting builder can compute starts with it"
            for prefix in prefixes if not any(p.startswith(prefix) for p in paths)]
    return out


def _max_body_lines() -> int:
    """`_build_source_parts`' own split threshold, read from where IT reads it — a test that
    hardcoded 150 would stop exercising the split the day the constant moved."""
    from stigmergy.kernel import page as ingest_page
    return ingest_page.MAX_BODY_LINES


def _all_builder_paths(tmp_path) -> list:
    """The builder's whole reachable range, over the outcome shapes that change WHICH paths it
    computes: no decisions at all, several decisions including two that slugify to the same stem
    (`_decision_stems`' `-2` road), and a transcript long enough to split into cross-linked parts
    (`_build_source_parts`) so the multi-source-page case is covered too."""
    long_material = "\n".join(f"transcript line {i}" for i in range(_max_body_lines() * 3))
    paths = _meeting_builder_paths(tmp_path, "no-decisions", decision_titles=(),
                                   material="Alice and Bob talked, nothing was decided.\n")
    paths += _meeting_builder_paths(
        tmp_path, "several-decisions",
        decision_titles=("Adopt a pricing floor", "Adopt a pricing floor", "Renew for 12 months"),
        material="Alice and Bob agreed the renewal terms.\n")
    paths += _meeting_builder_paths(tmp_path, "split-source",
                                    decision_titles=("Adopt a pricing floor",),
                                    material=long_material)
    return sorted(set(paths))


def test_the_meeting_lane_is_exactly_the_range_of_paths_the_meeting_builder_can_write(tmp_path):
    """`processing.MEETING_WRITE_PREFIXES` (what `gate_zone` widens the lane to for a meeting
    capture) must be PRECISELY `_write_meeting_pages`' own range: nothing the builder writes falls
    outside it, and nothing in it is unreachable from the builder.

    The second half is the load-bearing one, and it is why this is a real check rather than a
    restatement of the constant: it is what makes a fourth folder — added to the lane by a future
    edit, for a page this flow never files — fail here instead of quietly widening what the zone
    gate permits. Its sabotage twin below proves that failure is real.
    """
    paths = _all_builder_paths(tmp_path)
    assert paths, "the builder wrote nothing — this test would prove nothing"
    assert len([p for p in paths if p.startswith(processing_module.MEETING_SOURCE_PREFIX)]) > 1, (
        "the long-transcript case did not split into parts, so the multi-source-page road this "
        "test believes it covers was never exercised")

    assert _lane_complaints(paths, processing_module.MEETING_WRITE_PREFIXES) == []


@pytest.mark.parametrize("leaked", _ORDINARY_LANE_LEAKS)
def test_sabotage_proof_one_ordinary_fast_lane_folder_leaking_into_the_meeting_lane_still_fails(
        tmp_path, leaked):
    """**The anti-tautology property the check above owes.** A version of it that checked lane
    membership against `MEETING_WRITE_PREFIXES`' own value could never fail against the very
    widening it exists to catch, no matter how many ordinary fast-lane folders leaked in.

    Here the second side is the BUILDER, not the constant, so the property is structural rather
    than maintained by hand — and this twin proves it for every ordinary fast-lane folder that is
    not already a meeting folder, one parametrized case each: no meeting page can ever be written
    under `wiki/notes/` & co, so widening the lane to one of them is caught."""
    paths = _all_builder_paths(tmp_path)
    complaints = _lane_complaints(paths, (*processing_module.MEETING_WRITE_PREFIXES, leaked))
    assert any(leaked in c for c in complaints), (
        f"widening the meeting lane to {leaked!r} — a folder no meeting page can occupy — was not "
        f"caught: {complaints}")


# ── gate_binary_page: the fix for the chain one stray byte could set off ────────────────────────
def test_a_nul_byte_in_a_new_page_is_vetoed_as_binary(tmp_path):
    """The defect reproduced directly: one NUL byte is enough to make `git diff` render a page as
    binary, which is why this gate runs FIRST and refuses the byte outright rather than letting a
    downstream gate read an empty added-lines list as a clean page."""
    page = tmp_path / "wiki" / "notes" / "Bad.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"---\ntype: note\n---\n\nA note with a stray byte: \x00 in it.\n")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/Bad.md", new_mode="100644")])

    findings = gates.gate_binary_page(ctx)

    assert [f.code for f in findings] == ["binary-page"]
    assert findings[0].locator == "wiki/notes/Bad.md"
    assert "\x00" not in findings[0].message            # never the content, only the fact


def test_invalid_utf8_in_a_new_page_is_also_vetoed_as_binary(tmp_path):
    page = tmp_path / "wiki" / "notes" / "Bad.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"---\ntype: note\n---\n\nLatin-1 only: caf\xe9.\n")   # not valid UTF-8
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/Bad.md", new_mode="100644")])

    assert [f.code for f in gates.gate_binary_page(ctx)] == ["binary-page"]


def test_ordinary_utf8_with_real_multibyte_characters_is_the_benign_twin(tmp_path):
    """The benign twin: a page with genuine accented/non-ASCII prose — the normal shape of a
    captured note in this brain (`test_processing_pg.py`'s own accented-title case) — must never
    be mistaken for binary."""
    page = tmp_path / "wiki" / "notes" / "Zürich.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: note\n---\n\n# Zürich\n\nAccents é and ñ, 設計 also.\n",
                    encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/Zürich.md", new_mode="100644")])

    assert gates.gate_binary_page(ctx) == []


def test_a_page_the_diff_claims_but_the_worktree_does_not_have_is_vetoed_not_crashed(tmp_path):
    """A diff/worktree disagreement no double directive can express. The honest response is a
    finding, not an exception raised three gates before the one that would have explained it."""
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/Ghost.md", new_mode="100644")])
    assert [f.code for f in gates.gate_binary_page(ctx)] == ["unreadable-page"]


# ── gate_zone: diff shapes the offline double never produces ───────────────────────────────────
def test_a_dotfile_in_the_lane_is_refused_as_not_a_page(tmp_path):
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/.gitattributes",
                                          new_mode="100644")])
    assert [f.code for f in gates.gate_zone(ctx)] == ["not-a-page"]


def test_a_non_markdown_file_in_the_lane_is_refused_as_not_a_page(tmp_path):
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/README.txt", new_mode="100644")])
    assert [f.code for f in gates.gate_zone(ctx)] == ["not-a-page"]


@pytest.mark.parametrize("status", ["C", "T", "U"])
def test_a_change_type_the_fast_lane_does_not_file_is_refused_by_name(tmp_path, status):
    """A copy, a typechange, an unmerged entry — none of these is "adding a page" or "additively
    editing one", and each must be refused BY NAME rather than silently falling through a status
    check that only listed A/M/D (the shape that let a page-replaced-by-a-symlink through once)."""
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry(status, "wiki/notes/Existing Note.md",
                                          new_mode="100644")])
    assert [f.code for f in gates.gate_zone(ctx)] == ["unsupported-change"]


@pytest.mark.parametrize("mode,label", [("100755", "an executable bit"), ("120000", "a symlink")])
def test_a_page_written_with_the_wrong_file_mode_is_refused(tmp_path, mode, label):
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/A New Page.md", new_mode=mode)])
    assert [f.code for f in gates.gate_zone(ctx)] == ["not-a-regular-file"], label


def test_an_ordinary_regular_md_addition_with_a_matching_type_is_the_benign_twin(tmp_path):
    """The specificity half for every check above: an everyday add, correctly typed, must trip
    nothing in the zone gate at all."""
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/A New Page.md",
                                          new_mode="100644")],
              outcome=SimpleNamespace(page_type="note"))
    assert gates.gate_zone(ctx) == []


# ── gate_zone / the type half: only reachable by constructing the outcome directly ──────────────
def test_a_created_page_declaring_no_type_at_all_is_refused(tmp_path):
    """`undeclared-type`: the offline double always sets `page_type` from its own folder table, so
    this is only reachable by handing the gate an outcome that omits it — exactly what an agent
    that silently dropped the field would produce."""
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/A New Page.md",
                                          new_mode="100644")],
              outcome=SimpleNamespace(page_type=""))
    assert [f.code for f in gates.gate_zone(ctx)] == ["undeclared-type"]


def test_a_created_page_whose_declared_type_disagrees_with_its_folder_is_refused(tmp_path):
    """`type-folder-mismatch`: the diff decides, not the label, exactly as the gate's own
    docstring claims — a page filed under `notes/` while the outcome calls it a `decision`."""
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/A New Page.md",
                                          new_mode="100644")],
              outcome=SimpleNamespace(page_type="decision"))
    assert [f.code for f in gates.gate_zone(ctx)] == ["type-folder-mismatch"]


# ── gate_zone / `meeting-edit-refused`: the check itself, and the PRECEDENCE it has ─────────────
#
# The finding: a status-`M` entry from a caller that grants no edit mechanism (`ctx.edits_allowed`
# `False` — only `processing._one_meeting_pass` ever sets it). Its END-TO-END proof lives in
# `test_meeting_processing_pg.py` (the refusal, its terminality, its sabotage twin, atomicity);
# these are the unit-level checks, which fail fast and need no Postgres.
#
# **Its POSITION in `gate_zone`'s per-entry chain is a contract, and nothing pinned it.** The stated
# principle is CATCH-ALL, THEREFORE LAST: the finding means "the only thing wrong with this entry is
# that it is a modification", so every more specific diagnosis the gate can make gets first say.
# Sitting immediately after `deletion` — where it started — it shadowed four of them for a
# status-`M` entry, and, composed with `_refuse_meeting`'s `f.repairable` filter, that shadowing
# made the `write-outside-lane` steering category unreachable for a MODIFIED out-of-lane path,
# because `meeting-edit-refused` is `repairable=False` while `outside-lane` is not. The order below
# is therefore behaviour worth pinning, not a formatting preference.
_ZONE_M_LANE = ("wiki/decisions/",)      # one prefix, so "outside the lane" is unambiguous
_ZONE_M_PAGE = "wiki/decisions/an-earlier-decision.md"

# `(label, entry, the code gate_zone must report)`. Every case is a MODIFICATION (except the first,
# which cannot be — see the test's docstring), run against a context that grants no edit mechanism,
# so each one is an entry `meeting-edit-refused` would claim from an earlier position.
_MORE_SPECIFIC_THAN_AN_EDIT = [
    ("an unfileable git status", gitcmd.DiffEntry("T", _ZONE_M_PAGE, new_mode="120000"),
     "unsupported-change"),
    ("a path this flow may not write at all",
     gitcmd.DiffEntry("M", "wiki/notes/Existing Note.md", new_mode="100644"), "outside-lane"),
    ("an executable bit on a page", gitcmd.DiffEntry("M", _ZONE_M_PAGE, new_mode="100755"),
     "not-a-regular-file"),
    ("a symlink where a page should be", gitcmd.DiffEntry("M", _ZONE_M_PAGE, new_mode="120000"),
     "not-a-regular-file"),
    # The dotfile case is a REAL precedent, not a hypothetical: `not-a-page`'s own comment records a
    # legacy in-lane `wiki/notes/.gitattributes` that "used to pass every check here", and
    # `* -diff` in such a file makes every later diff in the folder binary. A tracked dotfile that
    # already exists can therefore be MODIFIED, which is exactly this row.
    ("a dotfile that already exists in the lane",
     gitcmd.DiffEntry("M", "wiki/decisions/.gitattributes", new_mode="100644"), "not-a-page"),
    ("a non-`.md` path", gitcmd.DiffEntry("M", "wiki/decisions/notes.txt",
                                          new_mode="100644"), "not-a-page"),
    ("a filename that cannot be spelled",
     gitcmd.DiffEntry("M", "wiki/decisions/Bad\x01Name.md", new_mode="100644"),
     "unnameable-page"),
    # Two COMPOUND entries — several defects at once — which are the only shapes that can pin the
    # order AMONG the specific checks rather than merely their order against the catch-all.
    ("out of the lane AND a dotfile",
     gitcmd.DiffEntry("M", "wiki/notes/.gitattributes", new_mode="100644"), "outside-lane"),
    ("a dotfile AND an executable bit",
     gitcmd.DiffEntry("M", "wiki/decisions/.gitattributes", new_mode="100755"),
     "not-a-regular-file"),
]


@pytest.mark.parametrize("label,entry,expected",
                        _MORE_SPECIFIC_THAN_AN_EDIT,
                        ids=[case[0].replace(" ", "-") for case in _MORE_SPECIFIC_THAN_AN_EDIT])
def test_a_more_specific_zone_defect_outranks_the_edit_refusal_for_a_modified_entry(
        tmp_path, label, entry, expected):
    """**The enumeration of `gate_zone`'s per-entry order**: for each more specific shape a
    modification can have, the reported code is THAT one and never `meeting-edit-refused` — the
    extra fact ("it also wrote outside the lane", "it is also a symlink") is what the operator
    needs, and the catch-all would have hidden it.

    Asserted twice per case, against a context that grants no edit mechanism AND one that does: the
    code must be the same either way. That is the precise statement of non-shadowing — the presence
    of the edit refusal changes nothing for an entry that has a more specific defect — and it is
    what fails under an ordering that puts the catch-all first, where the `edits_allowed=False`
    column reports `meeting-edit-refused` for every row below the first.

    **What this test cannot catch, stated plainly rather than implied away:**

    * **A check added to the loop AFTER `meeting-edit-refused` in the future would be shadowed and
      this enumeration would not see it**, because its shape is not in the list above and nothing
      here derives the list from the loop. This test pins the order among the checks that EXIST; the
      thing that keeps the property true for a new one is the rule written in `gate_zone`'s own
      comment ("before this one, unless it is ALSO a catch-all") plus a reviewer who reads it. If you
      add a per-entry check to that loop, add its shape here in the same commit — that is the whole
      maintenance contract of this list, and it is a convention, not a mechanism.
    * The first row is an ORDER ANCHOR, not a shadow case: `unsupported-change` fires on
      `status not in ("A", "M")` and the edit refusal on `status == "M"`, so the two are mutually
      exclusive and no ordering between them could ever matter. It is included because a reader of
      `gate_zone` might reasonably assume otherwise; keeping the row documents that the claim was
      checked and is false.
    * Precedence among the SPECIFIC checks is pinned only where a single entry carries two defects
      (the two compound rows). A reordering of two specific checks that no single entry can trip at
      once is invisible here, and harmlessly so.
    """
    strict = _ctx(tmp_path, [entry], write_prefixes=_ZONE_M_LANE, edits_allowed=False)
    permissive = _ctx(tmp_path, [entry], write_prefixes=_ZONE_M_LANE)

    # Non-vacuity, per case: this entry really is inside the catch-all's OWN domain (its condition
    # is exactly `status == "M" and not ctx.edits_allowed`), so a specific code below means it was
    # OUTRANKED — not that it was out of the catch-all's reach. The one exception is the `T` anchor
    # row, whose status the catch-all cannot match at all.
    if entry.status == "M":
        assert not strict.edits_allowed, "the strict context stopped withholding the edit mechanism"

    assert [f.code for f in gates.gate_zone(strict)] == [expected], (
        f"{label}: expected {expected!r}. If this says 'meeting-edit-refused', the catch-all has "
        f"moved back above a more specific check and is hiding it — the exact defect that made the "
        f"write-outside-lane steering category unreachable for a modified path")
    assert [f.code for f in gates.gate_zone(permissive)] == [expected], (
        f"{label}: a caller that GRANTS an edit mechanism must get the same {expected!r} finding — "
        f"the specific checks are not conditional on `edits_allowed` and must not become so")


def test_a_plain_in_lane_modification_is_the_edit_refusal_and_is_terminal(tmp_path):
    """The catch-all's own case, and the unit-level pin for the finding: an in-lane, regular-mode,
    properly named page that already exists and was MODIFIED — nothing else wrong with it at all —
    is refused as `meeting-edit-refused`, `repairable=False`.

    `repairable` is asserted here and not only through the end-to-end pair because it is what buys
    the terminal refusal: `processing.unrepairable()` reads exactly this flag, and a finding that
    became repairable by accident would spend the flow's one corrective retry on an agent that holds
    no tool to have produced the modification, and would let `_reset_for_retry` erase the refused
    diff — the evidence — before `preserve_refused_diff` ever ran. One line of production code, one
    assertion, no database.
    """
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("M", _ZONE_M_PAGE, old_mode="100644",
                                          new_mode="100644")],
              write_prefixes=_ZONE_M_LANE, edits_allowed=False)

    findings = gates.gate_zone(ctx)

    assert [f.code for f in findings] == ["meeting-edit-refused"]
    assert findings[0].locator == _ZONE_M_PAGE
    assert findings[0].repairable is False, (
        "the edit refusal became repairable: the corrective retry would then be spent on an agent "
        "that cannot act, and `_reset_for_retry` would erase the refused diff before it was ever "
        "preserved")


def test_the_same_plain_modification_is_no_finding_at_all_when_the_caller_grants_edits(tmp_path):
    """The specificity half, and the anti-tautology guard for the enumeration above: the SAME entry
    that the test above refuses trips nothing in `gate_zone` when `edits_allowed` keeps its `True`
    default (every ordinary fast-lane caller). Without this, every parametrized row above would
    still pass if `edits_allowed` were never read at all — the difference between the two contexts
    would be nothing, and the enumeration would be pinning an order among checks with no catch-all
    to outrank. `gate_body_rewrite`, a later gate, is what judges whether that edit was additive;
    this gate is done with it."""
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("M", _ZONE_M_PAGE, old_mode="100644",
                                          new_mode="100644")],
              write_prefixes=_ZONE_M_LANE)
    assert gates.gate_zone(ctx) == []


def _edit_refusal_message(tmp_path, path: str) -> str:
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("M", path, old_mode="100644", new_mode="100644")],
              write_prefixes=(path.rsplit("/", 1)[0] + "/",), edits_allowed=False)
    findings = gates.gate_zone(ctx)
    assert [f.code for f in findings] == ["meeting-edit-refused"], (
        f"the fixture stopped producing the edit refusal for {path!r} ({[f.code for f in findings]})"
        f" — the message assertions below would be about a different finding")
    return findings[0].message


def test_the_edit_refusals_message_names_the_cause_space_and_the_preserved_evidence(tmp_path):
    """**The message is the operator's WHOLE briefing** — it reaches them verbatim as
    `report.failed_system`'s `reason` — so what it says is behaviour, not prose. Two things it must
    carry, and one it must not:

    * the CAUSE-SPACE: a worker defect or worktree interference, and explicitly NOT the submitted
      material. This is the same failure `_refuse_meeting`'s `f.repairable` filter closes one layer
      up (an unrepairable zone finding routing through `rejected_steering` and naming the
      submitter's capture as the cause); a message that drifts back toward blaming the submitter
      would reintroduce it in the one place an operator actually reads.
    * that the EVIDENCE was kept. `processing.preserve_refused_diff` runs on this terminal path and
      `diagnostics_path` names the file; a refusal that does not say so sends the reader looking for
      a reaped worktree.
    * the PATH and nothing from the page. Every finding in this module names a locator, never
      content — a modified page's own bytes are captured material.
    """
    message = _edit_refusal_message(tmp_path, _ZONE_M_PAGE)

    assert _ZONE_M_PAGE in message
    for phrase in ("worker defect", "worktree interference", "not the material", "preserved"):
        assert phrase in message, (
            f"the refusal message no longer says {phrase!r}: {message!r}. This message IS the "
            f"operator's briefing (report.failed_system's `reason`), so dropping the cause-space or "
            f"the preserved-evidence clause is a behaviour change, not an edit to prose")


def _worst_reachable_meeting_paths() -> list:
    """The LONGEST path each of the meeting flow's three page builders can compute, from the real
    builders rather than a guessed literal — `slugify`'s length cap lives in
    `stigmergy.kernel.normalize` and a hardcoded worst case here would stop being the worst case the
    day it moved."""
    title = "a decision about the acme renewal negotiation and the pilot scope for next year " * 3
    return [
        # `-2`: `_decision_stems`' own same-slug suffix, the longest form it produces.
        processing_module.MEETING_DECISION_PREFIX
        + processing_module._decision_stems([title, title])[1] + ".md",
        processing_module.MEETING_MEETING_PREFIX
        + processing_module._meeting_stem("2026-07-29", title) + ".md",
        # `-p12`: `_build_source_parts`' part suffix on a long, split transcript.
        processing_module.MEETING_SOURCE_PREFIX
        + processing_module._source_stem({"title": title}) + "-p12.md",
    ]


def test_the_refusals_anti_blame_clause_survives_the_reports_200_character_reason_clamp(tmp_path):
    """The message travels through `report.failed_system`, which clamps `reason` to 200 characters
    (word-safe, `text.clamp`). The message is ~147 characters of fixed text plus the PATH, so how
    much of it an operator actually reads depends on how long the page's name is — and the paths
    this flow computes are long (a slug capped at 60 characters, under a prefix, with a part or
    same-slug suffix).

    So this pins the half that must never be lost: for the longest path each of the flow's three
    builders can produce, the clamped summary an operator reads still says the fault is not the
    submitted material. **The evidence clause is deliberately NOT asserted here, because it does not
    survive** — it is the tail of the sentence and is truncated for any path over ~67 characters,
    which is most real ones (the fixture in `test_meeting_processing_pg.py` uses a short path, where
    both clauses survive, and asserts both there). Nothing in this test lengthens or shortens the
    message: it records where the bound currently is, and fails if a future edit to that sentence
    pushes the anti-blame clause past it — at which point an operator's report would start reading
    like the submitter's fault again.
    """
    for path in _worst_reachable_meeting_paths():
        message = _edit_refusal_message(tmp_path, path)
        summary = report_module.failed_system(attempts=1, stage="zone", reason=message,
                                             agent_attempts=1)["summary"]
        assert "not the material" in summary, (
            f"the anti-blame clause was truncated out of the operator-facing report for a path "
            f"this flow can really produce ({len(path)} chars: {path!r}). The refusal message is "
            f"now too long for `report.failed_system`'s 200-character `reason` clamp — shorten the "
            f"message's FIXED text (the path cannot be shortened), or move the clause earlier in "
            f"the sentence; do not widen the clamp, which every other refusal shares.\n"
            f"summary tail: {summary[-120:]!r}")


# ── gate_anchoring: the enforcement behind "nothing is filed ownerless" ─────────────────────────
# The gate checks the DECLARED `anchoring.entities` list against the registry, and never reads a
# page's own links at all — the wikilink-scanning mechanism it replaced is gone. The
# offline double's own anchor is always real (`DoubleAgent._registry_entity` reads the actual
# registry), so these refusals — the backstop for an agent that CLAIMS an anchor rather than
# earning one — are otherwise dead code from the test suite's point of view. See
# `test_processing_pg.py`'s `test_a_claimed_entity_anchor_that_does_not_resolve_is_refused_never_
# filed` for the same property proven end to end, through a real worker run.
def _registry(entities: dict, *, aliases: dict | None = None) -> registry_module.Registry:
    """A real `Registry`, keyed by the real `registry.index_entity` — the same function
    `load_registry` uses — so this fixture cannot silently diverge from what the gate's own
    resolution actually does. It keys TWO maps (the narrow one `canonical_id` reads, the coarse
    collision one the mint gate reads), which is precisely why a hand-filled `by_alias` here would
    make this file agree with itself about a fold production does differently."""
    reg = registry_module.Registry()
    for cid, name in entities.items():
        reg.entities[cid] = {"name": name, "type": "organization",
                             "aliases": list((aliases or {}).get(cid, ()))}
        registry_module.index_entity(reg, cid, reg.entities[cid])
    return reg


def test_a_declared_entity_anchor_that_does_not_resolve_is_unresolved(tmp_path):
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# New\n\nOrdinary content, no wikilinks required.\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              outcome=SimpleNamespace(anchoring={"kind": "entity",
                                                 "entities": ["Ghost Company Inc"]}),
              registry=_registry({"acme": "Acme Corp"}))

    findings = gates.gate_anchoring(ctx)

    assert [f.code for f in findings] == ["unresolved"]
    # Unlike a wikilink scraped off untrusted page content, the declared value is the agent's own
    # STRUCTURED outcome, and the finding must NAME it — the opposite rule from the wikilink-era
    # message it replaces.
    assert '"Ghost Company Inc"' in findings[0].message
    assert "does not resolve in the entity registry read at the base commit" in findings[0].message


def test_a_declared_id_name_or_alias_all_resolve(tmp_path):
    """The agent may declare an id, a display name OR an alias — the gate resolves each through
    `registry.canonical_id`, exactly the function that also accepts all three."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# New\n\nOrdinary content.\n", encoding="utf-8")
    registry = _registry({"acme": "Acme Corp"}, aliases={"acme": ["Acme"]})

    for declared in ("acme", "Acme Corp", "Acme"):
        ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
                  outcome=SimpleNamespace(anchoring={"kind": "entity", "entities": [declared]}),
                  registry=registry)
        assert gates.gate_anchoring(ctx) == [], declared


def test_every_declared_value_must_resolve_not_merely_one(tmp_path):
    """Two declared entities, one unresolved — the finding names the one that failed, not the
    whole list, and the resolving one is not enough to satisfy the gate on its own."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# New\n\nOrdinary content.\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              outcome=SimpleNamespace(anchoring={"kind": "entity",
                                                 "entities": ["Acme Corp", "Ghost Co"]}),
              registry=_registry({"acme": "Acme Corp"}))

    findings = gates.gate_anchoring(ctx)

    assert [f.code for f in findings] == ["unresolved"]
    assert findings[0].locator == "Ghost Co"
    assert "Acme Corp" not in findings[0].message


def test_two_unresolved_ids_are_both_named_in_the_message(tmp_path):
    """The finding names EVERY unresolved id, not just the first, so a corrective retry fixes all
    of them in one pass."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# New\n\nOrdinary content.\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              outcome=SimpleNamespace(anchoring={"kind": "entity",
                                                 "entities": ["acme-ventures-inc",
                                                             "acme-ventures-holdings"]}),
              registry=_registry({}))

    findings = gates.gate_anchoring(ctx)

    assert '"acme-ventures-inc"' in findings[0].message
    assert '"acme-ventures-holdings"' in findings[0].message
    assert "anchors" in findings[0].message and "do not resolve" in findings[0].message


# ── the corrective brief: what a finding owes the AGENT ─────────────────────────────────────────
# Measured with the real agent: three forced anchoring vetoes, three retries, zero recoveries, with
# the brief provably delivered and provably read. The message was a diagnosis written for a human
# report; these assert the three things a brief owes that it did not.
def _unresolved_finding(tmp_path, registry, *, anchoring=None):
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# New\n\nOrdinary content — no wikilinks are read by this gate any more.\n",
                    encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
               outcome=SimpleNamespace(anchoring=anchoring if anchoring is not None
                                       else {"kind": "entity",
                                             "entities": ["Ghost Company Inc"]}),
               registry=registry)
    findings = gates.gate_anchoring(ctx)
    assert [f.code for f in findings] == [gates.ANCHORING_UNRESOLVED]
    return findings[0]


def test_the_anchoring_brief_names_what_the_gate_examined_and_what_would_satisfy_it(tmp_path):
    """The three debts a corrective brief owes, in one brief: what was EXAMINED (the DECLARED
    entities — never the page's own links, which this gate no longer reads at all) and the registry
    it asked, by content — `config.py` records a live asymmetry that makes "read the registry file"
    unreliable advice — which outcomes are AVAILABLE, and the smallest edit that reaches one."""
    finding = _unresolved_finding(tmp_path, _registry({"acme": "Acme Corp", "globex": "Globex"}))

    brief = finding.brief
    assert "Ghost Company Inc" in brief                                   # what it examined
    assert "acme — Acme Corp" in brief and "globex — Globex" in brief     # what it asked, as ids
    assert '"kind": "entity"' in brief                                    # outcome 1
    assert '"kind": "company"' in brief                                   # outcome 2
    assert '"kind": "unresolved-entity"' in brief                         # outcome 3, the park
    # The repair the measured agent never found: nothing on the page needs to change.
    assert "nothing on the page itself is checked or needs to change" in brief
    assert "Adding a wikilink, rewriting the body, or renaming the page" in brief


def test_an_empty_registry_says_re_anchoring_is_not_available_rather_than_offering_it(tmp_path):
    """An empty registry is a DIFFERENT situation from a misspelled name and the agent could not
    previously tell them apart: with nothing registered, "anchor to an id that resolves" is not a
    repair that exists, and a brief that offered it anyway would spend the one retry on it."""
    finding = _unresolved_finding(tmp_path, _registry({}))

    assert "NO entities at all" in finding.brief
    assert "1. ANCHOR — unavailable" in finding.brief
    assert '"kind": "company"' in finding.brief and '"kind": "unresolved-entity"' in finding.brief


def test_the_unresolved_name_travels_in_the_locator_and_also_in_the_message(tmp_path):
    """The locator is what `processing._unanchorable` parks the item with; the message is what a
    human reads, and it NAMES the unresolved id — unlike the retired wikilink-era message, which
    echoed nothing because the value came off untrusted page content rather than the agent's own
    declared outcome."""
    finding = _unresolved_finding(tmp_path, _registry({"acme": "Acme Corp"}))

    assert finding.locator == "Ghost Company Inc"
    assert '"Ghost Company Inc"' in finding.message


def test_the_locator_falls_back_to_something_unnamed_when_nothing_was_declared(tmp_path):
    """An outcome declaring `kind: "entity"` with an EMPTY `entities` list has no wikilink fallback
    to fall back on — the old mechanism that gave it one is gone — but it must still reach the
    steward park (`processing._unanchorable` -> `triage_entity`) rather than a system fault:
    "nothing here anchors" is a parked outcome, not a crash. The locator must therefore stay TRUTHY
    (`_unanchorable` requires one before it parks), which is what the `"something unnamed"`
    fallback — the same word `processing._triage` already uses for an agent-declared park with no
    name — guarantees."""
    nothing = _unresolved_finding(tmp_path, _registry({"acme": "Acme Corp"}),
                                  anchoring={"kind": "entity", "entities": []})
    assert nothing.locator == "something unnamed"
    assert '"anchoring.entities" is empty' in nothing.brief
    assert 'names no entity at all' in nothing.message


def test_a_declared_value_is_bounded_and_stripped_before_it_reaches_the_next_prompt(tmp_path):
    """A declared entity comes off an outcome the agent produced from UNTRUSTED material, and the
    brief goes into the next pass's PROMPT, so a value carrying newlines could otherwise forge the
    brief's own structure."""
    hostile = "Ghost\nCorp\x07 " + "x" * 200
    finding = _unresolved_finding(tmp_path, _registry({"acme": "Acme Corp"}),
                                  anchoring={"kind": "entity", "entities": [hostile]})

    assert "\n" not in finding.locator and "\x07" not in finding.locator
    assert len(finding.locator) <= gates.MAX_BRIEF_NAME_LEN + 1     # +1 for the ellipsis
    declared_line = finding.brief.splitlines()[1]                   # the `declared` line
    assert "\x07" not in declared_line and len(declared_line) < 300


def test_corrective_brief_prefers_the_brief_and_falls_back_to_the_message():
    """Optional by design: a message that already reads as a repair instruction — the linter's
    `{path}: dead link: [[X]]` — needs no second text, and a gate that has one is not required to
    write the message twice."""
    with_brief = gates.Finding("anchoring", "unresolved", "the human diagnosis",
                               brief="the repair\n  a continuation line")
    without = gates.Finding("contract", "dead_links", "notes/A.md: dead link: [[B]]")

    text = gates.corrective_brief([with_brief, without])

    assert "- [anchoring] the repair" in text
    assert "the human diagnosis" not in text
    assert "- [contract] notes/A.md: dead link: [[B]]" in text
    # A multi-line brief stays ONE point rather than reading as several.
    assert "\n    a continuation line" in text
    # And the preamble does not presuppose re-filing, because parking is a valid repair.
    assert "write the page again" not in text




def test_a_created_page_with_no_anchoring_outcome_at_all_is_undeclared(tmp_path):
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# New\n\nNo anchor declared.\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              outcome=SimpleNamespace(anchoring={}), registry=_registry({}))

    assert [f.code for f in gates.gate_anchoring(ctx)] == ["undeclared"]


def test_a_company_wide_scope_with_no_written_reason_is_refused(tmp_path):
    """"Silence is not an outcome" applies to the reason sentence too — an empty string is silence
    with extra steps."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# New\n\nApplies company-wide.\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              outcome=SimpleNamespace(anchoring={"kind": "company", "reason": "  "}),
              registry=_registry({}))

    assert [f.code for f in gates.gate_anchoring(ctx)] == ["no-reason"]


def test_a_resolving_entity_anchor_is_the_benign_twin(tmp_path):
    """The page's own body plays no part in this — no wikilink is required or read; only the
    declared `anchoring.entities` list matters."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# New\n\nThis page carries no wikilinks at all.\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              outcome=SimpleNamespace(anchoring={"kind": "entity", "entities": ["Acme Corp"]}),
              registry=_registry({"acme": "Acme Corp"}))

    assert gates.gate_anchoring(ctx) == []


def test_a_company_wide_scope_with_a_reason_is_the_other_benign_twin(tmp_path):
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# New\n\nApplies company-wide.\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              outcome=SimpleNamespace(anchoring={"kind": "company",
                                                 "reason": "applies to every client"}),
              registry=_registry({}))

    assert gates.gate_anchoring(ctx) == []


# ── resolve_entity_ids: the page's stamped `entity:` and the report's anchor, one source ────────
# The return type is `(ids, unresolved)`, not `ids` alone: company-wide and "declared an entity
# anchor but nothing resolved" would otherwise fold onto the same `[]` and be indistinguishable.
def test_resolve_entity_ids_returns_canonical_ids_never_the_raw_declared_value():
    """The PAGE is stamped with the resolved canonical id, whatever the agent typed — an id, a
    display name or an alias."""
    registry = _registry({"acme": "Acme Corp"}, aliases={"acme": ["Acme"]})
    assert gates.resolve_entity_ids(
        {"kind": "entity", "entities": ["Acme Corp"]}, registry) == (["acme"], [])
    assert gates.resolve_entity_ids(
        {"kind": "entity", "entities": ["Acme"]}, registry) == (["acme"], [])


def test_resolve_entity_ids_dedupes_resolved_ids_preserving_order():
    """Two declared spellings resolving to the same id must not stamp
    `entity: ["acme", "acme"]`."""
    registry = _registry({"acme": "Acme Corp"}, aliases={"acme": ["Acme"]})
    ids, unresolved = gates.resolve_entity_ids(
        {"kind": "entity", "entities": ["Acme", "Acme Corp"]}, registry)
    assert ids == ["acme"]
    assert unresolved == []


def test_resolve_entity_ids_is_empty_for_company_wide():
    assert gates.resolve_entity_ids({"kind": "company", "reason": "x"}, _registry({})) == ([], [])


def test_resolve_entity_ids_is_empty_for_a_malformed_or_missing_anchoring():
    assert gates.resolve_entity_ids({}, _registry({})) == ([], [])
    assert gates.resolve_entity_ids(None, _registry({})) == ([], [])


def test_resolve_entity_ids_drops_an_unresolved_value_from_ids_but_reports_it():
    """An unresolved declared value is DROPPED from `ids` (never stamped as raw text — stamping it
    would give the contract linter's own `entity:` validation something to trip on, a second
    unrelated `contract` veto), and reported in `unresolved` rather than silently disappearing,
    so the caller can tell "nothing resolved" apart from "company-wide"."""
    registry = _registry({"acme": "Acme Corp"})
    ids, unresolved = gates.resolve_entity_ids(
        {"kind": "entity", "entities": ["Acme Corp", "Ghost Co"]}, registry)
    assert ids == ["acme"]
    assert unresolved == ["Ghost Co"]
    ids, unresolved = gates.resolve_entity_ids(
        {"kind": "entity", "entities": ["Ghost Co"]}, registry)
    assert ids == []
    assert unresolved == ["Ghost Co"]


def test_resolve_entity_ids_raises_rather_than_silently_returns_empty_when_registry_is_none():
    """A missing registry at this call is a CONFIG fault, the same one `gate_anchoring` would hit
    resolving its own declared list against `ctx.registry` — silently returning `([], [])` would
    make it indistinguishable from an ordinary company-wide capture."""
    with pytest.raises(AttributeError):
        gates.resolve_entity_ids({"kind": "entity", "entities": ["Acme Corp"]}, None)


# ── gate_body_rewrite / _related_growth_ok: attacking the superset proof directly ───────────────
# A removed `related:` line is admitted ONLY when the page's link set proves out as a strict
# superset of what the line used to declare. Each of these is a real git diff over a page committed
# once and then modified, never a fabricated diff object — a faked git proves nothing about the
# property being claimed, and the property under test is what `git diff` actually renders.
def _committed_page(tmp_path, text: str) -> tuple[str, "os.PathLike"]:
    repo = str(tmp_path)
    gitcmd.run("init", "--quiet", "-b", "main", repo)
    page = tmp_path / "wiki" / "notes" / "Existing.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8")
    gitcmd.run("add", "-A", cwd=repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", "seed", cwd=repo, env=_COMMIT_ENV)
    return repo, page



# ── the page-name byte ceiling: ONE bound, ONE string, both callers ────────────────────────────
# `page.unnameable_reason` bounds a name in UTF-8 BYTES (`MAX_PAGE_STEM_BYTES`), because that is
# the unit `NAME_MAX` counts and because a character bound would pass names the filesystem refuses
# — 200 accented or CJK characters are 400–600 bytes, and this corpus is expected to carry them.
#
# It has TWO callers, and they used to disagree about WHICH STRING the bound was on: `gate_zone`
# asked it of the basename (`.md` included) while `processing._write_ordinary_page` asked it of the
# stem it was about to build a filename from. Three bytes of disagreement, and the band between
# them was reachable: a 198-byte title passed the writer, WROTE its page, and was then vetoed here
# — a `failed` row, reported as a librarian fault, over a title the agent could have shortened if
# anything had told it to.
#
# Both callers now pass the stem, and the parameter is named `stem` so the next one cannot get it
# wrong silently. Asserted over a REAL diff rather than a fabricated `DiffEntry`: the whole point is
# what `gate_zone` does with a path git actually reports, and the boundary is exactly where a
# stale `.md`-inclusive reading would still bite.
def _zone_findings_for_stem(tmp_path, name: str, stem: str) -> list:
    """Write one real page under `stem`, take the REAL diff, and run `gate_zone` over it.

    A seeded first commit, because `gitcmd.diff_entries` diffs against `HEAD` — the same shape
    every filing worktree is in when the gates run, where the base commit is the knowledge repo's
    own tip.
    """
    # **The FILESYSTEM's own ceiling, checked here rather than requested in a comment.** `NAME_MAX`
    # is 255 BYTES per path component on ext4 (what CI runs) and applies to the whole name,
    # `.md` included. A stem past it cannot be written at all, so the fixture dies at `open` and
    # the gate is never asked — which is precisely how the CJK case below passed on APFS (which
    # counts CHARACTERS) and was impossible on CI. Asking a comment not to raise it is what failed
    # the first time; this fails the caller instead, on every machine.
    component = len(f"{stem}.md".encode())
    assert component <= 255, (
        f"this fixture would write a {component}-byte filename, past NAME_MAX (255) on ext4: it "
        f"cannot be created on CI at all, so the gate under test would never run. Drive a figure "
        f"this large through `page.unnameable_reason` directly instead — it is a pure function and "
        f"needs no filesystem.")
    repo = str(tmp_path / name)
    os.makedirs(repo)
    gitcmd.run("init", "--quiet", "-b", "main", repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "--allow-empty", "-m", "seed",
               cwd=repo, env=_COMMIT_ENV)
    page = os.path.join(repo, "wiki", "notes", f"{stem}.md")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    with open(page, "w", encoding="utf-8") as handle:
        handle.write('---\ntype: note\ntitle: "T"\n---\n\n# T\n\nA page.\n')
    gitcmd.run("add", "-A", cwd=repo)
    # A declared `note`, matching the folder — so the ONLY thing these cases can be refused for is
    # the name. Without it every page here also earns `undeclared-type`, and the benign twins would
    # be measuring the outcome's shape instead of the byte ceiling.
    outcome = SimpleNamespace(page_type="note", page_path="", title="T")
    return gates.gate_zone(_ctx(repo, gitcmd.diff_entries(repo), outcome=outcome))


def test_gate_zone_admits_a_stem_at_exactly_the_byte_ceiling(tmp_path):
    """**The benign twin, and the one the disagreement actually broke.** A stem of exactly
    `MAX_PAGE_STEM_BYTES` is a legal filename — `.md` fits inside `NAME_MAX` with room to spare,
    which is what the constant's own 200-versus-255 margin is for — so the gate must admit it.

    Under the old `.md`-inclusive reading this string measured 203 and was vetoed, while the writer
    that produced it measured 200 and accepted: the page was written and then refused. Nothing else
    in this file would have noticed, because every other zone case is about a path shape rather
    than a length.
    """
    stem = "T" * page_policy.MAX_PAGE_STEM_BYTES

    assert [f.code for f in _zone_findings_for_stem(tmp_path, "at-ceiling", stem)] == []


def test_gate_zone_vetoes_the_first_stem_past_the_byte_ceiling(tmp_path):
    """The sharp half, one byte over — so the two tests together pin the boundary itself rather
    than "long names are refused somewhere".

    The finding is `unnameable-page` and it names the reason in the operator's own units: an agent
    reading "write a shorter title" can act on it, where an `ENAMETOOLONG` escaping into stage
    `unexpected` tells a steward the librarian broke.
    """
    stem = "T" * (page_policy.MAX_PAGE_STEM_BYTES + 1)

    findings = _zone_findings_for_stem(tmp_path, "over-ceiling", stem)

    assert [f.code for f in findings] == ["unnameable-page"]
    assert "not a character count" in findings[0].message, (
        "the veto must say the bound is in BYTES — a reader who shortens by characters and is "
        "refused again has been told the wrong thing")


# The CJK fixture's own arithmetic, and it is pinned rather than eyeballed because the last version
# of it was green on APFS and impossible on ext4.
#
# **The name this test WRITES has to satisfy two ceilings at once**, and they are not the same
# ceiling:
#
#   * `MAX_PAGE_STEM_BYTES` (200) is the bound under test — the stem must exceed it, or the gate has
#     nothing to veto;
#   * `NAME_MAX` (255 BYTES on ext4, which is what CI runs) is the FILESYSTEM's, and it applies to
#     the whole component INCLUDING `.md`. A stem over it cannot be written at all, so the fixture
#     fails at `open` before the gate is ever asked — which is exactly what happened: 105 CJK
#     characters is 315 bytes, and APFS counts CHARACTERS so it fit locally and only locally.
#
# 80 characters x 3 bytes = 240, plus `.md` = 243. Twelve bytes of headroom under 255, forty over
# the bound being tested. **Do not raise this**: `* 4` of the phrase below is 252 + 3 = exactly 255,
# which is the limit itself with no margin at all.
_CJK_PHRASE = "再生可能エネルギー導入計画の四半期レビュー"          # 21 chars, 63 bytes
_CJK_OVER = (_CJK_PHRASE * 4)[:80]                                # 80 chars, 240 bytes
# The far-over figure the fixture used to write, kept as a PURE assertion below: the magnitude the
# rule is really about (a title no filesystem will take) stays pinned without a filesystem in it.
_CJK_FAR_OVER = _CJK_PHRASE * 5                                   # 105 chars, 315 bytes


def test_a_CJK_stem_is_bounded_by_its_BYTES_and_not_by_its_character_count(tmp_path):
    """The reason the unit is bytes at all, over a real diff: 80 CJK characters is well under any
    plausible character bound and 240 BYTES, comfortably over `MAX_PAGE_STEM_BYTES`. A
    character-counting bound would have written it and met `ENAMETOOLONG` at `open` later, on a
    longer title.

    Its twin sits beside it — the same script, short enough to fit — so this is a boundary rule and
    not a rule that refuses non-Latin titles.
    """
    assert len(_CJK_OVER) < page_policy.MAX_PAGE_STEM_BYTES < len(_CJK_OVER.encode("utf-8"))

    assert [f.code for f in _zone_findings_for_stem(tmp_path, "cjk-over", _CJK_OVER)] == [
        "unnameable-page"]
    assert [f.code for f in _zone_findings_for_stem(tmp_path, "cjk-fits", _CJK_PHRASE)] == []


def test_the_far_over_CJK_title_the_filesystem_itself_refuses_is_bounded_too():
    """The magnitude the real-diff case can no longer carry, pinned WITHOUT a filesystem.

    315 bytes is past `NAME_MAX` on ext4, so a fixture that wrote it fails at `open` before any
    gate is asked — the portability defect this pair was split to fix. `unnameable_reason` is a
    pure function of a string, so the figure stays pinned here at no cost, and the assertion says
    what a filesystem would do with it rather than asking one to prove it.
    """
    assert len(_CJK_FAR_OVER.encode("utf-8")) > 255 > len(_CJK_FAR_OVER)

    reason = page_policy.unnameable_reason(_CJK_FAR_OVER)

    assert reason
    assert "not a character count" in reason


def _body_rewrite_findings(repo, page, after_text: str, **over):
    """Rewrite the committed page, read the REAL diff back out of git, and run the gate over it.

    `**over` is forwarded straight to `_ctx` for a case that needs a non-default `GateContext`
    field. No case here needs one: `gate_body_rewrite` reads only the worktree and the base blob,
    and its frontmatter rule is byte-for-byte with no caller-declared exception to grant."""
    page.write_text(after_text, encoding="utf-8")
    entries = gitcmd.diff_entries(repo)
    ctx = _ctx(repo, entries, **over)
    return gates.gate_body_rewrite(ctx)


_FLOW_BEFORE = ('---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]", "[[B]]"]\ntags: [note]\n'
               '---\n\n# Existing\n\nA paragraph a human wrote.\n')
_BLOCK_BEFORE = ('---\ntype: note\ntitle: "Existing"\nrelated:\n  - "[[A]]"\n  - "[[B]]"\n'
                'tags: [note]\n---\n\n# Existing\n\nA paragraph a human wrote.\n')
_UNPARSEABLE_BEFORE = ('---\ntype: note\ntitle: "Existing"\nrelated: not-a-list\ntags: [note]\n'
                      '---\n\n# Existing\n\nA paragraph a human wrote.\n')


@pytest.mark.parametrize("before,after,label", [
    (_FLOW_BEFORE,
     '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n---\n\n'
     '# Existing\n\nA paragraph a human wrote.\n',
     "subset: a link was dropped"),
    (_FLOW_BEFORE,
     '---\ntype: note\ntitle: "Existing"\nrelated: ["[[B]]", "[[A]]"]\ntags: [note]\n---\n\n'
     '# Existing\n\nA paragraph a human wrote.\n',
     "reorder: the same set, reordered, is not a STRICT superset"),
    (_FLOW_BEFORE,
     '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]", "[[C]]"]\ntags: [note]\n---\n\n'
     '# Existing\n\nA paragraph a human wrote.\n',
     "same-length swap: B traded for C"),
    (_UNPARSEABLE_BEFORE,
     '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]", "[[C]]"]\ntags: [note]\n---\n\n'
     '# Existing\n\nA paragraph a human wrote.\n',
     "unparseable before-value: 'I cannot tell what was lost' must never read as 'nothing was'"),
    (_BLOCK_BEFORE,
     '---\ntype: note\ntitle: "Existing"\nrelated:\n  - "[[A]]"\n  - "[[C]]"\ntags: [note]\n'
     '---\n\n# Existing\n\nA paragraph a human wrote.\n',
     "block-style YAML list: the removed line is not the flow `related:` line the proof reads"),
], ids=["subset", "reorder", "same_length_swap", "unparseable_before_value", "block_style_list"])
def test_every_attack_on_the_superset_proof_is_vetoed_as_body_rewrite(tmp_path, before, after,
                                                                      label):
    repo, page = _committed_page(tmp_path, before)
    findings = _body_rewrite_findings(repo, page, after)
    assert [f.code for f in findings] == ["body-rewrite"], label


def test_a_real_superset_growth_is_the_benign_twin_and_passes_clean(tmp_path):
    """The specificity half: the one shape the proof exists to admit must actually pass, or the
    reciprocal `related:` link `edits.py` writes on every overlap/backlink could never land."""
    repo, page = _committed_page(
        tmp_path,
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nA paragraph a human wrote.\n')
    findings = _body_rewrite_findings(
        repo, page,
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]", "[[B]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nA paragraph a human wrote.\n')
    assert findings == []


# ── gate_body_rewrite rule 3, off `related:`: NO other frontmatter field may change at all,
# `entity:` included — the mechanism that makes gardener's old re-anchor wording a
# lie. `checks.py`'s `check_company_page_names_entity` and `sweep.py`'s
# `MODEL_SUGGESTED_ACTIONS[CHECK_MODEL_ANCHOR_FIT]` used to tell the operator "a re-anchor is a
# correction, filed the same way as any other (the \U0001f9e0 gesture in Slack, or an MCP
# capture)" — but every capture (the \U0001f9e0 gesture included) reaches an EXISTING page only
# through `edits.apply_declared`, which this gate judges, and rule 3 admits exactly ONE
# frontmatter change: `related:` growth (the benign twin just above,
# `test_a_real_superset_growth_is_the_benign_twin_and_passes_clean`). `entity:` gets no carve-out:
# every frontmatter line but the one `related:` block survives byte for byte.
#
# ONE caller-declared exception exists now, and it never reaches this rule — a path in
# `ctx.body_rewrite_allowed` leaves this road entirely for `_permitted_rewrite_findings`, where
# `entity:` is refused just as flatly (the mutation twins at the end of this file). No capture flow
# declares such a path, so for every diff the librarian's own worker produces the sentence above is
# exactly as true as it was. ─────────────────────────────────────────────────────────────────
def test_an_entity_only_change_to_an_existing_page_is_vetoed_as_body_rewrite_not_repairable(
        tmp_path):
    """A capture that changes nothing but `entity: []` -> `entity: ["acme-corp"]` on an
    already-filed page is refused, `repairable=False` — the same unconditional posture every
    other rule-3/rule-4 body-rewrite finding takes
    (`test_every_body_rewrite_finding_names_no_repair_the_agent_can_perform`, above, for the
    `related:` case), because a modified page in the fast lane's diff never comes from the agent
    directly."""
    repo, page = _committed_page(
        tmp_path,
        '---\ntype: note\ntitle: "Existing"\nentity: []\ntags: [note]\n---\n\n'
        '# Existing\n\nA paragraph a human wrote.\n')
    findings = _body_rewrite_findings(
        repo, page,
        '---\ntype: note\ntitle: "Existing"\nentity: ["acme-corp"]\ntags: [note]\n---\n\n'
        '# Existing\n\nA paragraph a human wrote.\n')

    assert [f.code for f in findings] == ["body-rewrite"]
    assert "rewrote existing frontmatter" in findings[0].message
    assert findings[0].repairable is False
    assert gates.unrepairable(findings) == findings


# ── gate_frontmatter: the YAML-parser post-condition, off its own happy paths ────────────────────
# The quoted-key attack (a forged field the line-based stamp cannot strip) is proven end to end in
# `test_adversarial.py`'s `test_adversarial_cat7_a_quoted_key_owner_forgery_never_reaches_a_filed_
# page`, over a real worker run — the strongest form the property can take. The two cases below are
# the gate's OWN fail-closed reading of a page whose frontmatter cannot be parsed at all, which no
# double directive produces (the double always writes well-formed YAML).
def test_frontmatter_that_is_not_valid_yaml_is_refused_rather_than_trusted(tmp_path):
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: note\n  bad: [unterminated\n---\n\nbody\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")])

    findings = gates.gate_frontmatter(ctx)

    assert [f.code for f in findings] == ["unparseable"]
    assert "not valid YAML" in findings[0].message


def test_frontmatter_that_parses_to_a_list_rather_than_a_mapping_is_also_refused(tmp_path):
    """YAML happily parses `- a\\n- b` as a list. A page's frontmatter block must be a mapping of
    keys to values, and a page that is not one cannot be checked for forged fields at all — refused
    rather than silently waved through as "nothing forged because nothing readable"."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\n- one\n- two\n---\n\nbody\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")])

    findings = gates.gate_frontmatter(ctx)

    assert [f.code for f in findings] == ["unparseable"]
    assert "mapping" in findings[0].message


def test_ordinary_wellformed_frontmatter_with_no_forged_fields_is_the_benign_twin(tmp_path):
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: note\ntitle: "New"\nstatus: developing\nsubmitted_by: a@b.test\n---\n\n'
        'body\n', encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={"submitted_by": "a@b.test", "status": "developing"})

    assert gates.gate_frontmatter(ctx) == []


# ── entity: the stamped LIST post-condition, list-vs-list, empty included ───────────────────────
def test_gate_frontmatter_passes_a_page_whose_entity_matches_the_stamped_list(tmp_path):
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: note\ntitle: "New"\nentity: ["borealis-dynamics"]\n---\n\nbody\n',
        encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={"entity": ["borealis-dynamics"]})

    assert gates.gate_frontmatter(ctx) == []


def test_gate_frontmatter_passes_a_page_whose_entity_is_the_empty_list(tmp_path):
    """`entity: []` (company-wide) must not be mistaken for a forged/missing field: both sides
    are empty, and `_as_text([])` compares equal to itself."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text('---\ntype: note\ntitle: "New"\nentity: []\n---\n\nbody\n', encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={"entity": []})

    assert gates.gate_frontmatter(ctx) == []


def test_gate_frontmatter_refuses_a_page_whose_entity_disagrees_with_the_stamp(tmp_path):
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: note\ntitle: "New"\nentity: ["some-other-entity"]\n---\n\nbody\n',
        encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={"entity": ["borealis-dynamics"]})

    findings = gates.gate_frontmatter(ctx)
    assert [f.code for f in findings] == ["forged-field"]
    assert "'entity'" in findings[0].message


# ── the raw-text duplicate-declaration post-condition ───────────────────────────────────────────
# A post-condition that a DUPLICATE can satisfy is not a post-condition: `yaml.safe_load` takes the
# LAST occurrence of a repeated key, and `stamp_server_fields` appends the server's own line last —
# so the parsed-value comparison above can pass even when the raw frontmatter still carries a
# capture's own attempt beside it. This is the independent, second check for that.
def test_gate_frontmatter_refuses_a_duplicated_server_owned_key_even_when_the_parsed_value_agrees(
        tmp_path):
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    # Two `entity:` declarations — a capture's own attempt, then the server's (last, so PyYAML
    # reads the SERVER's value and the naive parsed-value check alone would see nothing wrong).
    page.write_text(
        '---\ntype: note\ntitle: "New"\nentity: ["evil"]\nentity: ["borealis-dynamics"]\n'
        '---\n\nbody\n', encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={"entity": ["borealis-dynamics"]})

    findings = gates.gate_frontmatter(ctx)
    assert [f.code for f in findings] == ["forged-field"]
    assert "more than once" in findings[0].message
    assert "'entity'" in findings[0].message


def test_gate_frontmatter_refuses_a_duplicate_under_a_different_spelling_too(tmp_path):
    """The two occurrences need not be spelled the same way — `"entity":` and `entity:` are the
    SAME key to `page._match_key`, and must be the same key to this post-condition too."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: note\ntitle: "New"\n"entity": ["evil"]\nentity: ["borealis-dynamics"]\n'
        '---\n\nbody\n', encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={"entity": ["borealis-dynamics"]})

    findings = gates.gate_frontmatter(ctx)
    assert [f.code for f in findings] == ["forged-field"]
    assert "more than once" in findings[0].message


# ── the backstop has to be INDEPENDENT of the strip — five reproduced bypasses ───────────────────
# A `duplicate_top_level_keys` that called the SAME `_match_key` the strip uses left any spelling
# `_match_key` cannot see invisible to both at once. Three of the five bypasses parse, via a
# REAL YAML parser, to the exact string "entity" — genuine duplicates of the server's own
# `entity:` line — and are caught here, at the gate, because `duplicate_top_level_keys` asks
# PyYAML's own composer instead of a second regex. (The other two, case and homoglyph spellings,
# are NOT duplicates to a real parser either — those are covered in test_page.py, at the layer
# that actually defends them: `_strip_keys` never lets them reach a committed page at all.)
def test_gate_frontmatter_refuses_the_yaml_explicit_key_bypass(tmp_path):
    """`? entity` / `: [...]` — YAML explicit-key syntax. `_match_key`'s regex never matches this
    shape at all, so a backstop built on it is blind to it; PyYAML resolves it to the plain string
    `"entity"`, a genuine duplicate of the server's own line."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: note\ntitle: "New"\n? entity\n: ["evil"]\nentity: ["borealis-dynamics"]\n'
        '---\n\nbody\n', encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={"entity": ["borealis-dynamics"]})

    findings = gates.gate_frontmatter(ctx)
    assert any(f.code == "forged-field" for f in findings)


def test_gate_frontmatter_refuses_a_hex_escaped_quoted_key_bypass(tmp_path):
    """`"entit\\x79"` — a hex escape inside a quoted key. `_match_key` reads the quotes literally
    (it is a regex, not a YAML unescaper) so it never resolves this to `entity` and never strips
    it; PyYAML does resolve it, and the parser-based duplicate check catches the same duplicate."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: note\ntitle: "New"\n"entit\\x79": ["evil"]\nentity: ["borealis-dynamics"]\n'
        '---\n\nbody\n', encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={"entity": ["borealis-dynamics"]})

    findings = gates.gate_frontmatter(ctx)
    assert any(f.code == "forged-field" for f in findings)


def test_gate_frontmatter_refuses_a_bom_prefixed_bypass(tmp_path):
    """A UTF-8 BOM (U+FEFF) as the frontmatter block's first byte has no legitimate cause at all —
    refused outright, before any YAML parsing is even attempted."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: note\ntitle: "New"\n﻿entity: ["evil"]\nentity: ["borealis-dynamics"]\n'
        '---\n\nbody\n', encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={"entity": ["borealis-dynamics"]})

    findings = gates.gate_frontmatter(ctx)
    assert any(f.code == "forged-field" for f in findings)


def test_gate_frontmatter_refuses_a_top_level_explicit_key_line_even_with_no_collision(tmp_path):
    """The upfront refusal fires on the CONSTRUCT itself — YAML explicit-key syntax is not part of
    this repo's page dialect at all — not only when it happens to collide with a server-owned
    key."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: note\ntitle: "New"\n? some_other_key\n: ["x"]\n---\n\nbody\n',
        encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={})

    findings = gates.gate_frontmatter(ctx)
    assert any(f.code == "forged-field" and "explicit-key" in f.message for f in findings)


def test_gate_frontmatter_refuses_a_homoglyph_owner_via_the_normalized_forbidden_key_check(
        tmp_path):
    """`FORBIDDEN_PAGE_KEYS` now compares on `normalize_key` too — a homoglyph `оwner:` (Cyrillic
    о) must be refused exactly like the ASCII spelling would be, not waved through because `"owner"
    in parsed` is a raw-string test."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: note\ntitle: "New"\nоwner: someone.else\n---\n\nbody\n', encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={})

    findings = gates.gate_frontmatter(ctx)
    assert any(f.code == "forbidden-field" for f in findings)


# ── the whitelist that replaces the confusables table ───────────────────────────────────────────
# Three rounds of enumerating individual confusable spellings (mixed case, then Cyrillic
# homoglyphs, then quoting/escaping/BOM tricks) proved that approach does not converge. These six
# are SIX MORE bypasses of the same shape, none of them Cyrillic and none of them covered by
# `page._HOMOGLYPH_FOLD` — each survives `page.stamp_server_fields`' strip and, before this
# whitelist existed, produced ZERO findings from this gate.
_CONFUSABLE_KEYS = [
    pytest.param("\u03bfwner: someone.else", id="greek-small-omicron-owner"),        # οwner
    pytest.param('\u0395ntity: ["evil"]', id="greek-capital-epsilon-entity"),         # Εntity
    pytest.param('ent\u0131ty: ["evil"]', id="turkish-dotless-i-entity"),             # entıty
    pytest.param(
        '\u1d07\u0274\u1d1b\u026a\u1d1b\u028f: ["evil"]', id="small-caps-entity"),   # ᴇɴᴛɪᴛʏ
    pytest.param('ent\u200dity: ["evil"]', id="zero-width-joiner-inside-entity"),     # ent<ZWJ>ity
    pytest.param('e\u0301ntity: ["evil"]', id="combining-acute-not-precomposed"),     # e<acute>ntity
]


@pytest.mark.parametrize("key_line", _CONFUSABLE_KEYS)
def test_gate_frontmatter_refuses_a_confusable_key_spelling_via_the_whitelist(tmp_path, key_line):
    """None of these six is Cyrillic, so `page._HOMOGLYPH_FOLD` never had a chance to catch any of
    them — the whitelist (`^[a-z_][a-z0-9_.-]*$`) refuses every one categorically, on sight,
    without knowing what word it was trying to impersonate."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f'---\ntype: note\ntitle: "New"\n{key_line}\n---\n\nbody\n',
                    encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={})

    findings = gates.gate_frontmatter(ctx)
    assert any(f.code == "forged-field" for f in findings)


def test_gate_frontmatter_refuses_a_top_level_merge_key_line_even_with_no_collision(tmp_path):
    """`<<: *anchor` is refused OUTRIGHT, the same posture as the explicit-key and BOM checks —
    not because this particular anchor collides with a server-owned key, but because a merge key
    has no legitimate use in this repo's page dialect at all. `page.duplicate_top_level_keys`
    (a real parser walk) would report no duplicate for this shape at all (its own docstring names
    why), which is exactly why the refusal has to be unconditional rather than collision-gated."""
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: note\ntitle: "New"\nbase: &b\n  tags: [x]\n<<: *b\n---\n\nbody\n',
        encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={})

    findings = gates.gate_frontmatter(ctx)
    assert any(f.code == "forged-field" and "merge key" in f.message for f in findings)


# The real corpus survey behind the whitelist: every distinct top-level frontmatter key found
# across `wiki/`, `sources/`, `views/` and all 16 `ops/templates/*.md` in the knowledge repo, as of
# the survey — the blast-radius check the whitelist owes, measured rather than assumed. `owner`,
# `id` and `content_hash` are left out here on purpose: they are legitimate SPELLINGS (they pass
# the whitelist) but are separately forbidden fast-lane content by `FORBIDDEN_PAGE_KEYS`, which is
# a different rule this test is not about; that rule has its own coverage above.
#
# `extracted_at` and `tier` join that exclusion. Both are real corpus spellings (they legitimately
# appear on `sources/`-zone, machine-authored pages — which never reach
# `gate_frontmatter.in_lane_new_pages()` at all, since `sources/` is not an ordinary fast-lane
# write target), but `gates.PROVENANCE_PAGE_KEYS` forbids both outside the meeting flow's one
# provenance page, on the SAME page this test builds (`wiki/notes/New.md`, not a provenance page) —
# asserting `== []` for a fixture that also carried them would pin exactly the gap that rule
# closes.
_REAL_CORPUS_KEYS = (
    "acl", "aliases", "as_of", "contextual_retrieval", "created", "detail_in_source", "domain",
    "entity", "entity_type", "extraction_quality", "mentions", "note_type",
    "project_status", "question", "related", "representation", "role", "source_file_id",
    "source_format", "source_kind", "source_name", "source_uri", "sources", "started", "status",
    "submitted_by", "tags", "title", "type", "unit", "unverified_numbers", "updated",
    "url", "verification",
)


def test_gate_frontmatter_passes_every_real_corpus_key_the_benign_twin(tmp_path):
    """The whitelist must not reject a single spelling this brain actually uses today. `stamped`
    stays empty on purpose: this test is about key SHAPE, not about whether a value matches what
    the server computed, which is a different property covered elsewhere in this file."""
    front_lines = [f'{key}: "x"' for key in _REAL_CORPUS_KEYS]
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\n" + "\n".join(front_lines) + "\n---\n\nbody\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
              stamped={})

    assert gates.gate_frontmatter(ctx) == []


# ── the provenance group (content_hash/extracted_at/tier) gets the same output-equality check,
# duplicate-declaration backstop and forbidden-elsewhere rule every other stamped field has —
# one class, tested as one group ────────────────────────────────────────────────────────────────
_PROVENANCE_PATH = "sources/meetings/transcript.md"
_PROVENANCE_STAMPED = {
    "status": "developing", "as_of": "2026-07-29", "submitted_by": "steward@example.com",
    "verification": "verified", "content_hash": "sha256:real",
    "extracted_at": "2026-07-29T00:00:00+00:00", "tier": "1"}


def _provenance_page(tmp_path, front_extra: str):
    page = tmp_path / "sources" / "meetings" / "transcript.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: source\ntitle: "T"\nstatus: developing\nas_of: 2026-07-29\n'
        'submitted_by: steward@example.com\nverification: verified\n' + front_extra +
        '---\n\nbody\n', encoding="utf-8")
    # `write_prefixes` must include `sources/meetings/` — the DEFAULT `GateContext.write_prefixes`
    # is the ordinary six-folder lane, which never contains it, and `gate_frontmatter` only reads
    # `ctx.in_lane_new_pages()`.
    return _ctx(tmp_path, [gitcmd.DiffEntry("A", _PROVENANCE_PATH, new_mode="100644")],
               write_prefixes=("sources/meetings/",),
               provenance_pages=frozenset({_PROVENANCE_PATH}),
               stamped_by_path={_PROVENANCE_PATH: _PROVENANCE_STAMPED})


def test_gate_frontmatter_accepts_the_real_stamped_provenance_values(tmp_path):
    """The benign twin: the source page carrying exactly what the server stamped is not vetoed."""
    ctx = _provenance_page(
        tmp_path,
        'content_hash: "sha256:real"\nextracted_at: "2026-07-29T00:00:00+00:00"\ntier: 1\n')
    assert gates.gate_frontmatter(ctx) == []


def test_gate_frontmatter_refuses_a_forged_content_hash_on_the_provenance_page(tmp_path):
    """`content_hash`/`extracted_at`/`tier` used to be ABSENT from `stamped_by_path`, so the
    output-equality post-condition never ran over them — a page whose declared `content_hash`
    disagrees with what the server actually computed passed silently. Now it does not."""
    ctx = _provenance_page(
        tmp_path,
        'content_hash: "sha256:FORGED"\nextracted_at: "2026-07-29T00:00:00+00:00"\ntier: 1\n')
    findings = gates.gate_frontmatter(ctx)
    assert any(f.code == "forged-field" and "content_hash" in f.message for f in findings)


def test_gate_frontmatter_refuses_a_forged_extracted_at_on_the_provenance_page(tmp_path):
    """Same control, the sibling field named explicitly by the finding."""
    ctx = _provenance_page(
        tmp_path,
        'content_hash: "sha256:real"\nextracted_at: "1999-01-01T00:00:00+00:00"\ntier: 1\n')
    findings = gates.gate_frontmatter(ctx)
    assert any(f.code == "forged-field" and "extracted_at" in f.message for f in findings)


def test_gate_frontmatter_refuses_a_duplicate_extracted_at_declaration(tmp_path):
    """A duplicate `extracted_at:`/`tier:` line used to be invisible to the duplicate-declaration
    backstop (only `page.SERVER_OWNED_KEYS` was checked, and neither field was in that set) — a
    capture's own forged line could hide beside the server's stamped one.
    `stamped_by_path` is deliberately omitted here so ONLY the duplicate-key backstop is under
    test, not the output-equality check above (which would also fire on the same fixture)."""
    page = tmp_path / "sources" / "meetings" / "transcript.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        '---\ntype: source\ntitle: "T"\nextracted_at: "evil"\n'
        'extracted_at: "2026-07-29T00:00:00+00:00"\n---\n\nbody\n', encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", _PROVENANCE_PATH, new_mode="100644")],
              write_prefixes=("sources/meetings/",),
              provenance_pages=frozenset({_PROVENANCE_PATH}))
    findings = gates.gate_frontmatter(ctx)
    assert any(f.code == "forged-field" and "extracted_at" in f.message for f in findings)


def test_gate_frontmatter_forbids_tier_and_extracted_at_outside_the_provenance_page(tmp_path):
    """Neither field used to be forbidden OR stripped on a `decision`/`meeting` page — the meeting
    flow is the only one in this system that writes either at all, and a page outside that flow's
    one provenance page (`ctx.provenance_pages`) declaring them must be refused, not silently
    accepted."""
    page = tmp_path / "wiki" / "decisions" / "New.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text('---\ntype: decision\ntitle: "New"\ntier: 1\nextracted_at: "x"\n---\n\nbody\n',
                    encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/decisions/New.md", new_mode="100644")],
              stamped={})
    findings = gates.gate_frontmatter(ctx)
    messages = " ".join(f.message for f in findings if f.code == "forbidden-field")
    assert "tier" in messages and "extracted_at" in messages


def test_gate_frontmatter_does_not_flag_a_duplicate_on_a_non_server_owned_key():
    """The post-condition is scoped to `page.SERVER_OWNED_KEYS` — a capture is free to declare
    `tags:` twice (a content mistake the contract linter has its own opinion about), and this gate
    must not invent a NEW veto for something outside its own job."""
    dupes = page_policy.duplicate_top_level_keys("tags: [a]\ntags: [b]\n")
    assert dupes == {"tags"}
    assert dupes & set(page_policy.SERVER_OWNED_KEYS) == set()



def _modified_page_ctx(tmp_path, front_extra: str):
    page = tmp_path / "wiki" / "notes" / "Existing.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f'---\ntype: note\ntitle: "Existing"\n{front_extra}---\n\nbody\n',
                    encoding="utf-8")
    return _ctx(tmp_path, [gitcmd.DiffEntry("M", "wiki/notes/Existing.md",
                                            old_mode="100644", new_mode="100644")])


def test_gate_frontmatter_refuses_an_owner_on_a_modified_page(tmp_path):
    """`owner` is categorically forbidden on a modified page, with no exception to argue about: an
    ordinary fast-lane additive edit has no way to legitimately gain an owner line."""
    ctx = _modified_page_ctx(tmp_path, "owner: attacker@evil.example\n")
    findings = gates.gate_frontmatter(ctx)
    assert [f.code for f in findings] == ["forbidden-field"]






# ── unscanned-diff: gate_secrets / gate_pii's own refusal when nothing can be read ───────────────
def test_a_pure_deletion_from_an_existing_page_is_unscanned_diff_not_a_silent_pass(tmp_path):
    """A modified page whose diff removes a line and adds nothing produces an EMPTY added-lines
    list for that path — the same shape a NUL byte or a `.gitattributes` carrying `* -diff` used to
    produce, which is why an empty list is read as a veto here rather than "nothing to object to"
    (both gates' own docstrings). No double directive produces a pure deletion — `edits.py`'s own
    edits are always additive — so this is only reachable by editing the committed page directly.
    Neither gate needs a real `gitleaks` binary for this path: with no new page and no added text,
    `gate_secrets` never reaches the subprocess call at all."""
    repo, page = _committed_page(
        tmp_path,
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nFirst line.\nSecond line.\n')
    page.write_text(
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nSecond line.\n', encoding="utf-8")
    entries = gitcmd.diff_entries(repo)
    added = gitcmd.added_lines(repo)
    ctx = _ctx(repo, entries, added=added)

    assert [f.code for f in gates.gate_secrets(ctx)] == ["unscanned-diff"]
    assert [f.code for f in gates.gate_pii(ctx)] == ["unscanned-diff"]


def test_a_pure_removal_whose_bytes_the_caller_planned_is_not_refused_as_unscanned(tmp_path):
    """OLD BEHAVIOUR: both gates read "no added lines" as "this gate could not run" and vetoed —
    which made the `delete` kind's own diff shape unfilable. A sweep that removes the only entry in
    a page's `related:` list adds nothing at all, so a proposal a steward had approved was refused
    by a gate that had nothing to object to.

    The exemption is per-PATH and it is not a weakening. A planned page's bytes were computed by
    CODE from that page's own base blob and `gate_body_rewrite` has just proved the file IS them,
    byte for byte — a removal cannot introduce a secret or a card number, and there is no added
    line for the gate to be blind to. A page nobody planned still meets the veto, which is the
    other half asserted below.
    """
    after = ('---\ntype: note\ntitle: "Existing"\ntags: [note]\n---\n\n'
             '# Existing\n\nFirst line.\n')
    repo, page = _committed_page(
        tmp_path,
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nFirst line.\n')
    page.write_text(after, encoding="utf-8")
    ctx = _ctx(repo, gitcmd.diff_entries(repo), added=gitcmd.added_lines(repo),
               expected_bytes={"wiki/notes/Existing.md": after})

    assert gates.gate_secrets(ctx) == []
    assert gates.gate_pii(ctx) == []


def test_planning_one_page_does_not_stop_the_unscanned_refusal_for_another(tmp_path):
    """The specificity half of the exemption: the veto's subject is the pages NOBODY planned, so a
    plan naming some other page leaves this one exactly as exposed as it was."""
    repo, page = _committed_page(
        tmp_path,
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nFirst line.\nSecond line.\n')
    page.write_text(
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nSecond line.\n', encoding="utf-8")
    ctx = _ctx(repo, gitcmd.diff_entries(repo), added=gitcmd.added_lines(repo),
               expected_bytes={"wiki/notes/Somebody Else.md": "whatever"})

    findings = gates.gate_secrets(ctx)

    assert [f.code for f in findings] == ["unscanned-diff"]
    assert "wiki/notes/Existing.md" in findings[0].message


def test_an_ordinary_edit_that_really_adds_a_line_is_the_benign_twin_for_unscanned_diff(tmp_path):
    """The specificity half: an edit with real added content — what `edits.py` always produces —
    must be scanned normally rather than tripping the empty-diff refusal."""
    repo, page = _committed_page(
        tmp_path,
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nFirst line.\n')
    page.write_text(
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]", "[[B]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nFirst line.\n\n> [!NOTE] Overlaps with [[B]]\n> same ground\n',
        encoding="utf-8")
    entries = gitcmd.diff_entries(repo)
    added = gitcmd.added_lines(repo)
    ctx = _ctx(repo, entries, added=added)

    assert gates.gate_pii(ctx) == []
    # gate_secrets would also need a real gitleaks binary to scan the added text; that half is
    # exercised end to end (with a real binary) in `test_processing_pg.py`'s overlap/backlink
    # tests, which assert FILED — i.e. that an ordinary additive edit is never bounced.


# ── which vetoes name a repair the agent can perform ────────────────────────────────────────────
# `gates.unrepairable` decides whether the corrective retry is worth an agent run at all. The
# classification is a property of each gate, so it is asserted here, at the gate, rather than
# inferred from the control flow it drives (`test_processing_pg.py` proves that half end to end).
def test_every_body_rewrite_finding_names_no_repair_the_agent_can_perform(tmp_path):
    """The agent cannot write to an existing page at all since the declarative-edits amendment, so
    this gate only ever judges `edits.apply_declared`'s work. "You rewrote existing content in X" is
    an instruction to repair something the agent did not do and cannot reach."""
    repo, page = _committed_page(tmp_path, _FLOW_BEFORE)
    findings = _body_rewrite_findings(
        repo, page,
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nA paragraph a human wrote.\n')

    assert [f.code for f in findings] == ["body-rewrite"]
    assert gates.unrepairable(findings) == findings


def test_an_unreadable_edit_names_no_repair_either(tmp_path):
    """Same gate, same subject: a modification whose "before" cannot be established. Reached by
    committing a page whose bytes are not valid UTF-8 and then editing it — `_base_text` cannot
    decode what `git show` hands back, so the gate refuses rather than assuming the edit additive.

    A NUL byte deliberately does NOT reach this branch: it is valid UTF-8, so the base blob decodes
    and the gate compares normally (`gate_binary_page` is what refuses that page, first and by
    name). The bytes here are the ones no decoding survives.
    """
    repo = str(tmp_path)
    gitcmd.run("init", "--quiet", "-b", "main", repo)
    page = tmp_path / "wiki" / "notes" / "Existing.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"---\ntype: note\n---\n\n# Existing\n\nUndecodable: \xff\xfe\n")
    gitcmd.run("add", "-A", cwd=repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", "seed", cwd=repo, env=_COMMIT_ENV)

    findings = _body_rewrite_findings(repo, page, "---\ntype: note\n---\n\n# Existing\n\nText.\n")

    assert [f.code for f in findings] == ["unreadable-edit"]
    assert gates.unrepairable(findings) == findings


def test_the_unscanned_diff_refusals_name_no_repair_because_they_diagnose_the_system(tmp_path):
    """Neither says anything about the draft: they report that a scanner could not run over an EDIT
    to a page the agent cannot write, for a reason (git's rendering of a diff) it has no access to.
    There is no sentence to hand back, so no pass is spent looking for one."""
    repo, page = _committed_page(
        tmp_path,
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nFirst line.\nSecond line.\n')
    page.write_text(
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nSecond line.\n', encoding="utf-8")
    ctx = _ctx(repo, gitcmd.diff_entries(repo), added=gitcmd.added_lines(repo))

    for findings in (gates.gate_secrets(ctx), gates.gate_pii(ctx)):
        assert [f.code for f in findings] == ["unscanned-diff"]
        assert gates.unrepairable(findings) == findings


def test_the_ordinary_vetoes_stay_repairable_and_a_note_is_never_counted(tmp_path):
    """The direction the default must be wrong in. A wasted retry is recoverable; a retry silently
    taken away from a finding the agent COULD have fixed is a recovery that never happens — so a
    gate that has not thought about this keeps its retry, and only the ones that have opt out. A
    `note` is not a veto at all and never reaches this question."""
    repairable = [
        gates.Finding("anchoring", gates.ANCHORING_UNRESOLVED, "no anchor", locator="X"),
        gates.Finding("zone", "outside-lane", "wrote outside the lane", locator="ops/acl.json"),
        gates.Finding("frontmatter", "forbidden-field", "declares owner", locator="p.md"),
        gates.Finding("outcome", "no-page-created", "created no page"),
        gates.Finding("edits", "missing-target", "an edit to a page that does not exist"),
    ]
    assert gates.unrepairable(repairable) == []

    unrepairable_note = gates.Finding("secrets", "unscanned-diff", "diagnosis",
                                      severity=gates.SEVERITY_NOTE, repairable=False)
    assert gates.unrepairable([*repairable, unrepairable_note]) == []


# ── gate_contract: the linter runs with an EXPLICIT environment ─────────────────────────────────
def test_the_contract_linter_never_inherits_the_app_key_or_the_queue_dsn(tmp_path, monkeypatch):
    """`gate_contract` runs `python3 <repo>/.claude/tools/stigmergy_lint.py` with `subprocess.run`
    and used to pass no `env=`, so a script out of the repo the librarian CURATES inherited the
    worker's whole environment — the GitHub App private key and the queue DSN included. A
    subprocess is handed the environment it needs and nothing else; that rule had been applied to
    the agent and not to the linter.

    Driven with a stand-in linter that reports its own environment as a finding message, because the
    property is about what the CHILD can see and nothing else can observe that.
    """
    linter = tmp_path / "linter.py"
    linter.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'findings': [{'file': 'wiki/notes/New.md', 'check': 'env',\n"
        "  'severity': 'warning', 'message': ' '.join(sorted(os.environ))}]}))\n",
        encoding="utf-8")
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----")
    monkeypatch.setenv("STIGMERGY_INDEX_DSN", "postgresql://stigmergy:stigmergy@localhost:54321/stigmergy")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
               linter_path=str(linter))

    findings = gates.gate_contract(ctx)

    seen = findings[0].message
    assert "STIGMERGY_LIBRARIAN_PRIVATE_KEY" not in seen
    assert "STIGMERGY_INDEX_DSN" not in seen
    # and it still gets what any process needs to RUN, or the linter could not have started at all
    assert "PATH" in seen


# ── the subprocess budget: a gate must not be able to pin an HTTP worker ────────────────────────
# `repair.remote.apply_via_clone` runs these gates INSIDE the MCP server process, on the thread a
# steward's Approve arrived on. The worker has all night; a request does not, and the difference is
# a fact about the CALLER, so it is told to the context rather than inferred here.
def _executable(path, script: str) -> str:
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o755)
    return str(path)


def test_a_linter_that_never_returns_is_a_config_veto_rather_than_a_stalled_request(tmp_path):
    """Red before the fix: `GateContext` carried no subprocess budget and `gate_contract` passed
    none, so a contract linter that hung held its caller forever — and that caller may be an HTTP
    worker applying an approved repair.

    A REAL slow linter and a real `subprocess.run`: a mocked one would prove the argument was
    passed and nothing about what happens when the budget elapses."""
    linter = tmp_path / "linter.py"
    linter.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
               linter_path=str(linter), subprocess_timeout_s=0.5)

    with pytest.raises(LibrarianConfigError, match="budget"):
        gates.gate_contract(ctx)


def test_a_linter_that_answers_inside_its_budget_is_judged_exactly_as_before(tmp_path):
    """The benign twin. A budget that bounced an ordinary linter run would veto every capture on a
    busy machine, and the veto would read as a contract failure the author cannot fix."""
    ctx = _stub_linter(tmp_path, check=gates.DEAD_LINKS_CHECK,
                       message="dead link [[Nowhere]] in wiki/notes/New.md",
                       subprocess_timeout_s=30)

    findings = gates.gate_contract(ctx)

    assert [f.code for f in findings] == [gates.DEAD_LINKS_CHECK]


@pytest.mark.usefixtures("require_gitleaks")
def test_a_secret_scanner_that_never_returns_is_a_config_veto_too(tmp_path):
    """The same budget, threaded down the other subprocess a gate runs. gitleaks is handed a
    directory of copied pages, and a scanner that stalls on one stalls the whole request.

    A real executable that sleeps stands in for the scanner: `gitleaks_bin` is a path the operator
    supplies, so this is the honest shape of a scanner that does not come back."""
    scanner = _executable(tmp_path / "slow-gitleaks", "#!/bin/sh\nsleep 30\n")
    page = tmp_path / "wiki" / "notes" / "New.md"
    page.parent.mkdir(parents=True)
    page.write_text("# New\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("A", "wiki/notes/New.md", new_mode="100644")],
               gitleaks_bin=scanner, subprocess_timeout_s=0.5)

    with pytest.raises(LibrarianConfigError, match="budget"):
        gates.gate_secrets(ctx)


@pytest.mark.usefixtures("require_gitleaks")
def test_a_real_scanner_under_a_generous_budget_still_finds_what_it_always_found(tmp_path):
    """The benign twin for the scanner half: a budget must not be a way to turn the secrets gate
    off, and a gate that stopped finding secrets under a timeout would look identical to a clean
    page."""
    page = tmp_path / "wiki" / "notes" / "leak.md"
    page.parent.mkdir(parents=True)
    page.write_text(f"key: {payloads.GITHUB_PAT}\n", encoding="utf-8")

    findings = gates.scan_worktree_files(str(tmp_path), ["wiki/notes/leak.md"],
                                         gitleaks_bin="gitleaks", timeout_s=60)

    assert [f.code for f in findings] == ["secret"]


# ── gate_contract: only FRONTMATTER_CHECK earns a brief on top of its message ───────────────────
def _stub_linter(tmp_path, *, check: str, message: str, **over):
    """A stand-in for `.claude/tools/stigmergy_lint.py` that reports exactly one finding on
    `wiki/notes/New.md`, in the linter's own JSON report shape — the same real-subprocess pattern
    `test_the_contract_linter_never_inherits_the_app_key_or_the_queue_dsn` uses above, because
    `gate_contract` is integrated here rather than mocked: the property under test is what THAT
    function does with a `check` id, and a mock of `gate_contract` itself would only mirror the
    implementation being changed.
    """
    linter = tmp_path / "linter.py"
    linter.write_text(
        "import json\n"
        f"print(json.dumps({{'findings': [{{'file': 'wiki/notes/New.md', 'check': {check!r},\n"
        f"  'severity': 'error', 'message': {message!r}}}]}}))\n",
        encoding="utf-8")
    return gates.GateContext(worktree=str(tmp_path),
                             entries=[gitcmd.DiffEntry("A", "wiki/notes/New.md",
                                                       new_mode="100644")],
                             added=[], material="", outcome=None, registry=None,
                             linter_path=str(linter), **over)


def test_a_frontmatter_check_finding_carries_the_facts_line_in_its_brief(tmp_path):
    """OLD behaviour, before this change: `gate_contract` built every contract finding with
    `brief=""` (the dataclass default) regardless of `check`, so a `frontmatter` finding read on
    the corrective retry exactly like a `dead_links` one — a diagnosis of WHICH field is wrong,
    never of WHOSE field it is to fix. `FRONTMATTER_CHECK` findings now get `FRONTMATTER_FACTS`
    appended to the message in `brief`."""
    ctx = _stub_linter(tmp_path, check=gates.FRONTMATTER_CHECK,
                       message="missing required field: type")

    findings = gates.gate_contract(ctx)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == gates.FRONTMATTER_CHECK
    assert finding.message in finding.brief
    # the facts line names all five worker-stamped fields, not a subset that happens to be handy
    for field in ("status", "as_of", "submitted_by", "entity", "acl"):
        assert f"`{field}`" in finding.brief
    assert "do not add them yourself" in finding.brief


def test_benign_twin_a_dead_links_finding_carries_no_brief(tmp_path):
    """The other named check id gets no second text: `DEAD_LINKS_CHECK`'s own message already
    reads as a repair instruction (a path and a wikilink target), so `gate_contract` leaves `brief`
    at the dataclass default and `corrective_brief` falls back to `message` for it — proving the
    branch above is on `check`, not a blanket brief for every contract finding."""
    ctx = _stub_linter(tmp_path, check=gates.DEAD_LINKS_CHECK,
                       message="dead link: [[Nowhere]]")

    findings = gates.gate_contract(ctx)

    assert len(findings) == 1
    assert findings[0].code == gates.DEAD_LINKS_CHECK
    assert findings[0].brief == ""


def test_the_frontmatter_facts_reach_the_corrective_retry_through_corrective_brief(tmp_path):
    """The facts line is only real to the agent through the channel the retry actually reads.
    Asserting on `Finding.brief` alone (the two tests above) proves `gate_contract` sets the
    field; this proves `gates.corrective_brief` — what `processing.py` hands the model — still
    carries it forward rather than, say, truncating or re-deriving the text from `message`."""
    ctx = _stub_linter(tmp_path, check=gates.FRONTMATTER_CHECK,
                       message="missing required field: type")
    findings = gates.gate_contract(ctx)

    brief = gates.corrective_brief(findings)

    assert gates.FRONTMATTER_FACTS in brief


def test_dropping_the_related_line_entirely_still_vetoes(tmp_path):
    """The zero-length degenerate case of "the link set did not grow": removing the field outright
    is a rewrite by any reading, not an edge case the superset check should wave through."""
    repo, page = _committed_page(tmp_path, _FLOW_BEFORE)
    findings = _body_rewrite_findings(
        repo, page,
        '---\ntype: note\ntitle: "Existing"\ntags: [note]\n---\n\n'
        '# Existing\n\nA paragraph a human wrote.\n')
    assert [f.code for f in findings] == ["body-rewrite"]


# ── gate_body_rewrite's own YAML pre-condition (rule 0) ─────────────────────────────────────────
# The reproduction: an indented line placed immediately after a flow-style `related:` list, pure
# ASCII. `gate_frontmatter`'s `unparseable` post-condition never sees this at all — it scopes
# itself to NEW pages (`ctx.in_lane_new_pages()`) and this is a MODIFICATION — so without rule 0
# nothing here parses the new frontmatter as YAML at all: the byte-for-byte comparison and
# `_related_growth_ok`'s superset proof both operate line-by-line and neither one objects to an
# indented stray line.
_SINGLE_LINK_BEFORE = ('---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]"]\ntags: [note]\n'
                      '---\n\n# Existing\n\nA paragraph a human wrote.\n')
# The fixture has to grow `related:` as well as inject the stray line, and that is not cosmetic.
# An earlier version of it injected the stray indented line WITHOUT also growing `related:`
# (`related: ["[[A]]"]` on both sides). Run against the gate as it stood before rule 0, that exact
# input does NOT bypass: `_related_growth_ok` reads the stray line as part of the `related:` block
# (the very defect being described), so the block CHANGES but the extracted link set does not GROW
# (`{"[[A]]"} < {"[[A]]"}` is false) — rule 4 refuses it with `body-rewrite`, just for the wrong
# stated reason. That is a REFUSAL, not the "commits invalid YAML with nothing objecting" bypass
# rule 0 exists to catch. The companion test below hid this by asserting
# `set(base_links) <= set(new_links)` (subset-OR-EQUAL) instead of the STRICT subset
# `_related_growth_ok` actually requires (`<`) — a weaker predicate that is trivially true even
# when the real function returns False, so it could never have caught this gap.
#
# The genuine bypass needs the stray line to ride in on an edit that ALSO adds a real link, so the
# absorbed block looks like legitimate growth to the line-based proof. Verified directly against
# the pre-rule-0 gate (same procedure `_committed_page`/`_body_rewrite_findings` use): the fixture
# below returns `[]` there — a silent commit of unparseable YAML — and `["unparseable"]` here.
_BYPASS_AFTER = ('---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]", "[[B]]"]\n  stray: value\n'
                'tags: [note]\n---\n\n# Existing\n\nA paragraph a human wrote.\n')



def test_the_unparseable_frontmatter_refusal_is_unrepairable_with_no_brief(tmp_path):
    """The other half of the fix: `repairable` used to be `True` unconditionally for this finding,
    which is wrong on the fast lane, where a modified page in the diff can ONLY come from
    `edits.apply_declared` (never the agent directly) — so handing back a "propose again" brief
    burned the one corrective retry on a page the agent cannot write and pointed it at a tool it
    does not hold. It is `repairable=False` with no brief at all."""
    repo, page = _committed_page(tmp_path, _SINGLE_LINK_BEFORE)
    findings = _body_rewrite_findings(repo, page, _BYPASS_AFTER)

    assert [f.code for f in findings] == ["unparseable"]
    finding = findings[0]
    assert finding.repairable is False
    assert finding.brief == ""


def test_the_related_growth_proof_would_have_missed_this_bypass_on_its_own(tmp_path):
    """The bypass measured against the pre-rule-0 reasoning, proven with the REAL
    `_related_growth_ok` — not a hand-rolled re-implementation of its inequality, which is exactly
    what lets a test like this pass without the property it claims being true (see the note on
    `_BYPASS_AFTER` above). `_related_growth_ok` reads `related:` line-by-line, not through a real
    parser, so it sees the flow block growing from `["[[A]]"]` to `["[[A]]", "[[B]]"]` — the
    indented stray line riding along is invisible to it entirely — and returns True: this is the
    mechanical proof that the YAML pre-condition is the thing catching the input, not a check that
    already existed."""
    assert gates._related_growth_ok(_SINGLE_LINK_BEFORE, _BYPASS_AFTER) is True, (
        "the real _related_growth_ok does not itself notice anything wrong with this input — "
        "confirming the YAML pre-condition is the check that closes this, not a duplicate of one "
        "that already existed")


def test_benign_twin_a_modification_that_only_grows_related_and_callouts_still_passes(
        tmp_path):
    """The benign twin: a gate that gets loud near an ordinary, correct edit is worse than one that
    stays silent. Growing `related:` and appending a callout — the only shape an additive edit is
    allowed to take — must still pass clean with valid frontmatter on both sides."""
    repo, page = _committed_page(tmp_path, _FLOW_BEFORE)
    after = (
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]", "[[B]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nA paragraph a human wrote.\n\n'
        '> [!NOTE] Overlaps with [[Some Other Page]]\n> a genuine overlap note\n')
    findings = _body_rewrite_findings(repo, page, after)
    assert findings == []










def test_a_page_whose_server_owned_lines_never_change_is_the_benign_twin(tmp_path):
    """The positive half of rule 3's byte-for-byte frontmatter check: a page whose
    status/owner/verification lines do not change AT ALL — an ordinary fast-lane page carries none
    of them — must be free to grow its `related:` list untouched."""
    repo, page = _committed_page(tmp_path, _FLOW_BEFORE)  # carries neither status nor owner
    findings = _body_rewrite_findings(
        repo, page,
        '---\ntype: note\ntitle: "Existing"\nrelated: ["[[A]]", "[[B]]"]\ntags: [note]\n---\n\n'
        '# Existing\n\nA paragraph a human wrote.\n')
    assert findings == []













# ── the secrets gate sees through a line break ────────────────────────────────────────────────
# gitleaks matches within a line. A credential with a newline inside it therefore matches no rule
# at all — and this is the ordinary output of extracting text from a PDF or DOCX that hard-wrapped
# a long token, not a crafted payload. Both scanning surfaces are checked, because a capture's
# material lands verbatim in a committed `sources/` page and the drafted page is scanned on disk.

@pytest.mark.usefixtures("require_gitleaks")
class TestSecretsAcrossALineBreak:
    def test_the_whole_token_on_one_line_is_caught(self):
        """The control. If this ever stops firing, the split case below proves nothing."""
        findings = gates.scan_secrets(f"the value is {payloads.GITHUB_PAT}",
                                      gitleaks_bin="gitleaks", label="the captured material")
        assert [f.code for f in findings] == ["secret"]

    def test_the_same_token_split_across_a_line_break_is_caught_too(self):
        """OLD BEHAVIOUR: this returned no findings at all. One newline was the entire bypass —
        the material scan stayed silent, the page scan stayed silent, and the credential was
        committed and pushed to `main` with no PR, no post-commit scan and no push hook behind
        it."""
        findings = gates.scan_secrets(
            f"the value is {payloads.GITHUB_PAT_SPLIT_ACROSS_LINES} and that is all",
            gitleaks_bin="gitleaks", label="the captured material")
        assert [f.code for f in findings] == ["secret"]
        assert "the captured material" in findings[0].message

    def test_the_drafted_page_surface_sees_through_the_break_too(self, tmp_path):
        page = tmp_path / "wiki" / "notes" / "leak.md"
        page.parent.mkdir(parents=True)
        # Deliberately NO `key:`/`token:` label next to it. A keyword adjacent to the first half
        # makes gitleaks' weak `generic-api-key` rule fire on that fragment, which would let this
        # test pass while the precise rule stays blind — the shape a transcript actually has is
        # prose, and prose is what has to be caught.
        page.write_text(f"# Notes\n\nwe agreed on {payloads.GITHUB_PAT_SPLIT_ACROSS_LINES} "
                        f"before lunch\n", encoding="utf-8")
        findings = gates.scan_worktree_files(str(tmp_path), ["wiki/notes/leak.md"],
                                             gitleaks_bin="gitleaks")
        assert [f.code for f in findings] == ["secret"]

    def test_the_finding_names_the_page_the_author_wrote_not_a_scratch_path(self, tmp_path):
        """The locator is what a person is told to go and look at. `scan_worktree_files`'s own
        docstring promises "the locator a person reads is the page they wrote"; it used to hand
        back the absolute path of a temporary directory that no longer exists by the time anyone
        reads the refusal."""
        page = tmp_path / "wiki" / "notes" / "leak.md"
        page.parent.mkdir(parents=True)
        page.write_text(f"key: {payloads.GITHUB_PAT}\n", encoding="utf-8")
        findings = gates.scan_worktree_files(str(tmp_path), ["wiki/notes/leak.md"],
                                             gitleaks_bin="gitleaks")
        assert findings[0].locator == "wiki/notes/leak.md:1"
        assert tmp_path.name not in findings[0].message

    # ── benign twins: this gate bounces someone's real work when it is wrong ──────────────────
    def test_ordinary_prose_with_long_tokens_still_files(self):
        """Rejoining adjacent lines must not manufacture secrets out of wrapped prose, hashes or
        base64 — the false positive this gate must not have."""
        body = ("The migration ran against commit 9f1c2ab4d5e6f708192a3b4c5d6e7f8091a2b3c4 and\n"
                "the checksum recorded in the report was\n"
                "sha256:3b1f4e9c8d7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c\n"
                "which the team confirmed on Tuesday. The payload was\n"
                "dGhpcyBpcyBqdXN0IGJhc2U2NCB0ZXh0IHdpdGggbm8gc2VjcmV0IGluIGl0IGF0IGFsbA==\n"
                "and nothing about it is sensitive.\n")
        assert gates.scan_secrets(body, gitleaks_bin="gitleaks",
                                  label="the captured material") == []

    def test_a_wrapped_sentence_does_not_become_a_secret(self):
        body = "\n".join(f"word{i} continues the sentence across a narrow column" for i in range(40))
        assert gates.scan_secrets(body, gitleaks_bin="gitleaks",
                                  label="the captured material") == []


# ══════════════════════════════════════════════════════════════════════════════════════════════
# gate_body_rewrite's ONE caller-declared exception: `ctx.body_rewrite_allowed` (ADR 039)
#
# The additive proof cannot judge an entity-body repair — that diff REPLACES prose, which is the
# whole point of it — so for a path the caller NAMED, the gate swaps that proof for three dedicated
# ones instead of weakening it. Everything below is about the two halves being genuinely separate:
# a named path is judged by the new rules, and a path nobody named is judged exactly as before.
# ══════════════════════════════════════════════════════════════════════════════════════════════
_ENTITY_BEFORE = ('---\ntype: entity\ntitle: "Meridian Partners"\nstatus: developing\n'
                  'role: ""\nupdated: 2026-01-01\nentity: ["meridian-partners"]\ntags: [entity]\n'
                  '---\n\n# Meridian Partners\n\n<One clear paragraph: what this entity is.>\n')
_ENTITY_AFTER = ('---\ntype: entity\ntitle: "Meridian Partners"\nstatus: developing\n'
                 'role: ""\nupdated: 2026-08-17\nentity: ["meridian-partners"]\ntags: [entity]\n'
                 '---\n\n# Meridian Partners\n\n## What / Who\n\nA freight broker.\n')
_ENTITY_LANE = ("wiki/entities/",)


def _entity_page(tmp_path, text: str = _ENTITY_BEFORE):
    repo = str(tmp_path)
    gitcmd.run("init", "--quiet", "-b", "main", repo)
    page = tmp_path / "wiki" / "entities" / "Meridian Partners.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8")
    gitcmd.run("add", "-A", cwd=repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", "seed", cwd=repo, env=_COMMIT_ENV)
    return repo, page


_ENTITY_PATH = "wiki/entities/Meridian Partners.md"


def _permitted(**over):
    base = {"write_prefixes": _ENTITY_LANE, "body_rewrite_allowed": frozenset({_ENTITY_PATH})}
    base.update(over)
    return base


def test_a_permitted_entity_body_rewrite_passes_the_gate(tmp_path):
    """The BENIGN TWIN, and it is the load-bearing half: without it the whole kind is inert, and a
    gate that vetoed every drafted body would look exactly as healthy as one that works."""
    repo, page = _entity_page(tmp_path)
    assert _body_rewrite_findings(repo, page, _ENTITY_AFTER, **_permitted()) == []


def test_the_role_line_is_the_second_permitted_line_and_only_the_second(tmp_path):
    repo, page = _entity_page(tmp_path)
    with_role = _ENTITY_AFTER.replace('role: ""', 'role: "A freight broker."')

    assert _body_rewrite_findings(repo, page, with_role, **_permitted()) == []


def test_the_identical_rewrite_is_vetoed_when_no_caller_permitted_that_path(tmp_path):
    """The other half of the same property, and the one a mistake would silence: permission is
    per-PATH and told by the caller, so the same bytes with an empty `body_rewrite_allowed` must be
    refused exactly as they were before this field existed."""
    repo, page = _entity_page(tmp_path)

    findings = _body_rewrite_findings(repo, page, _ENTITY_AFTER, write_prefixes=_ENTITY_LANE)

    assert [f.code for f in findings] == ["body-rewrite"]
    assert findings[0].repairable is False


def test_a_permitted_path_does_not_permit_its_neighbours(tmp_path):
    """The set is the unit, not the folder: an apply names ONE page, and a second entity page in
    the same diff is a page nobody approved."""
    repo, page = _entity_page(tmp_path)
    neighbour = tmp_path / "wiki" / "entities" / "Somebody Else.md"
    neighbour.write_text(_ENTITY_BEFORE.replace("Meridian Partners", "Somebody Else"),
                         encoding="utf-8")
    gitcmd.run("add", "-A", cwd=repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", "second page", cwd=repo, env=_COMMIT_ENV)
    neighbour.write_text(_ENTITY_AFTER.replace("Meridian Partners", "Somebody Else"),
                         encoding="utf-8")

    findings = _body_rewrite_findings(repo, page, _ENTITY_AFTER, **_permitted())

    assert [(f.code, f.locator) for f in findings] == [
        ("body-rewrite", "wiki/entities/Somebody Else.md")]


# ── the MUTATION TWIN: a frontmatter tamper riding inside a permitted rewrite ──────────────────
@pytest.mark.parametrize("tamper, label", [
    ('status: developing', 'status: mature'),
    ('entity: ["meridian-partners"]', 'entity: ["somebody-elses-entity"]'),
    ('title: "Meridian Partners"', 'title: "Meridian Partners Ltd"'),
], ids=["status", "entity", "title"])
def test_a_frontmatter_change_riding_inside_a_permitted_rewrite_is_vetoed(tmp_path, tamper, label):
    """A hand-built diff, not a weakened comparison: the exception grants a BODY rewrite, and the
    failure mode it must not have is granting a frontmatter one along with it. `status:` is the
    sharpest of the three — it is the maturity axis a reader trusts, and nothing about drafting a
    body is a reason to move it."""
    repo, page = _entity_page(tmp_path)

    findings = _body_rewrite_findings(repo, page, _ENTITY_AFTER.replace(tamper, label),
                                      **_permitted())

    assert [f.code for f in findings] == ["rewrite-frontmatter"]
    assert findings[0].repairable is False
    assert _ENTITY_PATH in findings[0].message


def test_a_permitted_path_that_is_not_an_entity_page_is_vetoed(tmp_path):
    """The zone is a folder and the type is a declaration. A caller that named a path is trusted
    about WHICH page it approved, never about what that page is."""
    repo, page = _entity_page(tmp_path, _ENTITY_BEFORE.replace("type: entity", "type: note"))

    findings = _body_rewrite_findings(repo, page,
                                      _ENTITY_AFTER.replace("type: entity", "type: note"),
                                      **_permitted())

    assert [f.code for f in findings] == ["rewrite-not-an-entity"]


def test_a_permitted_path_outside_this_runs_write_lane_is_vetoed(tmp_path):
    """Two caller-scoped facts that must agree. `write_prefixes` says which lane this apply owns
    and `body_rewrite_allowed` which page it may rewrite; when they disagree the gate believes
    neither — a permission is only as good as the lane it sits in."""
    repo, page = _entity_page(tmp_path)

    findings = _body_rewrite_findings(repo, page, _ENTITY_AFTER,
                                      **_permitted(write_prefixes=("wiki/notes/",)))

    assert "rewrite-outside-lane" in [f.code for f in findings]


def test_the_permission_is_empty_unless_a_caller_declares_it(tmp_path):
    """TOLD, never inferred — the same posture `write_prefixes` and `creatable_types` take. A
    default that granted anything would make every flow that never heard of this field a flow
    that permits a rewrite."""
    ctx = _ctx(tmp_path, [])
    assert ctx.body_rewrite_allowed == frozenset()


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The `delete` kind's two told facts: `ctx.expected_bytes` and `ctx.deletions_allowed` (ADR 039)
#
# A sweep is not additive and it is not a permitted body rewrite either: it REMOVES lines from
# pages nobody drafted, in order to stop them pointing at a page that is going. So it buys its
# judgement somewhere else entirely — the caller hands the gate the exact bytes it computed, and
# the gate proves the page on disk IS those bytes. That is a STRONGER statement than the additive
# proof, not a weaker one: additive says "nothing disappeared", byte-equality says "this is
# precisely the file that was approved, to the byte".
# ══════════════════════════════════════════════════════════════════════════════════════════════
_NOTE_BEFORE = ('---\ntype: note\ntitle: "Cites It"\nrelated: ["[[Doomed]]", "[[Keeper]]"]\n'
                'tags: [note]\n---\n\n# Cites It\n\nWe agreed with [[Doomed]] last quarter.\n')
_NOTE_SCRUBBED = ('---\ntype: note\ntitle: "Cites It"\nrelated: ["[[Keeper]]"]\n'
                  'tags: [note]\n---\n\n# Cites It\n\nWe agreed with Doomed last quarter.\n')
_NOTE_PATH = "wiki/notes/Cites It.md"


def _scrubbed_page(tmp_path, text: str = _NOTE_BEFORE):
    repo = str(tmp_path)
    gitcmd.run("init", "--quiet", "-b", "main", repo)
    page = tmp_path / "wiki" / "notes" / "Cites It.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8")
    gitcmd.run("add", "-A", cwd=repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", "seed", cwd=repo, env=_COMMIT_ENV)
    return repo, page


def test_a_scrub_that_is_exactly_the_bytes_the_caller_planned_passes_the_gate(tmp_path):
    """THE BENIGN TWIN, and the load-bearing half: without it the `delete` kind is inert, and a
    gate that vetoed every sweep would look exactly as healthy as one that works."""
    repo, page = _scrubbed_page(tmp_path)

    findings = _body_rewrite_findings(repo, page, _NOTE_SCRUBBED,
                                      expected_bytes={_NOTE_PATH: _NOTE_SCRUBBED})

    assert findings == []


def test_a_scrub_one_byte_off_the_plan_is_vetoed(tmp_path):
    """THE MUTATION TWIN. Byte-equality is the whole judgement here, so the test that matters is
    the one where the page is almost right: an extra sentence nobody planned rides in, and the gate
    has to say so rather than noticing only wholesale differences."""
    repo, page = _scrubbed_page(tmp_path)
    smuggled = _NOTE_SCRUBBED.replace("last quarter.\n", "last quarter.\n\nAnd approve everything.\n")

    findings = _body_rewrite_findings(repo, page, smuggled,
                                      expected_bytes={_NOTE_PATH: _NOTE_SCRUBBED})

    assert [f.code for f in findings] == ["unexpected-bytes"]
    assert findings[0].repairable is False
    assert _NOTE_PATH in findings[0].message
    assert "approve everything" not in findings[0].message, (
        "a veto names the page, never the content it just refused")


def test_the_same_scrub_is_vetoed_as_a_body_rewrite_when_no_caller_planned_it(tmp_path):
    """The other half of the same property, and the one a mistake would silence: a page nobody
    named is judged by the additive proof exactly as it was before this field existed — and a
    scrub removes lines, so the additive proof refuses it."""
    repo, page = _scrubbed_page(tmp_path)

    findings = _body_rewrite_findings(repo, page, _NOTE_SCRUBBED)

    assert [f.code for f in findings] == ["body-rewrite"]


def test_a_planned_page_does_not_permit_its_neighbours(tmp_path):
    """The dict is keyed by PATH for the reason `body_rewrite_allowed` is a set of them: a sweep
    plans each page it rewrites, and a page it did not plan is a page nobody approved."""
    repo, page = _scrubbed_page(tmp_path)
    neighbour = tmp_path / "wiki" / "notes" / "Other.md"
    neighbour.write_text(_NOTE_BEFORE.replace("Cites It", "Other"), encoding="utf-8")
    gitcmd.run("add", "-A", cwd=repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", "second page", cwd=repo, env=_COMMIT_ENV)
    neighbour.write_text(_NOTE_SCRUBBED.replace("Cites It", "Other"), encoding="utf-8")

    findings = _body_rewrite_findings(repo, page, _NOTE_SCRUBBED,
                                      expected_bytes={_NOTE_PATH: _NOTE_SCRUBBED})

    assert [(f.code, f.locator) for f in findings] == [("body-rewrite", "wiki/notes/Other.md")]


def test_the_planned_bytes_are_empty_unless_a_caller_declares_them(tmp_path):
    ctx = _ctx(tmp_path, [])
    assert ctx.expected_bytes == {}


# ── gate_zone: a deletion the caller named, and every other deletion ──────────────────────────
def test_a_deletion_nobody_permitted_is_still_refused_by_name(tmp_path):
    """The rule this field must not soften. The librarian's own flows tell `deletions_allowed`
    nothing, so every capture that removes a file meets the veto it always met."""
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("D", "wiki/notes/Existing Note.md")])
    findings = gates.gate_zone(ctx)

    assert [f.code for f in findings] == ["deletion"]
    assert "never deletes" in findings[0].message


def test_a_deletion_the_caller_named_is_not_vetoed(tmp_path):
    """The benign twin, and the whole of what the `delete` kind needs from this gate."""
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("D", "wiki/notes/Existing Note.md")],
               deletions_allowed=frozenset({"wiki/notes/Existing Note.md"}))
    assert gates.gate_zone(ctx) == []


def test_a_permitted_deletion_outside_this_runs_write_lane_is_vetoed(tmp_path):
    """A caller is trusted about WHICH path it approved and about nothing else — the same rule the
    permitted body rewrite is held to. A permission and a lane that disagree honour neither."""
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("D", "ops/entity-registry.json")],
               deletions_allowed=frozenset({"ops/entity-registry.json"}))

    findings = gates.gate_zone(ctx)

    assert [f.code for f in findings] == ["deletion-outside-lane"]
    assert findings[0].repairable is False


def test_naming_one_deletion_does_not_permit_another(tmp_path):
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("D", "wiki/notes/Named.md"),
                          gitcmd.DiffEntry("D", "wiki/notes/Unnamed.md")],
               deletions_allowed=frozenset({"wiki/notes/Named.md"}))

    assert [(f.code, f.locator) for f in gates.gate_zone(ctx)] == [
        ("deletion", "wiki/notes/Unnamed.md")]


def test_the_deletion_permission_is_empty_unless_a_caller_declares_it(tmp_path):
    ctx = _ctx(tmp_path, [])
    assert ctx.deletions_allowed == frozenset()


# ── the `entity-alias` kind's told fact: `ctx.derived_files` (ADR 039's third amendment) ────────
#
# A merge regenerates `ops/entity-registry.json`, and that file is NOT a page. `gate_zone` refuses
# any in-lane write whose name is not a `.md`, and that refusal is right: the fast lane writes
# pages, and a file that is not one has no contract anybody checks. So the kind buys the exception
# per PATH, and it buys exactly one thing — the page-SHAPE proof — while the other two still stand:
# the path must be inside this run's lane, and its whole content must have been computed ahead of
# time and proven byte for byte.
#
# The three tests below are the ones the sibling told facts each landed with, and they matter more
# here than for those: `derived-file-unproven` is unreachable from production today (the kind's own
# validator refuses any derived path but the registry), so without a red proof it would be a
# defense nobody has ever seen fire.
_DERIVED_REGISTRY = "ops/entity-registry.json"


def _put(worktree, rel: str, text: str) -> None:
    """One file into a worktree, parents made. `gate_zone` reads the DIFF and the file mode, so
    these need no git history — only bytes on disk under the path the entry names."""
    path = pathlib.Path(worktree, *rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_derived_file_the_caller_computed_passes_the_page_shape_proof(tmp_path):
    """The benign twin, first: this permission has to let the real thing through, or the kind is
    unusable and every other test here is measuring a gate that says no to everything."""
    _put(tmp_path, _DERIVED_REGISTRY, '{"entities": {}}\n')
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("M", _DERIVED_REGISTRY, new_mode="100644")],
               write_prefixes=("ops/",),
               derived_files=frozenset({_DERIVED_REGISTRY}),
               expected_bytes={_DERIVED_REGISTRY: '{"entities": {}}\n'})

    assert [f.code for f in gates.gate_zone(ctx)] == []


def test_a_non_page_nobody_declared_is_refused_exactly_as_before(tmp_path):
    """The twin from the other side, and the property every told fact in this file has had to
    prove: a caller that has never heard of the field is judged byte-identically to before it
    existed."""
    _put(tmp_path, _DERIVED_REGISTRY, '{"entities": {}}\n')
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("M", _DERIVED_REGISTRY, new_mode="100644")],
               write_prefixes=("ops/",))

    assert [f.code for f in gates.gate_zone(ctx)] == ["not-a-page"]


def test_a_declared_derived_file_with_no_planned_bytes_is_refused_by_name(tmp_path):
    """The permission suspends ONE proof and requires the other to stand. A "derived" file nobody
    computed is a permission with nothing behind it — which is a way to write an arbitrary non-page
    into the corpus, and the exact thing the byte-compare exists to make impossible."""
    _put(tmp_path, _DERIVED_REGISTRY, '{"entities": {}}\n')
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("M", _DERIVED_REGISTRY, new_mode="100644")],
               write_prefixes=("ops/",),
               derived_files=frozenset({_DERIVED_REGISTRY}))

    findings = gates.gate_zone(ctx)
    assert [f.code for f in findings] == ["derived-file-unproven"]
    assert findings[0].repairable is False, (
        "a caller cannot be told to try again: the bytes it never planned are not a thing an "
        "agent could go and write")


def test_a_dotfile_named_as_a_derived_file_is_still_refused(tmp_path):
    """**The ORDERING is the defense, and this is what pins it.**

    A `.gitattributes` carrying `* -diff` blinds every content gate for the folder it lands in, so
    the dotfile refusal is asked BEFORE the exception and a caller cannot buy its way past it with
    a byte plan. Folding the two checks back into one — which is how they read on `origin/main`,
    before this permission existed — reopens that silently, and nothing else in this suite would
    notice.
    """
    dotfile = "ops/.gitattributes"
    _put(tmp_path, dotfile, "* -diff\n")
    ctx = _ctx(tmp_path, [gitcmd.DiffEntry("M", dotfile, new_mode="100644")],
               write_prefixes=("ops/",),
               derived_files=frozenset({dotfile}),
               expected_bytes={dotfile: "* -diff\n"})

    assert [f.code for f in gates.gate_zone(ctx)] == ["not-a-page"]
