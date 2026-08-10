"""The dollar figure the SDK reports per agent run reaches the row a person reads.

OLD BEHAVIOUR: `AgentRun.cost_usd` was captured from the SDK's ResultMessage and died with the
run object — no report key, no log line — so a week of staged filings could not answer "what did
filing this cost?" without the provider's own dashboard (ADR 031, D2). These are pure unit tests
over the three seams the number now travels: the per-pass bank (`AgentPasses.cost_usd`), the
Result stamp (`_stamp_cost`), and the exception road a `failed` report takes
(`at_agent_attempt(n, cost_usd=…)` → `worker._agent_cost_usd` → `report.failed_system`).
"""
import types

from stigmergy.librarian import processing, report, worker
from stigmergy.librarian.agent import AgentRun
from stigmergy.librarian.errors import AgentError


def test_stamp_cost_writes_the_summed_passes_spend_onto_the_report():
    passes = processing.AgentPasses(count=2, cost_usd=0.1234567)
    result = processing.Result("filed", "", report={"status": "filed"})
    stamped = processing._stamp_cost(result, passes)
    assert stamped is result                        # stamped in place, not copied
    assert result.report["cost_usd"] == 0.123457    # rounded, never a float tail


def test_stamp_cost_zero_is_a_real_answer():
    """A loop that spent nothing (a park re-file, an offline double) says `0.0` out loud — the
    absence of the key is reserved for outcomes that never entered the agent loop at all."""
    result = processing.Result("filed", "", report={})
    processing._stamp_cost(result, processing.AgentPasses())
    assert result.report["cost_usd"] == 0.0


def test_one_pass_banks_the_spend_before_the_outcome_is_judged(tmp_path):
    """The returning half of the bank: a run that RETURNS an `AgentRun` whose outcome is unusable
    still spent real money, and the bank sits between the run and the outcome judgment so the
    fault raised below it cannot skip the figure. (The raising half — the majority fault shape —
    is the twin test below.)"""
    class BrokeAgent:
        def run(self, **kwargs):
            return AgentRun(outcome=None, cost_usd=0.37)

    deps = types.SimpleNamespace(settings=types.SimpleNamespace(), agent=BrokeAgent())
    item = {"id": 1, "kind": "raw", "payload": {}, "hints": {}, "submitted_by": "a@b", "reply": ""}
    passes = processing.AgentPasses()
    try:
        processing._one_pass(None, item, deps, "material", str(tmp_path), "", passes=passes)
    except AgentError:
        pass
    else:  # pragma: no cover - the outcome above is None, so the pass must refuse
        raise AssertionError("a pass with no usable outcome must raise AgentError")
    assert passes.cost_usd == 0.37


def test_a_pass_that_dies_mid_run_still_banks_its_priced_spend(tmp_path):
    """The raising twin — and the MAJORITY fault shape: a non-`success` stop, the tool-call
    budget and an unreadable outcome file are all raised INSIDE `agent.run()`, after the SDK's
    ResultMessage has already priced the run. `agent._priced` attaches that figure to the fault
    and the loop banks it off the exception; without that road, a single-pass failed item
    reported `cost_usd: 0.0` after paying a full run — exactly the rows an operator most wants
    priced. A returning fake alone proves banking only for the one fault shape that returns."""
    class DiesMidRun:
        def run(self, **kwargs):
            ex = AgentError("the agent run ended as 'error_max_turns' after 30 turn(s)")
            ex.run_cost_usd = 0.29
            raise ex

    deps = types.SimpleNamespace(settings=types.SimpleNamespace(), agent=DiesMidRun())
    item = {"id": 2, "kind": "raw", "payload": {}, "hints": {}, "submitted_by": "a@b", "reply": ""}
    passes = processing.AgentPasses()
    try:
        processing._one_pass(None, item, deps, "material", str(tmp_path), "", passes=passes)
    except AgentError:
        pass
    else:  # pragma: no cover - the stub raises unconditionally
        raise AssertionError("the stub raises; _one_pass must propagate it")
    assert passes.cost_usd == 0.29


def test_the_exception_road_carries_attempts_and_cost_to_the_failed_report():
    ex = AgentError("the agent exceeded its budget").at_agent_attempt(2, cost_usd=0.42)
    assert worker._agent_attempts(ex) == 2
    assert worker._agent_cost_usd(ex) == 0.42
    rep = processing.failure_result(
        item={"id": 7, "attempts": 1}, stage="agent", reason="budget",
        agent_attempts=worker._agent_attempts(ex), cost_usd=worker._agent_cost_usd(ex)).report
    assert rep["agent_attempts"] == 2 and rep["cost_usd"] == 0.42


def test_a_pre_agent_fault_prices_at_zero_not_a_guess():
    """`CaptureError` and friends cannot carry the counter or the cost — `getattr` answers zero
    for both, which is the honest figure for a fault that pre-dates the agent."""
    ex = ValueError("evidence blob missing")
    assert worker._agent_attempts(ex) == 0
    assert worker._agent_cost_usd(ex) == 0.0
    rep = report.failed_system(attempts=1, stage="evidence", reason="missing")
    assert rep["cost_usd"] == 0.0
