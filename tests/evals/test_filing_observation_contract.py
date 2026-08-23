"""`run_filing._observe` against REAL reports and a REAL commit — the join the scorer cannot see.

`test_filing_scorer.py` proves the scorer discriminates; every `observed` dict there is written by
hand. That is exactly the shape of a yardstick that lies: rename `pages_filed` in `report.py`, or
write the identities it introduced under a key the instrument does not read, and the scorer tests stay
green while the facet reports 0.00 for every backend forever — a number that reads as a failing
model and is actually a broken instrument. It would be discovered after paying for a real run, and
nothing in the table would say so.

That is not hypothetical, and this file is where it was caught three times. The plural collapse
retired `report.needs_input`'s singular ask key and the two park cases here had to move; the
file-first write path then retired `needs_input` itself, and `proposals` reads `entities_born` off
the SAME reports that carry `page_path` and `pages_filed` — a rename there is exactly as invisible
and exactly as expensive. The third was `filed_meeting`: the one pipe deleted that builder, `_observe`
kept reading the key, and the two cases below were left FAILING as the alarm rather than deleted,
because a green suite over a facet that scores 0.00 forever is the failure this file exists for.
Each time the cases that moved were replaced rather than dropped.

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
# What a transcript establishes, filed as the ordinary pages it is. These were a `decision` pair in
# `wiki/decisions/` and a `meeting` page above them until the one pipe retired both types: a meeting
# is an EVENT, so its transcript is archived and what it settled is filed like anything else.
COMPANY_CONCLUSION = "wiki/notes/Shared review checklist scope.md"
ENTITY_CONCLUSION = "wiki/notes/Northwind second wave goes depot by depot.md"
# The verbatim archive. Written by code from the captured material for EVERY capture, and
# deliberately NOT among the pages a capture declares.
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
            (COMPANY_CONCLUSION, _page("note", "Shared review checklist scope", [])),
            (ENTITY_CONCLUSION, _page("note", "Northwind second wave goes depot by depot",
                                      ["northwind-freight"])),
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

    F03 is the richest fast-lane case: a note filed with an entity anchor against material that
    continues an account an existing page already holds. If any of these keys ever stops lining up,
    the golden set's expectations become unreachable and the instrument reports a backend that
    cannot file.
    """
    env, sha = filed
    entry = expectations["F03-declared-edit-related-growth"]
    rep = report_module.filed(page_path=NOTE, commit=sha,
                              anchoring={"kind": "entity", "entities": ["Northwind Freight"]},
                              links=[], overlaps=[], findings=[],
                              agent_rationale="continues the onboarding account")

    observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), 1, env)

    assert observed["type"] == "note" and observed["folder"] == "wiki/notes"
    assert observed["anchor"] == {"kind": "entity", "ids": ["northwind-freight"]}
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
    distinction is the whole point of F05: anchoring a company-wide conclusion to whichever
    organization the corpus talks about most is the tempting failure.

    This is the benign twin of the unreadable-page test below: `company` has to keep meaning
    company for a page that genuinely says so, or the fix that stopped an absent page scoring as
    company-wide would have made F05 unreachable instead.

    F05's page is a `note` in `wiki/notes/` and was a `decision` in `wiki/decisions/` until the one
    pipe retired that type. Only the placement moved — the anchoring judgment this case exists for
    is the same one.
    """
    env, sha = filed
    entry = expectations["F05-company-wide-decision"]
    rep = report_module.filed(page_path=COMPANY_CONCLUSION, commit=sha,
                              anchoring={"kind": "company", "reason": "applies everywhere"},
                              links=[], overlaps=[], findings=[])

    observed = _observe(_result(schema.FILED, f"{COMPANY_CONCLUSION}@{sha}", rep), 1, env)

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
    ghost = "wiki/notes/Never committed.md"
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
    stopped on; the file-first write path removed the function with the state, and the same judgment now arrives as
    `entities_born` on an ORDINARY `report.filed` — the identities the filing created
    unconfirmed, in the commit that landed the page.

    That move is the reason this test exists rather than a scorer-level one. `entities_born` is
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
        entities_born=[{"id": "halcyon-grid", "name": "Halcyon Grid", "type": "organization"}])

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


def test_an_ADDED_SPELLING_is_observed_beside_the_score_and_never_inside_it(filed):
    """The near miss a red `proposals` cell needs in front of it, and the one thing that must not be
    folded into the facet: a filing that read the name as a REGISTERED entity's spelling taught the
    registry an alias instead of introducing an identity. That is a different — and often correct —
    outcome, so it is reported and never scored. Folding it in would let a backend that recognised
    nothing new score the facet by adding spellings."""
    env, sha = filed
    rep = report_module.filed(
        page_path=NOTE, commit=sha, anchoring={"kind": "entity", "entities": ["Northwind Freight"]},
        links=[], overlaps=[], findings=[],
        aliases_added=[{"entity": "northwind-freight", "alias": "Northwind"}])

    observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), 1, env)

    assert observed["added_aliases"] == ["northwind-freight: Northwind"]
    assert observed["proposals"] == []
    assert run_filing.score_phase({"proposals": ["Northwind"]}, observed)["proposals"] is False


# ── a capture that establishes several pages, each anchoring on its own ───────────────────────

def _multi_page_report(sha, *, entities_born=()):
    """The report a TRANSCRIPT leaves now: an ordinary `report.filed`, declaring both pages it
    established in `pages_filed` and its verbatim archive in `source_pages`.

    OLD BEHAVIOUR: this called `report.filed_meeting`, a second builder with its own `meeting_page`
    and `decisions` keys. There is one builder because there is one pipe, which is the whole claim
    these two tests exist to keep honest — a transcript's report has to be readable by the same
    code that reads a typed note's, or the instrument grows a meeting-shaped branch again.
    """
    return report_module.filed(
        page_path=ENTITY_CONCLUSION, commit=sha,
        pages_filed=[ENTITY_CONCLUSION, COMPANY_CONCLUSION],
        source_pages=[SOURCE],
        anchoring={"kind": "entity", "entities": ["Northwind Freight"]},
        links=[], overlaps=[], findings=[], entities_born=list(entities_born))


def test_a_transcript_meets_F08s_expectation_with_each_pages_own_anchor(filed, expectations):
    """The property a single-anchor implementation cannot express: one of these two conclusions
    belongs to an entity and the other to nobody, and each anchor is read from that page's OWN
    committed frontmatter rather than from the first page's or from the report's prose.

    **REPLACES `test_a_filed_meeting_meets_F08s_expectation_with_each_decisions_own_anchor`,** which
    read `report['filed_meeting']['decisions']` and observed a `meeting` page's own type and folder.
    Neither exists: `filed_meeting` was deleted with the flow, `meeting` and `decision` are types no
    flow can create, and the pages a transcript establishes are ordinary `note`s in `wiki/notes/`.
    The join being pinned is unchanged and is the expensive one — `_observe` reads `pages_filed`,
    `report.filed` writes it, and a rename on either side would score F08 and F09 0.00 for every
    backend forever while the table read as a model that cannot file a transcript.
    """
    env, sha = filed
    entry = expectations["F08-meeting-two-decisions"]

    observed = _observe(_result(schema.FILED, f"{ENTITY_CONCLUSION}@{sha}",
                                _multi_page_report(sha)), 1, env)

    assert observed["type"] == "note" and observed["folder"] == "wiki/notes"
    assert observed["pages"] == [
        {"path": ENTITY_CONCLUSION, "anchor": {"kind": "entity", "ids": ["northwind-freight"]}},
        {"path": COMPANY_CONCLUSION, "anchor": {"kind": "company", "ids": []}}]
    assert run_filing.score_phase(entry["expect"], observed) == \
           dict.fromkeys(entry["expect"], True)


def test_the_verbatim_archive_is_observed_beside_the_page_set_and_never_inside_it(filed):
    """`sources/` is the captured material, not something the capture established, and the two are
    separate keys on the report for exactly that reason.

    Folded into `pages_filed` it would break the `pages` facet in the direction nobody
    investigates: F08 expects two pages and would observe three, so a CORRECT filing would score a
    granularity miss. It is observed and never scored — WHERE the archive lands is
    `processing._source_attachment`'s answer to the capture's kind with no agent judgment in it, so
    a facet over it would be a cell that can never fail.
    """
    env, sha = filed
    observed = _observe(_result(schema.FILED, f"{ENTITY_CONCLUSION}@{sha}",
                                _multi_page_report(sha)), 1, env)

    assert observed["source_pages"] == [SOURCE]
    assert SOURCE not in [page["path"] for page in observed["pages"]]
    assert "source_pages" not in run_filing.FACETS, (
        "the archive is reported, never scored: which folder it lands in is code's decision alone")
    leaked = dict(observed, pages=[*observed["pages"], {"path": SOURCE, "anchor": {"kind": "company",
                                                                                   "ids": []}}])
    assert run_filing.score_phase({"pages": [{"anchor": {"kind": "entity",
                                                        "ids": ["northwind-freight"]}},
                                             {"anchor": {"kind": "company", "ids": []}}]},
                                  leaked)["pages"] is False


def test_an_ordinary_capture_observes_the_one_page_it_filed_rather_than_no_pages(filed):
    """The benign twin, and the state twelve of the fourteen captures are in. `report.filed` fills
    `pages_filed` from the account's declaration and falls back to `[page_path]`, so a capture that
    established one page observes a one-entry set — never an empty one, which would make every
    `pages` expectation fail for a reason that has nothing to do with the backend."""
    env, sha = filed
    rep = report_module.filed(page_path=NOTE, commit=sha,
                              anchoring={"kind": "entity", "entities": ["Northwind Freight"]},
                              links=[], overlaps=[], findings=[])

    observed = _observe(_result(schema.FILED, f"{NOTE}@{sha}", rep), 1, env)

    assert observed["pages"] == [
        {"path": NOTE, "anchor": {"kind": "entity", "ids": ["northwind-freight"]}}]
    assert observed["source_pages"] == []


def test_a_transcript_proposes_through_the_same_key_a_typed_note_does(filed, expectations):
    """The point the one pipe argues, made falsifiable: the same input must not behave differently
    per door. F09 arrives as `kind="meeting"` and F02 as `kind="raw"`, and both leave their proposal
    on `entities_born` of the SAME `report.filed` — which is what lets F09 be scored on the facet
    F02 is scored on rather than on a transcript-shaped variant of it.

    **REPLACES `test_a_meeting_proposes_through_the_same_key_the_fast_lane_does`,** whose whole
    subject was that a SECOND builder (`report.filed_meeting`) carried `entities_born` under the
    same name. There is no second builder, so what is left to prove is that a multi-page filing
    still carries it — a report shape that grew a page list is exactly where a key gets dropped.

    Only the `proposals` cell of F09's expectation is asserted, and that is a property of THIS
    fixture rather than a hedge: the commit above holds F08's page set, whose titles
    `test_a_transcript_meets_F08s_expectation_with_each_pages_own_anchor` scores in full. Scoring
    F09's `pages` here would be asserting that F08's pages are F09's.
    """
    env, sha = filed
    entry = expectations["F09-meeting-proposes"]
    rep = _multi_page_report(sha, entities_born=[{"id": "project-wren", "name": "Project Wren",
                                                  "type": "project"}])

    observed = _observe(_result(schema.FILED, f"{ENTITY_CONCLUSION}@{sha}", rep), 1, env)

    assert observed["proposals"] == ["Project Wren"]
    assert run_filing.score_phase(entry["expect"], observed)["proposals"] is True


# **DELETED with the `reuse` facet:**
# `test_a_meeting_that_lost_a_decision_on_the_way_back_is_observed_as_not_preserved`,
# `test_a_meeting_that_re_filed_its_parked_distillation_intact_is_observed_as_preserved`,
# `test_a_re_file_that_carried_no_stored_distillation_is_credited_with_preserving_one`,
# `test_the_row_says_whether_there_was_anything_to_preserve_in_the_first_place` and
# `test_the_at_risk_flag_is_absent_from_a_phase_that_filed_no_meeting`. All five pinned the
# observation contract between `processing._reuse_note` and `run_filing._observe_reuse`, key for
# key, and asked whether a meeting re-filed after a park had lost a decision on the way back.
# Neither function exists: a meeting is never re-filed, so nothing makes the round trip and there is
# no distillation stored between two passes to preserve. The property they protected — a conclusion
# that went missing must be visible — belongs to `pages`, which counts the pages that landed.


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
