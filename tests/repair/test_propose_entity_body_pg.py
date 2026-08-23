"""The `entity-body` road, end to end: an entity page whose body says nothing about the entity in,
a rewritten page on the remote out — or a recorded reason why not.

The two properties this file exists for, both of them about what does NOT happen:

  · **an entity with fewer than two anchored pages never reaches the model.** A body drafted from
    nothing is the placeholder with better grammar, and under the capture-is-the-approval change it
    would not cost a steward a
    decision — it would cost the corpus a commit nobody asked for.
  · **the additive road is untouched.** Both kinds ride the same pass, the same ceiling and the
    same memory, and a finding of one kind must never be answered in the other's vocabulary.

Real findings, a real checkout, the offline double for the model, the real gates, and a real bare
remote as the last word: this is the one kind that REPLACES prose, so "it was derived" and "it
landed" are two different claims and both are made here.
"""
import os

import pytest

from stigmergy.gardener import checks as gardener_checks
from stigmergy.gardener import sweep as gardener_sweep
from stigmergy.kernel.result import fake_result
from stigmergy.repair import entity_body, schema, store
from stigmergy.repair import run as repair_run
from stigmergy.repair.settings import RepairSettings
from tests.librarian import support as librarian_support
from tests.repair import support

# Every pass here applies through the nine gates, so a machine without gitleaks cannot exercise any
# of it: skip on a laptop, FAIL in CI.
pytestmark = pytest.mark.usefixtures("require_gitleaks")

WRITTEN_BUT_EMPTY = (f"# {support.ENTITY_STEM}\n\n{support.ENTITY_STEM} is a company we work "
                     f"with.\n")


def _seed(conn, repo_env, *, anchored: int = 2) -> tuple[int, int]:
    support.seed_entity(repo_env, anchored=anchored)
    run_id = support.seed_gardener_run(conn)
    return run_id, support.seed_placeholder_body(conn, run_id)


def _seed_written_but_empty(conn, repo_env, *, anchored: int = 2) -> tuple[int, int]:
    support.seed_entity(repo_env, anchored=anchored, body=WRITTEN_BUT_EMPTY)
    run_id = support.seed_gardener_run(conn)
    return run_id, support.seed_empty_entity_body(conn, run_id)


# ── the happy path ────────────────────────────────────────────────────────────────────────────
def test_a_placeholder_finding_lands_as_one_body_commit(conn, repo_env, settings):
    """The whole road in one assertion set. The ledger says what was derived; the remote says the
    page really changed — and for THIS kind the second half is the one that matters, because it is
    the only repair in the system that destroys text somebody could have written."""
    run_id, finding_id = _seed(conn, repo_env)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, settings)

    assert (result.applied, result.failed) == (1, 0)
    (row,) = store.recent(conn)
    assert row["status"] == schema.STATUS_APPLIED
    assert row["kind"] == schema.KIND_ENTITY_BODY
    assert row["finding_ids"] == [finding_id]
    assert row["target_paths"] == [support.ENTITY_PAGE]
    assert row["finding_subjects"] == [[support.ENTITY_PAGE]]
    assert [op["op"] for op in row["ops"]] == [schema.KIND_ENTITY_BODY]
    assert row["ops"][0]["path"] == support.ENTITY_PAGE
    assert row["content_key"] == schema.content_key(row["ops"], kind=schema.KIND_ENTITY_BODY)
    # …and the page on the REMOTE lost its placeholder and kept its identity.
    landed = support.remote_page(repo_env.bare, support.ENTITY_PAGE)
    assert "<One clear paragraph" not in landed
    assert f"# {support.ENTITY_STEM}" in landed
    assert 'entity: ["meridian-partners"]' in landed
    assert support.commit_count(repo_env.bare) == before + 1


def test_the_body_the_ledger_stored_is_the_body_that_landed(conn, repo_env, settings):
    """The propose-time proof used to be asserted here — "what is on the table is what the applier
    would perform". Nothing is on a table any more, so the property is the one that took its
    place: the op the row carries and the prose on the remote are the same bytes, and the stored
    diff IS the reading nobody gave this page beforehand."""
    _seed(conn, repo_env)

    support.run_pass(conn, repo_env, settings)

    (row,) = store.recent(conn)
    landed = support.remote_page(repo_env.bare, support.ENTITY_PAGE)
    assert row["ops"][0]["body_markdown"].strip() in landed
    assert librarian_support.changed_paths(repo_env.bare, row["applied_commit"]) == [
        support.ENTITY_PAGE]
    assert row["diff"].startswith("diff --git")


