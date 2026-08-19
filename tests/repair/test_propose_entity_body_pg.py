"""The `entity-body` proposer road: a placeholder entity page in, a drafted body out — or a
recorded reason why not.

The two properties this file exists for, both of them about what does NOT happen:

  · **an entity with fewer than two anchored pages never reaches the model.** A body drafted from
    nothing is the placeholder with better grammar, and it would cost a steward a decision to
    arrive at the state they were already in.
  · **the additive road is untouched.** Both kinds ride the same run, the same ceiling and the
    same dismissal memory, and a finding of one kind must never be answered in the other's
    vocabulary.

Real findings, a real checkout, the offline double for the model, and the real
`entity_body.validate` as the last thing between a draft and the table.
"""
import asyncio
import os

import pytest

from stigmergy.gardener import checks as gardener_checks
from stigmergy.gardener import sweep as gardener_sweep
from stigmergy.repair import entity_body, proposer, schema, store
from stigmergy.repair.settings import RepairSettings
from tests.repair import support


def _propose(conn, settings):
    return asyncio.run(proposer.propose_from_findings(conn, settings=settings))


def _seed(conn, repo_env, *, anchored: int = 2) -> tuple[int, int]:
    support.seed_entity(repo_env, anchored=anchored)
    run_id = support.seed_gardener_run(conn)
    return run_id, support.seed_placeholder_body(conn, run_id)


# ── the happy path ────────────────────────────────────────────────────────────────────────────
def test_a_placeholder_finding_with_anchored_pages_becomes_one_body_proposal(conn, repo_env,
                                                                             settings):
    run_id, finding_id = _seed(conn, repo_env)

    result = _propose(conn, settings)

    assert result.proposed == 1
    (row,) = store.pending_proposals(conn)
    assert row["kind"] == schema.KIND_ENTITY_BODY
    assert row["finding_ids"] == [finding_id]
    assert row["target_paths"] == [support.ENTITY_PAGE]
    assert row["finding_subjects"] == [[support.ENTITY_PAGE]]
    assert [op["op"] for op in row["ops"]] == [schema.KIND_ENTITY_BODY]
    assert row["ops"][0]["path"] == support.ENTITY_PAGE
    assert row["ops"][0]["body_markdown"].strip()
    assert row["content_key"] == schema.content_key(row["ops"], kind=schema.KIND_ENTITY_BODY)
    # The proposer READS. The placeholder is still on disk, untouched.
    assert "<One clear paragraph" in support.page_text(repo_env.repo, support.ENTITY_PAGE)


def test_the_stored_draft_still_applies_to_the_checkout_it_was_derived_from(conn, repo_env,
                                                                            settings):
    """The propose-time proof, asserted directly rather than through its consequence: what is on
    the table is what the applier would perform. Anything else is an Approve button that cannot
    work — and for this kind the failure would be discovered with the page's body already gone."""
    _seed(conn, repo_env)
    _propose(conn, settings)

    (row,) = store.pending_proposals(conn)
    assert entity_body.validate(repo_env.repo, row["ops"]) == []


def test_the_draft_cites_the_pages_it_was_drafted_from(conn, repo_env, settings):
    """Not a style preference: a body whose facts trace to nothing is exactly what a steward
    cannot check, and every wikilink in it has to resolve or the contract linter refuses the page
    at apply time."""
    _seed(conn, repo_env)
    _propose(conn, settings)

    (row,) = store.pending_proposals(conn)
    assert "[[Meridian Note 1]]" in row["ops"][0]["body_markdown"]


# ── the model finding rides the SAME road ─────────────────────────────────────────────────────
# `model-empty-entity-body` (#78) is the judgment twin of `entity-placeholder-body`: the page's body
# is written and says nothing about the entity rather than still being the template. One question,
# one answer, one road — so what these tests prove is that the second check reaches the road at all
# and is answered identically, not that a second road exists.
def _seed_written_but_empty(conn, repo_env, *, anchored: int = 2) -> tuple[int, int]:
    support.seed_entity(repo_env, anchored=anchored,
                        body=f"# {support.ENTITY_STEM}\n\n"
                             f"{support.ENTITY_STEM} is a company we work with.\n")
    run_id = support.seed_gardener_run(conn)
    return run_id, support.seed_empty_entity_body(conn, run_id)


