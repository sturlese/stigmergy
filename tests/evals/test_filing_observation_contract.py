"""`run_filing._observe` against REAL reports and a REAL commit — the join the scorer cannot see.

`test_filing_scorer.py` proves the scorer discriminates; every `observed` dict there is written by
hand. That is exactly the shape of a yardstick that lies: rename `pages_edited` in `report.py`, or
park through a builder that writes `unresolved_names` where the instrument reads `unresolved_name`,
and the scorer tests stay green while the facet reports 0.00 for every backend forever — a number
that reads as a failing model and is actually a broken instrument. It would be discovered after
paying for a real run, and nothing in the table would say so. That second example is not
hypothetical: the plural collapse retired the singular ask key, and the two park cases below are
the ones that had to move.

So nothing is canned here. Reports come from `librarian.report`'s own builders, the reuse block
from `processing._reuse_note`, the pages out of a REAL `git` commit through the same
`support.read_filed_page` a real run uses, and the expectations are the golden set's own — read
from `evals/filing/expected/expectations.json` rather than restated. Keyless and Postgres-free:
report building, `git` and frontmatter parsing need neither.
"""
import json
from pathlib import Path

import pytest

from evals import run_filing
from stigmergy.capture import schema
from stigmergy.kernel.frontmatter import split_frontmatter
from stigmergy.librarian import gitcmd
from stigmergy.librarian import report as report_module
from tests.librarian import support

ROOT = Path(__file__).resolve().parents[2]
EXPECTATIONS = ROOT / "evals" / "filing" / "expected" / "expectations.json"

COMMIT_ENV = {"GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@stigmergy.test",
              "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@stigmergy.test"}

NOTE = "wiki/notes/Northwind second wave depot sequencing.md"
COMPANY_DECISION = "wiki/decisions/Shared review checklist scope.md"
ENTITY_DECISION = "wiki/decisions/Northwind second wave goes depot by depot.md"
MEETING = "wiki/meetings/2026-07-22 Rollout and review sync.md"
SOURCE = "sources/meetings/2026-07-22-rollout-and-review-sync.md"


def _page(page_type: str, title: str, entity: list) -> str:
    return (f"---\ntype: {page_type}\ntitle: \"{title}\"\nstatus: developing\n"
            f"created: 2026-07-22\nupdated: 2026-07-22\nentity: {json.dumps(entity)}\n"
            f"related: []\nsources: []\n---\n\n# {title}\n\nBody.\n")


@pytest.fixture(scope="module")
def filed(tmp_path_factory):
    """One real commit holding the page set these tests read back.

    Real git, for the reason the librarian suite gives: a faked `git show` would prove nothing
    about the property being claimed — that what a reader of the knowledge repo will actually see
    is what got scored.
    """
    repo = str(tmp_path_factory.mktemp("filed-repo"))
    gitcmd.run("init", "--quiet", "-b", "main", repo)
    for path, text in (
            (NOTE, _page("note", "Northwind second wave depot sequencing", ["northwind-freight"])),
            (COMPANY_DECISION, _page("decision", "Shared review checklist scope", [])),
            (ENTITY_DECISION, _page("decision", "Northwind second wave goes depot by depot",
                                    ["northwind-freight"])),
            (MEETING, _page("meeting", "Rollout and review sync", [])),
            (SOURCE, _page("source", "Rollout and review sync transcript", []))):
        full = Path(repo) / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
    gitcmd.run("add", "-A", cwd=repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", "file the golden page set", cwd=repo,
              env=COMMIT_ENV)
    sha = gitcmd.run("rev-parse", "HEAD", cwd=repo).stdout.strip()
    return support.RepoEnv(bare="", repo=repo), sha


@pytest.fixture(scope="module")
def expectations():
    entries = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))["expectations"]
    return {entry["id"]: entry for entry in entries}


def _observe(result, attempts, env):
    """Exactly the call `run_filing._drive` makes — same helpers, same real modules."""
    return run_filing._observe(result, attempts, env=env, support=support,
                               split_frontmatter=split_frontmatter)


def _result(status, ref, report):
    """The real `processing.Result`. Imported here rather than at module scope: it is the one
    import in this file that pulls the Postgres driver in, and nothing else here needs it."""
    from stigmergy.librarian.processing import Result
    return Result(status, ref, report)


# ── the fast lane ──────────────────────────────────────────────────────────────────────────────

