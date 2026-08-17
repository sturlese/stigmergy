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
from stigmergy.librarian import agent as agent_module
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


class _RecordingMeetingAgent:
    """Wraps an agent, counts `run_meeting` calls and keeps every call's keyword arguments.

    The count is the direct measurement of how many agent passes a run spent, independent of the
    counter the report happens to carry; the recorded kwargs are how a test asserts what the WORKER
    handed the agent — the prompt is composed inside the backend, so the port call is the only place
    the worker's own contribution is visible without reaching into a model. Everything else
    delegates, so this is a transparent wrapper rather than a stand-in for the double."""

    def __init__(self, inner):
        self.inner = inner
        self.meeting_calls = 0
        self.calls = []

    def run_meeting(self, **kwargs):
        self.meeting_calls += 1
        self.calls.append(kwargs)
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


# REMOVED: `test_the_meeting_flow_never_reaches_the_edit_path_a_raising_apply_declared_does_not_`
# `disturb_a_filing`. It stubbed `edits.apply_declared` into raising and asserted a meeting still
# filed — a behavioural lock on the property "this flow files only NEW pages", whose own docstring
# named the condition for its removal: *"If this call is intended, that refusal is the contract that
# has to change first — and this test then needs to be replaced by the test of whatever mechanism
# replaces it."* Both halves of that condition are met by ADR-038: the meeting flow now HAS the fast
# lane's declared-edit mechanism, `_one_meeting_pass` calls `edits.apply_declared` on every pass,
# and `GateContext.edits_allowed` keeps its `True` default here. The test is removed rather than
# inverted because its subject no longer exists.
# REPLACED BY: `test_a_declared_backlink_from_a_meeting_lands_on_the_existing_page_and_is_reported`
# and `test_a_declared_meeting_edit_code_refuses_produces_no_commit` below — the mechanism's own
# benign and adversarial halves.


# ── the worker hands this flow the corpus it files into (ADR-038) ───────────────────────────────
def test_the_worker_hands_the_meeting_agent_the_gathered_context_it_files_into(rig, clean_queue):
    """The asymmetry #37 names, closed: the meeting flow's agent now receives the SAME deterministic
    gathered context the ordinary flow's does, built by the WORKER from this item's own worktree.

    Asserted at the PORT CALL, not in the prompt: `build_meeting_prompt` is invoked inside the
    backend, so the recorded `gathered` keyword is the only place the worker's own contribution is
    visible without a model in the loop — and it is the exact string the backend puts in the prompt,
    since it arrives already rendered (one context builder, one fence discipline, `_one_pass`' own
    reason for building it in the worker).

    Every clause is a property of `agent.render_gathered`'s NO-TOOLS defaults, which are what this
    flow must get: the fence around the page-derived half, the preface's statement that there is no
    tool to look further with, and the `link_names` vocabulary sentence. A run handed the ORDINARY
    backend's seeded sentences instead would tell a tool-less agent to go searching, which is the
    one thing it cannot do — so the seeded preface is asserted ABSENT rather than merely the
    no-tools one present.
    """
    _env, base_deps = rig
    recording = _RecordingMeetingAgent(base_deps.agent)
    deps = dataclasses.replace(base_deps, agent=recording)

    _, result = _file_meeting(
        clean_queue, deps,
        "DOUBLE:decisions=1\nAlice and Bob agreed the Acme renewal terms.")

    assert result.status == schema.FILED, result.report.get("summary")
    assert recording.meeting_calls == 1
    gathered = recording.calls[0]["gathered"]
    assert gathered, "the worker handed the meeting agent no gathered context at all"
    assert agent_module.GATHERED_PREFACE_NO_TOOLS in gathered, (
        f"the context was not framed for a tool-less reader:\n{gathered}")
    assert processing._SEEDED_GATHERED_SENTENCES["preface"] not in gathered, (
        "the meeting agent was handed the EXPLORING backend's preface, which tells a reader with "
        "no tools at all to look further whenever the context is not enough")
    assert f"<<<{agent_module._FENCE_TOKEN}" in gathered, (
        f"the page-derived half is not inside the fence — titles and excerpts are content people "
        f"wrote, and unfenced they read as instructions:\n{gathered}")
    assert "link_names" in gathered, (
        f"the wikilink vocabulary is missing, so the agent cannot know which [[name]] resolves:"
        f"\n{gathered}")
    # The corpus really was read: the fixture repo's own entity page is a page the gatherer can
    # rank or name, so an empty-but-well-formed block would fail here.
    assert "Acme Corp" in gathered, (
        f"nothing from the checkout reached the block — the gatherer ran over an empty corpus, and "
        f"the assertions above would pass on a context that holds nothing:\n{gathered}")