def test_an_empty_body_finding_becomes_one_body_proposal(conn, repo_env, settings):
    """The end-to-end criterion the fifth check exists for: without this the finding has no path to
    zero and #78 only moved the problem to a report nobody can act on."""
    run_id, finding_id = _seed_written_but_empty(conn, repo_env)

    result = _propose(conn, settings)

    assert result.proposed == 1
    (row,) = store.pending_proposals(conn)
    assert row["kind"] == schema.KIND_ENTITY_BODY
    assert row["finding_ids"] == [finding_id]
    assert row["target_paths"] == [support.ENTITY_PAGE]
    assert row["finding_subjects"] == [[support.ENTITY_PAGE]]
    assert row["ops"][0]["path"] == support.ENTITY_PAGE
    assert row["ops"][0]["body_markdown"].strip()
    # The written-but-empty sentence is still on disk: the proposer READS.
    assert "is a company we work with" in support.page_text(repo_env.repo, support.ENTITY_PAGE)


def test_the_stored_draft_for_a_written_but_empty_page_still_applies_to_the_checkout(
        conn, repo_env, settings):
    """The kind's own validator has no placeholder precondition, and this is what says so: the
    body being REPLACED is somebody's prose rather than the template, and the applier would still
    perform exactly what is on the table."""
    _seed_written_but_empty(conn, repo_env)
    _propose(conn, settings)

    (row,) = store.pending_proposals(conn)
    assert entity_body.validate(repo_env.repo, row["ops"]) == []


def test_an_empty_body_finding_with_one_anchored_page_produces_no_draft_and_says_why(
        conn, repo_env, settings, monkeypatch):
    """The existing floor holds for the new check too, and stays where it is: `MIN_ANCHORED_PAGES`
    is enforced in the proposer BEFORE the model is asked, so the gardener never has to know about
    it and a reported page with too little evidence costs a recorded reason rather than a call."""
    def refuse(*args, **kwargs):
        raise AssertionError("the drafter was built for an entity with nothing to draft from")

    monkeypatch.setattr(proposer, "build_entity_body_drafter", refuse)
    _seed_written_but_empty(conn, repo_env, anchored=1)

    result = _propose(conn, settings)

    assert result.proposed == 0
    assert store.pending_proposals(conn) == []
    assert any("too-few-anchored-pages" in reason and support.ENTITY_PAGE in reason
               for reason in result.skip_reasons)


def test_both_body_checks_are_on_the_one_road_and_neither_is_on_the_additive_one():
    """Named directly rather than through a consequence: a finding rides exactly one road, and the
    two vocabularies do not mix."""
    expected = frozenset({gardener_checks.CHECK_ENTITY_PLACEHOLDER_BODY,
                          gardener_sweep.CHECK_MODEL_EMPTY_ENTITY_BODY})
    assert expected == proposer.BODY_PROPOSABLE_CHECKS
    assert not (proposer.BODY_PROPOSABLE_CHECKS & proposer.EDIT_PROPOSABLE_CHECKS)


def test_a_declined_draft_is_not_re_proposed_under_the_other_body_check(conn, repo_env, settings):
    """The dismissal memory keys on the PAGE (`finding_subjects`), not on the check that named it —
    so a steward who declined a drafted body is not asked the same question again the night the
    other half of the pair reports the same page."""
    _seed(conn, repo_env)
    _propose(conn, settings)
    (row,) = store.pending_proposals(conn)
    store.mark_decided(conn, row["id"], status=schema.STATUS_REJECTED, decided_by=support.STEWARD,
                       notes="the draft reads like a brochure")

    run_id = support.seed_gardener_run(conn)
    support.seed_empty_entity_body(conn, run_id)
    result = _propose(conn, settings)

    assert result.proposed == 0
    assert result.skipped_known == 1


