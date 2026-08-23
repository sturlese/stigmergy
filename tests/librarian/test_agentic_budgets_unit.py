"""What BOUNDS an iterating filing run, and what it costs to have iterated (the agentic pydantic harness, D5–D6).

Two ceilings and one instrument, all three exercised against a real `pydantic_ai.Agent` driven by a
scripted `FunctionModel` over a real git checkout. No key, no network, no provider: the model is
injected through the backend's own `model_factory` seam, and the PRICE is looked up by the
CONFIGURED id regardless — which is the property that makes an offline run price exactly the
arithmetic a paid one would.

**The two ceilings are not substitutes and this file keeps them apart.** `settings.max_turns` is the
REQUEST ceiling (`UsageLimits(request_limit=…)`) — how many times the model may go round with its
tools — and `settings.timeout_s` is the wall clock the whole run is wrapped in. Thirty fast requests
fit inside five minutes and one hanging provider fills it with nothing, so a suite that proved one
would say nothing about the other.

**Every ceiling here has its benign twin, at the boundary rather than far from it.** A budget test
that only ever trips measures a gate's sensitivity and never its specificity, and this gate can
refuse a capture whose filing genuinely needed the looking — so the same script is run at the number
that must pass and at the number that must not.

**The money half is the reason the backend exists.** A run that priced at nothing would report every
filing as free, and free is the one direction this instrument must never be wrong in — so the cost
is asserted to GROW with the requests that produced it, to be reproducible, and to move with the
configured model's own row in `pricing.PRICES` rather than with whatever the seam injected.
"""
import dataclasses
import json
import math
import pathlib

import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import config, pricing, report
from stigmergy.librarian.errors import AgentError, OutcomeShapeError
from stigmergy.librarian.filing_port import AgentRun
from stigmergy.librarian.pydantic_backend import PydanticFilingAgent
from tests.librarian import support

PRICED_MODEL = "openai:gpt-5.6-terra"
MATERIAL = "The Acme Corp renewal window was confirmed at the sync."

# A well-formed account in the LEGACY envelope's shape — the agent names the page it wrote and
# carries no page text, which is what `structured_ordinary = False` obliges. Written to the outcome
# FILE by the model's own `write_page` call, because that is the whole channel change in the agentic pydantic harness.
ACCOUNT = {
    "decision": "file",
    "page_path": "wiki/notes/Acme Corp Renewal Window.md",
    "page_type": "note",
    "title": "Acme Corp Renewal Window",
    "anchoring": {"kind": "entity", "entities": ["Acme Corp"], "reason": ""},
    "links_created": [], "overlaps": [], "edits": [], "findings": [],
    "summary": "filed the renewal note",
}


def _settings(**overrides) -> config.Settings:
    return dataclasses.replace(
        config.Settings(repo="/nonexistent/knowledge-repo", model=PRICED_MODEL), **overrides)


def _repo(tmp_path, name: str = "git") -> str:
    """A real checkout: the tools read `git ls-files`, so a run that writes needs one."""
    return support.build_repo(str(tmp_path / name)).repo


def _writing_model(*, searches: int = 0, account: dict | None = None, delay: float = 0.0):
    """A `FunctionModel` that searches `searches` times, writes an account, then says something the
    backend ignores.

    One request per turn, so the request COUNT is a property of the script rather than of the
    model's mood: `searches` tool-calling requests, one write, one final message — `searches + 2`
    requests and `searches + 1` tool calls, which is what the budget cases below are pinned to.

    `delay` is how long each request takes to come back, for the wall clock's benign twin: a model
    that answers instantly proves nothing about a bound measured in seconds.
    """
    import asyncio

    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    async def _script(messages, info):
        if delay:
            await asyncio.sleep(delay)
        turn = len([m for m in messages if m.kind == "request"])
        if turn <= searches:
            return ModelResponse(parts=[ToolCallPart("search_pages", {"query": "renewal window"})])
        if turn == searches + 1:
            return ModelResponse(parts=[ToolCallPart(
                "write_page", {"path": agent_module.OUTCOME_FILENAME,
                               "content": json.dumps(account or ACCOUNT)})])
        return ModelResponse(parts=[TextPart("filed it")])

    return FunctionModel(_script)