# ── the declared-edit mechanism this flow gained (ADR-038) ──────────────────────────────────────
# The meeting agent still holds no tool that can reach any page: it DECLARES the edit in its account
# and the worker performs it (`edits.apply_declared`), which is the fast lane's own arrangement,
# reached through the same functions and judged by the same `gate_body_rewrite`.
#
# **The one folder a meeting can really edit is `wiki/decisions/`.** `edits.validate` admits the
# three EDITABLE folders (`page.FOLDER_BY_TYPE`: notes, decisions, concepts) while this flow's own
# lane is `MEETING_WRITE_PREFIXES` (sources/meetings, wiki/meetings, wiki/decisions), so a declared
# edit to `wiki/notes/` passes validation and is then refused by `gate_zone` as out-of-lane. The
# lane is deliberately not widened to match — it is the meeting BUILDER's range, pinned as such by
# `test_gates_unit.py::test_the_meeting_lane_is_exactly_the_range_of_paths_the_meeting_builder_can_`
# `write` — and the brief is what tells the agent which pages it may name.
_EARLIER_DECISION_PATH = "wiki/decisions/an-earlier-acme-decision.md"

# A decision page an EARLIER meeting filed: real, lint-clean, committed at the base commit, and in
# the meeting flow's own lane (`processing.MEETING_DECISION_PREFIX`) — which is what makes it the
# right target for every test below. An out-of-lane page (`wiki/notes/Existing Note.md`, the fast
# lane's own additive-edit fixture) would be refused by `gate_zone`'s `outside-lane` check whatever
# the account declared, so a test built on one could never show the edit mechanism doing any work.
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
overlaps with this older decision") is appended to when a capture declares one.

## Why this page exists

A meeting distiller that can declare edits needs a page which already exists, sits INSIDE
the meeting flow's own write prefixes, and is a plausible target for an additive edit — so
that a run which modifies it does so for the reason under test and any refusal is about
that reason too, never an unrelated one (the page was outside the lane, the page was not
there at all, the edit was not additive).

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


# The material every test in this section shares, byte for byte: the halves must differ in exactly
# one thing (what the account declares, or what leaks into the worktree), so the fixture is shared
# rather than retyped.
_REVISIT_MATERIAL = ("DOUBLE:decisions=2\n"
                     "Alice and Bob revisited the Acme renewal and agreed the pilot scope.")

# The stem `processing._decision_stems` computes for the double's FIRST decision on that material —
# the basename of a page this capture creates, which is what the double declares its edits to link
# and therefore what must appear in the edited page's `related:`. Derived from the same slug
# function production uses, never typed out, so a change to either side is caught here rather than
# by an assertion that quietly stopped matching anything.
_FIRST_DECISION_STEM = processing._decision_stems(["Q3 sync — decision 1"])[0]