SECOND_ENTITY_ID = "cofers"
SECOND_ENTITY_STEM = "Cofers"
SECOND_ENTITY_PAGE = f"wiki/entities/{SECOND_ENTITY_STEM}.md"


def _seed_a_second_entity(repo_env, *, anchored: int = 2) -> str:
    """A SECOND registered entity page, written-but-empty, with its own anchored notes.

    Its notes are written here rather than through `seed_entity(anchored=…)` because that helper
    names every note `Meridian Note N` — a second entity seeded through it would overwrite the
    first entity's evidence and quietly turn a two-page test into a one-page one.
    """
    support.seed_entity(repo_env, entity_id=SECOND_ENTITY_ID, stem_name=SECOND_ENTITY_STEM,
                        body=f"# {SECOND_ENTITY_STEM}\n\n{SECOND_ENTITY_STEM} is a company we "
                             f"work with.\n",
                        anchored=0, push=False)
    for n in range(anchored):
        support.write_anchored_note(repo_env, f"{SECOND_ENTITY_STEM} Note {n + 1}",
                                    entity_id=SECOND_ENTITY_ID, push=False)
    return SECOND_ENTITY_PAGE


def test_two_body_findings_naming_different_pages_produce_two_proposals(conn, repo_env, settings):
    """The body road is per PAGE, not per run: two entity pages reported the same night — one by
    each half of the pair — are two questions and get two drafts. A road that answered only the
    first would leave the second page reported and unrepaired for as long as the report kept
    naming it."""
    support.seed_entity(repo_env, anchored=2, push=False)
    second = _seed_a_second_entity(repo_env)
    run_id = support.seed_gardener_run(conn)
    support.seed_placeholder_body(conn, run_id)
    support.seed_empty_entity_body(conn, run_id, page=second)

    result = _propose(conn, settings)

    assert result.proposed == 2
    rows = store.pending_proposals(conn)
    assert {row["kind"] for row in rows} == {schema.KIND_ENTITY_BODY}
    assert sorted(row["target_paths"][0] for row in rows) == sorted([support.ENTITY_PAGE, second])
    assert all(len(row["ops"]) == 1 for row in rows)


def test_two_body_findings_still_share_the_one_run_ceiling(conn, repo_env, settings):
    """One night, one inbox — proven WITHIN the body road as well as across the two roads. Both
    findings are draftable and the ceiling is one, so exactly one draft is on the table and the
    run says the other page was not proposed rather than dropping it silently."""
    support.seed_entity(repo_env, anchored=2, push=False)
    second = _seed_a_second_entity(repo_env)
    run_id = support.seed_gardener_run(conn)
    support.seed_placeholder_body(conn, run_id)
    support.seed_empty_entity_body(conn, run_id, page=second)
    one = RepairSettings(repo=settings.repo, max_proposals_per_run=1)

    result = _propose(conn, one)

    assert result.proposed == 1
    assert len(store.pending_proposals(conn)) == 1
    assert any("run-ceiling-reached(1)" in reason and "1 further finding(s)" in reason
               for reason in result.skip_reasons)


def test_two_body_findings_naming_the_SAME_page_still_produce_one_draft_at_most(conn, repo_env,
                                                                                  settings):
    """The acceptance criterion's second half — "one draft at most" — asked of the road that would
    have to produce two.

    The gardener's structural exclusion is the FIRST line and the one that is meant to hold: these
    two findings cannot co-exist for one page in a real run
    (`sweep.select_empty_body_pages`). This test constructs the state anyway, because the criterion
    is about the DRAFT and the drafting happens one package away: `schema.content_key` is
    `kind+path` for this kind, so the second answer about the same page meets a key that is already
    on the table and is dropped with a recorded reason. Defence in depth, not a licence to drop the
    exclusion — if this ever starts producing two rows, the steward sees the same page twice in one
    inbox.
    """
    support.seed_entity(repo_env, anchored=2,
                        body=f"# {support.ENTITY_STEM}\n\n{support.ENTITY_STEM} is a company we "
                             f"work with.\n")
    run_id = support.seed_gardener_run(conn)
    support.seed_placeholder_body(conn, run_id)
    support.seed_empty_entity_body(conn, run_id)

    result = _propose(conn, settings)

    assert result.findings_seen == 2, "both findings DID reach the road"
    assert result.proposed == 1
    rows = store.pending_proposals(conn)
    assert [row["target_paths"] for row in rows] == [[support.ENTITY_PAGE]]
    assert any("content key already exists" in reason for reason in result.skip_reasons)


