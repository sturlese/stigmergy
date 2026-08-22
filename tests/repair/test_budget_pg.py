"""The two budgets a repair pass spends: the MODEL budget one call runs under, and the CEILINGS on
how much of the corpus one pass may change.

The bug the first half was written for (issue #75, staging 2026-08-17, the night AFTER #73 landed):
a repair pass over the real 25-finding corpus produced **three** repairs, all from the body road,
and both edits-road batches were skipped with `usage-budget-exhausted`. The additive road proposed
nothing at all — and since "the next run retries it" with the same batch shape, the same findings
exhaust the same way every night, a permanent-retry loop that reads as a healthy `ok` row in
`job_runs`.

`PROPOSER_LIMITS` was a CONSTANT (26 requests / 24 tool calls) while `settings.batch_size` decided
how many findings that constant had to cover. At the default of 8 that is **three tool calls per
finding**, and the proposer reads pages through tools: three reads per finding leaves it no room to
look at anything it was not handed. Framed correctly — and #75 says so in its own voice — the
defect is the BUDGET, not the agent: exploration is the feature. So the budget scales with the
batch, and the default batch shrinks so that one lapse costs fewer findings.

The second half is what ADR 044 turned from an inbox length into a blast radius. Nobody reads a
repair before it lands, so `max_repairs_per_run` bounds COMMITS, and `max_merges_per_run` — a
second, tighter number — bounds the one kind that retires an identity. Both are proven on what
reaches the remote, and both have to SAY what they deferred: a pass that quietly did less than it
saw reads as "the corpus is nearly clean".

Keyless by construction, and in TWO halves. The doubles assert on the budget the code HANDS a
batch, which is a real property of the code under test — not a simulation of a model's appetite.
The half below `_real_agent` hands that budget to a real pydantic-ai agent driven by the library's
own `FunctionModel`: whether 24 tool calls is enough for three findings, and whether a corrective
retry starts from zero, are facts about the library that ENFORCES the bound, and a double counting
its own tool calls would only prove that a test can count.
"""
import asyncio
import contextlib

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from stigmergy.kernel import llm as kernel_llm
from stigmergy.kernel.result import fake_result
from stigmergy.repair import run as repair_run
from stigmergy.repair import schema, store
from stigmergy.repair.settings import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_MERGES_PER_RUN,
    DEFAULT_MAX_REPAIRS_PER_RUN,
    RepairSettings,
)
from tests.librarian import support as librarian_support
from tests.repair import support

# Every pass that LANDS anything here runs the nine gates, so a machine without gitleaks cannot
# exercise the ceiling half at all: skip on a laptop, FAIL in CI.
pytestmark = pytest.mark.usefixtures("require_gitleaks")

# The finding count that caught this on staging. Not a round number on purpose: it is the shape the
# eval set did not have, and the reason this file exists.
REALISTIC_FINDING_COUNT = 29


class _BudgetRecorder:
    """Answers every batch with nothing, and records the `usage_limits` it was handed alongside the
    number of findings that batch carried.

    Recording the budget rather than consuming it is deliberate. What went wrong on staging was not
    that a model was greedy — it was that the code handed 8 findings a budget sized for a couple.
    That is a property of this code, and a double that pretended to spend tokens would be proving
    something about a fake instead."""

    def __init__(self):
        self.batches: list[tuple[int, object]] = []

    async def run(self, prompt, *, deps=None, usage_limits=None):
        findings = repair_run._parse_finding_headers(prompt)
        self.batches.append((len(findings), usage_limits))
        return fake_result(repair_run.ProposalBatch(proposals=[]))


def _seed(conn, count: int) -> int:
    run_id = support.seed_gardener_run(conn)
    for _ in range(count):
        support.seed_unlinked_mention(conn, run_id, pages=(support.NOTE_A, support.NOTE_B))
    return run_id


# ── the property the staging night falsified ──────────────────────────────────────────────────
def test_every_edits_batch_gets_a_workable_tool_allowance_per_finding(conn, repo_env, monkeypatch):
    """RED before #75: with `tool_calls_limit` fixed at 24 and the default batch at 8, every batch
    was handed THREE tool calls per finding — and the proposer reads pages through tools.

    The number of batches does not matter and neither does what the model answers; what matters is
    that no batch is ever asked to judge more findings than its budget can pay for.

    **What this test alone does NOT catch, and it is worth knowing which sibling does.** Now that
    the default batch is 3, a straight revert to the old fixed 24 would leave this GREEN — 8 per
    finding, above the floor. The defect only shows above a batch of 4. The tests that red on that
    revert are `test_the_short_final_batch_pays_for_the_findings_it_actually_carries` and
    `test_an_operator_who_raises_the_batch_is_given_the_budget_that_goes_with_it`, and that is why
    they exist rather than reading as extra."""
    recorder = _BudgetRecorder()
    monkeypatch.setattr(repair_run, "build_proposer", lambda *_a, **_k: recorder)
    _seed(conn, REALISTIC_FINDING_COUNT)

    support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo))

    assert recorder.batches, "no batch reached the model at all"
    for size, limits in recorder.batches:
        per_finding = limits.tool_calls_limit / size
        assert per_finding >= repair_run.MIN_TOOL_CALLS_PER_FINDING, (
            f"a batch of {size} finding(s) was handed {limits.tool_calls_limit} tool calls "
            f"({per_finding:.1f} per finding) — below the floor of "
            f"{repair_run.MIN_TOOL_CALLS_PER_FINDING}, which is what exhausted the budget on the "
            f"first real corpus")