def _append_callout(worktree: str, path: str) -> None:
    """Append one genuinely additive overlap callout to `path` inside `worktree`.

    Through `page.with_callout` — the REAL helper `edits.apply` calls — rather than a hand-rolled
    `> [!NOTE]` string, so this mutation has exactly the shape a declared edit legitimately
    produces and `gate_body_rewrite`'s rule 2 (`_appended_callout_only`) is exercised on its own
    terms. Only the callout half of an edit: `edits.apply` also calls `with_related_link` first,
    and leaving that out keeps the demonstration resting on rule 2 alone (an append below the body)
    instead of also on rule 4's `related:`-growth proof.

    The note is deliberately unremarkable prose — no digits, no wikilink to anything unresolved,
    nothing a gate other than the one under test could have an opinion about. A mutation that draws
    an unrelated veto turns a refusal into "refused, but for the wrong reason", and a test that
    refuses for the wrong reason lies about what it proved.
    """
    full = os.path.join(worktree, path)
    with open(full, encoding="utf-8") as f:
        before = f.read()
    after = page_policy.with_callout(
        before, kind="overlap", name="Acme Corp",
        note="the same Acme renewal this meeting revisited, from the other side")
    assert after != before, (
        f"page.with_callout changed nothing in {path} — the mutation this test depends on did not "
        f"happen, so it is not exercising an additive edit at all")
    with open(full, "w", encoding="utf-8") as f:
        f.write(after)


def _rewrite_the_body(worktree: str, path: str) -> None:
    """Replace a paragraph of `path`'s body in place — a NON-additive change, the kind
    `gate_body_rewrite` exists to refuse.

    The counterpart to `_append_callout`: same page, same flow, same leak site, and the ONE
    difference is that the base version is no longer a prefix of the result. Nothing else about the
    page changes — no digits appear, no link is added or removed, the frontmatter is untouched — so
    a veto here is attributable to the rewrite and to nothing that came with it.
    """
    full = os.path.join(worktree, path)
    with open(full, encoding="utf-8") as f:
        before = f.read()
    after = before.replace(
        "That the renewal conversation with Acme would be revisited later in the year",
        "That the renewal conversation with Acme was settled and needs no further discussion")
    assert after != before, (
        f"the rewrite changed nothing in {path} — the sentence this test edits is no longer in the "
        f"fixture page, so it is not exercising a body rewrite at all")
    with open(full, "w", encoding="utf-8") as f:
        f.write(after)


def _leak_an_undeclared_edit_into_the_meeting_flow(monkeypatch, path: str, mutate) -> None:
    """Make the meeting flow's write phase mutate an existing in-lane page that NO declaration named.

    A status-`M` entry in the meeting's diff produced by code inside the flow's own worktree, which
    is the only shape one can take that `edits.apply_declared` did not produce: the meeting agent
    holds no tool that can write to any page at all, and its one legal write — the outcome file — is
    the single exception `agent.confined_write` makes. Wrapping `_write_meeting_pages` puts the
    mutation exactly where a worker defect or worktree interference would put it: after the set is
    built, before `_one_meeting_pass` applies the declared edits, builds the `GateContext` and runs
    the gates over the whole diff. The real builder runs first and its return value is passed
    through untouched, so every page of the set is still genuinely code's own.
    """
    real_write_meeting_pages = processing._write_meeting_pages

    def _also_mutate_an_existing_page(worktree, *args, **kwargs):
        written = real_write_meeting_pages(worktree, *args, **kwargs)
        if not isinstance(written, list):    # a `list` is the collision veto: nothing was written
            mutate(worktree, path)
        return written

    monkeypatch.setattr(processing, "_write_meeting_pages", _also_mutate_an_existing_page)


