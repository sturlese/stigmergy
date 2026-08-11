"""`processing.process_meeting_item` end to end, over a real Postgres queue, a real git repo +
bare remote, and the offline double's `run_meeting` — the meeting flow's whole contract, mirroring
`test_processing_pg.py`'s style: claim through `worker.process_next` (which dispatches by `kind`),
assert on the FILED PAGES read back from the repo's object database, never on what the double
merely drafted.
"""
import asyncio
import dataclasses
import json
import os
import pathlib

import pytest

from stigmergy.capture import dispositions, queue, schema
from stigmergy.librarian import gates, processing, worker
from stigmergy.librarian import page as page_policy
from stigmergy.librarian import report as report_module
from stigmergy.views import regenerate as views_regenerate
from tests import adversarial_payloads as payloads
from tests.librarian import support

_GLOBEX_PAGE = ('---\ntype: entity\ntitle: "Globex"\nstatus: developing\ncreated: 2026-07-01\n'
               'updated: 2026-07-01\ntags: [entity, organization]\nentity: [globex]\n'
               'related: []\nsources: []\n---\n\n# Globex\n\nA second, independently anchored '
               'entity — padded past the contract linter\'s thirty-line minimum, the same way the '
               'fixture Acme Corp page pads itself, so this page is real, lint-clean and anchored '
               'on its own, never a throwaway string with no bearing on the corpus it sits in. '
               'Nothing about this entity has anything to do with Acme, on purpose: the whole '
               'point of seeding it is to have something the meeting flow below never touches, so '
               'its own view is a control the post-meeting hook must leave alone.\n')


def _seed_globex(env, deps):
    """A second registered entity, with its own committed, anchored page, plus a view already
    regenerated once — the untouched control the sibling-view test needs. Mutates `deps.registry`
    in place (the same object `_file_meeting` reads `touched_ids` resolution and the view hook's
    `registry=deps.registry` argument from), mirroring `tests/views/test_regenerate.py`'s own
    pattern for a second entity."""
    import os

    deps.registry.entities["globex"] = {"name": "Globex", "type": "organization", "aliases": []}
    os.makedirs(os.path.join(env.repo, "wiki", "entities"), exist_ok=True)
    with open(os.path.join(env.repo, "wiki", "entities", "Globex.md"), "w") as f:
        f.write(_GLOBEX_PAGE)
    support.commit_and_push(env.repo, "chore: seed a second, independent entity (globex)")
    outcome = asyncio.run(views_regenerate.regenerate_entity(
        env.repo, "globex", registry=deps.registry))
    assert outcome.action == "written", "globex's own view failed to seed"
    return support.read_filed_page(env.bare, "main", "views/globex.md")


def _file_meeting(conn, deps, material, **kw):
    support.submit_meeting(conn, deps, material, **kw)
    return worker.process_next(conn, deps)


def _row(conn, submission_id):
    return queue.get_submission_trace(conn, submission_id)


def _with_diagnostics(base_deps, directory):
    """`Deps` whose refused diffs land in a per-test directory — `test_processing_pg.py`'s own
    helper of the same name, needed here because the preserved diff's `# refused by:` header is the
    only surface that names a refusal's `gate/code` (the submitter-facing report carries a sentence
    on purpose)."""
    return dataclasses.replace(
        base_deps,
        settings=dataclasses.replace(base_deps.settings, refused_diff_root=str(directory)))


def _refused_by(result) -> str:
    """The `gate/code` names the preserved refused diff was refused by, read off its own
    `# refused by:` header line (`processing.refused_diff_digest`)."""
    text = pathlib.Path(result.diagnostics_path).read_text(encoding="utf-8")
    header = next((row for row in text.splitlines() if row.startswith("# refused by:")), "")
    return header.split(":", 1)[1].strip() if header else ""


