"""Shutdown and scheduling around the periodic pass — the two places an unattended maintenance
loop hurts the process it lives in.

`test_view_sweep_unit.py` pins that a `stopping`/`releasing` worker starts no new pass. These are
the questions that leaves open, and each one is a way the pass could be worse than the feature is
worth:

- does a worker that is BUSY ever pay for a pass? (the developer's idle-branch test proves the
  pass runs when the queue is empty and never that it does not run when it is not);
- does a signal that arrives MID-pass cost the shutdown a whole poll interval on top of the pass?
- the WORKER never interrupts a running pass — cancellation is the pass's own cooperative
  `should_stop`, consulted between entities (`tests/views/test_sweep_convergence.py` makes it
  fire), so the shutdown-delay bound is ONE entity's regeneration, not a ceiling's worth;
- the interval is scheduled off the pass's START. That is what stops a faulting pass re-attempting
  every tick; it also means a pass slower than its own interval is due again the moment it ends.

Keyless, Postgres-free and clock-free by construction: the pass and the clock are injected, which
is `Worker`'s own seam. The one wall-clock assertion below is a LOWER bound on responsiveness (an
upper bound on elapsed time), never a sleep waiting for something to happen.
"""
import dataclasses
import time
import types

import pytest

from stigmergy.librarian import worker
from stigmergy.views import regenerate as views_regenerate
from tests.views.conftest import FakeConn