def test_an_empty_body_finding_for_a_page_since_deleted_costs_a_reason_not_a_crash(
        conn, repo_env, settings, monkeypatch):
    """The gardener reads the checkout at 03:00 and the proposer reads it again later; a steward
    can delete an entity page in between. The finding still names it, and the road must end in a
    recorded reason rather than a traceback that costs every OTHER finding in the run its draft.
    No model is asked either — there is nothing on disk to draft from."""
    def refuse(*args, **kwargs):
        raise AssertionError("the drafter was built for a page that no longer exists")

    monkeypatch.setattr(proposer, "build_entity_body_drafter", refuse)
    support.seed_entity(repo_env, anchored=2,
                        body=f"# {support.ENTITY_STEM}\n\n{support.ENTITY_STEM} is a company we "
                             f"work with.\n")
    os.remove(os.path.join(repo_env.repo, *support.ENTITY_PAGE.split("/")))
    run_id = support.seed_gardener_run(conn)
    support.seed_empty_entity_body(conn, run_id)

    result = _propose(conn, settings)

    assert result.proposed == 0
    assert store.pending_proposals(conn) == []
    assert any(support.ENTITY_PAGE in reason for reason in result.skip_reasons)
    assert result.run_id, "the run still records itself — one absent page is not a dead run"


# ── the pre-model skip: not enough to draft from ──────────────────────────────────────────────
def test_an_entity_with_one_anchored_page_never_reaches_the_model(conn, repo_env, settings,
                                                                  monkeypatch):
    """The model is not asked, and that is asserted by making the ask FAIL: a run that quietly
    called it and then discarded the answer would look identical from the outside, and would keep
    costing money every night for an entity nothing has been written about."""
    def refuse(*args, **kwargs):
        raise AssertionError("the drafter was built for an entity with nothing to draft from")

    monkeypatch.setattr(proposer, "build_entity_body_drafter", refuse)
    _seed(conn, repo_env, anchored=1)

    result = _propose(conn, settings)

    assert result.proposed == 0
    assert store.pending_proposals(conn) == []
    assert any("anchored" in reason for reason in result.skip_reasons)


def test_two_anchored_pages_is_the_floor_and_it_is_met(conn, repo_env, settings):
    """The benign twin of the skip: the bound is a floor, not a wall — exactly two pages is enough
    evidence to draft from, and a rule that demanded more would leave every young entity with a
    placeholder forever."""
    _seed(conn, repo_env, anchored=2)
    assert _propose(conn, settings).proposed == 1


# ── the two roads share one run ───────────────────────────────────────────────────────────────
def test_a_placeholder_finding_and_an_edits_finding_ride_the_same_run(conn, repo_env, settings):
    """Both kinds, one pass, one `job_runs` row. The additive road answers its finding in its own
    vocabulary and this one answers its own — a run that let either road see the other's findings
    would propose a backlink for a page with no body, or a body for two pages that fail to link."""
    run_id, _ = _seed(conn, repo_env)
    support.seed_unlinked_mention(conn, run_id)

    result = _propose(conn, settings)

    kinds = sorted(row["kind"] for row in store.pending_proposals(conn))
    assert kinds == [schema.KIND_EDITS, schema.KIND_ENTITY_BODY]
    assert result.findings_seen == 2


def test_the_run_ceiling_bounds_both_roads_together(conn, repo_env, settings):
    """One night, one inbox. The ceiling is how many decisions a run may ask a person for, so a
    second road that carried its own budget would double the number quietly."""
    run_id, _ = _seed(conn, repo_env)
    support.seed_unlinked_mention(conn, run_id)
    one = RepairSettings(repo=settings.repo, max_proposals_per_run=1)

    result = _propose(conn, one)

    assert result.proposed == 1
    assert any("ceiling" in reason for reason in result.skip_reasons)


