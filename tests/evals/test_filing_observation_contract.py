"""`run_filing._observe` against REAL reports and a REAL commit — the join the scorer cannot see.

`test_filing_scorer.py` proves the scorer discriminates; every `observed` dict there is written by
hand. That is exactly the shape of a yardstick that lies: rename `pages_edited` in `report.py`, or
write the proposed identities under a key the instrument does not read, and the scorer tests stay
green while the facet reports 0.00 for every backend forever — a number that reads as a failing
model and is actually a broken instrument. It would be discovered after paying for a real run, and
nothing in the table would say so.

That is not hypothetical, and this file is where it was caught twice. The plural collapse retired
`report.needs_input`'s singular ask key and the two park cases here had to move; ADR 041 then
retired `needs_input` itself, and `proposals` reads `entities_proposed` off the SAME reports that
carry `page_path` and `pages_edited` — a rename there is exactly as invisible and exactly as
expensive, so the cases that moved were replaced rather than dropped.

So nothing is canned here. Reports come from `librarian.report`'s own builders, the pages out of a
REAL `git` commit through the same `support.read_filed_page` a real run uses, and the expectations
are the golden set's own — read from `evals/filing/expected/expectations.json` rather than
restated. Keyless and Postgres-free: report building, `git` and frontmatter parsing need neither.
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


# ── the refusal that commits nothing to read back, and the identities a filing creates ────────

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


def test_the_identity_a_filing_proposed_is_observed_off_the_reports_own_key(filed, expectations):
    """**REPLACES the two park cases.** `report.needs_input` used to carry the names a capture had
    stopped on; ADR 041 removed the function with the state, and the same judgment now arrives as
    `entities_proposed` on an ORDINARY `report.filed` — the identities the filing created
    unconfirmed, in the commit that landed the page.

    That move is the reason this test exists rather than a scorer-level one. `entities_proposed` is
    one key on a report the instrument already reads for four other facets, so a rename there is
    silent: `proposals` would score 0.00 for every backend forever while the table read as a model
    that never recognises anything, which is precisely the shape of the failure this file is for.

    Built from the REAL builder and scored against the golden set's own expectation, end to end.
    """
    env, sha = filed
    entry = expectations["F02-unknown-entity-proposed"]
    rep = report_module.filed(
        page_path=NOTE, commit=sha, anchoring={"kind": "entity", "entities": ["Halcyon Grid"]},
        links=[], overlaps=[], findings=[],
        entities_proposed=[{"id": "halcyon-grid", "name": "Halcyon Grid", "type": "organization"}])

    observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), 1, env)

    assert observed["proposals"] == ["Halcyon Grid"]
    assert run_filing.score_phase(entry["expect"], observed)["proposals"] is True


def test_a_filing_that_proposed_nothing_observes_an_empty_list_rather_than_a_missing_key(filed):
    """The benign twin, and the state twelve of the fourteen captures are in: an ordinary filing
    proposes no identity, and the key has to be there and empty. `score_phase` reads it with a
    `or []` fallback either way, so what this really pins is that the ordinary road did not grow a
    proposal nobody declared — a `proposals` facet that scored non-empty for every capture would
    read as a backend inventing identities."""
    env, sha = filed
    rep = report_module.filed(page_path=NOTE, commit=sha,
                              anchoring={"kind": "entity", "entities": ["Northwind Freight"]},
                              links=[], overlaps=[], findings=[])

    observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), 1, env)

    assert observed["proposals"] == []
    assert run_filing.score_phase({"proposals": ["Halcyon Grid"]},
                                  observed)["proposals"] is False


def test_a_proposed_SPELLING_is_observed_beside_the_score_and_never_inside_it(filed):
    """The near miss a red `proposals` cell needs in front of it, and the one thing that must not be
    folded into the facet: a filing that read the name as a REGISTERED entity's spelling proposed an
    alias instead of an identity. That is a different — and often correct — outcome, so it is
    reported and never scored. Folding it in would let a backend that recognised nothing new score
    the facet by proposing spellings."""
    env, sha = filed
    rep = report_module.filed(
        page_path=NOTE, commit=sha, anchoring={"kind": "entity", "entities": ["Northwind Freight"]},
        links=[], overlaps=[], findings=[],
        aliases_proposed=[{"entity": "northwind-freight", "alias": "Northwind"}])

    observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), 1, env)

    assert observed["proposed_aliases"] == ["northwind-freight: Northwind"]
    assert observed["proposals"] == []
    assert run_filing.score_phase({"proposals": ["Northwind"]}, observed)["proposals"] is False


# ── the meeting: a page SET, each decision anchoring on its own ────────────────────────────────

def _meeting_report(sha, *, entities_proposed=()):
    return report_module.filed_meeting(
        source_pages=[SOURCE], meeting_page=MEETING, commit=sha,
        decisions=[{"path": ENTITY_DECISION,
                    "anchoring": {"kind": "entity", "entities": ["Northwind Freight"]}},
                   {"path": COMPANY_DECISION,
                    "anchoring": {"kind": "company", "reason": "applies everywhere"}}],
        entities_proposed=list(entities_proposed))


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


def test_a_meeting_proposes_through_the_same_key_the_fast_lane_does(filed, expectations):
    """The meeting half of the proposal observation, and the point ADR 041 argues: the same input
    must not behave differently per door. `filed_meeting` is a different builder from `filed` and it
    carries `entities_proposed` under the same name, so one read serves both flows — which is what
    lets F09 be scored on the facet F02 is scored on rather than on a meeting-shaped variant of it.

    Only the `proposals` cell of F09's expectation is asserted, and that is a property of THIS
    fixture rather than a hedge: the commit above holds F08's page set, whose decision titles are
    the ones `test_a_filed_meeting_meets_F08s_expectation_with_each_decisions_own_anchor` scores in
    full. Scoring F09's `decisions` here would be asserting that F08's pages are F09's.
    """
    env, sha = filed
    entry = expectations["F09-meeting-proposes"]
    rep = _meeting_report(sha, entities_proposed=[{"id": "project-wren", "name": "Project Wren",
                                                   "type": "project"}])

    observed = _observe(_result(schema.FILED, f"{MEETING}@{sha}", rep), 1, env)

    assert observed["proposals"] == ["Project Wren"]
    assert run_filing.score_phase(entry["expect"], observed)["proposals"] is True


# **DELETED with the `reuse` facet (ADR 041):**
# `test_a_meeting_that_lost_a_decision_on_the_way_back_is_observed_as_not_preserved`,
# `test_a_meeting_that_re_filed_its_parked_distillation_intact_is_observed_as_preserved`,
# `test_a_re_file_that_carried_no_stored_distillation_is_credited_with_preserving_one`,
# `test_the_row_says_whether_there_was_anything_to_preserve_in_the_first_place` and
# `test_the_at_risk_flag_is_absent_from_a_phase_that_filed_no_meeting`. All five pinned the
# observation contract between `processing._reuse_note` and `run_filing._observe_reuse`, key for
# key, and asked whether a meeting re-filed after a park had lost a decision on the way back.
# Neither function exists: a meeting is never re-filed, so nothing makes the round trip and there is
# no distillation stored between two passes to preserve. The property they protected — a decision
# that went missing must be visible — belongs to `decisions`, which counts the pages that landed.


# ── the cost axes, counted at the seam rather than read from a report ──────────────────────────

@pytest.mark.parametrize("attempts, bounces", [(0, 0), (1, 0), (2, 1)])
def test_bounces_are_the_agent_passes_beyond_the_first(filed, attempts, bounces):
    """`report.filed` does not carry the agent's attempt count — that is why `CountingAgent` wraps
    the injected `Deps.agent` instead, and why no production code changed to build this eval. A
    corrective retry is the second and last pass an item may spend; a refusal decided before the
    agent ran spends none."""
    env, sha = filed
    rep = report_module.filed(page_path=NOTE, commit=sha, anchoring={"kind": "company"},
                              links=[], overlaps=[], findings=[])
    observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), attempts, env)
    assert (observed["attempts"], observed["bounces"]) == (attempts, bounces)