def test_a_filed_note_meets_the_golden_sets_own_expectation_end_to_end(filed, expectations):
    """`report.filed` + a real commit -> every facet F03 names, True.

    F03 is the richest fast-lane case: a note, an entity anchor, and one declared edit performed
    on somebody else's page. If any of these five keys ever stops lining up, the golden set's
    expectations become unreachable and the instrument reports a backend that cannot file.
    """
    env, sha = filed
    entry = expectations["F03-declared-edit-related-growth"]
    edited = entry["expect"]["edits"]
    rep = report_module.filed(page_path=NOTE, commit=sha,
                              anchoring={"kind": "entity", "entities": ["Northwind Freight"]},
                              links=[], overlaps=[], findings=[], pages_edited=list(edited),
                              agent_rationale="continues the onboarding account")

    observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), 1, env)

    assert observed["type"] == "note" and observed["folder"] == "wiki/notes"
    assert observed["anchor"] == {"kind": "entity", "ids": ["northwind-freight"]}
    assert observed["edits"] == list(edited)
    assert run_filing.score_phase(entry["expect"], observed) == \
           dict.fromkeys(entry["expect"], True)


def test_the_anchor_is_read_off_the_page_and_not_off_the_reports_prose(filed):
    """The design claim, made falsifiable: the report here SAYS Quillon Labs while the page that
    landed carries `entity: ["northwind-freight"]`, and the scorer follows the page.

    `report["anchored_to"]` is a rendered sentence for a human — it can carry the agent's own
    spelling of a name, and scoring it would make the instrument depend on `report.py`'s wording.
    The page's `entity:` is what `processing._stamp` wrote from `gates.resolve_entity_ids`.
    """
    env, sha = filed
    rep = report_module.filed(page_path=NOTE, commit=sha,
                              anchoring={"kind": "entity", "entities": ["Quillon Labs"]},
                              links=[], overlaps=[], findings=[])
    observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), 1, env)
    assert "Quillon" in observed["anchored_to"]
    assert observed["anchor"] == {"kind": "entity", "ids": ["northwind-freight"]}


def test_a_company_wide_page_is_observed_as_company_and_not_as_nothing(filed, expectations):
    """`entity: []` on a page that FILED can only be the company-wide outcome — `gate_anchoring`
    refuses to let a page whose declared entity did not resolve reach a commit at all. The
    distinction is the whole point of F05: anchoring a company-wide decision to whichever
    organization the corpus talks about most is the tempting failure.

    This is the benign twin of the unreadable-page test below: `company` has to keep meaning
    company for a page that genuinely says so, or the fix that stopped an absent page scoring as
    company-wide would have made F05 unreachable instead.
    """
    env, sha = filed
    entry = expectations["F05-company-wide-decision"]
    rep = report_module.filed(page_path=COMPANY_DECISION, commit=sha,
                              anchoring={"kind": "company", "reason": "applies everywhere"},
                              links=[], overlaps=[], findings=[])

    observed = _observe(_result(schema.FILED, f"{COMPANY_DECISION}@{sha}", rep), 1, env)

    assert observed["anchor"] == {"kind": "company", "ids": []}
    assert run_filing.score_phase(entry["expect"], observed) == \
           dict.fromkeys(entry["expect"], True)


def test_the_page_is_read_at_the_sha_in_result_ref_and_never_at_the_branch_tip(filed):
    """A filed meeting triggers view regeneration, which pushes a SECOND commit on top. Reading
    the tip instead of `result_ref` has bitten this repo before, and here it would score a page
    the capture never wrote.

    Simulated the only way that is honest: a real second commit that rewrites the filed page.
    """
    env, sha = filed
    page = Path(env.repo) / NOTE
    original = page.read_text(encoding="utf-8")
    page.write_text(_page("concept", "Rewritten by a later commit", ["quillon-labs"]),
                    encoding="utf-8")
    gitcmd.run("add", "-A", cwd=env.repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", "a later, unrelated commit",
              cwd=env.repo, env=COMMIT_ENV)
    try:
        rep = report_module.filed(page_path=NOTE, commit=sha,
                                  anchoring={"kind": "entity", "entities": ["Northwind Freight"]},
                                  links=[], overlaps=[], findings=[])
        observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), 1, env)
    finally:
        page.write_text(original, encoding="utf-8")
        gitcmd.run("add", "-A", cwd=env.repo)
        gitcmd.run("commit", "--quiet", "--no-verify", "-m", "restore", cwd=env.repo,
                  env=COMMIT_ENV)
    assert observed["type"] == "note"
    assert observed["anchor"] == {"kind": "entity", "ids": ["northwind-freight"]}