def test_the_request_budget_cannot_bind_first_at_any_batch_size():
    """`tool_calls_limit` is the WORK ceiling; `request_limit` is only the runaway bound above it.
    A conservative model spends one request per tool call, so a request budget at or below the tool
    budget starves a legitimate batch before its work bound is reached.

    This used to be pinned for ONE pair of constants. It has to hold for every batch size the
    settings can produce, or the property is pinned for the default and free to break everywhere
    else — which is exactly what a derived budget makes possible."""
    for batch_size in (1, 2, 3, 5, 8, 13, 50):
        limits = repair_run.batch_limits(batch_size)
        assert limits.request_limit >= limits.tool_calls_limit + 2, (
            f"batch_size={batch_size} produced request_limit={limits.request_limit} against "
            f"tool_calls_limit={limits.tool_calls_limit}")


def test_the_budget_grows_with_the_batch_and_never_shrinks():
    """Monotonic, so a bigger batch can never be handed a smaller budget — the failure mode a
    formula with a floor and a cap can produce by accident."""
    sizes = [1, 2, 3, 4, 8, 16, 32]
    tool_budgets = [repair_run.batch_limits(n).tool_calls_limit for n in sizes]
    assert tool_budgets == sorted(tool_budgets)


def test_a_single_finding_batch_still_gets_room_to_explore():
    """The floor matters at the small end too: one finding names two pages, and a proposer that
    could only afford those two reads would never find the third page that is the better link
    target. Exploration is the feature this budget exists to pay for."""
    limits = repair_run.batch_limits(1)
    assert limits.tool_calls_limit >= repair_run.MIN_TOOL_CALLS_PER_FINDING


# ── the default batch size ────────────────────────────────────────────────────────────────────
def test_the_default_batch_is_small_enough_that_one_lapse_is_cheap():
    """8 was the toy-corpus default. A batch is the unit of LOSS — a lapsed budget skips the whole
    batch — so the staging run lost eight findings per lapse and stored nothing from the additive
    road. #75's own range is two to three."""
    assert DEFAULT_BATCH_SIZE <= 3


def test_the_batch_size_env_override_still_validates_the_same_way(monkeypatch):
    """The default moved; the operator's door did not. A non-integer and a non-positive value both
    still refuse BY NAME rather than silently falling back."""
    from stigmergy.repair.settings import BATCH_SIZE_ENV
    from stigmergy.server.errors import StartupError

    monkeypatch.setenv(BATCH_SIZE_ENV, "7")
    assert RepairSettings.from_env().batch_size == 7
    monkeypatch.setenv(BATCH_SIZE_ENV, "nonsense")
    with pytest.raises(StartupError, match=BATCH_SIZE_ENV):
        RepairSettings.from_env()
    monkeypatch.setenv(BATCH_SIZE_ENV, "0")
    with pytest.raises(StartupError, match=BATCH_SIZE_ENV):
        RepairSettings.from_env()


# ── the benign twin, and it is the cost property ──────────────────────────────────────────────
def test_a_corpus_with_nothing_to_repair_still_costs_no_model_call(conn, repo_env, monkeypatch):
    """A bigger per-finding budget must not become a bigger bill for a corpus that is already
    clean. A smaller default batch means MORE batches, so this is the direction the change could
    quietly get wrong."""
    recorder = _BudgetRecorder()
    monkeypatch.setattr(repair_run, "build_proposer", lambda *_a, **_k: recorder)
    support.seed_gardener_run(conn)      # a completed run carrying no proposable finding

    result = support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo))

    assert recorder.batches == []
    assert result.applied == 0