def test_the_body_that_landed_cites_the_pages_it_was_drafted_from(conn, repo_env, settings):
    """Not a style preference: a body whose facts trace to nothing is exactly what nobody can
    check, and every wikilink in it has to resolve or the contract linter refuses the page — which
    is now a `failed` row rather than a question somebody declines."""
    _seed(conn, repo_env)

    support.run_pass(conn, repo_env, settings)

    assert "[[Meridian Note 1]]" in support.remote_page(repo_env.bare, support.ENTITY_PAGE)


# ── the model finding rides the SAME road ─────────────────────────────────────────────────────
# `model-empty-entity-body` (#78) is the judgment twin of `entity-placeholder-body`: the page's body
# is written and says nothing about the entity rather than still being the template. One question,
# one answer, one road — so what these tests prove is that the second check reaches the road at all
# and is answered identically, not that a second road exists.
def test_an_empty_body_finding_lands_the_same_way(conn, repo_env, settings):
    """The end-to-end criterion the fifth check exists for: without this the finding has no path to
    zero and #78 only moved the problem to a report nobody can act on."""
    run_id, finding_id = _seed_written_but_empty(conn, repo_env)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 1
    (row,) = store.recent(conn)
    assert row["kind"] == schema.KIND_ENTITY_BODY
    assert row["finding_ids"] == [finding_id]
    assert row["target_paths"] == [support.ENTITY_PAGE]
    assert row["finding_subjects"] == [[support.ENTITY_PAGE]]
    assert row["ops"][0]["path"] == support.ENTITY_PAGE


def test_a_written_but_empty_body_is_REPLACED_on_the_remote(conn, repo_env, settings):
    """The kind's own validator has no placeholder precondition, and this is what says so — with
    the consequence made visible, which a stored row could not: the sentence somebody wrote is GONE
    from the page, and this road is the only thing in the system that may do that."""
    _seed_written_but_empty(conn, repo_env)

    support.run_pass(conn, repo_env, settings)

    landed = support.remote_page(repo_env.bare, support.ENTITY_PAGE)
    assert "is a company we work with" not in landed, (
        "the prose that was on the page is what this apply replaces — if it survived, the draft "
        "was appended rather than applied")
    assert "[[Meridian Note 1]]" in landed


def test_an_empty_body_finding_with_one_anchored_page_produces_no_draft_and_says_why(
        conn, repo_env, settings, monkeypatch):
    """The existing floor holds for the new check too, and stays where it is: `MIN_ANCHORED_PAGES`
    is enforced in the pass BEFORE the model is asked, so the gardener never has to know about it
    and a reported page with too little evidence costs a recorded reason rather than a call."""
    def refuse(*args, **kwargs):
        raise AssertionError("the drafter was built for an entity with nothing to draft from")

    monkeypatch.setattr(repair_run, "build_entity_body_drafter", refuse)
    _seed_written_but_empty(conn, repo_env, anchored=1)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 0
    assert store.recent(conn) == []
    assert support.commit_count(repo_env.bare) == before
    assert any("too-few-anchored-pages" in reason and support.ENTITY_PAGE in reason
               for reason in result.skip_reasons)


def test_both_body_checks_are_on_the_one_road_and_neither_is_on_the_additive_one():
    """Named directly rather than through a consequence: a finding rides exactly one road, and the
    two vocabularies do not mix."""
    expected = frozenset({gardener_checks.CHECK_ENTITY_PLACEHOLDER_BODY,
                          gardener_sweep.CHECK_MODEL_EMPTY_ENTITY_BODY})
    assert expected == repair_run.BODY_PROPOSABLE_CHECKS
    assert not (repair_run.BODY_PROPOSABLE_CHECKS & repair_run.EDIT_PROPOSABLE_CHECKS)


def test_a_body_this_loop_already_wrote_is_not_rewritten_under_the_other_body_check(
        conn, repo_env, settings):
    """The memory keys on the PAGE (`finding_subjects`), not on the check that named it — so a page
    this loop has already written is not written again the night the other half of the pair reports
    it. Without that, the two checks would take turns re-drafting one page forever, each rewrite a
    commit nobody asked for."""
    _seed(conn, repo_env)
    support.run_pass(conn, repo_env, settings)
    head = support.remote_head(repo_env.bare)

    run_id = support.seed_gardener_run(conn)
    support.seed_empty_entity_body(conn, run_id)
    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 0
    assert result.skipped_known == 1
    assert support.remote_head(repo_env.bare) == head


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


