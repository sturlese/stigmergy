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

import pytest

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