# ── the draft is validated, and a bad one is skipped rather than stored ───────────────────────
def test_a_draft_that_does_not_validate_is_skipped_with_a_recorded_reason(conn, repo_env,
                                                                          monkeypatch):
    """`CLEAN_LLM=fake-flawed` drafts a body that keeps a placeholder line — the one failure this
    road exists to prevent, since a "repair" that re-states the template is worse than the finding.
    The retry gets the same answer, deterministically, so the pass must end in a recorded skip
    rather than in a lucky second attempt."""
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    _seed(conn, repo_env)
    settings = RepairSettings(repo=repo_env.repo)

    result = _propose(conn, settings)

    assert result.proposed == 0
    assert store.pending_proposals(conn) == []
    assert any("placeholder" in reason for reason in result.skip_reasons)


# ── the dismissal memory works across kinds ───────────────────────────────────────────────────
def test_a_declined_body_draft_is_not_proposed_again(conn, repo_env, settings):
    """The same durable fact the additive road gets: `finding_subjects` is `[[the entity page]]`
    and `target_paths` is the same list, so the pre-model skip recognises the question under a new
    finding id — and this road's skip is worth more, because its model call is per entity."""
    _seed(conn, repo_env)
    _propose(conn, settings)
    (row,) = store.pending_proposals(conn)
    store.mark_decided(conn, row["id"], status=schema.STATUS_REJECTED, decided_by=support.STEWARD,
                       notes="the draft reads like a brochure")

    run_id = support.seed_gardener_run(conn)
    support.seed_placeholder_body(conn, run_id)
    result = _propose(conn, settings)

    assert result.proposed == 0
    assert result.skipped_known == 1


# ── the prompt: structure unfenced, every page body fenced ────────────────────────────────────
def test_the_prompt_names_the_pages_unfenced_and_fences_every_body(repo_env):
    """The posture the whole package rests on, applied to this road's own prompt: a path is
    structure the model may act on, and a page body is DATA somebody wrote. The entity page's body
    is the least trustworthy of all here — it is the placeholder text a drafter is being asked to
    replace, and it arrives through the same fence as everything else."""
    prompt = proposer.build_entity_body_prompt(
        support.ENTITY_PAGE, "---\ntype: entity\n---\n\n# X\n\n<a placeholder>\n",
        {support.NOTE_A: "a note body"})

    index = prompt.split(proposer.DETAILS_MARKER, 1)[0]
    assert support.ENTITY_PAGE in index
    assert support.NOTE_A in index
    assert "<a placeholder>" not in index, "a page body never reaches the unfenced half"
    assert "a note body" not in index


def test_the_drafters_frame_states_what_the_skill_cannot_widen():
    """The code-owned half of this road's system prompt, asserted clause by clause exactly as the
    additive road's is: a knowledge repo must not be able to widen what a drafter may do by
    rewriting its own procedure."""
    header = proposer.ENTITY_BODY_HEADER

    assert "never perform" in header
    assert "two tools, both READS" in header
    assert "H1" in header, "the page's own title is not the draft's to write"
    assert "frontmatter" in header
    assert "UNTRUSTED" in header or "never instructions to you" in header


@pytest.mark.parametrize("field", ["body_markdown", "role"])
def test_the_draft_schema_asks_for_exactly_the_two_fields_the_op_carries(field):
    assert field in proposer.EntityBodyDraft.model_fields


# ── the park: an empty body is the answer both briefs ask for ──────────────────────────────────
class _Empties:
    """A drafter that answers with an empty body, counting the calls it was asked for.

    The counting is the whole point of the class: what #83 is about is not WHETHER the empty draft
    is stored (it never was) but what the honest answer COSTS, and the only way to see a cost is
    to count calls.
    """

    def __init__(self, first: str = ""):
        self.calls: list[str] = []
        self.first = first

    async def run(self, prompt, *, deps=None, usage_limits=None):
        from stigmergy.kernel.result import fake_result
        self.calls.append(prompt)
        body = self.first if len(self.calls) == 1 else ""
        return fake_result(proposer.EntityBodyDraft(body_markdown=body))


