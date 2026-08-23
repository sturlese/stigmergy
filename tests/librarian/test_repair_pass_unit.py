"""The librarian worker's periodic repair pass: its own interval, its own watermark, and a fault
that is logged and swallowed.

Keyless and Postgres-free. `Worker.maybe_run_repairs` is exercised with an injected clock and an
injected pass — the interval is a timing contract, and a test that had to wait one out could only
prove it by sleeping — and `worker.run_repairs`' own guard (the gardener watermark) is driven with
a hand-built connection double, because what it decides is read from two tables and nothing else.

The sibling that proves the same shape for the OTHER maintenance pass is
`test_view_sweep_unit.py`; the two are deliberately parallel, and where this one differs is stated
in the docstring of the test that differs.
"""
import dataclasses

import pytest

from stigmergy.librarian import config, worker
from tests.views.conftest import FakeConn


class _Clock:
    """A hand-cranked monotonic clock. Injected rather than monkeypatched onto `time`, because the
    worker's `_sleep` reads the real one and a patched module clock would deadlock its slicing."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclasses.dataclass
class _Stats:
    findings_seen: int = 1
    applied: int = 1
    failed: int = 0
    skipped_known: int = 0
    skipped_invalid: int = 0
    failures: tuple = ()

    @property
    def stats(self) -> dict:
        return {"findings_seen": self.findings_seen, "applied": self.applied,
                "failed": self.failed, "skipped_known": self.skipped_known,
                "skipped_invalid": self.skipped_invalid, "failures": list(self.failures)}


class _CountingPass:
    """A stand-in for `worker.run_repairs`, counting calls, recording what it was handed, and
    optionally faulting."""

    def __init__(self, *, result=None, raises=None):
        self.calls = 0
        self.should_stops = []
        self._result = result if result is not None else _Stats()
        self._raises = raises

    def __call__(self, conn, deps, *, should_stop=None):
        self.calls += 1
        self.should_stops.append(should_stop)
        if self._raises is not None:
            raise self._raises
        return self._result


def _worker(deps, *, repair_pass, clock, interval=3600.0, on_output=lambda _m: None, conn=None):
    settings = dataclasses.replace(deps.settings, repair_interval_s=interval)
    return worker.Worker(conn, dataclasses.replace(deps, settings=settings),
                         on_output=on_output, repair_pass=repair_pass, now=clock)


# ── the schedule: skipped, never blocked ───────────────────────────────────────────────────────
def test_the_first_idle_tick_runs_a_pass_and_the_next_one_does_not(rig):
    """`None` means "due now": a worker that has just started answers the gardener's findings
    before it waits an interval out, the same posture the view sweep takes. What must NOT happen is
    a second pass on the following tick — the pass costs model calls, and an interval that only
    applied after the first run would make the first hour free of charge."""
    _env, deps = rig
    passes, clock = _CountingPass(), _Clock()
    w = _worker(deps, repair_pass=passes, clock=clock, conn=FakeConn())

    assert w.maybe_run_repairs() is True
    assert passes.calls == 1
    assert w.maybe_run_repairs() is False
    assert passes.calls == 1

    clock.advance(3600.0)
    assert w.maybe_run_repairs() is True
    assert passes.calls == 2


def test_the_due_time_is_scheduled_before_the_pass_runs_not_after(rig):
    """A pass slower than its own interval would otherwise owe another the instant it finished, and
    a FAULTING pass would re-attempt on every idle tick — which for this loop means model calls, not
    just a wasted parse. Proven through the faulting arm, where the two orderings differ."""
    _env, deps = rig
    passes, clock = _CountingPass(raises=RuntimeError("boom")), _Clock()
    w = _worker(deps, repair_pass=passes, clock=clock, conn=FakeConn())

    assert w.maybe_run_repairs() is True          # ran, faulted, swallowed
    assert w.maybe_run_repairs() is False         # and is not due again until the interval
    assert passes.calls == 1


def test_zero_turns_the_pass_off(rig):
    """`0` is a real setting, not a broken one: it is the switch a deployment reaches for while
    somebody investigates something, and it must stop the pass without stopping the worker."""
    _env, deps = rig
    passes, clock = _CountingPass(), _Clock()
    w = _worker(deps, repair_pass=passes, clock=clock, interval=config.REPAIR_PASS_OFF,
                conn=FakeConn())

    assert w.maybe_run_repairs() is False
    assert passes.calls == 0


@pytest.mark.parametrize("flag", ["stopping", "releasing"])
def test_a_shutdown_never_picks_up_a_fresh_pass(rig, flag):
    """Not starting one is the FIRST guard, and it matters more here than for the view sweep: this
    pass pushes. A worker on its way out must not begin a repair it will be killed in the middle
    of."""
    _env, deps = rig
    passes, clock = _CountingPass(), _Clock()
    w = _worker(deps, repair_pass=passes, clock=clock, conn=FakeConn())
    setattr(w, flag, True)

    assert w.maybe_run_repairs() is False
    assert passes.calls == 0


def test_a_fault_is_swallowed_so_the_queue_keeps_draining(rig):
    """Filing must never depend on maintenance. The pass raising is reported as "it ran" — because
    it did — and the loop goes on; what happened is in the log and, for anything that got as far as
    a repair, in the ledger."""
    _env, deps = rig
    passes, clock = _CountingPass(raises=RuntimeError("the model exploded")), _Clock()
    w = _worker(deps, repair_pass=passes, clock=clock, conn=FakeConn())

    assert w.maybe_run_repairs() is True


def test_the_pass_is_handed_the_workers_own_pause_reason(rig):
    """The one line that wires the seam, pinned: the pass is consulted BETWEEN repairs and must be
    able to see a signal that landed, or a capture that arrived, after it started. A copy of a flag
    taken at call time would answer the question as it was, not as it is."""
    _env, deps = rig
    passes, clock = _CountingPass(), _Clock()
    w = _worker(deps, repair_pass=passes, clock=clock, conn=FakeConn())

    assert w.maybe_run_repairs() is True
    assert passes.should_stops == [w._sweep_pause_reason]


def test_the_loop_runs_a_repair_pass_on_the_idle_branch(rig, monkeypatch):
    """"The queue is empty" is where maintenance belongs, and the pass must sit BEFORE the sleep —
    a worker about to wait a poll interval out has already answered whatever the gardener left.

    It also runs BESIDE the view sweep rather than instead of it: both are due on an idle tick, and
    a loop that ran one and returned would starve the other for a whole poll interval.
    """
    _env, deps = rig
    monkeypatch.setattr(worker.queue, "claim_next", lambda *a, **kw: None)
    monkeypatch.setattr(worker.queue, "release_expired", lambda *a, **kw: {})
    passes, clock = _CountingPass(), _Clock()
    sweeps = []
    w = _worker(deps, repair_pass=passes, clock=clock, conn=FakeConn())
    from stigmergy.views import regenerate as views_regenerate

    def _sweep(*_a, **_kw):
        sweeps.append(1)
        return views_regenerate.RunResult()

    monkeypatch.setattr(w, "_view_sweep", _sweep)
    slept = []
    monkeypatch.setattr(w, "_sleep", lambda s: (slept.append(s), setattr(w, "stopping", True)))

    assert w.run() == 0                       # nothing was in the queue

    assert passes.calls == 1
    assert len(sweeps) == 1
    assert slept == [deps.settings.poll_interval_s]


# ── the clause: a maintenance pass that did nothing says nothing ───────────────────────────────
def test_a_pass_that_did_nothing_prints_no_line():
    """`view_sweep_clause`'s rule, for the same reason: a line every interval saying "nothing to
    repair" would bury the passes that changed the corpus."""
    assert worker.repair_clause(None) == ""
    assert worker.repair_clause(_Stats(findings_seen=3, applied=0, skipped_known=3)) == ""