def test_a_page_absent_from_the_commit_can_never_score_as_a_company_wide_filing(filed,
                                                                               expectations):
    """The instrument's fault must not be able to buy a facet.

    A page named by the result and absent from the commit is an instrument or a filing fault, and
    an empty frontmatter reads exactly like `entity: []` — which on a page that really filed IS the
    company-wide outcome. Reported as `company`, an unreadable page would hand F05 — the one
    capture whose correct answer is company-wide — a wrong-but-lucky hit, in the direction nobody
    investigates. `unreadable` is a kind no expectation names, so it misses against every anchor
    expectation there is.

    An unreadable page still has to be an OBSERVATION and not an exception: a run that died on
    phase three would lose the captures behind it, and each of those costs a real agent pass.
    """
    env, sha = filed
    ghost = "wiki/decisions/Never committed.md"
    rep = report_module.filed(page_path=ghost, commit=sha,
                              anchoring={"kind": "company", "reason": "applies everywhere"},
                              links=[], overlaps=[], findings=[])

    observed = _observe(_result(schema.FILED, f"{ghost}@{sha}", rep), 1, env)

    assert observed["type"] == ""
    assert observed["anchor"] == {"kind": "unreadable", "ids": []}
    scored = run_filing.score_phase(expectations["F05-company-wide-decision"]["expect"], observed)
    assert scored["anchor"] is False and scored["type"] is False
    assert run_filing.score_phase({"anchor": {"kind": "entity", "ids": ["northwind-freight"]}},
                                  observed)["anchor"] is False


# ── the refusals and the park, which commit nothing to read back ──────────────────────────────

def test_the_duplicate_refusal_carries_the_reason_code_F04_is_scored_on(filed, expectations):
    """The one facet that must NOT move between backends: it is decided before any agent runs. A
    change here means the dedup levels moved, not the model — which is only true while the
    instrument reads the same `reason_code` the refusal writes."""
    env, _ = filed
    entry = expectations["F04-duplicate-rejection"]
    rep = report_module.rejected_duplicate(page_path=NOTE, as_of="2026-07-22")

    observed = _observe(_result(schema.REJECTED, "", rep), 0, env)

    assert observed["reason"] == schema.REASON_DUPLICATE
    assert run_filing.score_phase(entry["expect"], observed) == \
           dict.fromkeys(entry["expect"], True)


def test_a_park_on_one_name_is_observed_through_the_reports_plural_key(filed, expectations):
    """INVERTED by the plural collapse. `report.needs_input` used to write the singular
    `unresolved_name` for a one-name park and this test was named for that; it now writes
    `unresolved_names`, a one-element list, whatever the count. The instrument's job is unchanged
    and is why this test exists: a park it observed nothing from scores every `park_question` cell
    a miss while the backend did exactly the right thing, and that reads as a failing model rather
    than a broken yardstick.

    Built from the REAL builder, never a canned dict — which is what makes this the test that
    catches the rename."""
    env, _ = filed
    entry = expectations["F02-unknown-entity-parks"]
    rep = report_module.needs_input(
        submission_id=17, names=["Halcyon Grid"],
        candidates=[{"id": "northwind-freight", "name": "Northwind Freight", "aliases": []}],
        total_candidates=3)

    assert "unresolved_name" not in rep      # the shape the instrument is being read against
    observed = _observe(_result(schema.NEEDS_INPUT, "", rep), 1, env)

    assert observed["park_question"] == ["Halcyon Grid"]
    assert run_filing.score_phase(entry["expect"], observed) == \
           dict.fromkeys(entry["expect"], True)


def test_a_park_on_several_names_is_observed_through_the_reports_plural_key(filed):
    """One ask for all of them — a partial page set is worse than an honest park — so the
    instrument has to see every name the question covered."""
    env, _ = filed
    rep = report_module.needs_input(submission_id=18,
                                    names=["Project Wren", "Halcyon Grid"],
                                    candidates=[], total_candidates=0)
    observed = _observe(_result(schema.NEEDS_INPUT, "", rep), 1, env)
    assert observed["park_question"] == ["Project Wren", "Halcyon Grid"]
    assert run_filing.score_phase({"park_question": ["Project Wren", "Halcyon Grid"]},
                                  observed)["park_question"] is True