def test_two_body_findings_naming_different_pages_land_two_commits(conn, repo_env, settings):
    """The body road is per PAGE, not per pass: two entity pages reported the same night — one by
    each half of the pair — are two questions and get two commits. A road that answered only the
    first would leave the second page reported and unrepaired for as long as the report kept naming
    it."""
    support.seed_entity(repo_env, anchored=2, push=False)
    second = _seed_a_second_entity(repo_env)
    librarian_support.commit_and_push(repo_env.repo, "test: two thin entities")
    run_id = support.seed_gardener_run(conn)
    support.seed_placeholder_body(conn, run_id)
    support.seed_empty_entity_body(conn, run_id, page=second)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 2
    rows = store.recent(conn)
    assert {row["kind"] for row in rows} == {schema.KIND_ENTITY_BODY}
    assert sorted(row["target_paths"][0] for row in rows) == sorted([support.ENTITY_PAGE, second])
    assert all(len(row["ops"]) == 1 for row in rows)
    assert support.commit_count(repo_env.bare) == before + 2, "one repair, one commit — twice"


def test_two_body_findings_still_share_the_one_pass_ceiling(conn, repo_env, settings):
    """One pass, one bounded blast radius — proven WITHIN the body road as well as across the
    roads. Both findings are draftable and the ceiling is one, so exactly one page is rewritten and
    the pass says the other was deferred rather than dropping it silently."""
    support.seed_entity(repo_env, anchored=2, push=False)
    second = _seed_a_second_entity(repo_env)
    librarian_support.commit_and_push(repo_env.repo, "test: two thin entities")
    run_id = support.seed_gardener_run(conn)
    support.seed_placeholder_body(conn, run_id)
    support.seed_empty_entity_body(conn, run_id, page=second)
    one = RepairSettings(repo=settings.repo, max_repairs_per_run=1)

    result = support.run_pass(conn, repo_env, one)

    assert result.applied == 1
    assert len(store.recent(conn, status=schema.STATUS_APPLIED)) == 1
    assert any("run-ceiling-reached(1)" in reason and "1 further finding(s)" in reason
               for reason in result.skip_reasons)


def test_two_body_findings_naming_the_SAME_page_still_rewrite_it_at_most_once(conn, repo_env,
                                                                              settings):
    """"One rewrite at most" asked of the road that would have to produce two.

    The gardener's structural exclusion is the FIRST line and the one that is meant to hold: these
    two findings cannot co-exist for one page in a real run (`sweep.select_empty_body_pages`). This
    test constructs the state anyway, because the criterion is about the COMMIT and the drafting
    happens one package away: `schema.content_key` is `kind+path` for this kind, so the second
    answer about the same page meets a key that is already in the ledger and is dropped with a
    recorded reason. Defence in depth, not a licence to drop the exclusion — if this ever starts
    producing two rows, one page's body was rewritten twice in one pass.
    """
    support.seed_entity(repo_env, anchored=2, body=WRITTEN_BUT_EMPTY)
    run_id = support.seed_gardener_run(conn)
    support.seed_placeholder_body(conn, run_id)
    support.seed_empty_entity_body(conn, run_id)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, settings)

    assert result.findings_seen == 2, "both findings DID reach the road"
    assert result.applied == 1
    assert [row["target_paths"] for row in store.recent(conn)] == [[support.ENTITY_PAGE]]
    assert any("content key is already in the ledger" in reason
               for reason in result.skip_reasons)
    assert support.commit_count(repo_env.bare) == before + 1


def test_an_empty_body_finding_for_a_page_since_deleted_costs_a_reason_not_a_crash(
        conn, repo_env, settings, monkeypatch):
    """The gardener reads the corpus at 03:00 and the pass reads it again later; somebody can
    delete an entity page in between. The finding still names it, and the road must end in a
    recorded reason rather than a traceback that costs every OTHER finding in the pass its repair.
    No model is asked either — there is nothing in the tree to draft from."""
    def refuse(*args, **kwargs):
        raise AssertionError("the drafter was built for a page that no longer exists")

    monkeypatch.setattr(repair_run, "build_entity_body_drafter", refuse)
    support.seed_entity(repo_env, anchored=2, body=WRITTEN_BUT_EMPTY)
    os.remove(os.path.join(repo_env.repo, *support.ENTITY_PAGE.split("/")))
    # COMMITTED, because the pass derives against a worktree detached at the base it will apply to
    # — an uncommitted removal is not a corpus that moved, it is a dirty checkout.
    librarian_support.commit_and_push(repo_env.repo, "test: the entity page was deleted")
    run_id = support.seed_gardener_run(conn)
    support.seed_empty_entity_body(conn, run_id)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 0
    assert store.recent(conn) == []
    assert any(support.ENTITY_PAGE in reason for reason in result.skip_reasons)
    assert result.run_id, "the pass still records itself — one absent page is not a dead pass"