# ── the pass ceiling: it binds on what LANDS ──────────────────────────────────────────────────
class _OnePerFinding:
    """A model double that answers each finding in the prompt with one valid backlink, counting the
    calls it was asked for. It reads the prompt through the package's own rule
    (`_parse_finding_headers`: the index, never the fenced half), so it cannot be steered by page
    content either."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, deps=None, usage_limits=None):
        self.calls += 1
        specs = [repair_run.ProposalSpec(
            finding_ids=[f["id"]],
            ops=[repair_run.EditOp(op="backlink", path=f["pages"][0],
                                   link=support.stem(f["pages"][1]))],
            rationale="one valid backlink per finding")
            for f in repair_run._parse_finding_headers(prompt)]
        return fake_result(repair_run.ProposalBatch(proposals=specs))


# How many pages the distinct-pair seeding draws from. Small on purpose: a pass stops at its
# ceiling long before it reaches the end of a realistic night, so what has to be distinct is the
# HEAD of the finding list — the tail exists to prove the pass says it left something behind.
_POOL = 8


def _seed_distinct_pairs(conn, repo_env, count: int) -> int:
    """`count` unlinked-mention findings over a small pool of ASCII-stemmed pages, consecutive
    pages paired so the first `_POOL` findings are all DIFFERENT repairs.

    Distinct matters here and nowhere else in this file: a ceiling can only be shown to bind on
    what LANDS if the repairs behind it are different repairs — identical ones would be dropped by
    the content key instead, and the test would pass with no ceiling in the code at all. ASCII
    stems because a backlink whose target is the fixture's accented page trips a pre-existing
    defect on the librarian's own declared-edit path (`tests/repair/test_propose_pg.py` says
    where).
    """
    pages = [support.write_note(repo_env, f"Budget Note {n}", push=False) for n in range(_POOL)]
    librarian_support.commit_and_push(repo_env.repo, "test: a pool of pages to repair between")
    run_id = support.seed_gardener_run(conn)
    for n in range(count):
        support.seed_unlinked_mention(conn, run_id,
                                      pages=(pages[n % _POOL], pages[(n + 1) % _POOL]))
    return run_id


def test_the_pass_ceiling_bounds_the_commits_a_night_may_push(conn, repo_env, monkeypatch):
    """A smaller batch means more batches, and `max_repairs_per_run` is the other half of the
    bound — the one that keeps a 400-finding night from becoming 400 model calls and 400 commits.
    Asserted AT the new default batch, and on what reached the REMOTE: under ADR 044 this number is
    a blast radius rather than an inbox length."""
    double = _OnePerFinding()
    monkeypatch.setattr(repair_run, "build_proposer", lambda *_a, **_k: double)
    _seed_distinct_pairs(conn, repo_env, REALISTIC_FINDING_COUNT)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env,
                              RepairSettings(repo=repo_env.repo, max_repairs_per_run=5))

    assert result.applied == 5
    assert len(store.recent(conn, status=schema.STATUS_APPLIED, limit=50)) == 5
    assert support.commit_count(repo_env.bare) == before + 5
    assert double.calls == 2, "the pass kept asking after its ceiling was full"
    # No silent cap: whatever the pass left out, it SAID so, and the durable row says the same.
    assert any("run-ceiling-reached(5)" in reason for reason in result.skip_reasons)
    with conn.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1",
                    (repair_run.JOB_NAME,))
        assert cur.fetchone()[0]["skip_reasons"] == result.skip_reasons


# ── the merge ceiling: the one kind that retires an identity gets a tighter number ─────────────
def _seed_merge_pairs(repo_env, stems) -> list[tuple[str, str]]:
    """One duplicate-identity PAIR per stem — a survivor, an absorbed page carrying an alias, and
    one note anchored to the absorbed identity — with the registry derived by the real generator.

    Written here rather than through `support.seed_duplicate_pair`, which seeds exactly one pair
    under fixed names: a ceiling is only observable with more pairs than it allows.
    """
    pairs = []
    for stem in stems:
        slug = stem.lower()
        survivor = support.write_entity_page(repo_env, stem, slug, push=False)
        absorbed = support.write_entity_page(repo_env, f"{stem} Holdings", f"{slug}-holdings",
                                             aliases=(f"{stem} Grupo",), push=False)
        support.write_anchored_note(repo_env, f"{stem} Note", entity_id=f"{slug}-holdings",
                                    push=False)
        pairs.append((survivor, absorbed))
    support.regenerate_registry(repo_env, push=False)
    librarian_support.commit_and_push(repo_env.repo, "test: duplicate identity pairs")
    return pairs


def test_the_merge_ceiling_is_a_second_number_and_it_is_tighter():
    """Deliberately not a fraction of the pass ceiling: what makes a merge different is not its
    size. A merge marks an identity superseded and re-anchors every page that named it, and no
    later pass undoes it — so a night that is wrong about what is a duplicate should be wrong about
    three entities, not twenty."""
    assert DEFAULT_MAX_MERGES_PER_RUN < DEFAULT_MAX_REPAIRS_PER_RUN
    assert RepairSettings().max_merges_per_run == DEFAULT_MAX_MERGES_PER_RUN


def test_the_merge_ceiling_binds_on_what_lands_even_when_the_pass_ceiling_does_not(
        conn, repo_env, monkeypatch):
    """The number that matters on this road is its OWN. The pass ceiling is at its default of
    twenty and nowhere near binding; the merge ceiling is one, and exactly one identity is retired
    on the remote. What the ceiling deferred is counted in the pass's `job_runs.stats` rather than
    dropped silently — an operator reading that row has to be able to tell "there was one
    duplicate" from "there were two and we did one"."""
    pairs = _seed_merge_pairs(repo_env, ("Alfa", "Bravo"))
    run_id = support.seed_gardener_run(conn)
    for pair in pairs:
        support.seed_duplicate_entity_finding(conn, run_id, pages=pair)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env,
                              RepairSettings(repo=repo_env.repo, max_merges_per_run=1))

    assert result.applied == 1
    assert support.commit_count(repo_env.bare) == before + 1
    superseded = [path for _survivor, path in pairs
                  if "superseded_by" in support.remote_page(repo_env.bare, path)]
    assert len(superseded) == 1, "the merge ceiling did not bound what reached the remote"
    assert any("run-ceiling-reached(1)" in reason and "1 further finding(s)" in reason
               for reason in result.skip_reasons)
    with conn.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1",
                    (repair_run.JOB_NAME,))
        assert cur.fetchone()[0]["skip_reasons"] == result.skip_reasons


def test_two_merges_under_the_default_ceiling_both_land(conn, repo_env, settings):
    """The benign twin, and it is the one that matters: a ceiling this tight can bounce a night's
    real work, so "it stops at one when it is one" is only half the property. At the default of
    three, two duplicate pairs are two merges and both reach the remote.

    **OLD BEHAVIOUR: the second merge always failed, and was then never derived again.** Every
    merge regenerates `ops/entity-registry.json`, and a pass DERIVES every repair against one base
    but APPLIES each against a base fetched for it — so the moment the first merge pushed, the
    second's plan no longer matched the tree and `entity_alias.validate` refused it with
    `plan-drift`. That made `max_merges_per_run` effectively ONE however it was configured, and the
    refusal was recorded `failed`, whose `content_key` is remembered forever — so the second pair
    was retired by a race.

    Two fixes, and both are in the design rather than in this test. `run._replanned` re-derives a
    merge's ops in the tree it is about to commit from, from the ONE thing the model decided
    (which identity survives); and `errors.CorpusMovedError` records a drift refusal with no
    content key, so anything this class still catches is derived again next pass.
    """
    pairs = _seed_merge_pairs(repo_env, ("Alfa", "Bravo"))
    run_id = support.seed_gardener_run(conn)
    for pair in pairs:
        support.seed_duplicate_entity_finding(conn, run_id, pages=pair)
    before = support.commit_count(repo_env.bare)

    result = support.run_pass(conn, repo_env, settings)

    assert result.applied == 2
    assert support.commit_count(repo_env.bare) == before + 2
    for _survivor, absorbed in pairs:
        assert "superseded_by" in support.remote_page(repo_env.bare, absorbed)
    assert not any("ceiling" in reason for reason in result.skip_reasons)