def test_a_pass_that_applied_something_says_what_it_did():
    line = worker.repair_clause(_Stats(findings_seen=4, applied=2, failed=1, skipped_known=1))
    assert "4 finding(s) seen" in line
    assert "2 applied" in line
    assert "1 failed" in line


def test_a_failure_is_named_in_the_line_not_only_counted():
    """A count with no sentence sends an operator to the ledger to find out what happened; the
    first few sentences are what makes the line worth printing at all."""
    line = worker.repair_clause(
        _Stats(findings_seen=1, applied=0, failed=1,
               failures=("edits: the gates refused this repair, so nothing was committed",)))
    assert "the gates refused this repair" in line


# ── the watermark: the pass answers a gardener run, not the clock ──────────────────────────────
class _WatermarkConn:
    """Enough of a connection for `worker.run_repairs`' guard: the latest completed gardener run
    and the latest repair `job_runs` row, whatever the query asks for. Both are plain values, so a
    test states the two timestamps that decide and nothing else."""

    def __init__(self, *, gardener_at, repair_at):
        self.gardener_at = gardener_at
        self.repair_at = repair_at

    def cursor(self):
        return _WatermarkCursor(self)


class _WatermarkCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        job = (params or ("",))[0]
        if job == "gardener":
            self.row = ((1, None, self.conn.gardener_at, {})
                        if self.conn.gardener_at is not None else None)
        else:
            self.row = ((2, "ok", None, self.conn.repair_at, {}, "")
                        if self.conn.repair_at is not None else None)

    def fetchone(self):
        return self.row


