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


def test_an_agent_that_declares_no_shape_is_REFUSED_before_a_single_pass_is_spent(tmp_path):
    """**The loud refusal itself, which nothing else exercises any more.**

    `_one_pass` reads `structured_ordinary` with no default: a wrapper that swallowed the
    declaration would otherwise take the exploring branch while wrapping a structured backend, and
    every ordinary capture would be refused for carrying no `page_path` — a misconfiguration
    wearing a filing-quality result's clothes.

    Once every wrapper in the suite declares it, the refusal has no remaining caller and becomes an
    uncovered branch nobody would notice going wrong. This is its own test, and it belongs beside
    the cost seams for a reason the assertions carry: the refusal fires BEFORE the run, so the pass
    banks nothing — a fault raised after a paid run and one raised instead of it must not be
    confusable on the row.

    The stub is deliberately conforming in every OTHER way: both port methods, a priced envelope.
    The one thing missing is the declaration.
    """
    class NoDeclaration:
        def run(self, **kwargs):    # pragma: no cover — the refusal fires before this
            return AgentRun(outcome=None, cost_usd=0.99)

        def run_meeting(self, **kwargs):    # pragma: no cover — the ordinary flow never calls it
            return AgentRun()

    deps = types.SimpleNamespace(settings=types.SimpleNamespace(), agent=NoDeclaration())
    item = {"id": 3, "kind": "raw", "payload": {}, "hints": {}, "submitted_by": "a@b", "reply": ""}
    passes = processing.AgentPasses()

    try:
        processing._one_pass(None, item, deps, "material", str(tmp_path), "", passes=passes)
    except AgentError as ex:
        message = str(ex)
    else:  # pragma: no cover - the agent above declares nothing, so the pass must refuse
        raise AssertionError("a backend declaring no shape must be refused, never defaulted")

    assert "structured_ordinary" in message                  # the member
    assert "NoDeclaration" in message                        # ...and who is missing it
    assert "WRAPPER" in message, (
        "the refusal must name the wrapper case: that is where this goes wrong in practice, and a "
        "reader who wrote the wrapper needs to be told to copy the attribute from what it wraps")
    assert passes.cost_usd == 0.0, (
        "the refusal banked a spend — it fires before the run, so there is nothing to bank")


def test_one_pass_banks_the_spend_before_the_outcome_is_judged(tmp_path):
    """The returning half of the bank: a run that RETURNS an `AgentRun` whose outcome is unusable
    still spent real money, and the bank sits between the run and the outcome judgment so the
    fault raised below it cannot skip the figure. (The raising half — the majority fault shape —
    is the twin test below.)"""
    class BrokeAgent:
        # The declared port member (ADR 033). `False` is the exploring shape, which is what this
        # test means; `_one_pass` REFUSES an agent that declares nothing rather than defaulting it.
        structured_ordinary = False
        wants_gathered = False

        def run(self, **kwargs):
            return AgentRun(outcome=None, cost_usd=0.37)

    deps = types.SimpleNamespace(settings=types.SimpleNamespace(), agent=BrokeAgent())
    item = {"id": 1, "kind": "raw", "payload": {}, "hints": {}, "submitted_by": "a@b", "reply": ""}
    passes = processing.AgentPasses()
    try:
        processing._one_pass(None, item, deps, "material", str(tmp_path), "", passes=passes)
    except AgentError as ex:
        # The fault has to be the one this test is about. A bare `except AgentError: pass` also
        # swallows a MISCONFIGURATION fault raised before the run — which is exactly what happened
        # when the port grew `structured_ordinary`: the pass never ran, the bank was never reached,
        # and only the cost assertion below noticed. Naming the fault is what makes the assertion
        # under it mean "the bank ran", rather than "something raised".
        assert "no usable account" in str(ex), f"a different fault was swallowed: {ex}"
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
        structured_ordinary = False          # the declared port members — see the twin above
        wants_gathered = False

        def run(self, **kwargs):
            ex = AgentError("the agent run ended as 'error_max_turns' after 30 turn(s)")
            ex.run_cost_usd = 0.29
            raise ex

    deps = types.SimpleNamespace(settings=types.SimpleNamespace(), agent=DiesMidRun())
    item = {"id": 2, "kind": "raw", "payload": {}, "hints": {}, "submitted_by": "a@b", "reply": ""}
    passes = processing.AgentPasses()
    try:
        processing._one_pass(None, item, deps, "material", str(tmp_path), "", passes=passes)
    except AgentError as ex:
        # The stub's own fault, not a misconfiguration one raised before the run — see the twin.
        assert "error_max_turns" in str(ex), f"a different fault was swallowed: {ex}"
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