# ── the COST claim, which is prose everywhere else ────────────────────────────────────────────
def test_the_default_batch_costs_exactly_what_the_fixed_budget_used_to():
    """`docs/reference/repair.md` and `repair/index.md` both say the bill per MODEL CALL is
    unchanged and that it is the BATCH that was resized to fit it. That sentence is what makes this
    change cheap to review, and until now nothing pinned it: the per-finding floor, the orientation
    term and the default batch can each move on their own and leave the prose quietly false.

    26 requests / 24 tool calls is the pair the deleted `PROPOSER_LIMITS` constant held. If this
    fails, either the price of a call changed or the documentation did — and whichever it was has
    to be corrected in the same commit, not here."""
    limits = repair_run.batch_limits(DEFAULT_BATCH_SIZE)
    assert (limits.request_limit, limits.tool_calls_limit) == (26, 24)


def test_a_degenerate_batch_size_clamps_to_the_single_finding_budget():
    """`RepairSettings.from_env` refuses zero and negative BY NAME — but a `RepairSettings`
    constructed directly does not, and neither would a future caller computing a size from
    something. A budget of zero tool calls does not save money: it fails EVERY call at full model
    cost and lands as `usage-budget-exhausted`, which is the exact failure this file exists for."""
    single = repair_run.batch_limits(1)
    for degenerate in (0, -1):
        assert repair_run.batch_limits(degenerate) == single
        assert repair_run.batch_limits(degenerate).tool_calls_limit > 0


def test_the_body_roads_budget_is_the_one_it_had_and_not_a_single_finding_batch():
    """`BODY_DRAFT_LIMITS` is 26/24 — the pair that road has always had — and it is deliberately
    NOT `batch_limits(1)` (12 tool calls), which is what an argument from symmetry produces.

    Issue #75 was the ADDITIVE road's defect. On the staging night that found it, the body road was
    the only one that produced anything at all, and halving the allowance of the half that works —
    for consistency, with no measurement of what a body draft actually spends — is a risk taken for
    nothing. This test is here so the next "let's make these two consistent" refactor has to argue
    for itself: move the number when there is an observation behind it, and rewrite this docstring
    with the observation in it."""
    assert (repair_run.BODY_DRAFT_LIMITS.request_limit,
            repair_run.BODY_DRAFT_LIMITS.tool_calls_limit) == (26, 24)
    assert repair_run.batch_limits(1) != repair_run.BODY_DRAFT_LIMITS, (
        "the body road's budget has been pinned to the single-finding batch — that is the symmetry "
        "change #75 deliberately did not make")


def test_the_request_budget_cannot_bind_before_the_tool_budget_on_the_body_road():
    """The body road's half of the headroom property, which arrived here with `PROPOSER_LIMITS`'
    deletion: `tool_calls_limit` is the WORK ceiling and `request_limit` is only the runaway bound
    above it, so a request budget at or below the tool budget starves a legitimate draft before its
    work bound is reached. It was 6 against 24 and the first real night on staging died on it
    (2026-08-17). The derived half of the same property is
    `test_the_request_budget_cannot_bind_first_at_any_batch_size`, in this file.

    Not a duplicate of the test above it, and the difference is which change each one survives: the
    one above pins the SIZE this road was given and exists to make a shrink argue for itself; this
    pins the SHAPE, and it is what still has to hold on the day somebody moves that size with a
    measurement behind it."""
    # The literal 2, not `REQUEST_HEADROOM_OVER_TOOLS`: this road's constant is independent of the
    # edits road's formula, and reading the headroom from that formula would make this assertion
    # agree with any value the formula ever takes, including none.
    assert (repair_run.BODY_DRAFT_LIMITS.request_limit
            >= repair_run.BODY_DRAFT_LIMITS.tool_calls_limit + 2)


# ── the budget follows the findings a CALL carries, not the setting ───────────────────────────
def test_the_short_final_batch_pays_for_the_findings_it_actually_carries(conn, repo_env,
                                                                         monkeypatch):
    """Four findings at the default batch of three: one full call, then one carrying a single
    finding. The short one is handed `batch_limits(1)` and not the full batch's allowance.

    This is the one decision here a reviewer could reasonably have made differently — pay every
    call the same, on the grounds that the leftover at the end of a pass is as hard as any other
    finding — so it is pinned rather than left to be re-derived from `_propose_edits` by whoever
    reads it next."""
    recorder = _BudgetRecorder()
    monkeypatch.setattr(repair_run, "build_proposer", lambda *_a, **_k: recorder)
    _seed(conn, DEFAULT_BATCH_SIZE + 1)

    support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo))

    assert [size for size, _ in recorder.batches] == [DEFAULT_BATCH_SIZE, 1]
    assert recorder.batches[0][1] == repair_run.batch_limits(DEFAULT_BATCH_SIZE)
    assert recorder.batches[1][1] == repair_run.batch_limits(1), (
        "the short final batch was paid a full batch's allowance — the budget is following "
        "settings.batch_size again rather than the prompt")
    assert recorder.batches[0][1] != recorder.batches[1][1], (
        "every call in the pass got the same budget — a derived budget that does not vary with the "
        "prompt is the fixed one wearing a formula")