def test_a_declared_backlink_from_a_meeting_lands_on_the_existing_page_and_is_reported(
        rig, clean_queue):
    """**The mechanism ADR-038 gave this flow, end to end.** The account declares a `backlink` on a
    decision page an earlier meeting filed; the WORKER performs it (`edits.apply_declared`), it
    lands inside the meeting's OWN commit as a status-`M` entry, every gate passes, and the report a
    human reads names the page that was touched.

    Four things asserted, each for its own reason:

    * **the edit really happened, in git**, read back out of the meeting commit's own tree rather
      than off the working copy — a page edited in a worktree that never reached a commit is not an
      edit at all;
    * **it was additive**: a `backlink` touches the frontmatter's `related:` list and NOTHING else,
      so additivity is asserted the way `gate_body_rewrite` establishes it — the body is byte-for-byte
      the base's, and the `related:` list GREW rather than being rewritten (the link the page already
      carried is still there). That is what makes the gate's permission the reason this filed, and
      not a gate that happened to be looking elsewhere;
    * **the set still filed atomically** — the new pages and the edit are ONE commit, which is what
      "the diff the gates approved is the diff that lands" means here;
    * **the report names it.** `pages_edited` was a hardcoded `(none)` line in `report.filed_meeting`
      for as long as this flow had no edit mechanism. A page a commit changes and no surface names is
      a page nobody knows was touched — that is the harm the old `edits_allowed=False` control
      existed to prevent, and reporting it is what makes granting the mechanism honest.
    """
    env, deps = rig
    existing = _seed_earlier_decision(env)
    before = support.read_filed_page(env.bare, "main", existing)

    item, result = _file_meeting(
        clean_queue, deps, f"DOUBLE:meeting-backlink={existing}\n{_REVISIT_MATERIAL}")

    assert result.status == schema.FILED, (
        f"a declared backlink onto an existing decision page did not file: "
        f"findings={result.findings!r}, report={result.report!r}")

    _, meeting_sha = result.result_ref.rsplit("@", 1)
    rows = support.changed_paths_with_status(env.repo, meeting_sha)
    assert ("M", existing) in rows, (
        f"the declared edit is not in the meeting's own commit: {rows}")

    after = support.read_filed_page(env.repo, meeting_sha, existing)
    assert f"[[{_FIRST_DECISION_STEM}]]" in after, (
        f"the edited page's `related:` does not name the decision page this meeting filed "
        f"({_FIRST_DECISION_STEM!r}) — `edits.apply` wrote no link, so what landed is not the "
        f"declared backlink:\n{after}")
    assert page_policy.body_lines(after) == page_policy.body_lines(before), (
        "the declared backlink changed the page's BODY: a `related:` link is a frontmatter edit and "
        "nothing else, so a body that moved means `edits.apply` rewrote a page instead of linking "
        "to one")
    assert "[[Acme Corp]]" in after, (
        "the link this page already carried is gone: the `related:` list was rewritten rather than "
        "grown, which is not an additive edit however the new link got in")

    assert result.report.get("pages_edited") == [existing], (
        f"the report does not name the page this capture edited: "
        f"pages_edited={result.report.get('pages_edited')!r}")
    assert existing in result.report["summary"], (
        f"the operator-facing summary does not name the edited page — the `pages_edited` line is "
        f"still rendering `(none)`:\n{result.report['summary']}")
    assert _row(clean_queue, item["id"])["status"] == schema.FILED


def test_a_declared_overlap_from_a_meeting_appends_the_callout_the_other_pages_reader_sees(
        rig, clean_queue):
    """The second kind, which carries prose: an `overlap` adds the `related:` link AND a
    `> [!NOTE] Overlaps with [[…]]` callout carrying the agent's own sentence. The note is what a
    reader of the OTHER page sees, so it is asserted on the committed bytes rather than on the
    declaration."""
    env, deps = rig
    existing = _seed_earlier_decision(env)

    _, result = _file_meeting(
        clean_queue, deps, f"DOUBLE:meeting-overlap={existing}\n{_REVISIT_MATERIAL}")

    assert result.status == schema.FILED, (
        f"a declared overlap did not file: findings={result.findings!r}, report={result.report!r}")
    _, meeting_sha = result.result_ref.rsplit("@", 1)
    after = support.read_filed_page(env.repo, meeting_sha, existing)
    assert f"> [!NOTE] Overlaps with [[{_FIRST_DECISION_STEM}]]" in after, (
        f"the overlap callout is not on the edited page:\n{after}")
    assert "from the other side" in after, (
        f"the callout carries no note, so the reader of the other page is told nothing:\n{after}")