class _CountingMeetingAgent:
    """Wraps an agent and counts `run_meeting` calls — the direct measurement of how many agent
    passes a refusal spent, independent of the counter the report happens to carry. Everything else
    delegates, so this is a transparent wrapper rather than a stand-in for the double."""

    def __init__(self, inner):
        self.inner = inner
        self.meeting_calls = 0

    def run_meeting(self, **kwargs):
        self.meeting_calls += 1
        return self.inner.run_meeting(**kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


# ── dedup collapses same-KIND submissions only ──────────────────────────────────────────────────
def test_a_meeting_whose_material_matches_an_already_filed_ordinary_page_still_files_a_page_set(
        rig, clean_queue):
    """The dedup query used to match on `payload->>'sha256'` alone, ignoring `kind` — a meeting
    drop whose transcript digest happened to equal an already-filed ORDINARY capture's material
    would retry-collapse (level 1) or already-filed-reject (level 2) onto that single page's
    `result_ref`, producing a `filed`/`rejected` verdict for a meeting that never produced a page
    SET at all. Same raw material, byte for byte, submitted once as `kind="raw"` and once as
    `kind="meeting"`: the second must go through the genuine meeting flow, not collapse onto the
    first."""
    env, deps = rig
    material = "DOUBLE:decisions=1\nA transcript about the Acme renewal, worth keeping twice over."

    support.submit(clean_queue, deps, material)
    _, ordinary_result = worker.process_next(clean_queue, deps)
    assert ordinary_result.status == schema.FILED

    support.submit_meeting(clean_queue, deps, material)
    _, meeting_result = worker.process_next(clean_queue, deps)
    assert meeting_result.status == schema.FILED
    # The genuine meeting flow ran (a page SET), not a dedup collapse onto the ordinary page above
    # (which would report no `filed_meeting` key at all — `report.filed`'s shape, not
    # `report.filed_meeting`'s).
    assert "filed_meeting" in meeting_result.report
    assert meeting_result.result_ref != ordinary_result.result_ref


# ── the queue accepts kind="meeting" through the same fenced claim ──────────────────────────────
def test_a_meeting_row_is_claimed_and_processed_through_the_same_claim(rig, clean_queue):
    env, deps = rig
    item, result = _file_meeting(clean_queue, deps, "DOUBLE:decisions=1\nA short transcript.")
    assert item["kind"] == schema.MEETING
    assert result.status == schema.FILED


# ── one App-bot commit, one source page, one meeting page, >=2 decision pages ───────────────────
def test_two_resolvable_entities_file_atomically_with_the_meeting_1to1_link_contract(rig, clean_queue):
    env, deps = rig
    before = support.branch_sha(env.bare)
    item, result = _file_meeting(
        clean_queue, deps,
        "DOUBLE:decisions=2\nAlice and Bob discussed the Acme renewal and the pilot scope.")
    assert result.status == schema.FILED
    after = support.branch_sha(env.bare)
    assert after != before

    # The branch tip is not the meeting commit itself: the post-meeting hook pushes a second
    # commit (the view regeneration) after the meeting's own commit, so `after` names the VIEW
    # commit, not the meeting one. Diffing/reading the meeting's own commit must go through
    # `result.result_ref`'s sha — the sha `process_item` actually attributed to this meeting
    # filing — never the branch tip, which is a proxy for "whatever committed last".
    _, meeting_sha = result.result_ref.rsplit("@", 1)
    changed = support.changed_paths(env.repo, meeting_sha)
    source = [p for p in changed if p.startswith("sources/meetings/")]
    meeting = [p for p in changed if p.startswith("wiki/meetings/")]
    decisions = [p for p in changed if p.startswith("wiki/decisions/")]
    assert len(source) == 1
    assert len(meeting) == 1
    assert len(decisions) == 2

    subject = support.commit_subject(env.repo, meeting_sha)
    assert subject.startswith("feat(meeting):")

    row = _row(clean_queue, item["id"])
    assert row["status"] == schema.FILED
    assert row["result_ref"].startswith("wiki/meetings/")
    report = row["report"]
    assert set(report["filed_meeting"]["source_pages"]) == set(source)
    assert report["filed_meeting"]["meeting_page"] == meeting[0]
    assert {d["path"] for d in report["filed_meeting"]["decisions"]} == set(decisions)


# ── every decision page carries entity/as_of/status/server-stamped fields ───────────────────────
def test_decision_pages_carry_the_required_frontmatter(rig, clean_queue):
    env, deps = rig
    _, result = _file_meeting(clean_queue, deps,
                              "DOUBLE:decisions=1\nAcme wants a Q3 pricing floor.",
                              meeting_date="2026-08-01")
    assert result.status == schema.FILED
    ref = result.result_ref
    sha = ref.rsplit("@", 1)[1]
    decision_path = result.report["filed_meeting"]["decisions"][0]["path"]
    page_text = support.read_filed_page(env.repo, sha, decision_path)
    assert "status: developing" in page_text
    assert "as_of: 2026-08-01" in page_text
    assert "entity: [" in page_text
    assert f"submitted_by: {support.DEFAULT_SUBMITTER}" in page_text


# ── an ordinary capture claiming type: meeting still parks (zone confinement) ───────────────────
def test_an_ordinary_capture_claiming_meeting_type_still_parks(rig, clean_queue):
    env, deps = rig
    # submit through the ORDINARY kind, not "meeting" — the fast lane's own whitelist must still
    # refuse `type: meeting` for a row claimed by the ordinary flow.
    support.submit(clean_queue, deps, "DOUBLE:triage-type=meeting\nA note about a meeting.")
    item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.TRIAGE
    assert item["kind"] != schema.MEETING


# ── several unresolved names produce ONE ask naming all of them ─────────────────────────────────
def test_several_unresolved_names_produce_one_ask_naming_all(rig, clean_queue):
    env, deps = rig
    item, result = _file_meeting(
        clean_queue, deps,
        "DOUBLE:meeting-triage=Nebula Systems,Quantum Labs\nA transcript about two prospects.")
    assert result.status == schema.NEEDS_INPUT
    assert "Nebula Systems" in result.report["summary"]
    assert "Quantum Labs" in result.report["summary"]
    assert result.report.get("unresolved_names") == ["Nebula Systems", "Quantum Labs"]


def test_a_non_resolving_reply_parks_the_whole_capture_atomically(rig, clean_queue):
    env, deps = rig
    before = support.branch_sha(env.bare)
    ack = support.submit_meeting(clean_queue, deps,
                                 "DOUBLE:meeting-triage=Nebula Systems,Quantum Labs\nTranscript.")
    worker.process_next(clean_queue, deps)   # -> needs_input, asked_at stamped
    queue.record_reply(clean_queue, ack["id"], answer="Nebula Systems, still not sure", actor=support.DEFAULT_SUBMITTER)
    item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.TRIAGE
    assert support.branch_sha(env.bare) == before   # nothing committed


def test_a_resolving_reply_files_the_whole_set(rig, clean_queue):
    env, deps = rig
    ack = support.submit_meeting(clean_queue, deps,
                                 "DOUBLE:meeting-triage=Acme Corp\nTranscript about Acme.")
    worker.process_next(clean_queue, deps)   # -> needs_input
    queue.record_reply(clean_queue, ack["id"], answer="Acme Corp", actor=support.DEFAULT_SUBMITTER)
    item, result = worker.process_next(clean_queue, deps)
    assert result.status == schema.FILED



# ── zone placement: the second-source-page sabotage (`DOUBLE:meeting-second-source`) is RETIRED,
# not merely fixed — the agent has no page-writing tool left at all (its one legal write is its own
# outcome file), so it cannot simulate writing a second page under `sources/meetings/` any more
# than it can write anywhere else. The analogous zone-placement veto this flow still has is an
# existing-page COLLISION (`processing._write_meeting_pages`'s own precheck), exercised below and
# in the atomicity test that follows it.
def test_a_decision_titled_onto_an_existing_page_is_refused(rig, clean_queue):
    env, deps = rig
    before = support.branch_sha(env.bare)
    item, result = _file_meeting(clean_queue, deps,
                                 "DOUBLE:decisions=1\nDOUBLE:meeting-collide\nA transcript.")
    assert result.status != schema.FILED
    assert support.branch_sha(env.bare) == before


def _assert_nothing_committed(env, before_sha: str, before_shas: set, before_paths: set) -> None:
    """Atomicity in its strong form: not merely that the branch tip did not move, but that the bare
    remote's real git history — every ref, every commit, every path any of them ever named — is
    byte-identical to what it was before the vetoed run. A return value (or even `branch_sha`
    alone) could not tell "no commit happened" apart from "a commit happened and was reset", which
    is exactly the gap a partial-set defect could hide in.
    """
    assert support.branch_sha(env.bare) == before_sha
    assert support.all_commit_shas(env.bare) == before_shas, (
        "the bare remote's commit history grew — a commit was made (or landed on some ref) even "
        "though the run was vetoed")
    assert support.all_ever_committed_paths(env.bare) == before_paths, (
        "a page from the vetoed set is reachable from SOME ref in the bare remote's history")


# ── atomicity negative — a terminal veto on ANY page of the set commits NOTHING, checked against
# the real git history of the bare remote rather than only a return value ───────────────────────
def test_atomicity_an_existing_page_collision_commits_nothing_in_the_repos_real_history(rig,
                                                                                         clean_queue):
    """The structural replacement for the retired `meeting-second-source` sabotage (see the comment
    above `test_a_decision_titled_onto_an_existing_page_is_refused`): the strong form of the
    atomicity check, over the collision precheck instead — not just `branch_sha` unmoved, but
    nothing under `sources/meetings/`, `wiki/meetings/` or `wiki/decisions/` ever reachable from
    any ref, even though one decision genuinely resolved."""
    env, deps = rig
    before_sha, before_shas = support.branch_sha(env.bare), support.all_commit_shas(env.bare)
    before_paths = support.all_ever_committed_paths(env.bare)

    item, result = _file_meeting(clean_queue, deps,
                                 "DOUBLE:decisions=2\nDOUBLE:meeting-collide\n"
                                 "A transcript about Acme and a second decision.")
    assert result.status != schema.FILED
    _assert_nothing_committed(env, before_sha, before_shas, before_paths)






# ── the meeting page's Decisions section links each filed decision 1:1.
# **The adversarial tests that used to live here are RETIRED, not merely fixed: each proved a
# DISAGREEMENT between two independent claims — the outcome's OWN declaration (`page_path`) and
# either the diff the agent's Write/Edit calls produced, or the meeting page's own committed body —
# could exist at all.** The agent now has no page-writing tool whatsoever (the surviving backend
# holds no tool at all, and the offline double's meeting path writes its outcome file and nothing
# else), and it
# no longer declares a `page_path` for anything — CODE is the sole author of every page, from ONE
# structured account (`processing._write_meeting_pages`), so "declared a decision it never wrote"
# and "wrote a decision it never declared" are not sabotage the double can still simulate; they are
# combinations the production code path cannot produce. Likewise the meeting page's own
# "## Decisions" section is BUILT by `_build_meeting_page` from the exact same `decision_stems`
# list `_write_meeting_pages` used to name the files it wrote (`processing._build_meeting_page`'s
# own docstring), so it cannot diverge from what was actually filed either.
#
# `decision-count-mismatch` and `meeting-links-mismatch` (`processing._cross_check_meeting_outcome`)
# both survive as DEFENSE IN DEPTH — self-checks that code's own construction is what it claims to
# be, not adversarial-outcome verification any more — and a control nobody has watched fail is a
# control nobody knows about, so each gets its red proof below, reached by patching the BUILDER a
# future regression could break rather than a double directive that can no longer exist.
def test_sabotage_proof_a_builder_bug_that_undercounts_decision_pages_is_still_caught(
        rig, clean_queue, monkeypatch):
    """Simulates the shape of bug `decision-count-mismatch` exists to catch: `_write_meeting_pages`
    writes one FEWER decision page than `outcome.decisions` describes (a stray `continue`, an
    off-by-one in a future refactor). Patches `processing._decision_stems` — the one place that
    turns N declared titles into N filesystem stems — to silently drop the last one, and confirms
    the capture is refused rather than filed with an incomplete set."""
    from stigmergy.librarian import processing

    env, deps = rig
    before = support.branch_sha(env.bare)
    orig = processing._decision_stems
    monkeypatch.setattr(processing, "_decision_stems", lambda titles: orig(titles)[:-1] or ["x"])

    item, result = _file_meeting(
        clean_queue, deps,
        "DOUBLE:decisions=2\nAlice and Bob discussed the Acme renewal and the pilot scope.")
    assert result.status != schema.FILED, (
        "sabotage check failed: undercounting the decision stems should have produced a "
        "decision-count-mismatch veto (fewer decision pages written than the outcome describes), "
        f"but the capture still reached {result.status!r}")
    assert support.branch_sha(env.bare) == before


def test_sabotage_proof_a_second_meeting_page_is_refused_by_the_arity_veto(
        rig, clean_queue, monkeypatch, tmp_path):
    """`meeting-page-count`'s VETO BRANCH — "a meeting capture files exactly one meeting page".

    The arity was pinned on the happy path only (`test_two_resolvable_entities_file_atomically_...`
    asserts `len(meeting) == 1` over the paths of a filing's own commit), and
    `test_meeting_brief_contract.py`'s removal record for this rule said so in as many words: "the
    veto BRANCH (what happens when the count is wrong) stays unpinned". A happy path cannot tell
    "the builder writes one meeting page" from "the check that would refuse two is dead" — deleting
    the `!= 1` branch outright would break no other test. This is that branch, reached the only way
    it can be: through a builder that writes a second page under `wiki/meetings/`, which is exactly
    the shape of the future refactor the check guards against (a stray extra write, a loop that
    files a per-attendee page).

    The second page is a byte-for-byte COPY of the real one under a second stem, on purpose: a
    well-formed meeting page draws no unrelated veto (`_stamp_meeting` stamps every page under
    `wiki/meetings/` it finds in the DIFF, so the copy is stamped and declared exactly like its
    twin), which is what lets this test assert `outcome/meeting-page-count` is the ONLY name on the
    refusal. A malformed extra page would be refused by the frontmatter and type checks too, and the
    proof would no longer be attributable to the arity veto.
    """
    env, base_deps = rig
    deps = _with_diagnostics(base_deps, tmp_path / "refused")
    before_sha, before_shas = support.branch_sha(env.bare), support.all_commit_shas(env.bare)
    before_paths = support.all_ever_committed_paths(env.bare)

    real_write_meeting_pages = processing._write_meeting_pages

    def _also_write_a_second_meeting_page(worktree, *args, **kwargs):
        written = real_write_meeting_pages(worktree, *args, **kwargs)
        if not isinstance(written, list):    # a `list` is the collision veto: nothing was written
            first = os.path.join(worktree, f"{processing.MEETING_MEETING_PREFIX}"
                                           f"{written['meeting_stem']}.md")
            with open(first, encoding="utf-8") as f:
                text = f.read()
            second = os.path.join(worktree, f"{processing.MEETING_MEETING_PREFIX}"
                                            f"{written['meeting_stem']}-second.md")
            with open(second, "w", encoding="utf-8") as f:
                f.write(text)
        return written

    monkeypatch.setattr(processing, "_write_meeting_pages", _also_write_a_second_meeting_page)

    item, result = _file_meeting(
        clean_queue, deps,
        "DOUBLE:decisions=2\nAlice and Bob discussed the Acme renewal and the pilot scope.")

    assert result.status != schema.FILED, (
        f"sabotage check failed: a second page under wiki/meetings/ should have produced a "
        f"meeting-page-count veto ('a meeting capture files exactly one meeting page'), but the "
        f"capture reached {result.status!r} — either the extra page was never written or that check "
        f"no longer fires, in which case the arity is now asserted on the happy path alone")
    assert result.diagnostics_path, "the refused diff was not preserved"
    assert _refused_by(result) == "outcome/meeting-page-count", (
        f"the refusal did not come from the arity veto alone: the preserved diff names "
        f"{_refused_by(result)!r}. A different name means something else refused the extra page "
        f"first and this test is not exercising `meeting-page-count`; an extra name means the copy "
        f"is drawing an unrelated veto and the proof is no longer attributable to the arity check")
    _assert_nothing_committed(env, before_sha, before_shas, before_paths)
    assert _row(clean_queue, item["id"])["status"] == result.status


# ── the meeting agent's ONE legal write, and why a page write is unreachable rather than vetoed.
# `test_a_knowledge_notes_write_under_the_meeting_flow_is_refused` (`DOUBLE:meeting-notes-write`)
# is RETIRED for the same structural reason as the block above: there is no writer in this flow
# that could put a page under `wiki/notes/`, or anywhere else.
#
# **The MECHANISM behind that sentence has been rewritten once and this note has to say which one
# is load-bearing today**, because the first version named two constants that no longer exist. It
# read: `agent.MEETING_ALLOWED_TOOLS` is exactly `("Write",)` and its only legal target is
# `.librarian-outcome.json` (`agent._MEETING_NO_PAGE_WRITES_RE`). Both retired with the tool-holding
# backend — the regex matched nothing and its `allowed_re` seam had no live caller, so it enforced
# nothing while reading as though it did.
#
# What holds it up now, and it is strictly simpler: the surviving backend holds NO tool at all (its
# whole answer is a structured object), and the offline double's meeting path calls `_write` exactly
# once, with `OUTCOME_FILENAME` — permitted by `agent.confined_write`'s own unconditional
# outcome-file exception, which is the same single rule both flows go through. That exception, and
# the fact that no caller can narrow the rule per flow any more, are pinned in
# `tests/librarian/test_agent_pure.py`.
#
# What remains live and still worth its own test: an injection PAYLOAD in the transcript naming
# this category still gets recorded as a finding, even though it can no longer cause anything — see
# `test_meeting_material_steering_toward_canonical_is_not_obeyed` and
# `test_meeting_material_the_finding_names_a_category_from_the_fixed_set` for that coverage.


# ── "the meeting flow files only NEW pages" — the lock that keeps it from REACHING the edit path
# (the refusal that catches a status-M entry which reaches the gates by any other route is the pair
# below this one) ────────────────────────────────────────────────────────────────────────────────
def test_the_meeting_flow_never_reaches_the_edit_path_a_raising_apply_declared_does_not_disturb_a_filing(
        rig, clean_queue, monkeypatch):
    """The meeting flow files only NEW pages. `edits.apply_declared` is simply never invoked from
    `_one_meeting_pass` — the contract holds because a line of code is absent, and the only thing
    that named it was a comment. `test_meeting_brief_contract.py`'s table used to "pin" it by
    grepping that comment, which proved nothing; this is the behavioural lock that replaces it, and
    the cheapest correct guard here is a test rather than new production code.

    **Scope, now that `edits_allowed`/`zone/meeting-edit-refused` exist:** this test covers the
    CALL — the flow never routes through the edit path. A status-`M` entry that reaches the gates
    by some other route is a different property, enforced in production and pinned by
    `test_a_genuinely_additive_edit_to_an_existing_in_lane_page_is_refused_terminally` and its
    sabotage twin below. The two are complementary and both worth keeping: this one fails if the
    flow starts CALLING the edit path (even were the refusal to be weakened), that pair fails if the
    refusal stops REFUSING (even were this call to stay absent).

    **The exact production change that makes this test fail:** any call to `edits.apply_declared`
    (or `edits.apply`) reached from a meeting filing. The stub below raises `AssertionError` when
    called, so the first meeting capture that routes through the edit path stops filing — the
    exception either propagates out of `process_meeting_item` or is converted into a refusal, and
    both break `result.status == schema.FILED`. The stub is installed on the `edits` MODULE, which
    is exactly the object `processing` holds (`from ... import edits`), so it cannot be bypassed by
    calling through a different alias.

    **And the property itself, independently of which function performs it:** every path in the
    meeting's OWN commit is an ADD. This half survives even a future edit path that is guarded
    behind an outcome field and therefore never calls the stub — it asserts the outcome (only new
    pages) rather than the mechanism. The material names Acme deliberately: the fixture repo
    already holds `wiki/entities/Acme Corp.md`, so an "additive edit" has a real, existing,
    plausible target here — this is not a commit with nothing it could have modified.

    The status check reads `result.result_ref`'s sha, never the branch tip: the post-meeting hook
    pushes a SECOND commit (the view regeneration) that legitimately MODIFIES `views/acme.md`
    — a different writer, out of scope for the meeting filing's own no-edits contract.
    """
    env, deps = rig

    def _refuse(*args, **kwargs):
        raise AssertionError(
            "the meeting flow called edits.apply_declared — this flow files only NEW pages and "
            "grants no edit mechanism at all (`GateContext.edits_allowed=False`), so any edit it "
            "performs is refused by `zone/meeting-edit-refused` rather than filed. If this call "
            "is intended, that refusal is the contract that has to change first — and this test "
            "then needs to be replaced by the test of whatever mechanism replaces it")

    monkeypatch.setattr(processing.edits, "apply_declared", _refuse)
    monkeypatch.setattr(processing.edits, "apply", _refuse)

    item, result = _file_meeting(
        clean_queue, deps,
        "DOUBLE:decisions=2\nAlice and Bob agreed the Acme renewal terms and the pilot scope.")

    assert result.status == schema.FILED, (
        f"an ordinary meeting filing did not survive an edits module that refuses to be called: "
        f"{result.detail if hasattr(result, 'detail') else result}")

    _, meeting_sha = result.result_ref.rsplit("@", 1)
    rows = support.changed_paths_with_status(env.repo, meeting_sha)
    assert rows, "the meeting commit touched nothing — this assertion would prove nothing"
    assert [r for r in rows if r[0] != "A"] == [], (
        f"the meeting's own commit did not only ADD pages: {rows} — a status other than A means "
        f"this flow edited a page that already existed, which no gate in it checks")
    assert _row(clean_queue, item["id"])["status"] == schema.FILED


# ── `edits_allowed=False` / `zone/meeting-edit-refused` — the CONTROL, and the sabotage twin that
# proves it is the control rather than an unexercised branch: before trusting a check, ask whether
# it can go red, and prove it ────────────────────────────────────────────────────────────────────
#
# What the pair is about. The lock above (`test_the_meeting_flow_never_reaches_the_edit_path_...`)
# proves the meeting flow does not CALL the edit path. It cannot prove what happens if a status-`M`
# entry reaches the gates anyway — and until `edits_allowed` existed the answer was: it files.
# `gate_body_rewrite` permits a genuinely additive edit BY DESIGN (rule 2, `_appended_callout_only`
# — a real, gated mechanism for the ordinary fast lane, where `edits.apply_declared` exists), so an
# appended callout on an existing in-lane page passed every gate, passed the contract linter, and
# landed inside the meeting's own commit as an `M`, reported on no surface a human reads. The
# refusal is what closes that, and these two tests are the two halves of its proof: the same
# fixture, once with the control off (it files, as an `M`) and once with it on (refused, terminal,
# no retry spent).
_EARLIER_DECISION_PATH = "wiki/decisions/an-earlier-acme-decision.md"

# A decision page an EARLIER meeting filed: real, lint-clean, committed at the base commit, and in
# the meeting flow's own lane (`processing.MEETING_DECISION_PREFIX`) — which is what makes it the
# right target. An out-of-lane page (`wiki/notes/Existing Note.md`, the fast lane's own
# additive-edit fixture) would be refused by `gate_zone`'s `outside-lane` check whether or not the
# new control exists, so a twin built on one could never show the control doing any work.
#
# Deliberately NOT the fixture repo's own `wiki/decisions/a-decision-from-a-previous-
# meeting.md`: that page's body states, as its own invariant, that no meeting capture in this suite
# ever touches it (the contract linter's per-capture `touched` filter never surfaces a finding about
# it), and the `meeting-collide` directive depends on its stem. Modifying it here would falsify that
# and couple two unrelated fixtures. Its frontmatter shape is mirrored, because that page is proven
# lint-clean, and the body is padded past the linter's thirty-line minimum for the same reason
# every other fixture page here is.
_EARLIER_DECISION_PAGE = """---
type: decision
title: "An earlier Acme decision"
status: developing
created: 2026-02-01
updated: 2026-02-01
tags: [decision, meeting]
related: ["[[Acme Corp]]"]
sources: []
---

# An earlier Acme decision

A decision about [[Acme Corp]] taken in a meeting months before the one the capture under
test files, committed to the base commit before that capture is ever claimed.

## What was decided

That the renewal conversation with Acme would be revisited later in the year, which is
exactly the kind of standing decision a genuinely additive callout ("this newer meeting
overlaps with this older decision") would be appended to on the ordinary fast lane, where
an edit mechanism really exists.

## Why this page exists

The meeting flow's contract is that it files only NEW pages. Proving that contract needs a
page which already exists, sits INSIDE the meeting flow's own three write prefixes, and is
a plausible target for an additive edit — so that a run which modifies it is refused for
the reason under test (this flow has no edit mechanism) and not for an unrelated one (the
page was outside the lane, the page was not there at all, the edit was not additive).

## Facts

- Committed and pushed to `origin/main` before the capture runs, because
  `gates._base_text` reads the "before" version out of the base commit
  (`git show HEAD:<path>` in the ephemeral worktree), not off the working tree.
- Carries no numeric claim of its own, so nothing here trips a figure-bearing check.
- Its stem collides with no page the meeting flow computes for this fixture
  (`_write_meeting_pages`' own existing-page precheck would refuse the whole capture
  first, and that refusal is a different control with its own tests).

## Connections

- Links [[Acme Corp]], the one entity the fixture registry registers, so the callout
  appended to it below names a page that genuinely resolves and the contract linter's
  `dead_links` rule has nothing to say about it.
"""


def _seed_earlier_decision(env) -> str:
    """Commit and PUSH `_EARLIER_DECISION_PAGE`, returning its path.

    Pushed, not merely written: the flow judges the diff against the BASE commit
    (`gitcmd.base_ref`, `origin/main`'s tip), so a page that exists only in the working tree is a
    page the run never sees — `support.commit_and_push`'s own docstring, and the same reason
    `_seed_globex` above uses it.
    """
    os.makedirs(os.path.join(env.repo, "wiki", "decisions"), exist_ok=True)
    with open(os.path.join(env.repo, _EARLIER_DECISION_PATH), "w", encoding="utf-8") as f:
        f.write(_EARLIER_DECISION_PAGE)
    support.commit_and_push(env.repo, "chore: seed a decision page an earlier meeting filed")
    return _EARLIER_DECISION_PATH


# The material for BOTH halves, byte for byte — the twin and the mirror must differ in exactly one
# thing (whether the control is in place), so the fixture is shared rather than retyped.
_REVISIT_MATERIAL = ("DOUBLE:decisions=2\n"
                     "Alice and Bob revisited the Acme renewal and agreed the pilot scope.")


def _append_callout(worktree: str, path: str) -> None:
    """Append one genuinely additive overlap callout to `path` inside `worktree`.

    Through `page.with_callout` — the REAL helper `edits.apply` calls on the ordinary fast lane —
    rather than a hand-rolled `> [!NOTE]` string, so this mutation has exactly the shape the fast
    lane legitimately produces and `gate_body_rewrite`'s rule 2 (`_appended_callout_only`) is being
    exercised on its own terms. Only the callout half of the fast lane's edit: `edits.apply` also
    calls `with_related_link` first, and leaving that out keeps the demonstration resting on rule 2
    alone (an append below the body) instead of also on rule 4's `related:`-growth proof.

    The note is deliberately unremarkable prose — no digits, no wikilink to anything unresolved,
    nothing a gate other than the one under test could have an opinion about. A mutation that draws
    an unrelated veto turns the loosened half below into "refused, but for the wrong reason", and a
    sabotage twin that refuses for the wrong reason lies about what it proved.
    """
    full = os.path.join(worktree, path)
    with open(full, encoding="utf-8") as f:
        before = f.read()
    after = page_policy.with_callout(
        before, kind="overlap", name="Acme Corp",
        note="the same Acme renewal this meeting revisited, from the other side")
    assert after != before, (
        f"page.with_callout changed nothing in {path} — the mutation this twin depends on did not "
        f"happen, so neither half of the pair is exercising an additive edit at all")
    with open(full, "w", encoding="utf-8") as f:
        f.write(after)


def _leak_an_additive_edit_into_the_meeting_flow(monkeypatch, path: str) -> None:
    """Make the meeting flow's write phase append that callout to an existing in-lane page.

    This is THE exploit, reproduced permanently: a status-`M` entry in the meeting's diff, produced
    by code inside the flow's own worktree, which is the only shape it can take (the meeting agent
    holds no tool that can write to any page at all, and its one legal write — the outcome file —
    is the single exception `agent.confined_write` makes). Wrapping `_write_meeting_pages` puts the edit exactly where a
    future edit mechanism, or a leak of `edits.apply_declared` into this flow, would put it: after
    the set is built, before `_one_meeting_pass` builds the `GateContext` and runs the gates over
    the whole diff. The real builder runs first and its return value is passed through untouched, so
    every page of the set is still genuinely code's own; nothing about the legitimate filing changes.
    """
    real_write_meeting_pages = processing._write_meeting_pages

    def _also_edit_an_existing_page(worktree, *args, **kwargs):
        written = real_write_meeting_pages(worktree, *args, **kwargs)
        if not isinstance(written, list):    # a `list` is the collision veto: nothing was written
            _append_callout(worktree, path)
        return written

    monkeypatch.setattr(processing, "_write_meeting_pages", _also_edit_an_existing_page)


def _grant_the_meeting_flow_an_edit_mechanism(monkeypatch) -> list:
    """The loosening: `edits_allowed` left at its `True` DEFAULT for the meeting flow's context.

    **Why this loosening and not monkeypatching `gate_zone`'s branch away.** The control has two
    parts — a caller-level declaration (`processing._one_meeting_pass` passing
    `edits_allowed=False`, one keyword on one `GateContext(...)`) and the gate branch that reads it.
    The BOUND being pinned is the declaration: what makes the meeting flow different from the fast
    lane is not that a branch exists in shared code, it is that THIS caller says it grants no edit
    mechanism. Reverting that one keyword — and nothing else — is the honest reproduction of the
    world before the refusal existed, and it leaves every gate in place and live:
    `gate_body_rewrite` still judges the modified page and the contract linter still runs over it.
    So when the loosened half FILES, that verdict is attributable to what it claims — a genuinely
    additive edit is permissible to every check that exists, and only the caller's declaration
    stops it here.

    Monkeypatching the branch away could not say that. There is no seam finer than `gate_zone`
    itself (the check is four lines inside a per-entry loop), so removing it means replacing the
    whole gate — which would also remove the deletion, unsupported-change, outside-lane, file-mode
    and created-type checks. A filing under that patch would prove only that a gate function was
    absent, not that the edit was permissible, and it would pin the plumbing (a branch exists) in
    place of the bound (which caller grants the mechanism).

    Returns the list of contexts built, so the caller can assert the loosening actually took effect
    rather than trusting that it did.
    """
    real_gate_context = gates.GateContext
    built = []

    def _permissive_gate_context(*args, **kwargs):
        kwargs["edits_allowed"] = True
        ctx = real_gate_context(*args, **kwargs)
        built.append(ctx)
        return ctx

    # On the `gates` MODULE, which is the exact object `processing` holds (`from ... import
    # gates`), so `_one_meeting_pass`' own `gates.GateContext(...)` call site is the one patched.
    monkeypatch.setattr(gates, "GateContext", _permissive_gate_context)
    return built


def test_sabotage_proof_without_the_edit_refusal_an_additive_meeting_edit_files_as_an_m(
        rig, clean_queue, monkeypatch):
    """**The red proof for `zone/meeting-edit-refused`** — a control nobody has watched fail is a
    control nobody knows about. With `edits_allowed` left at its `True` default for the meeting
    flow's context — the one keyword on `_one_meeting_pass`' `GateContext(...)`, reverted and
    nothing else — the appended callout on an existing in-lane decision page passes every gate that
    exists, passes the contract linter, and lands inside the MEETING'S OWN commit as a status-`M`
    entry.

    This is the world before the refusal existed, reproduced rather than remembered: it is what
    makes the mirror test below a proof about the control instead of an observation about a fixture.
    Every assertion here is also a guard on the fixture itself — if the mutation ever stops being
    applied, or stops being additive, or the target page stops being in the lane, this test fails
    instead of quietly passing while exercising nothing.

    Read on `result.result_ref`'s sha, never the branch tip: the post-meeting hook pushes a SECOND
    commit (the view regeneration) that legitimately modifies `views/acme.md`.
    """
    env, deps = rig
    existing = _seed_earlier_decision(env)
    before = support.read_filed_page(env.bare, "main", existing)

    contexts = _grant_the_meeting_flow_an_edit_mechanism(monkeypatch)
    _leak_an_additive_edit_into_the_meeting_flow(monkeypatch, existing)

    item, result = _file_meeting(clean_queue, deps, _REVISIT_MATERIAL)

    assert result.status == schema.FILED, (
        f"sabotage check failed: with the meeting flow's `edits_allowed=False` declaration reverted "
        f"to the default, a genuinely additive callout on an existing in-lane page should have "
        f"filed — got {result.status!r}, findings={result.findings!r}, "
        f"report={result.report!r}. Either the loosening did not take effect, or something ELSE is "
        f"now refusing this edit, which would mean the mirror test's green is not proof that "
        f"`zone/meeting-edit-refused` is what refuses it")
    assert contexts and all(ctx.edits_allowed for ctx in contexts), (
        f"the loosening never reached a GateContext ({len(contexts)} built) — this test would be "
        f"asserting that the flow files with the control IN PLACE, which is the opposite of its "
        f"purpose")

    _, meeting_sha = result.result_ref.rsplit("@", 1)
    rows = support.changed_paths_with_status(env.repo, meeting_sha)
    assert ("M", existing) in rows, (
        f"the additive edit did not land as a modification in the meeting's own commit: {rows} — "
        f"without that, this test proves nothing about a status-M entry reaching the gates")

    after = support.read_filed_page(env.repo, meeting_sha, existing)
    assert "> [!NOTE] Overlaps with [[Acme Corp]]" in after, (
        "the committed page does not carry the appended callout, so what filed was not the edit "
        "this twin claims to have made")
    assert after.startswith(before.rstrip("\n")), (
        "the edit was NOT additive (the base version is no longer a byte-for-byte prefix of the "
        "committed one) — `gate_body_rewrite` would then be refusing it as a rewrite, and this "
        "twin would be demonstrating that gate rather than the absence of the edit refusal")

    # And the harm, which is why the refusal is worth its own control: the landed edit appears on
    # no surface anybody reads. `report.filed_meeting` carries the source pages, the meeting page
    # and the decisions — it has no field for a page this capture merely modified, so an edit that
    # files here is invisible to the submitter, to the operator and to the audit row.
    assert existing not in json.dumps(result.report), (
        "the filed report now names the edited page — if this flow has deliberately gained a "
        "REPORTED edit mechanism, this whole pair needs rewriting against that contract, not this "
        "assertion relaxing")


def test_a_genuinely_additive_edit_to_an_existing_in_lane_page_is_refused_terminally(
        rig, clean_queue, monkeypatch, tmp_path):
    """The mirror: the same seeded page, the same leaked additive edit and the same material as the
    sabotage twin above, with the control in place — the only difference between the two runs is
    whether the meeting flow's context declares `edits_allowed=False` (the `Deps` here also route
    refused diffs into `tmp_path` and wrap the double in a transparent call counter, neither of
    which the flow can see). The edit is refused on `zone/meeting-edit-refused`, nothing is
    committed, and the refusal is TERMINAL — one agent pass, no corrective retry spent.

    Three things asserted, each for its own reason:

    * **the code, end to end.** `zone/meeting-edit-refused` is read out of the preserved refused
      diff's own `# refused by:` line (`processing.refused_diff_digest`), which is the only surface
      that names a finding's `gate/code` after a refusal — the submitter-facing report deliberately
      carries a sentence rather than a code. It is asserted as the ONLY name on that line, which is
      the strong form: it proves the entry was refused on its STATUS, categorically, and that
      `gate_body_rewrite` — which still runs over the same status-`M` entry — found nothing wrong
      with it. An additive edit really is permissible to every other check; this one refusal is what
      stops it.
    * **no corrective retry spent** (`repairable=False`). Counted twice, on purpose:
      `report["agent_attempts"] == 1` is what an operator reads, and `agent.meeting_calls == 1` is
      the direct measurement of how many times the agent actually ran. A finding that became
      repairable by accident would burn the flow's one retry on an agent that cannot act — it holds
      no tool that could have produced the modification and the retry hands it back the same
      transcript — and only the second assertion catches that if the first ever stops being wired.
    * **atomicity**, in the strong form the rest of this file uses: not merely that the branch tip
      did not move, but that the bare remote's history never held any of these pages on any ref.
    * **what the operator actually reads.** The finding's message reaches a human verbatim as
      `report.failed_system`'s `reason`, so the report's own summary is asserted to name the
      cause-space (a worker defect or worktree interference, explicitly NOT the submitted material,
      which is the failure `_refuse_meeting`'s `f.repairable` filter closes one layer up) and to say
      the refused diff was kept. Both
      clauses survive here because THIS fixture's path is short (47 characters); `reason` is clamped
      at 200 and the message is ~147 characters plus the path, so the evidence clause is truncated
      for the longer paths this flow can really compute. That bound is measured and pinned in
      `test_gates_unit.py::test_the_refusals_anti_blame_clause_survives_the_reports_200_character_`
      `reason_clamp`, which asserts only the clause that survives every reachable path.
    """
    env, base_deps = rig
    existing = _seed_earlier_decision(env)
    counting = _CountingMeetingAgent(base_deps.agent)
    deps = dataclasses.replace(_with_diagnostics(base_deps, tmp_path / "refused"), agent=counting)
    before_sha, before_shas = support.branch_sha(env.bare), support.all_commit_shas(env.bare)
    before_paths = support.all_ever_committed_paths(env.bare)

    _leak_an_additive_edit_into_the_meeting_flow(monkeypatch, existing)

    item, result = _file_meeting(clean_queue, deps, _REVISIT_MATERIAL)

    assert result.status == schema.FAILED, (
        f"an additive edit to an existing in-lane page must be refused by this flow, which has no "
        f"edit mechanism at all — got {result.status!r}, report={result.report!r}")
    assert result.diagnostics_path, "the refused diff was not preserved"
    assert _refused_by(result) == "zone/meeting-edit-refused", (
        f"the refusal did not come from the edit check alone: the preserved diff names "
        f"{_refused_by(result)!r}. Anything extra means another gate is ALSO refusing this edit "
        f"(so the sabotage twin above is not isolating this control), and a different name alone "
        f"means the status-M entry is being refused for some other reason entirely")
    assert result.report["agent_attempts"] == 1, (
        f"the refusal spent a corrective retry: agent_attempts="
        f"{result.report['agent_attempts']}. `meeting-edit-refused` is `repairable=False` because "
        f"the meeting agent cannot have produced this modification and cannot un-produce it on a "
        f"retry — a repairable finding here would burn the flow's one retry on an agent that "
        f"cannot act")
    assert counting.meeting_calls == 1, (
        f"the agent ran {counting.meeting_calls} times — the retry really was spent, whatever the "
        f"report's counter says")

    # The operator's briefing, on the surface a human really reads (the queue row's report, not the
    # finding object): the cause-space, the explicit "not the material", and the preserved evidence.
    summary = _row(clean_queue, item["id"])["report"]["summary"]
    for phrase in ("worker defect", "worktree interference", "not the material", "preserved"):
        assert phrase in summary, (
            f"the operator-facing report no longer says {phrase!r}. This is the sentence a human "
            f"gets for a refusal nobody can repair, and dropping the cause-space would let it read "
            f"as the submitter's fault again. If the message grew instead, the 200-character "
            f"`reason` clamp is eating it — see the bound pinned in "
            f"test_gates_unit.py.\nsummary: {summary!r}")

    _assert_nothing_committed(env, before_sha, before_shas, before_paths)
    assert _row(clean_queue, item["id"])["status"] == schema.FAILED


# ── the company-wide anchoring path for a decision page: the fast lane's own rule, asserted here
# for the meeting flow too ──────────────────────────────────────────────────────────────────────
def test_a_decision_page_can_anchor_company_wide_with_a_written_reason(rig, clean_queue):
    env, deps = rig
    item, result = _file_meeting(clean_queue, deps,
                                 "DOUBLE:decisions=2\nDOUBLE:meeting-company=2\n"
                                 "One decision about Acme, one that applies everywhere.")
    assert result.status == schema.FILED
    decisions = result.report["filed_meeting"]["decisions"]
    assert len(decisions) == 2
    entity_row, company_row = decisions[0], decisions[1]
    assert "company-wide scope" in company_row["anchored_to"]
    assert "applies to every customer this meeting touched" in company_row["anchored_to"]

    _, sha = result.result_ref.rsplit("@", 1)
    company_text = support.read_filed_page(env.repo, sha, company_row["path"])
    assert "entity: []" in company_text
    entity_text = support.read_filed_page(env.repo, sha, entity_row["path"])
    assert "entity: [" in entity_text and "entity: []" not in entity_text


# ── a planted credential is rejected and its payload/hints purged immediately — the same
# kind-agnostic path the ordinary flow takes, asserted here for `kind="meeting"` ─────────────────
def test_a_secret_in_the_transcript_material_is_rejected_and_purged_immediately(rig, clean_queue):
    env, deps = rig
    item, result = _file_meeting(clean_queue, deps,
                                 f"A transcript with a leaked token.\nToken: {payloads.GITHUB_PAT}")
    assert result.status == schema.REJECTED
    assert result.report.get(schema.REASON_CODE_KEY) == schema.REASON_SECRET

    with clean_queue.cursor() as cur:
        cur.execute("SELECT payload, hints FROM capture_queue WHERE id = %s", (item["id"],))
        payload, hints = cur.fetchone()
    assert payload is None
    assert hints is None


# ── a long transcript SPLITS rather than failing. This test was once its own opposite
# (`test_a_long_transcript_source_page_exceeds_the_size_bound_and_the_capture_fails`), and is
# re-purposed rather than deleted so the change of behaviour is visible: a transcript over the page
# contract's 150-line cap used to be a hard `gate_contract` error with no recovery; the code that
# authors the source page now splits it into cross-linked parts
# (`processing._build_source_parts`) and the capture files normally. ─────────────────────────────
def test_a_long_transcript_source_page_splits_into_cross_linked_parts_and_files(rig, clean_queue):
    env, deps = rig
    long_line = "The meeting covered another point of ongoing discussion in full detail today.\n"
    material = "DOUBLE:decisions=1\n" + (long_line * 200)
    item, result = _file_meeting(clean_queue, deps, material)
    assert result.status == schema.FILED, (
        f"the oversize source page was expected to split into parts and file, not to be refused "
        f"— got {result.status!r}, findings={result.findings!r}")
    source_pages = result.report["filed_meeting"]["source_pages"]
    assert len(source_pages) > 1, (
        f"a 200-line transcript is over MAX_BODY_LINES and should have split into more than one "
        f"source-page part — got {source_pages!r}")
    _, sha = result.result_ref.rsplit("@", 1)
    part1 = support.read_filed_page(env.repo, sha, source_pages[0])
    part2 = support.read_filed_page(env.repo, sha, source_pages[1])
    assert "Continues in [[" in part1
    assert "Continued from [[" in part2


# ── an injection attempt in the transcript is refused/reported by CATEGORY, never by payload
# substring — the meeting flow's own copy of `test_adversarial.py`'s cat. 1 tests ───────────────
def test_meeting_material_steering_toward_canonical_is_not_obeyed(rig, clean_queue):
    env, deps = rig
    _, result = _file_meeting(clean_queue, deps,
                              f"DOUBLE:decisions=1\n{payloads.STEER_DECLARE_CANONICAL}\n"
                              f"A transcript about Acme.")
    assert result.status == schema.FILED
    assert any("declare-canonical" in f for f in result.findings)
    decision_path = result.report["filed_meeting"]["decisions"][0]["path"]
    _, sha = result.result_ref.rsplit("@", 1)
    page_text = support.read_filed_page(env.repo, sha, decision_path)
    assert "status: developing" in page_text
    assert "status: canonical" not in page_text


def test_meeting_material_asking_to_reveal_credentials_files_ordinary_with_a_recorded_finding(
        rig, clean_queue):
    """Nothing here trips a veto, so refusing would punish a submitter for a sentence in a
    transcript — filed as ordinary content, with the attempt recorded."""
    _, deps = rig
    _, result = _file_meeting(clean_queue, deps,
                              f"DOUBLE:decisions=1\n{payloads.STEER_REVEAL_CREDENTIALS}\n"
                              f"A transcript about Acme.")
    assert result.status == schema.FILED
    assert any("reveal-credentials" in f for f in result.findings)



@pytest.mark.parametrize("payload", payloads.STEERING_PAYLOADS,
                        ids=["declare_canonical", "write_outside_lane", "reveal_credentials"])
def test_no_meeting_report_ever_quotes_the_planted_instruction_back(rig, clean_queue, payload):
    _, deps = rig
    _, result = _file_meeting(clean_queue, deps,
                              f"DOUBLE:decisions=1\n{payload}\nA transcript about Acme.")
    rendered = json.dumps(result.report) + report_module.render_prose(result.report)
    assert payload not in rendered
    for fragment in payload.split(" and "):
        assert fragment.strip() not in rendered


def test_meeting_material_the_finding_names_a_category_from_the_fixed_set(rig, clean_queue):
    _, deps = rig
    _, result = _file_meeting(clean_queue, deps,
                              f"DOUBLE:decisions=1\n{payloads.STEER_REVEAL_CREDENTIALS}\n"
                              f"A transcript about Acme.")
    named = [c for c in gates.INJECTION_CATEGORIES
            if any(c in finding for finding in result.findings)]
    assert named == ["reveal-credentials"]


def test_ordinary_meeting_material_raises_no_steering_finding(rig, clean_queue):
    """The benign twin for this category: an injection detector that fires on normal transcript
    prose would attach a false "tried to instruct the librarian" accusation to an honest meeting.

    Padded past the contract linter's 30-line minimum, because the source page is the transcript
    VERBATIM with no filler under it — a one-sentence fixture would earn a genuine (and here
    irrelevant) "thin page" note that has nothing to do with what this test checks. A real
    transcript is not one sentence long, and this fixture should not be either.
    """
    _, deps = rig
    line = ("Alice and Bob discussed the Acme renewal and the pilot scope, with nothing "
           "steering about it.\n")
    _, result = _file_meeting(clean_queue, deps, "DOUBLE:decisions=1\n" + (line * 32))
    assert result.status == schema.FILED
    assert result.findings == []


# ── the date-in-wikilink convention STEPPED DOWN from veto to gardener finding
# ([ADR 027](../../docs/decisions/027-the-contraction.md)). This test is re-purposed rather than
# deleted, because when a check stops applying you restate it or name its replacement — you never
# silently weaken it: the veto is GONE on purpose, so the same capture now FILES, and the
# convention's enforcement lives in `gardener.checks.check_date_bearing_body_links` (tested with
# its red proof and benign twin in `tests/gardener/test_checks_dossiers.py`). ───────────────────
def test_a_body_prose_link_to_the_date_bearing_meeting_stem_files_and_the_gardener_owns_the_nag(
        rig, clean_queue):
    env, deps = rig
    item, result = _file_meeting(clean_queue, deps,
                                 "DOUBLE:decisions=1\nDOUBLE:meeting-body-date-link\n"
                                 "A transcript about Acme with no figures of its own, dated "
                                 "2026-07-29.")
    assert result.status == schema.FILED
    assert support.branch_sha(env.bare) != ""       # the set landed; the gardener owns the nag
    # And the transcript's own date reaches the decision page it produced — the body-prose link
    # is not stripped on the way, it is simply nobody's veto any more.
    decision_path = result.report["filed_meeting"]["decisions"][0]["path"]
    _, sha = result.result_ref.rsplit("@", 1)
    assert "2026-07-29" in support.read_filed_page(env.repo, sha, decision_path)


# ── the worker hook's safety claim, and "untouched entities stay unchanged". The claim lives in
# `processing._file_meeting`'s own comment — "a view-regeneration fault must never turn a filed
# meeting into a `failed` capture" — and a comment is not a proof, so it is driven here against a
# real raise ────────────────────────────────────────────────────────────────────────────────────
def test_a_raising_view_regeneration_still_leaves_the_meeting_result_filed(rig, clean_queue,
                                                                              monkeypatch):
    """The safety claim, proven against a genuine raise inside the REAL `views_regenerate.run`
    (not a stub that bypasses it): `regenerate_entity` itself raises, so `regenerate.run`'s own
    `ops.job_run` context manager is what records the error row — the exact code path
    `_file_meeting`'s `except Exception` catches and merely logs. The meeting page SET must still
    be genuinely filed and readable; a downstream, best-effort step's fault must never roll back
    or fail an already-pushed, irreversible outcome."""
    env, deps = rig

    async def _raise(*a, **kw):
        raise RuntimeError("synthetic view regeneration fault")

    monkeypatch.setattr(views_regenerate, "regenerate_entity", _raise)

    item, result = _file_meeting(
        clean_queue, deps,
        "DOUBLE:decisions=2\nAlice and Bob discussed the Acme renewal and the pilot scope.")

    assert result.status == schema.FILED, (
        "a raising views.regenerate.run must never turn a filed meeting into a non-FILED "
        f"capture — got {result.status!r}, report={result.report!r}")
    # the meeting page set really did land: read back from the repo's object database, not merely
    # a happy return value (support.py's own posture for this whole suite).
    _, sha = result.result_ref.rsplit("@", 1)
    meeting_path = result.report["filed_meeting"]["meeting_page"]
    assert "Q3 sync" in support.read_filed_page(env.repo, sha, meeting_path)
    assert len(result.report["filed_meeting"]["decisions"]) == 2

    # the real `regenerate.run`'s own `job_run` context manager recorded the fault — the row
    # `_file_meeting`'s comment says "already exists by the time this except runs".
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status, error FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1",
                    (f"{views_regenerate.JOB_NAME}-on-meeting",))
        row = cur.fetchone()
    assert row is not None, "no job_runs row was written for the raising view regeneration"
    assert row[0] == "error"
    assert row[1] == "RuntimeError"