def test_an_operator_who_raises_the_batch_is_given_the_budget_that_goes_with_it(conn, repo_env,
                                                                                 monkeypatch):
    """`STIGMERGY_REPAIR_BATCH=8` is the OLD default, and rolling back to it has to be safe: the
    allowance rises with the batch instead of staying at 24 and starving it. A fixed budget failed
    exactly this, silently — the operator's knob went on working and the road behind it stopped
    proposing anything."""
    recorder = _BudgetRecorder()
    monkeypatch.setattr(repair_run, "build_proposer", lambda *_a, **_k: recorder)
    _seed(conn, 8)

    support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo, batch_size=8))

    ((size, limits),) = recorder.batches
    assert size == 8
    # Six per finding plus one finding's worth of orientation for the call itself.
    assert limits.tool_calls_limit == 54
    assert limits.tool_calls_limit > 24, (
        "a batch of eight is back on the old fixed ceiling — three reads per finding, which is the "
        "arithmetic that emptied the additive road on the first real corpus")


def test_the_smallest_batch_is_one_call_per_finding_and_each_one_is_paid_for(conn, repo_env,
                                                                             monkeypatch):
    """`STIGMERGY_REPAIR_BATCH=1` is where "more batches" becomes the cost question: the corpus
    that caught #75 is twenty-nine model calls at that setting, not two. Nobody is starved — each
    call gets a single finding's allowance — and the COUNT is asserted, so a change that made the
    small end cheap again by quietly regrouping findings has to come and say so here."""
    recorder = _BudgetRecorder()
    monkeypatch.setattr(repair_run, "build_proposer", lambda *_a, **_k: recorder)
    _seed(conn, REALISTIC_FINDING_COUNT)

    support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo, batch_size=1))

    assert len(recorder.batches) == REALISTIC_FINDING_COUNT
    assert all(size == 1 and limits == repair_run.batch_limits(1)
               for size, limits in recorder.batches)


# ── the body road: one entity per call, and the batch setting never reaches it ─────────────────
class _DrafterRecorder:
    """The package's own offline drafter, with the budget it was handed recorded on the way past.

    Wrapping rather than re-implementing: what is under test is the budget the ROAD hands a draft,
    and a double that answered differently from the one every other body-road test uses would be
    proving it about a road nothing else exercises."""

    def __init__(self):
        self.limits: list[object] = []
        self._drafter = repair_run.FakeEntityBodyDrafter()

    async def run(self, prompt, *, deps=None, usage_limits=None):
        self.limits.append(usage_limits)
        return await self._drafter.run(prompt, deps=deps, usage_limits=usage_limits)


@pytest.mark.parametrize("batch_size", [1, 8])
def test_the_body_roads_budget_reaches_the_drafter_whatever_the_batch_is(conn, repo_env,
                                                                         monkeypatch, batch_size):
    """`BODY_DRAFT_LIMITS` is what the drafter is actually run with — the constant asserted where
    it is USED, not only where it is declared — and `settings.batch_size` does not reach this road
    at either end of its range. One entity page per call is what that road does; a budget that
    moved with a batch it does not have would be a number derived from a fiction."""
    recorder = _DrafterRecorder()
    monkeypatch.setattr(repair_run, "build_entity_body_drafter", lambda *_a, **_k: recorder)
    support.seed_entity(repo_env, anchored=2)
    run_id = support.seed_gardener_run(conn)
    support.seed_placeholder_body(conn, run_id)

    result = support.run_pass(conn, repo_env,
                              RepairSettings(repo=repo_env.repo, batch_size=batch_size))

    assert result.applied == 1, result.skip_reasons
    assert recorder.limits == [repair_run.BODY_DRAFT_LIMITS]


# ── the corrective retry ──────────────────────────────────────────────────────────────────────
class _RejectedOnce:
    """Answers with a link that resolves to nothing, then — once the retry's brief arrives — with a
    real one, recording the budget each `agent.run` was handed."""

    def __init__(self):
        self.limits: list[object] = []

    async def run(self, prompt, *, deps=None, usage_limits=None):
        self.limits.append(usage_limits)
        link = ("a-page-that-does-not-exist" if len(self.limits) == 1
                else support.stem(support.NOTE_B))
        return fake_result(repair_run.ProposalBatch(proposals=[repair_run.ProposalSpec(
            finding_ids=[1],
            ops=[repair_run.EditOp(op="backlink", path=support.NOTE_A, link=link)],
            rationale="one backlink, wrong the first time")]))


def test_the_corrective_retry_is_handed_the_same_allowance_not_the_remainder():
    """Two `agent.run` calls, each given the caller's whole budget. A retry that inherited what the
    first call left over would be a brief the model cannot afford to answer — a call spent proving
    nothing, recorded as `usage-budget-exhausted` on a batch that had already done its reading."""
    double = _RejectedOnce()
    limits = repair_run.batch_limits(2)

    accepted, reasons = asyncio.run(repair_run.run_proposer(
        double, None, "## findings\n", corpus_paths={support.NOTE_A},
        link_names={support.stem(support.NOTE_B)}, finding_ids={1}, max_ops=6, max_proposals=20,
        usage_limits=limits))

    assert double.limits == [limits, limits]
    assert accepted and not reasons