@pytest.mark.parametrize("bad_target,label", [
    ("ops/acl.json", "outside the editable folders"),
    ("wiki/decisions/Does Not Exist.md", "a page that is not there"),
])
def test_a_declared_meeting_edit_code_refuses_produces_no_commit(
        rig, clean_queue, bad_target, label):
    """The adversarial twin, mirroring the fast lane's own
    (`test_processing_pg.py::test_a_declared_edit_code_refuses_produces_no_commit`). A declaration is
    untrusted input on this flow exactly as on that one: the target has to exist and be editable, and
    a bad one refuses the WHOLE page set rather than being silently skipped — `edits.apply_declared`
    is all-or-nothing, so a half-applied meeting is not a state this flow can reach."""
    env, deps = rig
    before_sha, before_shas = support.branch_sha(env.bare), support.all_commit_shas(env.bare)
    before_paths = support.all_ever_committed_paths(env.bare)

    _, result = _file_meeting(
        clean_queue, deps, f"DOUBLE:meeting-bad-edit={bad_target}\n{_REVISIT_MATERIAL}")

    assert result.status in (schema.REJECTED, schema.FAILED), label
    assert result.result_ref == ""
    _assert_nothing_committed(env, before_sha, before_shas, before_paths)


def test_a_declared_meeting_edit_never_reaches_a_page_this_capture_created(rig, clean_queue):
    """`own-page`: an edit declared against a page the same capture just wrote is a confusion, not an
    edit — the link belongs in the new page itself, which code writes freely. The target is the
    meeting page's own computed path, so this exercises `edits.validate`'s `path_key` comparison
    against `ctx.in_lane_new_pages()` on the SET, which is the meeting flow's version of the fast
    lane's single new page."""
    env, deps = rig
    before_sha, before_shas = support.branch_sha(env.bare), support.all_commit_shas(env.bare)
    before_paths = support.all_ever_committed_paths(env.bare)
    own = f"{processing.MEETING_DECISION_PREFIX}{_FIRST_DECISION_STEM}.md"

    _, result = _file_meeting(
        clean_queue, deps, f"DOUBLE:meeting-bad-edit={own}\n{_REVISIT_MATERIAL}")

    assert result.status in (schema.REJECTED, schema.FAILED), (
        f"an edit declared against a page this very capture created was accepted: "
        f"report={result.report!r}")
    _assert_nothing_committed(env, before_sha, before_shas, before_paths)


# ── what the gates still refuse now that this flow grants an edit mechanism ─────────────────────
#
# REMOVED, as a pair: `test_sabotage_proof_without_the_edit_refusal_an_additive_meeting_edit_files_`
# `as_an_m` and `test_a_genuinely_additive_edit_to_an_existing_in_lane_page_is_refused_terminally`.
# They were the two halves of one proof about `GateContext.edits_allowed=False`: with the control
# off a genuinely additive callout filed as an `M`, with it on the same callout was refused
# terminally on `zone/meeting-edit-refused`. ADR-038 removes their subject — this flow declares
# `edits_allowed` no longer, because it HAS an edit mechanism, so the refusal they proved is not a
# thing this flow does any more and the "world before it existed" they reproduced is the world we
# are now in. Re-pointing them at the loosened flow would leave two tests asserting that an edit
# files, which is what the pair immediately above already proves about a DECLARED one.
# The `edits_allowed=False` branch itself is unchanged, still live and still exercised — by
# `test_gates_unit.py`'s explicit contexts (`test_a_modification_is_refused_when_the_caller_grants_
# no_edit_mechanism` and the precedence table above it), which is where a check with no production
# caller belongs.
#
# WHAT THIS FLOW GAVE UP, stated plainly rather than left to be discovered: an UNDECLARED additive
# edit — a worker defect, worktree interference — now files, exactly as it does on the fast lane,
# because `gate_body_rewrite` permits an additive change BY DESIGN and nothing downstream asks
# whether a declaration produced it. The pair below is the honest record of that: the first test
# asserts the surviving control (a non-additive rewrite is still refused, terminally), the second
# pins the posture change itself so that tightening it later is a deliberate act with a red test
# behind it rather than a surprise.
def test_a_stray_non_additive_rewrite_inside_the_meeting_flow_is_still_refused(
        rig, clean_queue, monkeypatch, tmp_path):
    """The control that SURVIVES granting this flow an edit mechanism. A rewrite of an existing
    in-lane page's body — leaked into the flow's write phase, declared by nobody — is refused by
    `gate_body_rewrite`, nothing is committed, and the refusal is terminal.

    Read out of the preserved refused diff's own `# refused by:` line
    (`processing.refused_diff_digest`), which is the only surface that names a finding's
    `gate/code` after a refusal — the submitter-facing report deliberately carries a sentence. The
    name is asserted as the ONLY one on that line: anything extra would mean a second gate is also
    refusing this, and then this test would not be isolating the one it names.
    """
    env, base_deps = rig
    existing = _seed_earlier_decision(env)
    deps = _with_diagnostics(base_deps, tmp_path / "refused")
    before_sha, before_shas = support.branch_sha(env.bare), support.all_commit_shas(env.bare)
    before_paths = support.all_ever_committed_paths(env.bare)

    _leak_an_undeclared_edit_into_the_meeting_flow(monkeypatch, existing, _rewrite_the_body)

    item, result = _file_meeting(clean_queue, deps, _REVISIT_MATERIAL)

    assert result.status == schema.FAILED, (
        f"a body rewrite of an existing page was not refused: report={result.report!r}")
    assert result.diagnostics_path, "the refused diff was not preserved"
    assert _refused_by(result) == "zone/body-rewrite", (
        f"the refusal did not come from the body-rewrite gate alone: the preserved diff names "
        f"{_refused_by(result)!r}")
    _assert_nothing_committed(env, before_sha, before_shas, before_paths)
    assert _row(clean_queue, item["id"])["status"] == schema.FAILED