def test_an_empty_body_is_the_PARK_and_costs_one_model_call_not_two(conn, repo_env, settings,
                                                                    monkeypatch):
    """**The answer this road's own brief asks for, priced as an answer.** Before #83 an empty
    body was routed through the validator's error path: the retry brief re-stated the very
    instruction the model had just followed ("Return an EMPTY body rather than inventing one"),
    the model obeyed again, and the page was refused a second time — two calls, every run, for
    every entity whose corpus says nothing, forever.

    The recurrence itself is deliberate and stays: nothing durable remembers a decline, so the
    page is re-asked once the corpus has grown, which is the road's whole reason to exist.
    """
    double = _Empties()
    monkeypatch.setattr(proposer, "build_entity_body_drafter", lambda *a, **kw: double)
    _seed(conn, repo_env)

    result = _propose(conn, settings)

    assert len(double.calls) == 1, "an empty body is an answer, not a validation error to retry"
    assert result.proposed == 0
    assert store.pending_proposals(conn) == []
    assert any("declined" in reason and support.ENTITY_PAGE in reason
               for reason in result.skip_reasons)


def test_an_empty_body_arriving_on_the_RETRY_is_the_park_too(conn, repo_env, settings,
                                                             monkeypatch):
    """The half a check placed only before the first validation would miss. `_draft_retry` ends
    with "Return an EMPTY body rather than inventing one", so a first draft that fails for an
    UNRELATED reason can be answered with the park — and routing that through the error path
    records a refusal for an answer the retry brief explicitly asked for."""
    double = _Empties(first=proposer.FakeEntityBodyDrafter.FLAWED_BODY)
    monkeypatch.setattr(proposer, "build_entity_body_drafter", lambda *a, **kw: double)
    _seed(conn, repo_env)

    result = _propose(conn, settings)

    assert len(double.calls) == 2, "a real validation failure still gets its one corrective retry"
    assert result.proposed == 0
    assert any("declined" in reason for reason in result.skip_reasons)
    assert not any("refused" in reason for reason in result.skip_reasons)


def test_a_declined_draft_records_a_sentence_that_names_the_page(conn, repo_env, settings,
                                                                  monkeypatch):
    """The benign half of the reason vocabulary: a park is recorded through its OWN sentence, not
    through the refusal template with an empty reason list — which reads as
    `entity-body draft refused for X: ` and tells an operator that something went wrong."""
    monkeypatch.setattr(proposer, "build_entity_body_drafter", lambda *a, **kw: _Empties())
    _seed(conn, repo_env)

    result = _propose(conn, settings)

    (reason,) = [r for r in result.skip_reasons if support.ENTITY_PAGE in r]
    assert not reason.rstrip().endswith(":"), "a refusal with no reasons is a malformed sentence"
    assert "nothing yet to write" in reason


def test_a_flawed_draft_still_gets_its_one_corrective_retry_and_is_then_refused(conn, repo_env,
                                                                                monkeypatch):
    """The benign twin for the park check: recognising an empty body early must not disarm the
    retry for the failures it was built for. `fake-flawed` keeps a placeholder line in BOTH
    answers, so the road must end in a refusal — with reasons — rather than in a park."""
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    _seed(conn, repo_env)

    result = _propose(conn, settings=RepairSettings(repo=repo_env.repo))

    assert result.proposed == 0
    assert any("refused" in reason and "placeholder" in reason for reason in result.skip_reasons)


def test_the_validator_still_refuses_an_empty_body_at_apply_time(repo_env):
    """Two moments, two answers, and this is the one that must NOT move. The proposer never stores
    an empty draft, so the validator is the backstop for a stored op — and there an empty body
    would erase whatever prose the page already carries."""
    support.seed_entity(repo_env, anchored=2)
    op = {schema.OP_KIND_KEY: schema.KIND_ENTITY_BODY, "path": support.ENTITY_PAGE,
          "body_markdown": "", "role": ""}

    assert [f.code for f in entity_body.validate(repo_env.repo, [op])] == ["empty-body"]