# ── the real agent: the bound is enforced by the library that owns it ──────────────────────────
def _user_prompt(messages) -> str:
    """The prompt as the model received it — the first user part of THIS run's conversation, which
    is the retry's brief on a second `agent.run` and the batch prompt on the first."""
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                return str(part.content)
    return ""


class _ScriptedModel:
    """A model that READS pages through the real `read_page` tool before answering, then answers in
    the batch's own vocabulary: one backlink per finding, taken from the prompt's unfenced index
    exactly as the package's own offline double takes it (`_parse_finding_headers`), so it cannot
    be steered by page content either.

    `reads` is how many pages it opens per run; `runaway` never stops, which is what a genuinely
    exhausted batch looks like from outside; `flawed_first` answers with a link that resolves to
    nothing until the retry's brief arrives, so the corrective retry is a real second `agent.run`
    doing real work under a real budget.
    """

    # `FunctionModel` names itself after the callable it was given, and a class instance has no
    # `__name__` of its own.
    __name__ = "scripted-repair-model"

    def __init__(self, *, reads: int = 0, runaway: bool = False, flawed_first: bool = False):
        self.reads = reads
        self.runaway = runaway
        self.flawed_first = flawed_first
        self.reads_per_run: list[int] = []

    def __call__(self, messages, info) -> ModelResponse:
        prompt = _user_prompt(messages)
        so_far = sum(1 for m in messages for p in m.parts if isinstance(p, ToolCallPart))
        if so_far == 0:
            self.reads_per_run.append(0)
        if self.runaway or so_far < self.reads:
            self.reads_per_run[-1] += 1
            return ModelResponse(parts=[ToolCallPart("read_page", {"path": support.NOTE_A})])
        retrying = "VALIDATION ERROR" in prompt
        proposals = [
            {"finding_ids": [f["id"]],
             "ops": [{"op": "backlink", "path": f["pages"][0],
                      "link": ("a-page-that-does-not-exist" if self.flawed_first and not retrying
                               else support.stem(f["pages"][1]))}],
             "rationale": "scripted model: the backlink this finding asks for"}
            for f in repair_run._parse_finding_headers(prompt)]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
                                                 {"proposals": proposals})])


@contextlib.contextmanager
def _real_agent(monkeypatch, script):
    """A REAL pydantic-ai agent — the production builder, the production tools, the production
    limits object — driven by pydantic-ai's own `FunctionModel`.

    Keyless, and honest about what is faked: `kernel.llm.model_override` is the PUBLIC seam where
    a provider is replaced (issue #81 — this used to require reaching into `build_model`, another
    package's private-by-convention function), and it stands in for the external service while
    leaving everything this file is about untouched. The BUDGET is not faked — the library counts
    the tool calls and raises `UsageLimitExceeded` itself, which is precisely why the properties
    below are not written with a double.
    """
    monkeypatch.setenv("CLEAN_LLM", "openai")
    with kernel_llm.model_override(FunctionModel(script)):
        yield


def _seed_three_distinct(conn, repo_env) -> int:
    """Three unlinked-mention findings, each naming a different pair of ASCII-stemmed pages — so a
    batch at the default size can produce three DISTINCT repairs and the count means something."""
    other = support.write_note(repo_env, "Another Note")
    run_id = support.seed_gardener_run(conn)
    for pages in ((support.NOTE_A, support.DECISION), (support.DECISION, other),
                  (other, support.NOTE_A)):
        support.seed_unlinked_mention(conn, run_id, pages=pages)
    return run_id


def test_a_batch_that_legitimately_explores_comes_back_with_its_repairs(conn, repo_env,
                                                                        monkeypatch):
    """The benign twin, and in this file it is the one that matters most: every number here can
    bounce somebody's real work, and a file that only proves budgets are ENFORCED measures their
    sensitivity and never their specificity.

    A full default batch whose model reads six pages for EVERY finding — the floor
    `MIN_TOOL_CALLS_PER_FINDING` names, and twice what the old constant afforded a batch of eight —
    completes and lands all three repairs. Real agent, real tools, real checkout, real enforcement:
    "is 24 enough for three findings that actually explore" is a question about pydantic-ai, and a
    double that counted its own tool calls would answer a different one."""
    model = _ScriptedModel(reads=repair_run.MIN_TOOL_CALLS_PER_FINDING * DEFAULT_BATCH_SIZE)
    _seed_three_distinct(conn, repo_env)
    before = support.commit_count(repo_env.bare)

    with _real_agent(monkeypatch, model):
        result = support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo))

    assert model.reads_per_run == [repair_run.MIN_TOOL_CALLS_PER_FINDING * DEFAULT_BATCH_SIZE]
    assert [r for r in result.skip_reasons if "usage-budget" in r] == [], (
        "a batch reading the per-finding floor lapsed — the budget bounces the exploration it "
        "exists to pay for")
    assert result.applied == 3, result.skip_reasons
    assert support.commit_count(repo_env.bare) == before + 3


def test_a_retry_starts_from_a_fresh_budget_and_the_library_agrees(conn, repo_env, monkeypatch):
    """`run_proposer` hands the retry the same `UsageLimits` OBJECT it handed the first call, which
    is only a fresh budget if pydantic-ai applies limits per `agent.run` rather than accumulating
    against the object. That is third-party behaviour and it could change under us, so it is
    exercised rather than assumed: two runs, each reading nine pages under a single finding's
    twelve, and a cumulative reading of the same object would lapse the second one.

    Read as a benign twin too — the whole headroom argument is that a corrected answer is
    affordable, and a retry that could not pay for itself would turn every rejected batch into a
    recorded skip."""
    model = _ScriptedModel(reads=9, flawed_first=True)
    run_id = support.seed_gardener_run(conn)
    support.seed_unlinked_mention(conn, run_id, pages=(support.NOTE_A, support.DECISION))

    with _real_agent(monkeypatch, model):
        result = support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo))

    assert model.reads_per_run == [9, 9], "the corrective retry never happened"
    assert result.applied == 1, result.skip_reasons