def test_a_stray_additive_edit_now_files_which_is_the_fast_lanes_own_posture(
        rig, clean_queue, monkeypatch):
    """**The posture this flow adopted, pinned so that changing it back is deliberate.** The same
    leak site and the same page as the test above, with a genuinely ADDITIVE mutation: an appended
    callout no declaration named. It files.

    This is not a defect this test blesses — it is the fast lane's own long-standing posture,
    inherited the moment this flow stopped declaring `edits_allowed=False`: `gate_body_rewrite`
    permits an additive change by design (rule 2, `_appended_callout_only`) and no gate asks whether
    a declaration produced it. It is recorded as a test rather than a comment because a comment
    cannot fail: if a future change makes the meeting flow (or the fast lane) refuse an undeclared
    modification, this test goes red at the exact line that states the old rule, and whoever tightens
    it is told what they changed rather than discovering it in production.

    The one asymmetry that IS asserted here, because it is what makes the mechanism honest: the
    filed report names only what `edits.apply_declared` actually wrote. An undeclared edit rides
    along in the commit and appears in `pages_edited` nowhere — which is precisely why the declared
    path is the only one a submitter is ever told about.
    """
    env, deps = rig
    existing = _seed_earlier_decision(env)
    before = support.read_filed_page(env.bare, "main", existing)

    _leak_an_undeclared_edit_into_the_meeting_flow(monkeypatch, existing, _append_callout)

    _, result = _file_meeting(clean_queue, deps, _REVISIT_MATERIAL)

    assert result.status == schema.FILED, (
        f"an undeclared but genuinely additive edit was refused: findings={result.findings!r}, "
        f"report={result.report!r}. If this flow has deliberately regained a refusal for "
        f"undeclared modifications, this test is the record of the posture that changed — replace "
        f"it with the test of the new control rather than relaxing the assertion")
    _, meeting_sha = result.result_ref.rsplit("@", 1)
    rows = support.changed_paths_with_status(env.repo, meeting_sha)
    assert ("M", existing) in rows, (
        f"the additive edit did not land as a modification in the meeting's own commit: {rows} — "
        f"without that, this test proves nothing about a status-M entry reaching the gates")
    after = support.read_filed_page(env.repo, meeting_sha, existing)
    assert after.startswith(before.rstrip("\n")), (
        "the leaked edit was NOT additive, so `gate_body_rewrite` would be refusing it as a rewrite "
        "and this test would be demonstrating that gate instead of the posture it names")
    assert result.report.get("pages_edited") == [], (
        f"the report names a page no declaration asked for: "
        f"pages_edited={result.report.get('pages_edited')!r} — `pages_edited` is what "
        f"`edits.apply_declared` wrote, and an undeclared edit is by construction not in it")


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