def test_no_completed_gardener_run_means_no_pass(rig):
    """The ordinary state on a fresh deployment, and it is not a fault: there are no findings to
    answer, so there is nothing to derive from. `None` rather than an empty result, so the worker's
    one line stays silent."""
    _env, deps = rig
    assert worker.run_repairs(_WatermarkConn(gardener_at=None, repair_at=None), deps) is None


def test_a_gardener_run_older_than_the_last_pass_is_already_answered(rig):
    """The watermark, and the reason it exists: without it the loop would re-derive the same
    findings every interval — cheap only because the ledger's memory catches each one afterwards,
    which is paying for a model call to be told what a timestamp already knew."""
    import datetime

    gardener_at = datetime.datetime(2026, 8, 20, 5, 30, tzinfo=datetime.UTC)
    repair_at = datetime.datetime(2026, 8, 20, 6, 30, tzinfo=datetime.UTC)
    _env, deps = rig
    assert worker.run_repairs(_WatermarkConn(gardener_at=gardener_at, repair_at=repair_at),
                              deps) is None


def test_a_gardener_run_newer_than_the_last_pass_is_answered(rig, monkeypatch):
    """The benign twin: a gardener run that finished AFTER the last pass is exactly what this loop
    exists for, and the guard must let it through. The pass itself is stubbed — what is under test
    here is the decision to run one, not what it derives."""
    import datetime

    gardener_at = datetime.datetime(2026, 8, 21, 5, 30, tzinfo=datetime.UTC)
    repair_at = datetime.datetime(2026, 8, 20, 6, 30, tzinfo=datetime.UTC)
    _env, deps = rig
    ran = {}

    async def _fake_run(conn, **kwargs):
        ran.update(kwargs)
        return _Stats()

    from stigmergy.repair import run as repair_run
    monkeypatch.setattr(repair_run, "run_repairs", _fake_run)

    result = worker.run_repairs(_WatermarkConn(gardener_at=gardener_at, repair_at=repair_at), deps)
    assert result is not None
    assert ran["repo"] == deps.repo
    assert ran["branch"] == deps.settings.branch


def test_a_first_ever_pass_runs_with_no_watermark_at_all(rig, monkeypatch):
    """The other benign twin: a deployment that has never repaired anything has no `job_runs` row
    to compare against, and must not read as "already answered"."""
    import datetime

    gardener_at = datetime.datetime(2026, 8, 21, 5, 30, tzinfo=datetime.UTC)
    _env, deps = rig
    calls = []

    async def _fake_run(conn, **kwargs):
        calls.append(kwargs)
        return _Stats()

    from stigmergy.repair import run as repair_run
    monkeypatch.setattr(repair_run, "run_repairs", _fake_run)

    assert worker.run_repairs(_WatermarkConn(gardener_at=gardener_at, repair_at=None),
                              deps) is not None
    assert len(calls) == 1


# ── the settings' own domain ───────────────────────────────────────────────────────────────────
def test_a_negative_interval_is_refused_and_zero_is_not(rig):
    """Zero is the documented off switch; negative would make every idle tick "due" — and for this
    pass that means asking the gardener's tables on every poll, and model calls whenever a new run
    appears."""
    from stigmergy.librarian.errors import LibrarianConfigError

    _env, deps = rig
    dataclasses.replace(deps.settings, repair_interval_s=0).check_domains()      # legal
    with pytest.raises(LibrarianConfigError, match="repair_interval_s"):
        dataclasses.replace(deps.settings, repair_interval_s=-1).check_domains()


def test_an_interval_below_the_poll_interval_is_refused(rig):
    """The damage the branch above describes is reachable from INSIDE its domain: the pass is due
    at most once per idle poll, so anything below that runs on EVERY one. `0.9` for `900` is the
    typo that gets there, and it is not a fault the value's sign can catch."""
    from stigmergy.librarian.errors import LibrarianConfigError

    _env, deps = rig
    with pytest.raises(LibrarianConfigError, match="repair_interval_s"):
        dataclasses.replace(deps.settings, repair_interval_s=0.9,
                            poll_interval_s=5.0).check_domains()


def test_the_interval_is_env_resolved_through_from_args(monkeypatch):
    """`Settings.from_args` is the ONE place this package reads the environment, and an operator who
    sets a variable that nothing reads has been silently ignored."""
    from types import SimpleNamespace

    monkeypatch.setenv(config.REPAIR_INTERVAL_ENV, "120")
    assert config.Settings.from_args(SimpleNamespace()).repair_interval_s == 120.0