def test_the_guards_scope_is_exception_not_baseexception_a_keyboardinterrupt_still_propagates(
        rig, clean_queue, monkeypatch):
    """The red proof for the safety claim above, without mirroring `_file_meeting`'s own
    `except Exception:` in test code (no seam exists to disable that block
    from outside it — rewriting an equivalent try/except here would just be testing a COPY of the
    production guard, not the real one). Instead this proves the guard's precise, real SCOPE: it
    is `except Exception`, deliberately not a bare `except:` — swap the synthetic fault for a
    `KeyboardInterrupt` (a `BaseException`, exactly like `regenerate.run`'s own documented
    `KeyboardInterrupt` carve-out one layer down) and the SAME code path that quietly filed the
    meeting for an ordinary `RuntimeError` above now lets an operator's Ctrl-C during view
    regeneration propagate out of `worker.process_next` rather than being silently absorbed as
    "best effort" — the real guard disagreeing with itself on two fixtures that differ only in
    the exception's type is the honest proof available here, not a rewritten copy of the guard."""
    env, deps = rig

    async def _interrupt(*a, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(views_regenerate, "regenerate_entity", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        _file_meeting(clean_queue, deps,
                      "DOUBLE:decisions=2\nAlice and Bob discussed the Acme renewal and the "
                      "pilot scope.")


# ── "in the same run": the worker regenerates exactly the touched entities' views, and a SIBLING
# entity's view — never touched by this meeting — is left alone ─────────────────────────────────
def test_only_the_touched_entitys_view_regenerates_a_sibling_is_untouched(
        rig, clean_queue):
    env, deps = rig
    globex_page_before = _seed_globex(env, deps)
    before_meeting = support.branch_sha(env.bare)

    item, result = _file_meeting(
        clean_queue, deps,
        "DOUBLE:decisions=2\nAlice and Bob discussed the Acme renewal and the pilot scope.")
    assert result.status == schema.FILED

    after_all = support.branch_sha(env.bare)
    assert after_all != before_meeting

    # a `job_runs` row for the worker's on-meeting trigger, `ok`, with the touched entity in stats
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status, stats FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1",
                    (f"{views_regenerate.JOB_NAME}-on-meeting",))
        status, stats = cur.fetchone()
    assert status == "ok"
    assert stats.get("written") == 1                      # exactly one entity regenerated

    # the branch tip beyond the meeting's own commit is the view commit(s) this run pushed —
    # it must touch `views/acme.md` and NOTHING under `views/globex.md`.
    _, meeting_sha = result.result_ref.rsplit("@", 1)
    view_changed = support.changed_paths(env.repo, after_all)
    assert "views/acme.md" in view_changed
    assert "views/globex.md" not in view_changed
    assert meeting_sha != after_all                        # confirms a SECOND commit really landed

    # the sibling entity's own view is byte-identical to what `_seed_globex` committed —
    # untouched, not merely "not the changed-paths list of one commit" (defence in depth: a bug
    # that rewrote it back to the SAME bytes in an extra commit would slip the check above).
    globex_page_after = support.read_filed_page(env.bare, "main", "views/globex.md")
    assert globex_page_after == globex_page_before


# ── REMOVED: `test_sabotage_proof_disabling_the_date_bearing_body_link_check_lets_it_file`.
# It monkeypatched `_cross_check_meeting_outcome` away and asserted the capture FILES — but the
# test directly above gets that same FILED verdict with nothing disabled, because the veto this
# "sabotage proof" claimed to isolate stepped down to a gardener finding. Disabling a check and
# asserting the outcome you already get is a test that cannot fail for the reason it names.
# Its one unique assertion (the transcript's date reaches the filed decision page) moved up into
# that test; the convention's own red proof and benign twin live in
# `tests/gardener/test_checks_dossiers.py::check_date_bearing_body_links`.

# ══════════════════════════════════════════════════════════════════════════════════════════════
# A re-file after a park must not throw the distillation away.
#
# THE FINDING, from a real-agent run over a 55 KB transcript, in the sequence that produced it:
#
#   1. Pass 2 distilled SIX decisions and was refused `anchoring/unresolved`. Nothing was wrong
#      with the distillation; the entity it anchored on simply did not exist in the registry yet.
#      The refused diff lists all six.
#   2. A steward minted the entity and requeued.
#   3. Pass 3 threw that distillation away, re-read the 55 KB from scratch, and produced THREE.
#      Two of the three lost decisions are ones an attendee confirms were really taken.
#
# The filed decisions read as faithful, and were incomplete. No test could have produced this:
# every gate was correct, every page was lint-clean, and the second distillation looked perfectly
# plausible on its own.
#
# The rig below is that sequence, with the one substitution a test needs: a re-distillation is made
# DELIBERATELY LOSSY, so "the decisions were preserved" is a claim about the reuse rather than about
# the double happening to be deterministic.
# ══════════════════════════════════════════════════════════════════════════════════════════════
_UNREGISTERED = "Ledgerly"
_PARKING_MATERIAL = ("DOUBLE:decisions=2\n"
                     f"DOUBLE:meeting-anchor={_UNREGISTERED}\n"
                     "Alice and Bob agreed to make Ledgerly the single source of truth for internal "
                     "fund data, and to extract it in two tracks.")


class _LossyMeetingAgent:
    """An agent whose `run_meeting` produces FEWER decisions than the parked pass did, and counts
    its calls.

    This is the substitution that makes the test prove something. The offline double is
    deterministic, so re-running it over identical material would produce the identical
    distillation and "the decisions survived" would be true whether or not any reuse happened. A
    model is not deterministic — that is the entire finding — so the second pass here drops a
    decision on purpose. If the reuse works, `calls` stays at zero and both decisions file; if it
    does not, the loss is visible as exactly the shape a human caught by hand.
    """

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def run(self, **kw):
        return self.inner.run(**kw)

    def run_meeting(self, **kw):
        self.calls += 1
        run = self.inner.run_meeting(**kw)
        if run.outcome is not None and len(run.outcome.decisions) > 1:
            run.outcome = dataclasses.replace(
                run.outcome, decisions=tuple(run.outcome.decisions[:1]),
                summary="a lossy re-distillation: one decision dropped")
        return run


def _register(env, deps, name: str) -> None:
    """A steward mints the entity: `ops/entity-registry.json` gains it AND a real anchored page
    lands on `main`, in one pushed commit — the shape governed entity birth actually produces
    (`entities.birth.commit_message`: "Registry regenerated from wiki/entities/ in the same
    commit").

    **Written into the REPO, not into `deps.registry`.** `process_meeting_item` reloads the
    registry from the base commit on every pass (`base_inputs.load_registry(deps.repo, base)`), so
    an in-memory mutation would be invisible to the gate this test turns on — and the test would
    then be asserting something production cannot do. The in-memory copy is updated too, because
    the post-filing view hook reads `deps.registry`.
    """
    entity_id = name.lower()
    deps.registry.entities[entity_id] = {"name": name, "type": "organization", "aliases": []}
    os.makedirs(os.path.join(env.repo, "wiki", "entities"), exist_ok=True)
    page = _GLOBEX_PAGE.replace("Globex", name).replace("globex", entity_id)
    with open(os.path.join(env.repo, "wiki", "entities", f"{name}.md"), "w") as f:
        f.write(page)
    registry_path = os.path.join(env.repo, "ops", "entity-registry.json")
    with open(registry_path, encoding="utf-8") as f:
        data = json.load(f)
    data["entities"][entity_id] = {"name": name, "type": "organization", "aliases": []}
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    support.commit_and_push(env.repo, f"feat(entity): a steward mints {name}")


def _outcome_column(conn, submission_id):
    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM capture_queue WHERE id = %s", (submission_id,))
        return cur.fetchone()[0]


def test_a_park_keeps_the_distillation_it_produced(clean_queue, rig):
    """Step 1 of the walk: a complete, correct distillation refused for a reason that has nothing
    to do with its content. The decisions must survive the park, in the row itself — they used to
    exist only inside a discarded ephemeral worktree and a refused-diff file."""
    env, deps = rig
    item, result = _file_meeting(clean_queue, deps, _PARKING_MATERIAL)
    assert result.status == schema.TRIAGE          # parked on `anchoring/unresolved`
    assert _UNREGISTERED in result.report["summary"]

    stored = _outcome_column(clean_queue, item["id"])
    assert stored is not None, "the parked distillation was thrown away — this is the defect"
    assert stored["version"] == processing.OUTCOME_REUSE_VERSION
    titles = [d["title"] for d in stored["raw"]["decisions"]]
    assert len(titles) == 2, f"both decisions must survive the park, got {titles}"


def test_the_re_file_reuses_the_parked_distillation_and_spends_no_agent_pass(clean_queue, rig):
    """The whole sequence, as the walk ran it: park → mint → requeue → filed, with the parked
    decisions preserved and the model never asked to read the transcript again."""
    env, deps = rig
    item, result = _file_meeting(clean_queue, deps, _PARKING_MATERIAL)
    assert result.status == schema.TRIAGE

    _register(env, deps, _UNREGISTERED)                       # the steward mints it...
    dispositions.requeue(clean_queue, item["id"], actor="steward@example.com", note="minted")

    lossy = _LossyMeetingAgent(deps.agent)                    # ...and the model would now LOSE one
    _, refiled = worker.process_next(clean_queue, dataclasses.replace(deps, agent=lossy))

    assert refiled.status == schema.FILED
    assert lossy.calls == 0, (
        "the re-file called the agent again — that is the defect: a good distillation discarded "
        "because of an anchoring failure that had nothing to do with its content")
    filed = refiled.report["filed_meeting"]["decisions"]
    assert len(filed) == 2, (
        f"the re-file filed {len(filed)} decision page(s) where the parked pass had 2 — knowledge "
        f"was lost between the park and the re-file, which is exactly the defect this guards")
    # ...and the pages really are in the repo, anchored to the newly minted entity — read back
    # from the bare remote's object database, never from the (removed) worktree.
    for row in filed:
        page = support.read_filed_page(env.bare, "main", row["path"])
        assert f'entity: ["{_UNREGISTERED.lower()}"]' in page, (
            f"the re-filed decision page is not anchored to the minted entity:\n{page[:400]}")


def test_the_reused_filing_says_so_in_the_report(clean_queue, rig):
    """An operator reading a re-filed meeting must be able to tell which happened. The alternative
    used to be silence, and silence is what let the loss through."""
    env, deps = rig
    item, _ = _file_meeting(clean_queue, deps, _PARKING_MATERIAL)
    _register(env, deps, _UNREGISTERED)
    dispositions.requeue(clean_queue, item["id"], actor="steward@example.com", note="minted")

    _, refiled = worker.process_next(clean_queue, deps)
    assert refiled.report["distillation_reuse"]["reused"] is True
    assert "re-filed the distillation from the parked pass" in refiled.report["summary"]
    assert "the transcript was not read again" in refiled.report["summary"]


def test_a_genuine_re_distillation_diffs_the_two_outcomes(clean_queue, rig, monkeypatch):
    """**The instrument, which is the other half of the fix.** When the stored outcome genuinely
    cannot be re-filed, the model runs again — and the report must DIFF what changed, because a
    fresh distillation looks perfectly plausible on its own and diffing is the only reason the
    original loss was ever noticed.

    Forced here by making the stored outcome un-refilable (its declared anchor is still
    unresolvable — the steward minted nothing) while the fresh pass anchors normally and drops a
    decision. So: 2 parked, 1 filed, and the report has to name the one that vanished."""
    env, deps = rig
    item, _ = _file_meeting(clean_queue, deps, _PARKING_MATERIAL)
    parked_titles = [d["title"] for d in _outcome_column(clean_queue, item["id"])["raw"]["decisions"]]
    dispositions.requeue(clean_queue, item["id"], actor="steward@example.com",
                         note="requeued without minting anything")

    # The fresh pass ignores the `meeting-anchor` directive (so it CAN anchor) and drops one.
    real_run_meeting = deps.agent.run_meeting

    def _anchoring_but_lossy(**kw):
        run = real_run_meeting(**{**kw, "material": kw["material"].replace(
            f"DOUBLE:meeting-anchor={_UNREGISTERED}\n", "")})
        run.outcome = dataclasses.replace(run.outcome,
                                          decisions=tuple(run.outcome.decisions[:1]))
        return run

    monkeypatch.setattr(deps.agent, "run_meeting", _anchoring_but_lossy)
    _, refiled = worker.process_next(clean_queue, deps)

    assert refiled.status == schema.FILED
    reuse = refiled.report["distillation_reuse"]
    assert reuse["reused"] is False
    assert len(reuse["dropped"]) == 1, (
        f"a decision vanished between the parked pass ({parked_titles}) and this filing, and the "
        f"report did not name it: {reuse}")
    assert "RE-DISTILLED" in refiled.report["summary"]
    assert "DROPPED" in refiled.report["summary"]


def test_a_changed_reply_re_runs_the_model_rather_than_reusing(clean_queue, rig):
    """The reuse precondition that is not about the material. The submitter's reply is INPUT to the
    distillation (`agent.build_prompt` hands it to the model), and in the real walk pass 2 came
    AFTER a `brain_reply` — so this is a live case, not a hypothetical. A new answer means new
    information, and reusing an outcome produced without it would silently ignore what the human
    just said."""
    env, deps = rig
    item, _ = _file_meeting(clean_queue, deps, _PARKING_MATERIAL)
    _register(env, deps, _UNREGISTERED)
    dispositions.requeue(clean_queue, item["id"], actor="steward@example.com", note="minted")
    with clean_queue.cursor() as cur:   # the submitter answers, after the park
        cur.execute("UPDATE capture_queue SET reply = %s WHERE id = %s",
                    ("it is about Ledgerly, the fund data platform", item["id"]))

    counting = _LossyMeetingAgent(deps.agent)
    _, refiled = worker.process_next(clean_queue, dataclasses.replace(deps, agent=counting))
    assert counting.calls == 1, "a new reply must reach the model, not be skipped by a reuse"
    assert refiled.status == schema.FILED


def test_a_terminal_row_does_not_keep_the_distillation(clean_queue, rig):
    """Retention hygiene, and the reason it is not merely tidiness: the stored value holds the full
    drafted body of every page. Once a row is filed nothing can ever reuse it, so keeping it beside
    a closed row is accumulation with no consumer — the shape `capture.retention` exists to
    prevent."""
    env, deps = rig
    item, result = _file_meeting(clean_queue, deps,
                                 "DOUBLE:decisions=2\nA transcript about Acme's renewal.")
    assert result.status == schema.FILED
    assert _outcome_column(clean_queue, item["id"]) is None


# ══════════════════════════════════════════════════════════════════════════════════════════════
# A CHAIN of parks — the first version of the fix above reproduced the very loss it was written to
# prevent, one step earlier, inside itself.
#
# `queue.finish`'s COALESCE REPLACES on a non-None value, so this sequence lost knowledge with
# nothing reporting it:
#
#   park (2 decisions stored) → steward requeues having minted the WRONG name → the reuse is
#   vetoed → the fresh model run yields 1 → **the 1 overwrites the 2** → mint the right name →
#   requeue → the reuse re-files 1 decision and the report says "1 decision(s) preserved".
#
# Preserved from the *last* park, and silent about the loss before it — reassuring about exactly
# the failure the instrument exists to surface. `first_park_titles` is carried forward untouched so
# the diff outlives the chain and a process restart, and a park that shrinks the distillation says
# so at the pass that caused it rather than waiting for a filing that may never come.
# ══════════════════════════════════════════════════════════════════════════════════════════════
class _TruncatingMeetingAgent:
    """Keeps only the first decision, and keeps DECLARING the unresolved anchor so the pass parks
    again — which is what forms the chain. Distinct from `_LossyMeetingAgent` above, which is used
    on a pass that is expected to FILE."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def run(self, **kw):
        return self.inner.run(**kw)

    def run_meeting(self, **kw):
        self.calls += 1
        run = self.inner.run_meeting(**kw)
        if run.outcome is not None and len(run.outcome.decisions) > 1:
            run.outcome = dataclasses.replace(run.outcome,
                                              decisions=tuple(run.outcome.decisions[:1]))
        return run


def test_a_second_park_does_not_silently_replace_a_richer_first_one(clean_queue, rig):
    """Step 2 of the chain, in isolation: the stored outcome IS replaced (this function does not
    overrule the gates about which distillation is fileable), but the first park's titles survive
    and the park report names what was dropped — at the pass that dropped it."""
    env, deps = rig
    item, first = _file_meeting(clean_queue, deps, _PARKING_MATERIAL)
    assert first.status == schema.TRIAGE
    original = [d["title"] for d in _outcome_column(clean_queue, item["id"])["raw"]["decisions"]]
    assert len(original) == 2

    # requeued WITHOUT minting: the reuse is vetoed, so the model re-runs — and loses one
    dispositions.requeue(clean_queue, item["id"], actor="steward@example.com", note="not yet")
    truncating = _TruncatingMeetingAgent(deps.agent)
    _, second = worker.process_next(clean_queue, dataclasses.replace(deps, agent=truncating))

    assert second.status == schema.TRIAGE and truncating.calls >= 1
    stored = _outcome_column(clean_queue, item["id"])
    assert len(stored["raw"]["decisions"]) == 1          # the smaller set really did replace it...
    assert stored["first_park_titles"] == original       # ...and the original survives for the diff
    # ...and the loss is REPORTED here, not deferred to a filing that may never happen
    assert second.report["distillation_loss"]["dropped"] == [original[1]]
    assert "SMALLER distillation" in second.report["summary"]


def test_across_a_chain_of_parks_the_filing_diffs_against_the_FIRST_park(clean_queue, rig):
    """The whole chain, end to end and three hops deep. The filing must not say "preserved": a
    decision the capture started with is missing, and the report has to name it even though no
    model ran on the pass that filed."""
    env, deps = rig
    item, _ = _file_meeting(clean_queue, deps, _PARKING_MATERIAL)
    original = [d["title"] for d in _outcome_column(clean_queue, item["id"])["raw"]["decisions"]]

    dispositions.requeue(clean_queue, item["id"], actor="steward@example.com", note="not yet")
    _, second = worker.process_next(
        clean_queue, dataclasses.replace(deps, agent=_TruncatingMeetingAgent(deps.agent)))
    assert second.status == schema.TRIAGE

    _register(env, deps, _UNREGISTERED)                  # NOW the steward mints it
    dispositions.requeue(clean_queue, item["id"], actor="steward@example.com", note="minted")
    counting = _LossyMeetingAgent(deps.agent)
    _, filed = worker.process_next(clean_queue, dataclasses.replace(deps, agent=counting))

    assert filed.status == schema.FILED
    assert counting.calls == 0                           # the stored (smaller) outcome was reused
    reuse = filed.report["distillation_reuse"]
    assert reuse["reused"] is False, (
        "the filing claimed the parked distillation was preserved. It preserved the LAST park; a "
        "decision this capture started with is missing, which is the loss this diff exists for")
    assert reuse["model_ran"] is False                   # ...and no model ran on THIS pass
    assert reuse["dropped"] == [original[1]]
    assert "an EARLIER pass re-read the transcript" in filed.report["summary"]
    assert "DROPPED" in filed.report["summary"]


def test_a_clean_single_park_still_reports_preserved_the_benign_twin(clean_queue, rig):
    """The benign twin. The chain logic must not make every reuse report a loss: one park, one
    mint, one re-file — nothing dropped, and the report says preserved. Without this, a
    `_reuse_note` that always took the diff branch would pass both tests above."""
    env, deps = rig
    item, _ = _file_meeting(clean_queue, deps, _PARKING_MATERIAL)
    _register(env, deps, _UNREGISTERED)
    dispositions.requeue(clean_queue, item["id"], actor="steward@example.com", note="minted")

    _, filed = worker.process_next(clean_queue, deps)
    assert filed.status == schema.FILED
    assert filed.report["distillation_reuse"]["reused"] is True
    assert "distillation_loss" not in filed.report

# ── removed with ingest-time figure verification, and named rather than dropped in silence ──────
# A check that stops running must be impossible to miss, so what left is listed here instead of
# vanishing from the file. The tests below drove a HALLUCINATED FIGURE through the fast lane and
# asserted that a figure-verification gate vetoed it, that one corrective retry recovered it, or
# that the resulting report carried the right verdict. That gate is gone
# ([ADR 026](../../docs/decisions/026-the-purge.md) D2): ingest-time figure verification went with
# the trust layer, deliberately, and the accepted consequence is stated there — **an invented
# figure CAN sit on a page.** The reader's protection is the verbatim source one click away, the
# gardener, and `answer.verify_answer` at query time.
#
# So these are removed, not repaired: their subject no longer exists, and a test rewritten to
# assert the opposite would be measuring a decision, not a mechanism. What they ALSO covered
# incidentally — atomicity, the once-directive, the steering veto — is covered by the remaining
# tests in this file, which reach the same refusal shape through vetoes that still exist: the
# page-collision precheck, the second-meeting-page arity veto, and the additive-edit refusal.
# (An earlier version of this note said "zone, anchoring, secrets"; those are not the ones that
# actually carry it. Named correctly here rather than left approximately right.)
#
# Removed:
#   `test_a_hallucinated_figure_on_the_first_pass_recovers_on_the_corrective_retry`,
#   `test_atomicity_a_veto_on_the_meeting_page_commits_nothing_in_the_repos_real_history`,
#   `test_atomicity_a_veto_on_the_third_of_three_decision_pages_commits_nothing`,
#   `test_hallucinated_figure_is_vetoed_and_never_reaches_a_committed_page`,
#   `test_meeting_steering_that_also_trips_a_real_veto_is_rejected_never_obeyed`,
#   `test_sabotage_proof_widening_the_touched_set_makes_the_job_run_check_the_sibling_too`,
#   `test_the_recovery_above_only_happens_because_of_the_once_directive_not_by_accident`