# ── the pre-model skip: not enough to draft from ──────────────────────────────────────────────
def test_an_entity_with_one_anchored_page_never_reaches_the_model(conn, repo_env, settings,
                                                                  monkeypatch):
    """The model is not asked, and that is asserted by making the ask FAIL: a pass that quietly
    called it and then discarded the answer would look identical from the outside, and would keep
    costing money every night for an entity nothing has been written about."""
    def refuse(*args, **kwargs):
        raise AssertionError("the drafter was built for an entity with nothing to draft from")

    monkeypatch.setattr(repair_run, "build_entity_body_drafter", refuse)
    _seed(conn, repo_env, anchored=1)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 0
    assert store.recent(conn) == []
    assert any("anchored" in reason for reason in result.skip_reasons)


def test_two_anchored_pages_is_the_floor_and_it_is_met(conn, repo_env, settings):
    """The benign twin of the skip: the bound is a floor, not a wall — exactly two pages is enough
    evidence to draft from, and a rule that demanded more would leave every young entity with a
    placeholder forever."""
    _seed(conn, repo_env, anchored=2)
    assert support.run_pass(conn, repo_env, settings).applied == 1


# ── the two roads share one pass ──────────────────────────────────────────────────────────────
def test_a_placeholder_finding_and_an_edits_finding_ride_the_same_pass(conn, repo_env, settings):
    """Both kinds, one pass, one `job_runs` row, two commits. The additive road answers its finding
    in its own vocabulary and this one answers its own — a pass that let either road see the
    other's findings would land a backlink for a page with no body, or a body for two pages that
    fail to link."""
    run_id, _ = _seed(conn, repo_env)
    support.seed_unlinked_mention(conn, run_id, pages=(support.NOTE_A, support.DECISION))
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, settings)

    kinds = sorted(row["kind"] for row in store.recent(conn))
    assert kinds == [schema.KIND_EDITS, schema.KIND_ENTITY_BODY]
    assert result.findings_seen == 2
    assert (result.applied, support.commit_count(repo_env.bare)) == (2, before + 2)


def test_the_pass_ceiling_bounds_both_roads_together(conn, repo_env, settings):
    """One pass, one blast radius. The ceiling is how many commits a pass may push, so a second
    road that carried its own budget would double the number quietly."""
    run_id, _ = _seed(conn, repo_env)
    support.seed_unlinked_mention(conn, run_id, pages=(support.NOTE_A, support.DECISION))
    one = RepairSettings(repo=settings.repo, max_repairs_per_run=1)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, one)

    assert result.applied == 1
    assert support.commit_count(repo_env.bare) == before + 1
    assert any("ceiling" in reason for reason in result.skip_reasons)


# ── the draft is validated, and a bad one never reaches a tree ─────────────────────────────────
def test_a_draft_that_does_not_validate_is_skipped_with_a_recorded_reason(conn, repo_env,
                                                                          settings, monkeypatch):
    """`CLEAN_LLM=fake-flawed` drafts a body that keeps a placeholder line — the one failure this
    road exists to prevent, since a "repair" that re-states the template is a commit that changes
    nothing anyone can read and a `failed` row whose key is then remembered forever. The retry gets
    the same answer, deterministically, so the pass must end in a recorded skip rather than in a
    lucky second attempt."""
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    _seed(conn, repo_env)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 0
    assert store.recent(conn) == []
    assert support.commit_count(repo_env.bare) == before
    assert any("placeholder" in reason for reason in result.skip_reasons)