class _Clock:
    """A hand-cranked monotonic clock — the worker's `_sleep` still reads the real one, so this
    must be injected rather than patched onto `time`."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _worker(deps, *, sweeper, clock, interval=900.0, on_output=lambda _m: None, conn=None,
            poll_interval_s=None):
    settings = dataclasses.replace(deps.settings, view_sweep_interval_s=interval)
    if poll_interval_s is not None:
        settings = dataclasses.replace(settings, poll_interval_s=poll_interval_s)
    return worker.Worker(conn if conn is not None else FakeConn(),
                         dataclasses.replace(deps, settings=settings),
                         on_output=on_output, view_sweep=sweeper, now=clock)


@pytest.fixture()
def idle_loop(monkeypatch):
    """A worker loop whose queue is empty and whose startup stranded-claim recovery is a no-op —
    everything except the idle branch removed, so what the idle branch does is unambiguous."""
    monkeypatch.setattr(worker.queue, "claim_next", lambda *a, **kw: None)
    monkeypatch.setattr(worker.queue, "release_expired", lambda *a, **kw: {})
    return monkeypatch


# ── the pass belongs to the IDLE branch, and only to it ────────────────────────────────────────
def test_a_worker_with_work_in_the_queue_never_starts_a_maintenance_pass(rig, monkeypatch):
    """The specificity twin of "the loop sweeps on the idle branch". A pass that also ran while
    items were waiting would put a corpus parse, a fresh worktree and up to a ceiling's worth of
    model calls between a capture and its filing — the exact latency the queue exists to avoid.

    The clock is left far past due throughout, so nothing but the branch itself can be what
    prevented the pass.
    """
    env, deps = rig
    items = [{"id": 1}, {"id": 2}, {"id": 3}]
    monkeypatch.setattr(worker.queue, "release_expired", lambda *a, **kw: {})
    calls = {"n": 0}

    def sweeper(conn, d, **_kw):
        calls["n"] += 1
        return views_regenerate.RunResult()

    w = _worker(deps, sweeper=sweeper, clock=_Clock())

    def process_next(conn, d):
        if not items:
            w.stopping = True
            return None
        return items.pop(0), types.SimpleNamespace(status="filed")

    monkeypatch.setattr(worker, "process_next", process_next)
    w.run()

    assert calls["n"] == 0, "a maintenance pass ran while the queue still had items in it"


# ── a signal arriving mid-pass ─────────────────────────────────────────────────────────────────
def test_a_signal_arriving_mid_pass_stops_the_loop_without_a_second_pass_or_a_poll_wait(
        rig, idle_loop):
    """SIGTERM during a pass: the handler flips `stopping`, and the worker never cancels the
    pass from outside — it runs to wherever its own cooperative stop takes it. What must NOT
    happen afterwards is the loop paying its poll interval on the way out, or starting another
    pass.

    `_sleep` slices on the real clock and re-checks `stopping`, so the assertion is an UPPER bound
    on elapsed wall time and never a sleep waiting for something to happen: with a 4s poll interval
    and a flag that were ignored, the loop would sit in `_sleep` for four seconds before rechecking.
    """
    env, deps = rig
    passes = {"n": 0}

    def sweeper_that_receives_a_signal(conn, d, **_kw):
        passes["n"] += 1
        w.stopping = True                     # what `_on_sigterm` does, from inside the pass
        return views_regenerate.RunResult()

    # A REAL poll interval, well above the rig's own 0.1s: the point of the assertion is
    # that the loop does not sit in `_sleep` for one, so a tenth of a second proves nothing.
    w = _worker(deps, sweeper=sweeper_that_receives_a_signal, clock=_Clock(),
                poll_interval_s=4.0)

    started = time.monotonic()
    assert w.run() == 0
    elapsed = time.monotonic() - started

    assert passes["n"] == 1, "the loop started a second pass after the signal"
    assert elapsed < 1.0, (
        f"shutdown waited {elapsed:.2f}s after the pass finished — a signal must not cost a "
        f"whole 4s poll interval on top of the pass it already waited out")


def test_the_worker_never_interrupts_a_running_pass_and_still_reports_what_it_did(rig):
    """The WORKER's half of shutdown: it hands the flags to the pass and never cancels it from
    outside, and a flag flipped mid-pass must not suppress the report either — a shutdown that
    swallowed the line describing commits it had just pushed would leave an operator with pushed
    work and no record of it on the way out.

    The PASS's half — stopping between entities when the flag it was handed flips — is a property
    of `views.regenerate.run` and is proven there, against the real loop
    (`tests/views/test_sweep_convergence.py`). Together they bound the shutdown delay at one
    entity's regeneration.
    """
    env, deps = rig
    printed: list[str] = []
    finished = {"ok": False}

    def sweeper(conn, d, **_kw):
        w.stopping = True
        w.releasing = True
        result = views_regenerate.RunResult(checked=1, population=1)
        result.outcomes = [views_regenerate.RegenOutcome(entity_id="acme-corp", entity_name="Acme",
                                                         action="written")]
        finished["ok"] = True
        return result

    w = _worker(deps, sweeper=sweeper, clock=_Clock(), on_output=printed.append)

    assert w.maybe_sweep_views() is True
    assert finished["ok"], ("the worker cut the pass short from outside — stopping "
                            "between entities is the pass\u2019s own cooperative call")
    assert any("1 regenerated" in line for line in printed)


# ── the interval, and the one way it can still be defeated ─────────────────────────────────────
def test_the_due_time_survives_a_pass_that_raises_before_it_returns(rig):
    """The developer pins that a faulting pass is not retried on the next tick. This is the same
    property against the harder fault: one that escapes as a `BaseException` rather than an
    `Exception`, which `maybe_sweep_views`'s `except Exception` does not catch. The due time is
    assigned BEFORE the call, so even an escape that unwinds the whole loop cannot leave the next
    tick due — a `KeyboardInterrupt` during a sweep must not become a hot retry loop.
    """
    env, deps = rig

    def interrupted(conn, d, **_kw):
        raise KeyboardInterrupt

    clock = _Clock()
    w = _worker(deps, sweeper=interrupted, clock=clock, interval=900.0)

    with pytest.raises(KeyboardInterrupt):
        w.maybe_sweep_views()

    calls = {"n": 0}

    def counting(conn, d, **_kw):
        calls["n"] += 1
        return views_regenerate.RunResult()

    w._view_sweep = counting
    clock.advance(3.0)
    assert w.maybe_sweep_views() is False
    assert calls["n"] == 0


def test_a_pass_that_outlasts_its_own_interval_is_due_again_the_moment_it_ends(rig):
    """**Characterization, and a question for the operator — not an endorsement.**

    The interval is `pass start + interval`, which is what makes a faulting pass wait (the
    assignment happens before the call, so an escape cannot skip it). The price is here: a pass
    that runs longer than its own interval is already overdue when it returns, and the next idle
    tick starts another one immediately. There is no floor between passes.

    It is bounded per pass by the ceiling and unbounded per hour, so on a corpus where ten
    regenerations take longer than `STIGMERGY_LIBRARIAN_VIEW_SWEEP_INTERVAL_S` the loop runs
    back-to-back sweeps for as long as the corpus keeps changing. Whether that is the wanted
    behaviour is a decision, not a defect — scheduling off the END would fix it and reintroduce the
    hot-retry-on-fault the START ordering exists to prevent; a floor applied after the pass would
    fix both. Pinned so the choice is visible and cannot change by accident.
    """
    env, deps = rig
    clock = _Clock()
    calls = {"n": 0}

    def slow_pass(conn, d, **_kw):
        calls["n"] += 1
        clock.advance(2000.0)                 # the pass itself outlasts a 900s interval
        return views_regenerate.RunResult()

    w = _worker(deps, sweeper=slow_pass, clock=clock, interval=900.0)

    assert w.maybe_sweep_views() is True
    assert w.maybe_sweep_views() is True       # due again with no idle gap at all
    assert calls["n"] == 2