def test_a_genuinely_exhausted_batch_is_skipped_whole_and_the_body_road_still_runs(
        conn, repo_env, monkeypatch):
    """#73's degradation contract, re-pinned at the NEW default and with a REAL lapse: the model
    keeps reading until pydantic-ai stops it, so the exception comes from the code that owns the
    bound instead of from a double that raises the exception's name.

    A lapse is a fact about one batch. Both edits batches are skipped whole, each reason lands in
    `job_runs.stats`, the pass still records itself `ok`, and the body road — the half that was
    producing on the staging night — still gets its call and still lands its commit.

    The drafter is the offline double on purpose: with a runaway model wired at the provider seam,
    the body road would run away too, and what this test asks of that road is only that it still
    RAN."""
    model = _ScriptedModel(runaway=True)
    monkeypatch.setattr(repair_run, "build_entity_body_drafter",
                        lambda *_a, **_k: repair_run.FakeEntityBodyDrafter())
    support.seed_entity(repo_env, anchored=2)
    run_id = _seed_three_distinct(conn, repo_env)
    support.seed_unlinked_mention(conn, run_id,
                                  pages=(support.NOTE_A, "wiki/notes/Another Note.md"))
    support.seed_placeholder_body(conn, run_id)

    with _real_agent(monkeypatch, model):
        result = support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo))

    budget_reasons = [r for r in result.skip_reasons if "usage-budget-exhausted" in r]
    assert len(budget_reasons) == 2, result.skip_reasons
    assert "batch of 3 finding(s)" in budget_reasons[0]
    assert "batch of 1 finding(s)" in budget_reasons[1]
    # The first batch was cut off AT its work ceiling, not before it: the request budget did not
    # bind first. That is the staging failure #73 fixed (request_limit 6 against 24 tool calls),
    # asserted here as behaviour rather than as arithmetic about two constants.
    assert model.reads_per_run[0] >= repair_run.batch_limits(DEFAULT_BATCH_SIZE).tool_calls_limit
    # The pass is degraded, not dead: the body road landed, and the row says `ok`.
    assert result.applied == 1
    (row,) = store.recent(conn)
    assert row["kind"] == schema.KIND_ENTITY_BODY
    assert row["status"] == schema.STATUS_APPLIED
    with conn.cursor() as cur:
        cur.execute("SELECT status, stats FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1",
                    (repair_run.JOB_NAME,))
        status, stats = cur.fetchone()
    assert status == "ok"
    assert stats["skip_reasons"] == result.skip_reasons


class _BudgetBlownDrafter:
    """A drafter whose call lapses, the way pydantic-ai lapses one: the exception, from the seam
    the production code catches it at."""

    async def run(self, prompt, *, deps=None, usage_limits=None):
        raise UsageLimitExceeded("the next tool call(s) would exceed the tool_calls_limit of 24")


def test_a_lapsed_body_draft_costs_one_page_and_the_edits_roads_repair_survives(
        conn, repo_env, monkeypatch):
    """The body road's half of the degradation contract, and nothing exercised it: a draft that
    lapses names the PAGE it was drafting in `job_runs.stats`, the pass still records itself `ok`,
    and the additive road's commit from the same night is still on the remote.

    A double raises here rather than a runaway model — the real lapse is exercised on the edits
    road above, and what is untested is this road's HANDLER: one lapse is one entity page, never
    the pass and never the other road's work."""
    monkeypatch.setattr(repair_run, "build_entity_body_drafter",
                        lambda *_a, **_k: _BudgetBlownDrafter())
    support.seed_entity(repo_env, anchored=2)
    run_id = support.seed_gardener_run(conn)
    support.seed_unlinked_mention(conn, run_id, pages=(support.NOTE_A, support.DECISION))
    support.seed_placeholder_body(conn, run_id)

    result = support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo))

    assert any("usage-budget-exhausted" in reason and support.ENTITY_PAGE in reason
               for reason in result.skip_reasons), result.skip_reasons
    assert result.applied == 1
    (row,) = store.recent(conn)
    assert row["kind"] == schema.KIND_EDITS
    assert "[[a-decision-from-a-previous-meeting]]" in support.remote_page(repo_env.bare,
                                                                          support.NOTE_A)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1",
                    (repair_run.JOB_NAME,))
        assert cur.fetchone()[0] == "ok"


# ── the OTHER budget in a pass, and the same rule: do not pay for what you cannot use ──────────
def test_a_pass_whose_ceiling_filled_on_the_edits_road_never_pays_for_a_body_draft(
        conn, repo_env, monkeypatch):
    """When the pass's ceiling is already full, the body road records the ceiling reason INSTEAD of
    making a call. Asserted by making the ask FAIL: a pass that called the drafter and threw the
    answer away would look identical from the outside and cost the same money every night."""
    def refuse(*_a, **_k):
        raise AssertionError("a body drafter was built after the pass's ceiling was already full")

    monkeypatch.setattr(repair_run, "build_entity_body_drafter", refuse)
    support.seed_entity(repo_env, anchored=2)
    run_id = support.seed_gardener_run(conn)
    support.seed_unlinked_mention(conn, run_id, pages=(support.NOTE_A, support.DECISION))
    support.seed_placeholder_body(conn, run_id)

    result = support.run_pass(conn, repo_env,
                              RepairSettings(repo=repo_env.repo, max_repairs_per_run=1))

    assert result.applied == 1
    assert any("run-ceiling-reached(1)" in reason for reason in result.skip_reasons)