# ── the memory reaches this road too ──────────────────────────────────────────────────────────
def test_a_page_this_loop_already_wrote_is_not_written_again(conn, repo_env, settings):
    """The same durable fact the additive road gets: `finding_subjects` is `[[the entity page]]`
    and `target_paths` is the same list, so the pre-model skip recognises the question under a new
    finding id — and this road's skip is worth more, because its model call is per entity and its
    commit replaces prose."""
    _seed(conn, repo_env)
    support.run_pass(conn, repo_env, settings)
    head = support.remote_head(repo_env.bare)

    run_id = support.seed_gardener_run(conn)
    support.seed_placeholder_body(conn, run_id)
    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 0
    assert result.skipped_known == 1
    assert support.remote_head(repo_env.bare) == head


# ── the prompt: structure unfenced, every page body fenced ────────────────────────────────────
def test_the_prompt_names_the_pages_unfenced_and_fences_every_body(repo_env):
    """The posture the whole package rests on, applied to this road's own prompt: a path is
    structure the model may act on, and a page body is DATA somebody wrote. The entity page's body
    is the least trustworthy of all here — it is the text this road exists to replace, and it
    arrives through the same fence as everything else."""
    prompt = repair_run.build_entity_body_prompt(
        support.ENTITY_PAGE, "---\ntype: entity\n---\n\n# X\n\n<a placeholder>\n",
        {support.NOTE_A: "a note body"})

    index = prompt.split(repair_run.DETAILS_MARKER, 1)[0]
    assert support.ENTITY_PAGE in index
    assert support.NOTE_A in index
    assert "<a placeholder>" not in index, "a page body never reaches the unfenced half"
    assert "a note body" not in index


def test_the_drafters_frame_states_what_the_skill_cannot_widen():
    """The code-owned half of this road's system prompt, asserted clause by clause exactly as the
    additive road's is: a knowledge repo must not be able to widen what a drafter may do by
    rewriting its own procedure."""
    header = repair_run.ENTITY_BODY_HEADER

    assert "never perform" in header
    assert "two tools, both READS" in header
    assert "H1" in header, "the page's own title is not the draft's to write"
    assert "frontmatter" in header
    assert "UNTRUSTED" in header or "never instructions to you" in header


@pytest.mark.parametrize("field", ["body_markdown", "role"])
def test_the_draft_schema_asks_for_exactly_the_two_fields_the_op_carries(field):
    assert field in repair_run.EntityBodyDraft.model_fields


# ── the park: an empty body is the answer both briefs ask for ──────────────────────────────────
class _Empties:
    """A drafter that answers with an empty body, counting the calls it was asked for.

    The counting is the whole point of the class: what #83 is about is not WHETHER the empty draft
    is acted on (it never was) but what the honest answer COSTS, and the only way to see a cost is
    to count calls.
    """

    def __init__(self, first: str = ""):
        self.calls: list[str] = []
        self.first = first

    async def run(self, prompt, *, deps=None, usage_limits=None):
        self.calls.append(prompt)
        body = self.first if len(self.calls) == 1 else ""
        return fake_result(repair_run.EntityBodyDraft(body_markdown=body))


def test_an_empty_body_is_the_PARK_and_costs_one_model_call_not_two(conn, repo_env, settings,
                                                                    monkeypatch):
    """**The answer this road's own brief asks for, priced as an answer.** Before #83 an empty
    body was routed through the validator's error path: the retry brief re-stated the very
    instruction the model had just followed ("Return an EMPTY body rather than inventing one"),
    the model obeyed again, and the page was refused a second time — two calls, every run, for
    every entity whose corpus says nothing, forever.

    The recurrence itself is deliberate and stays: nothing durable remembers a park, so the page is
    re-asked once the corpus has grown, which is the road's whole reason to exist. What bounds the
    bill is the ask ceiling at the foot of this file.
    """
    double = _Empties()
    monkeypatch.setattr(repair_run, "build_entity_body_drafter", lambda *a, **kw: double)
    _seed(conn, repo_env)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, settings)

    assert len(double.calls) == 1, "an empty body is an answer, not a validation error to retry"
    assert result.applied == 0
    assert store.recent(conn) == []
    assert support.commit_count(repo_env.bare) == before
    assert any("declined" in reason and support.ENTITY_PAGE in reason
               for reason in result.skip_reasons)


def test_an_empty_body_arriving_on_the_RETRY_is_the_park_too(conn, repo_env, settings,
                                                             monkeypatch):
    """The half a check placed only before the first validation would miss. `_draft_retry` ends
    with "Return an EMPTY body rather than inventing one", so a first draft that fails for an
    UNRELATED reason can be answered with the park — and routing that through the error path
    records a refusal for an answer the retry brief explicitly asked for."""
    double = _Empties(first=repair_run.FakeEntityBodyDrafter.FLAWED_BODY)
    monkeypatch.setattr(repair_run, "build_entity_body_drafter", lambda *a, **kw: double)
    _seed(conn, repo_env)

    result = support.run_pass(conn, repo_env, settings)

    assert len(double.calls) == 2, "a real validation failure still gets its one corrective retry"
    assert result.applied == 0
    assert any("declined" in reason for reason in result.skip_reasons)
    assert not any("refused" in reason for reason in result.skip_reasons)