def _looping_model():
    """A model that searches for ever and never writes anything — the shape the request ceiling
    exists for, and the shape no timeout would catch quickly (each request returns instantly)."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    def _script(messages, info):
        return ModelResponse(parts=[ToolCallPart("search_pages", {"query": "renewal"})])

    return FunctionModel(_script)


def _slow_model(seconds: float):
    """A model that takes `seconds` to answer at all — the shape the WALL CLOCK exists for, and the
    one the request ceiling never sees (it is one request, and it never returns)."""
    import asyncio

    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    async def _script(messages, info):
        await asyncio.sleep(seconds)
        return ModelResponse(parts=[TextPart("eventually")])

    return FunctionModel(_script)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# AC2 — the REQUEST ceiling: `settings.max_turns` -> `UsageLimits(request_limit=…)`
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_a_model_that_loops_for_ever_is_stopped_by_the_request_ceiling(tmp_path):
    """**The fault is caught BY NAME and it names the variable that changes it.**

    The blanket arm one branch down would have reported this as "the filing agent run failed
    (UsageLimitExceeded)" — a class name, at somebody who can fix this in one environment variable.
    A capture whose filing genuinely needs more looking than the ceiling allows is a legitimate
    reason to raise it; a model looping is a reason not to. Neither is expressible if the message
    says only that something went wrong.

    **It is a bare `AgentError` and not an `OutcomeShapeError`, and that is a routing decision
    rather than a class choice**: `processing._run_in_worktree` retries the shape family and only
    the shape family, so a blown budget lands terminal instead of buying a second full loop at the
    same ceiling.
    """
    settings = _settings(max_turns=4)
    backend = PydanticFilingAgent(settings, model_factory=_looping_model)

    with pytest.raises(AgentError) as exc_info:
        backend.run(worktree=_repo(tmp_path), material=MATERIAL, hints={},
                    submitted_by="a@b.test", gathered="")

    message = str(exc_info.value)
    assert type(exc_info.value) is AgentError, (
        "a blown iteration budget was routed to the corrective retry, which would spend a second "
        "full loop reaching the same ceiling")
    assert not isinstance(exc_info.value, OutcomeShapeError)
    assert "4" in message and "$STIGMERGY_LIBRARIAN_MAX_TURNS" in message, message
    assert "UsageLimitExceeded" not in message, (
        "the blanket arm caught it: an operator got a class name where the budget was named")


def test_the_blown_budget_still_says_what_it_cost(tmp_path):
    """A loop that ran to its ceiling spent real money on every request in it, and the row an
    operator reads is composed from the exception (`processing` banks `run_cost_usd` off it). The
    figure has to be POSITIVE here, not merely present: the whole failure mode this instrument
    exists to close is a paid run reported as free."""
    backend = PydanticFilingAgent(_settings(max_turns=4), model_factory=_looping_model)

    with pytest.raises(AgentError) as exc_info:
        backend.run(worktree=_repo(tmp_path), material=MATERIAL, hints={},
                    submitted_by="a@b.test", gathered="")

    spend = getattr(exc_info.value, "run_cost_usd", None)
    assert isinstance(spend, float) and math.isfinite(spend) and spend > 0, (
        f"four requests were paid for and the fault reports {spend!r}")


@pytest.mark.parametrize("ceiling, trips", [(3, True), (4, False)],
                         ids=["one-request-short", "exactly-enough"])
def test_the_ceiling_is_measured_at_the_boundary_in_both_directions(tmp_path, ceiling, trips):
    """**The benign twin, at the number rather than near it.**

    One script — two searches, a write, a final message, so four requests — run at a ceiling one
    below what it needs and at exactly what it needs. A gate that only ever fires is unmeasured,
    and this one can refuse somebody's real capture: `request_limit` is the difference between "the
    model would not stop" and "the model needed one more look than the operator budgeted".
    """
    settings = _settings(max_turns=ceiling)
    backend = PydanticFilingAgent(settings, model_factory=lambda: _writing_model(searches=2))

    if trips:
        with pytest.raises(AgentError, match="STIGMERGY_LIBRARIAN_MAX_TURNS"):
            backend.run(worktree=_repo(tmp_path), material=MATERIAL, hints={},
                        submitted_by="a@b.test", gathered="")
        return

    run = backend.run(worktree=_repo(tmp_path), material=MATERIAL, hints={},
                      submitted_by="a@b.test", gathered="")
    assert run.outcome.decision == "file", (
        "a run that fits its budget exactly was refused — the ceiling is off by one")


@pytest.mark.parametrize("bad", [0, 1])
def test_the_backend_passes_max_turns_STRAIGHT_THROUGH_no_silent_clamp(tmp_path, bad):
    """**The number is passed straight to `UsageLimits`, NOT `max(..., 1)`** — the silent clamp was
    removed (M3). An earlier version rewrote a `0`/`1` up to a usable ceiling inside the backend,
    which is exactly the "the process quietly changed the value I set" failure this package refuses:
    `worker.startup_checks` now REFUSES `max_turns < 2` by name before a single item is claimed
    (`test_pydantic_preflight.py`), so a run that reaches the backend has a usable ceiling.

    Driven at the backend directly, which bypasses `startup_checks` — that is the point: with no
    clamp, a `0` or `1` reaching the loop trips the request ceiling on its own tokens rather than
    being silently promoted, and the fault still names the variable an operator would raise.
    """
    backend = PydanticFilingAgent(_settings(max_turns=bad),
                                  model_factory=lambda: _writing_model(searches=1))

    with pytest.raises(AgentError, match="STIGMERGY_LIBRARIAN_MAX_TURNS") as exc_info:
        backend.run(worktree=_repo(tmp_path), material=MATERIAL, hints={},
                    submitted_by="a@b.test", gathered="")

    assert f"used all {bad} of its" in str(exc_info.value), (
        f"the ceiling was clamped: the fault does not report the {bad} the operator set")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# AC2 — the WALL CLOCK, which the request ceiling cannot stand in for
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_a_provider_that_never_answers_is_cut_off_by_the_wall_clock_and_priced_at_zero(tmp_path):
    """**`0.0` is the HONEST figure here, and it is the only place in this file where it is.**

    Nothing ever arrived to price: the request was made and no response came back, so the usage
    accumulator holds nothing. Reporting a guess would be worse than reporting zero — but the FIELD
    has to be there, because `processing` reads `getattr(ex, "run_cost_usd", 0.0)` and cannot
    otherwise tell "spent nothing" from "nobody attached it".

    The budget is named in seconds because the operator's fix is a different number, and the fault
    is a bare `AgentError` for the same routing reason the request ceiling is.
    """
    backend = PydanticFilingAgent(_settings(timeout_s=0), model_factory=lambda: _slow_model(30))

    with pytest.raises(AgentError) as exc_info:
        backend.run(worktree=_repo(tmp_path), material=MATERIAL, hints={},
                    submitted_by="a@b.test", gathered="")

    assert "0s budget" in str(exc_info.value)
    assert hasattr(exc_info.value, "run_cost_usd"), "a timeout carried no spend field at all"
    assert exc_info.value.run_cost_usd == 0.0
    assert type(exc_info.value) is AgentError


def test_a_run_that_is_merely_slow_finishes_inside_its_budget(tmp_path):
    """The wall clock's benign twin, and it is REALLY slow rather than nominally so: every request
    in this run takes time to come back, which is the ordinary case — a provider always does.

    A bound that only ever fires is unmeasured, and this one wraps the WHOLE run: a timeout
    implemented per request rather than per run would pass the hostile case above and let a loop of
    thirty slow requests outlive the visibility lease, which is a capture two workers file.
    """
    backend = PydanticFilingAgent(_settings(timeout_s=30),
                                  model_factory=lambda: _writing_model(searches=2, delay=0.05))

    run = backend.run(worktree=_repo(tmp_path), material=MATERIAL, hints={},
                      submitted_by="a@b.test", gathered="")

    assert run.outcome.decision == "file"
    assert run.turns == 4, "the slow twin did not make the requests it was scripted to make"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# AC2 — the counters, and what downstream does with them (D6)
# ══════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("searches", [0, 1, 3])
def test_the_envelope_carries_the_frameworks_own_counts_and_not_an_invented_one(tmp_path, searches):
    """**Exact numbers, not "more than zero".** The script makes `searches + 2` requests and
    `searches + 1` tool calls by construction, so a backend that hardcoded a `1`, counted its own
    wrappers, or reported the tool calls as turns fails here rather than looking plausible.

    `RunUsage` is handed IN rather than read off the result, which is what makes these the
    framework's own accumulated numbers rather than a second count kept beside them — the duplicate
    answer this package refuses everywhere else.
    """
    backend = PydanticFilingAgent(_settings(),
                                  model_factory=lambda: _writing_model(searches=searches))

    run = backend.run(worktree=_repo(tmp_path), material=MATERIAL, hints={},
                      submitted_by="a@b.test", gathered="")

    assert (run.turns, run.tool_calls) == (searches + 2, searches + 1), (
        f"turns={run.turns} tool_calls={run.tool_calls} for a script that searched {searches} "
        f"time(s) and wrote once")


def test_the_counters_are_read_defensively_off_whatever_usage_object_arrives():
    """`_counted`, unit-tested at the one seam that can reach it — **returning-road only now**
    (Finding 2, option b).

    The four fault arms no longer call it: a fault raises rather than returns, so its envelope is
    discarded, and putting loop counters on an object nobody holds is a dead assignment. `_counted`
    is `(run, usage) -> None` and runs once, on the returning road, where the envelope self-describes.

    Both reads stay `getattr` with a zero default, for the reason they always were: the framework's
    usage object has grown fields before and an injected offline model may hand back a simpler one,
    and zero is a legitimate envelope value (the port says so), so a usage object missing the fields
    must produce zeros rather than an `AttributeError`.
    """
    class Usage:
        requests = 9
        tool_calls = 4

    class Older:
        pass

    run = AgentRun()
    assert PydanticFilingAgent._counted(run, Usage()) is None, (
        "_counted no longer returns the exception — it is returning-road only and returns None")
    assert (run.turns, run.tool_calls) == (9, 4)

    bare = AgentRun()
    PydanticFilingAgent._counted(bare, Older())
    assert (bare.turns, bare.tool_calls) == (0, 0)


def test_a_blown_budget_fault_carries_no_loop_counters_by_decision(tmp_path):
    """**Finding 2 resolved as option b, pinned so the narrowing does not silently regress.** the
    agentic pydantic harness was narrowed: `turns`/`tool_calls` are real on the RETURNING road and
    are deliberately NOT
    attached to a fault. The only thing a fault must carry is the spend (`run_cost_usd`), which
    `report.failed_system` reads; nothing downstream reads a counter off an exception.

    So the exception raised by a blown request ceiling carries `run_cost_usd` and neither counter —
    asserted directly, because the alternative (re-attaching them on the fault arms) would look
    harmless and reintroduce the dead assignment the developer removed.
    """
    backend = PydanticFilingAgent(_settings(max_turns=3), model_factory=_looping_model)

    with pytest.raises(AgentError) as exc_info:
        backend.run(worktree=_repo(tmp_path), material=MATERIAL, hints={},
                    submitted_by="a@b.test", gathered="")

    assert hasattr(exc_info.value, "run_cost_usd"), "the fault dropped the one field it must carry"
    assert not hasattr(exc_info.value, "turns") and not hasattr(exc_info.value, "tool_calls"), (
        "a loop counter was attached to a fault — the D6 narrowing (Finding 2, option b) regressed")


def test_the_submitters_report_carries_no_turn_counter_for_anything_to_choke_on():
    """The port's own claim, pinned where it would break: `report.filed` composes what a submitter
    and an operator read, and it has no field for a loop's counters at all. So a backend that
    started reporting real `turns` changes nothing downstream — there is no key to
    collide with, no consumer to surprise, and no `jsonb` column that grew.

    Asserted over every KEY the report carries, at any depth, rather than over a hand-picked one —
    the failure this rules out is a counter arriving under a name nobody predicted. Keys and not the
    flattened JSON, because the report also carries PROSE, and a summary that happens to contain the
    word "returns" is not a field called `turns`.
    """
    filed = report.filed(page_path="wiki/notes/A Page.md", commit="abc1234",
                         anchoring={"kind": "entity", "entities": ["Acme Corp"]},
                         links=["Acme Corp"], overlaps=[], findings=[],
                         agent_rationale="filed the renewal note")

    counters = [key for key in _every_key(filed)
                if any(word in key.lower() for word in ("turn", "tool_call", "request"))]
    assert not counters, f"`report.filed` has grown a loop counter: {counters}"


def _every_key(node) -> list:
    """Every mapping key in a nested structure, at any depth."""
    if isinstance(node, dict):
        return [str(key) for key in node] + [k for value in node.values()
                                             for k in _every_key(value)]
    if isinstance(node, (list, tuple)):
        return [k for value in node for k in _every_key(value)]
    return []


# ══════════════════════════════════════════════════════════════════════════════════════════════
# AC3 — the money: usage accumulates PER REQUEST, through the M1 pricing seam
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_an_iterating_run_costs_strictly_more_the_more_it_iterates(tmp_path):
    """**The claim the agentic pydantic harness's cost consequence rests on**, measured rather than assumed: the usage
    accumulator is handed to `Agent.run` and pydantic-ai adds every request's tokens to it, so a run
    that searched four times costs strictly more than the same run that searched none.

    Monotonicity is the part worth pinning, and it is why this is ONE test over a series rather than
    four parametrized cases: a backend that priced only the LAST response — the shape a
    `result.usage()` read would produce if the framework ever changed what that returns — reports a
    perfectly positive figure for every case individually, and the defect is visible only in the
    series being flat.

    Four independent runs: its own checkout, its own `Agent`, its own accumulator each time.
    """
    costs = []
    for index, searches in enumerate((0, 1, 2, 4)):
        backend = PydanticFilingAgent(_settings(),
                                      model_factory=lambda s=searches: _writing_model(searches=s))
        run = backend.run(worktree=_repo(tmp_path, f"git{index}"), material=MATERIAL, hints={},
                          submitted_by="a@b.test", gathered="")
        costs.append(run.cost_usd)

    assert all(cost > 0 for cost in costs), f"a real framework run priced at nothing: {costs}"
    assert costs == sorted(costs) and len(set(costs)) == len(costs), (
        f"the cost did not grow strictly with the number of requests: {costs}")
    assert costs[-1] > costs[0], "four searches cost no more than none — usage is not accumulating"


def test_two_identical_runs_cost_exactly_the_same(tmp_path):
    """Determinism, asserted for its own sake AND because the price-table cases below depend on it:
    "doubling the rate doubled the bill" means nothing if the same script bills differently twice.

    Two separate checkouts, so what is reproducible is the ARITHMETIC over a script rather than one
    directory's accidental state.
    """
    first = PydanticFilingAgent(_settings(), model_factory=lambda: _writing_model(searches=1))
    second = PydanticFilingAgent(_settings(), model_factory=lambda: _writing_model(searches=1))

    one = first.run(worktree=_repo(tmp_path, "a"), material=MATERIAL, hints={},
                    submitted_by="a@b.test", gathered="")
    two = second.run(worktree=_repo(tmp_path, "b"), material=MATERIAL, hints={},
                     submitted_by="a@b.test", gathered="")

    assert one.cost_usd == two.cost_usd


def test_the_bill_is_computed_from_the_CONFIGURED_models_own_row(tmp_path, monkeypatch):
    """**Which id the arithmetic is keyed on, proven by moving the price rather than by reading the
    code.**

    The model that answers is a `FunctionModel` injected through the offline seam; the id in
    `settings.model` is what `pricing.compute_cost_usd` is asked about. Doubling that row's three
    rates doubles the bill for the same script — which no lookup keyed on the injected model, and no
    hardcoded rate, could do.

    Compared with a one-cent-of-a-cent tolerance because both figures are rounded to six decimals
    independently (`compute_cost_usd` rounds its own result), so "exactly twice" is twice plus at
    most one rounding step.
    """
    base = PydanticFilingAgent(_settings(), model_factory=lambda: _writing_model(searches=1))
    single = base.run(worktree=_repo(tmp_path, "a"), material=MATERIAL, hints={},
                      submitted_by="a@b.test", gathered="")

    rates = pricing.PRICES[PRICED_MODEL]
    monkeypatch.setenv(pricing.PRICING_ENV,
                       json.dumps({PRICED_MODEL: [r * 2 for r in rates]}))
    dearer = PydanticFilingAgent(_settings(), model_factory=lambda: _writing_model(searches=1))
    doubled = dearer.run(worktree=_repo(tmp_path, "b"), material=MATERIAL, hints={},
                         submitted_by="a@b.test", gathered="")

    assert abs(doubled.cost_usd - 2 * single.cost_usd) <= 2e-6, (
        f"the price table moved and the bill did not follow it: {single.cost_usd} -> "
        f"{doubled.cost_usd}")


def test_pricing_a_DIFFERENT_model_leaves_this_runs_bill_alone(tmp_path, monkeypatch):
    """The specificity half of the twin above. A cost that moved when ANY row moved would pass the
    doubling test while being keyed on nothing in particular — this is what says the lookup is by
    the configured id and not by "whatever the table happens to hold"."""
    base = PydanticFilingAgent(_settings(), model_factory=lambda: _writing_model(searches=1))
    before = base.run(worktree=_repo(tmp_path, "a"), material=MATERIAL, hints={},
                      submitted_by="a@b.test", gathered="")

    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({"openai:gpt-9": [99.0, 99.0, 99.0]}))
    other = PydanticFilingAgent(_settings(), model_factory=lambda: _writing_model(searches=1))
    after = other.run(worktree=_repo(tmp_path, "b"), material=MATERIAL, hints={},
                      submitted_by="a@b.test", gathered="")

    assert after.cost_usd == before.cost_usd


def test_the_account_read_off_the_file_channel_is_the_one_that_travels(tmp_path):
    """The returning road's other half, stated here because it is what the money was spent ON: the
    final message is ignored by design and the account comes back from `.librarian-outcome.json`,
    already through `agent.parse_outcome`'s bounds. A model that says "I filed it" in prose and
    wrote no file has filed nothing — and the channel is DRAINED on the way out, so the account can
    never reach the diff `processing` takes a moment later."""
    repo = _repo(tmp_path)
    backend = PydanticFilingAgent(_settings(), model_factory=lambda: _writing_model(searches=1))

    run = backend.run(worktree=repo, material=MATERIAL, hints={}, submitted_by="a@b.test",
                      gathered="")

    assert run.outcome.page_path == ACCOUNT["page_path"]
    assert run.outcome.page is None, "the legacy envelope carries no page TEXT — the agent wrote it"
    assert not pathlib.Path(repo, agent_module.OUTCOME_FILENAME).exists()