# ── the spend record: the feedback loop the budgets never had (issue #81) ──────────────────────
def test_every_model_call_records_its_spend_beside_the_ceiling_it_ran_under(conn, repo_env,
                                                                            monkeypatch):
    """`job_runs.stats` recorded that a batch LAPSED and never how close a successful one came, so
    `MIN_TOOL_CALLS_PER_FINDING` was a reasoned number with no feedback loop behind it — the only
    signal the system produced was the failure the number was chosen to prevent (issue #75's whole
    shape). One record per call, with the limits the call ran under and pydantic-ai's own counts,
    persisted where an operator already reads the pass."""
    model = _ScriptedModel(reads=4)
    run_id = support.seed_gardener_run(conn)
    support.seed_unlinked_mention(conn, run_id, pages=(support.NOTE_A, support.DECISION))

    with _real_agent(monkeypatch, model):
        result = support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo))

    (call,) = result.model_calls
    assert call["road"] == schema.KIND_EDITS
    assert call["tool_calls_limit"] == repair_run.batch_limits(1).tool_calls_limit
    assert call["request_limit"] == repair_run.batch_limits(1).request_limit
    # pydantic-ai's OWN counts: `tool_calls` is the four reads (the structured ANSWER counts
    # against the LIMIT but not in this usage figure — `batch_limits`' own comment holds for the
    # bound, not the meter), and `requests` is one per model round: four reads plus the answer.
    assert call["tool_calls"] == 4
    assert call["requests"] == 5
    with conn.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1",
                    (repair_run.JOB_NAME,))
        stats = cur.fetchone()[0]
    assert stats["model_calls"] == result.model_calls, (
        "the durable row and the operator's screen disagree about what was spent")


def test_the_offline_double_records_the_limits_even_when_it_spends_nothing(conn, repo_env,
                                                                           monkeypatch):
    """The keyless twin: under `CLEAN_LLM=fake` the double reports zero usage, and the record
    still lands with the LIMITS right — the plumbing is the same code path a real night runs, so
    a broken persistence would be visible on any laptop."""
    monkeypatch.setenv("CLEAN_LLM", "fake")
    run_id = support.seed_gardener_run(conn)
    support.seed_unlinked_mention(conn, run_id, pages=(support.NOTE_A, support.DECISION))

    result = support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo))

    (call,) = result.model_calls
    assert call["road"] == schema.KIND_EDITS
    assert call["tool_calls_limit"] == repair_run.batch_limits(1).tool_calls_limit
    assert call["tool_calls"] == 0 and call["requests"] == 0


class _BodyScript:
    """The body road's scripted model: reads EVERY page the prompt's index names through the real
    `read_page` tool, then answers with a valid draft citing one of them — the biggest single
    brief this system builds, driven at full size."""

    __name__ = "scripted-body-model"

    def __init__(self):
        self.reads_per_run: list[int] = []

    def __call__(self, messages, info) -> ModelResponse:
        prompt = _user_prompt(messages)
        pages = [line.split(repair_run._PAGE_LINE, 1)[1] for line in prompt.splitlines()
                 if line.startswith(repair_run._PAGE_LINE)]
        so_far = sum(1 for m in messages for p in m.parts if isinstance(p, ToolCallPart))
        if so_far == 0:
            self.reads_per_run.append(0)
        if so_far < len(pages):
            self.reads_per_run[-1] += 1
            return ModelResponse(parts=[ToolCallPart("read_page", {"path": pages[so_far]})])
        body = ("## What / Who\n\nAcme Corp is what the anchored pages describe.\n\n## Facts\n\n"
                + "\n".join(f"- noted in [[{p.rsplit('/', 1)[-1].removesuffix('.md')}]]"
                            for p in pages[:3]))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
                                                 {"body_markdown": body, "role": ""})])


def test_the_largest_brief_this_system_builds_completes_at_full_size(conn, repo_env, monkeypatch):
    """Issue #81 item 5: `MAX_ANCHORED_PAGES` fenced pages in one prompt plus a read of every one
    of them is the biggest single model brief here, and nothing had ever driven that shape — so
    whether `BODY_DRAFT_LIMITS` can actually pay for it was folklore. Real agent, real tools, the
    library's own enforcement: ten fenced pages in, ten reads, a rewritten page on the remote,
    under budget."""
    support.seed_entity(repo_env, anchored=repair_run.MAX_ANCHORED_PAGES + 2)
    run_id = support.seed_gardener_run(conn)
    support.seed_placeholder_body(conn, run_id)
    model = _BodyScript()

    with _real_agent(monkeypatch, model):
        result = support.run_pass(conn, repo_env, RepairSettings(repo=repo_env.repo))

    assert model.reads_per_run == [repair_run.MAX_ANCHORED_PAGES], (
        "the brief did not carry the full anchored-page ceiling")
    assert result.applied == 1, result.skip_reasons
    (call,) = result.model_calls
    assert call["road"] == schema.KIND_ENTITY_BODY
    assert call["tool_calls"] == repair_run.MAX_ANCHORED_PAGES
    assert call["requests"] == repair_run.MAX_ANCHORED_PAGES + 1     # + the answering round
    assert call["tool_calls_limit"] == repair_run.BODY_DRAFT_LIMITS.tool_calls_limit