def test_a_declined_draft_records_a_sentence_that_names_the_page(conn, repo_env, settings,
                                                                 monkeypatch):
    """The benign half of the reason vocabulary: a park is recorded through its OWN sentence, not
    through the refusal template with an empty reason list — which reads as
    `entity-body draft refused for X: ` and tells an operator that something went wrong."""
    monkeypatch.setattr(repair_run, "build_entity_body_drafter", lambda *a, **kw: _Empties())
    _seed(conn, repo_env)

    result = support.run_pass(conn, repo_env, settings)

    (reason,) = [r for r in result.skip_reasons if support.ENTITY_PAGE in r]
    assert not reason.rstrip().endswith(":"), "a refusal with no reasons is a malformed sentence"
    assert "nothing yet to write" in reason


def test_a_flawed_draft_still_gets_its_one_corrective_retry_and_is_then_refused(conn, repo_env,
                                                                                settings,
                                                                                monkeypatch):
    """The benign twin for the park check: recognising an empty body early must not disarm the
    retry for the failures it was built for. `fake-flawed` keeps a placeholder line in BOTH
    answers, so the road must end in a refusal — with reasons — rather than in a park."""
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    _seed(conn, repo_env)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 0
    assert any("refused" in reason and "placeholder" in reason for reason in result.skip_reasons)


def test_the_validator_still_refuses_an_empty_body_at_apply_time(repo_env):
    """Two moments, two answers, and this is the one that must NOT move. The pass never derives an
    empty draft, so the validator is the backstop for a stored op — and there an empty body would
    erase whatever prose the page already carries."""
    support.seed_entity(repo_env, anchored=2)
    op = {schema.OP_KIND_KEY: schema.KIND_ENTITY_BODY, "path": support.ENTITY_PAGE,
          "body_markdown": "", "role": ""}

    assert [f.code for f in entity_body.validate(repo_env.repo, [op])] == ["empty-body"]


# ── the ask ceiling: the pass's one number bounds the bill, not only the commits (issue #103) ──
def test_a_night_of_parks_is_bounded_by_the_ask_ceiling_and_says_so(conn, repo_env, monkeypatch):
    """Red before the fix: the pass ceiling bounded what a run STORED, and a declined draft stores
    nothing — deliberately (#83: the park is re-asked once the corpus grows) — so a corpus full of
    thin entities could spend an unbounded number of model calls a night while landing nothing,
    invisible everywhere but the bill. The ask-budget is the SAME number the pass already has: no
    new knob, the recurrence stays deliberate, its nightly cost is bounded and the deferral is
    recorded."""
    calls = {"n": 0}

    class _AlwaysParks:
        async def run(self, prompt, *, deps=None, usage_limits=None):
            calls["n"] += 1
            return fake_result(repair_run.EntityBodyDraft(body_markdown=""))

    monkeypatch.setattr(repair_run, "build_entity_body_drafter", lambda *a, **kw: _AlwaysParks())
    support.seed_entity(repo_env, anchored=2)
    run_id = support.seed_gardener_run(conn)
    for n in range(4):
        support.seed_finding(conn, run_id, check=gardener_sweep.CHECK_MODEL_EMPTY_ENTITY_BODY,
                             subjects=[support.ENTITY_PAGE], detail=f"thin body, night {n}",
                             severity="info")

    settings = RepairSettings(repo=repo_env.repo, max_repairs_per_run=2)
    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 0
    assert calls["n"] == 2, "the road kept asking past the pass's own number"
    assert any("ask-ceiling-reached(2)" in reason for reason in result.skip_reasons)


def test_a_productive_night_is_untouched_by_the_ask_ceiling(conn, repo_env, settings):
    """The benign twin: a road whose asks LAND is already bounded by the pass ceiling, and the ask
    bound — the same number — cannot fire first. One finding, one call, one commit, no ceiling
    sentence of either kind."""
    _seed(conn, repo_env)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 1
    assert not any("ceiling" in reason for reason in result.skip_reasons)