# ── the instrument's LEGACY read, which no builder can produce any more ────────────────────────
# `_observe` still reads `unresolved_name` first and falls back to `unresolved_names`. Nothing
# writes the singular key now, so the two tests above exercise only the fallback — the primary
# branch became unreachable from any builder on the same day, and an unreachable branch that reads
# as coverage is what this file's own docstring is about.
#
# The report here is therefore a LITERAL, deliberately not round-tripped through a builder: what it
# stands for is a report written by the OLD code and read back by today's instrument — a run
# scored from a stored result, or a queue row parked before the collapse. A round-trip version
# would silently stop testing the thing it names the moment the builder changed, which is exactly
# what just happened to the case above.
def test_a_legacy_park_report_carrying_the_retired_singular_key_is_still_observed(filed):
    env, _ = filed
    legacy = {"status": schema.NEEDS_INPUT, "unresolved_name": "Halcyon Grid"}

    observed = _observe(_result(schema.NEEDS_INPUT, "", legacy), 1, env)

    assert observed["park_question"] == ["Halcyon Grid"]


# ── the meeting: a page SET, each decision anchoring on its own ────────────────────────────────

def _meeting_report(sha, *, reuse=None):
    return report_module.filed_meeting(
        source_pages=[SOURCE], meeting_page=MEETING, commit=sha,
        decisions=[{"path": ENTITY_DECISION,
                    "anchoring": {"kind": "entity", "entities": ["Northwind Freight"]}},
                   {"path": COMPANY_DECISION,
                    "anchoring": {"kind": "company", "reason": "applies everywhere"}}],
        reuse=reuse)


def test_a_filed_meeting_meets_F08s_expectation_with_each_decisions_own_anchor(filed,
                                                                              expectations):
    """The property a single-anchor implementation cannot express: one of these two decisions
    belongs to an entity and the other to nobody, and each anchor is read from that decision's own
    page rather than from the meeting's."""
    env, sha = filed
    entry = expectations["F08-meeting-two-decisions"]

    observed = _observe(_result(schema.FILED, f"{MEETING}@{sha}", _meeting_report(sha)), 1, env)

    assert observed["type"] == "meeting" and observed["folder"] == "wiki/meetings"
    assert observed["decisions"] == [
        {"path": ENTITY_DECISION, "anchor": {"kind": "entity", "ids": ["northwind-freight"]}},
        {"path": COMPANY_DECISION, "anchor": {"kind": "company", "ids": []}}]
    assert observed["source_pages"] == [SOURCE]
    assert run_filing.score_phase(entry["expect"], observed) == \
           dict.fromkeys(entry["expect"], True)


def test_a_meeting_that_lost_a_decision_on_the_way_back_is_observed_as_not_preserved(filed):
    """The reuse block is built by `processing._reuse_note` — the production function — and read
    by the instrument. `preserved` is the scored half, and it is false exactly when a decision the
    parked pass had distilled is not in what filed.
    """
    from stigmergy.librarian.agent import MeetingOutcome
    from stigmergy.librarian.processing import _Reuse, _reuse_note

    env, sha = filed
    parked = ("Northwind second wave goes depot by depot", "Shared review checklist scope")
    refiled = MeetingOutcome(decisions=({"title": parked[0]},))
    reuse = _reuse_note(_Reuse(prior_titles=parked, reused=False), refiled)
    assert reuse["dropped"] == [parked[1]], "the production diff itself stopped reporting a loss"

    observed = _observe(_result(schema.FILED, f"{MEETING}@{sha}", _meeting_report(sha, reuse=reuse)),
                        1, env)

    assert observed["reuse"]["preserved"] is False
    assert observed["reuse"]["dropped"] == [parked[1]]
    assert run_filing.score_phase({"reuse": {"decisions_preserved": True}},
                                  observed)["reuse"] is False


def test_a_meeting_that_re_filed_its_parked_distillation_intact_is_observed_as_preserved(filed,
                                                                                        expectations):
    """The benign twin: the same machinery on the road where nothing was lost has to say so, or
    the facet is a permanent accusation rather than a measurement. `reused` is recorded beside it
    and deliberately not scored — an agent that parks the way the meeting brief asks stores no
    distillation at all, and marking that down would penalise following the brief."""
    from stigmergy.librarian.agent import MeetingOutcome
    from stigmergy.librarian.processing import _Reuse, _reuse_note

    env, sha = filed
    parked = ("Northwind second wave goes depot by depot", "Shared review checklist scope")
    reuse = _reuse_note(_Reuse(prior_titles=parked, reused=True),
                        MeetingOutcome(decisions=({"title": parked[0]}, {"title": parked[1]})))

    observed = _observe(_result(schema.FILED, f"{MEETING}@{sha}", _meeting_report(sha, reuse=reuse)),
                        0, env)

    assert observed["reuse"] == {"preserved": True, "reused": True, "redistilled": False,
                                 "dropped": []}
    assert run_filing.score_phase(expectations["F09-meeting-parks"]["after_reply"],
                                  observed)["reuse"] is True


def test_a_re_file_that_carried_no_stored_distillation_is_credited_with_preserving_one(filed):
    """Recorded here because it is the instrument's weakest cell, and a weakness that is written
    down is not a trap.

    An agent that parks the way the meeting brief asks (`decision: "triage"`) stores nothing, so
    the re-file carries no `distillation_reuse` block, and `preserved` is True by construction —
    there was nothing recorded that could be shown to have been lost. On that road it is the
    `decisions` facet, not this one, that catches a decision going missing: F09's `after_reply`
    names both decisions it must come back with.
    """
    env, sha = filed
    observed = _observe(_result(schema.FILED, f"{MEETING}@{sha}", _meeting_report(sha)), 1, env)
    assert observed["reuse"] == {"preserved": True, "reused": False, "redistilled": True,
                                 "dropped": []}
    assert run_filing.score_phase({"reuse": {"decisions_preserved": True}},
                                  observed)["reuse"] is True


def test_the_row_says_whether_there_was_anything_to_preserve_in_the_first_place(filed):
    """`reuse: 1.00` means EITHER "the capture kept what it had distilled" OR "there was never
    anything to lose", and those are different claims about a backend. `reuse_at_risk` is what
    tells the two apart for whoever reads the row six months from now — True exactly on the road
    where a stored distillation reached the re-file, False on the brief-following triage road
    where nothing was stored.

    It rides BESIDE the reuse block rather than inside it, deliberately: that block is the
    observation contract between `processing._reuse_note` and this instrument, pinned key for key
    two tests up, and this flag is the instrument's own reading rather than a field the production
    report grew.
    """
    from stigmergy.librarian.agent import MeetingOutcome
    from stigmergy.librarian.processing import _Reuse, _reuse_note

    env, sha = filed
    parked = ("Northwind second wave goes depot by depot", "Shared review checklist scope")
    stored = _reuse_note(_Reuse(prior_titles=parked, reused=True),
                         MeetingOutcome(decisions=({"title": parked[0]}, {"title": parked[1]})))

    at_risk = _observe(_result(schema.FILED, f"{MEETING}@{sha}",
                               _meeting_report(sha, reuse=stored)), 0, env)
    nothing_stored = _observe(_result(schema.FILED, f"{MEETING}@{sha}", _meeting_report(sha)),
                              1, env)

    assert at_risk["reuse_at_risk"] is True
    assert nothing_stored["reuse_at_risk"] is False
    # Both roads score the facet the same, which is exactly why the flag has to exist.
    assert at_risk["reuse"]["preserved"] == nothing_stored["reuse"]["preserved"] is True
    assert "reuse_at_risk" not in at_risk["reuse"], (
        "the reuse block is a contract with `processing._reuse_note` — the instrument's own "
        "reading belongs beside it, not inside it")


def test_the_at_risk_flag_is_absent_from_a_phase_that_filed_no_meeting(filed):
    """A fast-lane filing has no distillation to preserve and no block to read; the flag must not
    appear there claiming False about a question that was never asked."""
    env, sha = filed
    rep = report_module.filed(page_path=NOTE, commit=sha, anchoring={"kind": "company"},
                              links=[], overlaps=[], findings=[])
    observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), 1, env)
    assert "reuse_at_risk" not in observed and "reuse" not in observed


# ── the cost axes, counted at the seam rather than read from a report ──────────────────────────

@pytest.mark.parametrize("attempts, bounces", [(0, 0), (1, 0), (2, 1)])
def test_bounces_are_the_agent_passes_beyond_the_first(filed, attempts, bounces):
    """`report.filed` does not carry the agent's attempt count — that is why `CountingAgent` wraps
    the injected `Deps.agent` instead, and why no production code changed to build this eval. A
    corrective retry is the second and last pass an item may spend; a reuse spends none."""
    env, sha = filed
    rep = report_module.filed(page_path=NOTE, commit=sha, anchoring={"kind": "company"},
                              links=[], overlaps=[], findings=[])
    observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), attempts, env)
    assert (observed["attempts"], observed["bounces"]) == (attempts, bounces)
