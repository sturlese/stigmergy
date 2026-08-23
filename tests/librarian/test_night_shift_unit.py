"""The librarian worker's two DAILY passes — the garden and the retention purge.

Keyless and Postgres-free. The sibling files for the two INTERVAL passes are
`test_view_sweep_unit.py` and `test_repair_pass_unit.py`; the four are deliberately parallel, and
what this one adds is the half a monotonic interval cannot express:

- **due-ness is a wall time**, so the clock injected here is a `datetime`, not a float;
- **due-ness survives a restart**, because it is answered from the last `job_runs` row rather than
  from an in-process timer. A worker restarts far more often than once a day, and an in-process
  timer would garden again every time one did. That property is what the ledger-reading tests
  below exist for, and it is the one a cron got for free.
"""
import dataclasses
import datetime
import types

import pytest

from stigmergy.librarian import config, schedule, worker
from tests.views.conftest import FakeConn

AT = (5, 7)
UTC = datetime.UTC


def _at(hour: int, minute: int = 0, day: int = 22) -> datetime.datetime:
    return datetime.datetime(2026, 8, day, hour, minute, tzinfo=UTC)


class _CountingPass:
    """A stand-in for `worker.run_garden` / `worker.run_retention`."""

    def __init__(self, *, result=None, raises=None):
        self.calls = 0
        self._result = {} if result is None else result
        self._raises = raises

    def __call__(self, conn, deps):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._result


class _Ledger(FakeConn):
    """A connection whose only job is to answer "when did this job last run" — the one read
    `daily_due` makes. Set `runs[job]` to a datetime, or leave it out for "never"."""

    def __init__(self, runs=None):
        super().__init__()
        self.runs = dict(runs or {})


@pytest.fixture()
def ledger(monkeypatch):
    """`schedule.last_run_at` patched to read the double above rather than Postgres. Patched at
    the SCHEDULE seam, not at `ops.latest_run`, so the test says what it is faking: the ledger
    read, not the queue."""
    store = _Ledger()

    def fake_last_run_at(conn, job):
        return store.runs.get(job)

    monkeypatch.setattr(schedule, "last_run_at", fake_last_run_at)
    return store


def _worker(deps, *, garden=None, purge=None, now=None, conn=None, **settings_overrides):
    settings = dataclasses.replace(deps.settings, **settings_overrides)
    clock = now or (lambda: _at(*AT))
    return worker.Worker(conn if conn is not None else FakeConn(),
                         dataclasses.replace(deps, settings=settings),
                         on_output=lambda _m: None,
                         garden=garden or _CountingPass(), purge=purge or _CountingPass(),
                         utcnow=clock)


# ── the arithmetic, in isolation ───────────────────────────────────────────────────────────────
def test_a_pass_is_not_due_before_its_time():
    assert schedule.daily_due(_at(4, 0), None, AT) is False
    assert schedule.daily_due(_at(5, 6), None, AT) is False
    assert schedule.daily_due(_at(5, 7), None, AT) is True


def test_a_pass_that_already_ran_today_is_not_due_again():
    """The restart property, stated as arithmetic: the answer comes from the LAST RUN, so a
    container that comes back at 05:08 does not garden a second time."""
    assert schedule.daily_due(_at(5, 30), _at(5, 8), AT) is False
    assert schedule.daily_due(_at(5, 30), _at(5, 8, day=21), AT) is True   # yesterday's run


def test_a_pass_that_missed_its_window_waits_for_tomorrow():
    """A worker that was down all night and starts at 23:00 must not run a pass whose whole point
    was to be followed by a day of repair passes answering its findings. Late is not the same as
    due, and the window is what says so."""
    assert schedule.daily_due(_at(5, 30), None, AT) is True
    assert schedule.daily_due(_at(23, 0), None, AT) is False


def test_a_naive_timestamp_is_read_as_utc_not_as_local_time():
    """`job_runs` can hand back a naive timestamp depending on the driver's configuration, and
    reading one as local time would move every schedule by the host's offset — a pass that fires
    at 05:07 in CI and 07:07 in production is a pass nobody can reason about."""
    naive = datetime.datetime(2026, 8, 22, 5, 8)
    assert schedule.daily_due(_at(5, 30), naive, AT) is False


def test_an_unreadable_time_falls_back_instead_of_refusing_to_boot(caplog):
    """A malformed INTERVAL is a startup refusal (a worker that polls wrong is worse than one that
    does not start). A malformed daily time is not: it only decides WHEN maintenance runs, and
    refusing to start over it would trade a filing outage for a scheduling typo. Logged, so the
    fallback is not silent."""
    with caplog.at_level("WARNING"):
        assert schedule.parse_daily("25:00", default="05:07") == (5, 7)
        assert schedule.parse_daily("half past four", default="05:07") == (5, 7)
        assert schedule.parse_daily("", default="04:42") == (4, 42)
    assert "unreadable daily schedule" in caplog.text
    assert schedule.parse_daily("00:00", default="05:07") == (0, 0)   # the benign twin: 0 is a time


# ── the worker's two daily passes ──────────────────────────────────────────────────────────────
def test_the_garden_runs_once_a_day_however_many_idle_ticks_there_are(rig, ledger):
    """The idle branch fires every poll interval — seconds apart. Without the ledger read this
    would garden on every one of them."""
    _env, deps = rig
    garden = _CountingPass()
    w = _worker(deps, garden=garden, conn=ledger)

    assert w.maybe_garden() is True
    assert garden.calls == 1
    ledger.runs[worker.GARDEN_JOB_NAME] = _at(*AT)     # the pass wrote its own job_runs row
    assert w.maybe_garden() is False
    assert garden.calls == 1


def test_a_restart_does_not_run_the_pass_a_second_time(rig, ledger):
    """The property an in-process timer cannot have. A fresh `Worker` — a redeploy, a crash, a
    scale event — asks the same ledger and gets the same answer."""
    _env, deps = rig
    garden = _CountingPass()
    ledger.runs[worker.GARDEN_JOB_NAME] = _at(5, 8)
    restarted = _worker(deps, garden=garden, now=lambda: _at(5, 30), conn=ledger)

    assert restarted.maybe_garden() is False
    assert garden.calls == 0


def test_yesterdays_run_does_not_satisfy_todays_pass(rig, ledger):
    """The benign twin of the test above: the ledger read must not become "it ran once, ever"."""
    _env, deps = rig
    garden = _CountingPass()
    ledger.runs[worker.GARDEN_JOB_NAME] = _at(5, 8, day=21)
    w = _worker(deps, garden=garden, now=lambda: _at(5, 30), conn=ledger)

    assert w.maybe_garden() is True
    assert garden.calls == 1


def test_the_purge_runs_on_its_own_time_not_the_gardens(rig, ledger):
    """The two are staggered, and each reads its own row. A shared "did the night shift run today"
    flag would let one pass suppress the other."""
    _env, deps = rig
    garden, purge = _CountingPass(), _CountingPass()
    w = _worker(deps, garden=garden, purge=purge, now=lambda: _at(4, 42), conn=ledger)

    assert w.maybe_purge() is True
    assert w.maybe_garden() is False      # 04:42 is before the garden's 05:07
    assert (purge.calls, garden.calls) == (1, 0)


@pytest.mark.parametrize("method", ["maybe_garden", "maybe_purge"])
def test_off_turns_a_daily_pass_off(rig, ledger, method):
    """`off` is a real setting: the switch a deployment reaches for while somebody investigates
    something. Deliberately a WORD rather than an empty string — an unset variable must mean "the
    default", so "I did not configure this" and "I do not want this" cannot be the same value."""
    _env, deps = rig
    garden, purge = _CountingPass(), _CountingPass()
    w = _worker(deps, garden=garden, purge=purge, conn=ledger,
                garden_at=config.DAILY_OFF, retention_at=config.DAILY_OFF,
                now=lambda: _at(23, 59))

    assert getattr(w, method)() is False
    assert (garden.calls, purge.calls) == (0, 0)


@pytest.mark.parametrize("flag", ["stopping", "releasing"])
@pytest.mark.parametrize("method", ["maybe_garden", "maybe_purge"])
def test_a_shutdown_never_picks_up_a_fresh_daily_pass(rig, ledger, flag, method):
    """A whole-corpus pass is the last thing to start on the way out."""
    _env, deps = rig
    garden, purge = _CountingPass(), _CountingPass()
    w = _worker(deps, garden=garden, purge=purge, conn=ledger)
    setattr(w, flag, True)

    assert getattr(w, method)() is False
    assert (garden.calls, purge.calls) == (0, 0)


@pytest.mark.parametrize("method,kwarg", [("maybe_garden", "garden"), ("maybe_purge", "purge")])
def test_a_fault_is_swallowed_so_the_queue_keeps_draining(rig, ledger, method, kwarg):
    """Filing never depends on maintenance. The pass raising is reported as "it ran" — because it
    did — and the loop goes on."""
    _env, deps = rig
    faulting = _CountingPass(raises=RuntimeError("boom"))
    w = _worker(deps, conn=ledger, **{kwarg: faulting})

    assert getattr(w, method)() is True
    assert faulting.calls == 1


def test_a_failed_pass_is_not_retried_all_night(rig, ledger):
    """`last_run_at` reads the last row WHATEVER its outcome, and this is why: the gardener writes
    an error row before it raises, so a bad night is one bad night rather than a pass re-attempted
    on every idle tick until morning."""
    _env, deps = rig
    garden = _CountingPass(raises=RuntimeError("the model exploded"))
    w = _worker(deps, garden=garden, conn=ledger)

    assert w.maybe_garden() is True
    ledger.runs[worker.GARDEN_JOB_NAME] = _at(*AT)     # the error row the gardener wrote
    assert w.maybe_garden() is False
    assert garden.calls == 1


def test_an_unreadable_ledger_postpones_the_pass_instead_of_killing_the_worker(rig, monkeypatch):
    """A database that is briefly unreadable must cost a maintenance pass, not the worker. The
    loop that calls this is the one draining the queue."""
    _env, deps = rig
    garden = _CountingPass()

    def explode(conn, job):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(schedule, "last_run_at", explode)
    w = _worker(deps, garden=garden)

    assert w.maybe_garden() is False
    assert garden.calls == 0


# ── what the operator reads ────────────────────────────────────────────────────────────────────
def test_the_garden_line_is_printed_even_when_it_found_nothing():
    """Unlike the two convergence passes, this one always prints when it runs: it is daily, so a
    line a day is not noise, and "the garden ran and found nothing" is the sentence an operator
    most wants to be able to see."""
    quiet = worker.garden_clause({"findings": 0, "pages_checked": 40, "entities_checked": 3})
    assert "40 page(s)" in quiet and "0 finding(s)" in quiet
    assert worker.garden_clause(None) == ""      # it did not run — nothing to say


def test_the_purge_line_is_printed_only_when_something_was_purged():
    """The asymmetry with the garden line, and it is deliberate: a nightly "purged 0" would bury
    the nights that removed somebody's material."""
    assert worker.retention_clause({"purged": 3}) == (
        "retention: purged the payload of 3 terminal capture(s)")
    assert worker.retention_clause({"purged": 0}) == ""
    assert worker.retention_clause(None) == ""


# ── the pass functions themselves, not a stand-in ──────────────────────────────────────────────
# Every test above injects the pass, which is right for the SCHEDULE — but it means `run_garden`
# and `run_retention` were never executed by this file. They were not executed anywhere: both
# import their library inside the function (so the filing path does not load a model stack at
# import time), and a name that does not exist inside a function body is invisible until it runs.
# `GardenerSettings.from_env()` was written here and shipped green; the class has no such
# classmethod. This is the test that would have said so.
def test_the_garden_pass_calls_the_gardener_the_way_the_gardener_is_actually_built(rig, monkeypatch):
    """Not a test of what the gardener DOES — that is `tests/gardener/`'s whole suite. This pins
    the SEAM: the symbols `run_garden` reaches for exist and take what it passes them."""
    _env, deps = rig
    seen = {}

    async def fake_run_gardener(conn, *, repo, settings):
        seen.update(repo=repo, settings=settings)
        return types.SimpleNamespace(findings=[{"check": "x"}], pages_checked=7,
                                     entities_checked=2)

    monkeypatch.setattr("stigmergy.gardener.run.run_gardener", fake_run_gardener)
    # A fake key, because `run_garden` checks it before it calls anything — the check's own tests
    # are above. Nothing here reaches a provider: `run_gardener` itself is the double.
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    stats = worker.run_garden(FakeConn(), deps)

    assert stats == {"findings": 1, "pages_checked": 7, "entities_checked": 2}
    assert seen["repo"] == deps.repo
    # Built through the gardener's OWN constructor, so a rename there fails here rather than at
    # 05:07 on a deployment.
    assert seen["settings"].model


def test_the_retention_pass_calls_the_purge_the_way_the_purge_is_actually_built(rig, monkeypatch):
    """`run_garden`'s twin, for the same reason and against the same class of slip."""
    _env, deps = rig
    seen = {}

    def fake_purge(conn, *, older_than_days):
        seen["older_than_days"] = older_than_days
        return {"purged": 2, "ids": [1, 2], "dry_run": False}

    monkeypatch.setattr("stigmergy.capture.retention.purge", fake_purge)
    result = worker.run_retention(FakeConn(), deps)

    assert result["purged"] == 2
    assert seen["older_than_days"] == deps.settings.retention_days


# ── the second model the night shift brought into this process ────────────────────────────────
def test_a_garden_pass_whose_model_this_worker_cannot_authenticate_is_refused(rig):
    """**The trap the capture-is-the-approval change created, and it is invisible to this keyless
    suite by construction.**

    The gardener's default model is a BARE id, which resolves through the OpenAI Responses API —
    and `OPENAI_API_KEY` is exactly what `bootstrap.READ_PATH_ONLY_ENV` strips before exec'ing the
    deployed worker. Left alone, the deterministic checks (which need no model) would keep working
    and every night would record a `partial` run: a pass that looks alive forever while two of its
    three model passes have never once run.

    The refusal has to name the DEAD END rather than say "export it", because in that container
    there is nothing to export — the strip happens after.

    It fails the PASS rather than the worker's startup, which is the same rule every other pass
    here follows: filing must never depend on maintenance, and refusing to boot over a nightly
    sweep would take the queue down for the one thing that does not need it."""
    _env, deps = rig
    with pytest.raises(Exception) as caught:
        worker.check_garden_model(deps.settings, environ={"ANTHROPIC_API_KEY": "x"})

    message = str(caught.value)
    assert "OPENAI_API_KEY" in message
    assert "strips" in message, "the refusal does not say the key is stripped, so it reads as a typo"
    assert config.GARDEN_AT_ENV in message and config.DAILY_OFF in message, (
        "the refusal offers no way out — an operator who does not want a garden pass must be told "
        "how to say so")


@pytest.mark.parametrize("environ, garden_at", [
    ({"OPENAI_API_KEY": "k"}, "05:07"),        # the key is there: nothing to refuse
    ({}, config.DAILY_OFF),                    # no pass at all: nothing to authenticate
])
def test_the_garden_model_check_does_not_fire_when_it_should_not(rig, environ, garden_at):
    """The benign twins. A refusal that fired on a correctly configured worker would cost a
    nightly pass for nothing."""
    _env, deps = rig
    settings = dataclasses.replace(deps.settings, garden_at=garden_at)

    worker.check_garden_model(settings, environ=environ)


def test_a_garden_pass_that_fails_records_a_row_so_it_is_visible_and_does_not_retry(rig, ledger):
    """The half that makes the refusal above worth having.

    A pass that only logged would be invisible — nothing reads a worker's stderr on a schedule —
    and, worse, `_daily_due` reads the ledger, so with no row written the pass would re-fire on
    every idle tick until morning. The error row is both the operator's signal (the console's Jobs
    page reads these rows) and the thing that makes the failure cost one night rather than a loop.
    """
    _env, deps = rig
    recorded = []
    w = _worker(deps, garden=_CountingPass(raises=RuntimeError("no key")), conn=ledger)
    w.conn = ledger

    import stigmergy.capture.ops as ops_module
    original = ops_module.record_job_run
    try:
        ops_module.record_job_run = lambda conn, job, **kw: recorded.append((job, kw))
        assert w.maybe_garden() is True
    finally:
        ops_module.record_job_run = original

    assert recorded, "a failed garden pass wrote no job_runs row — it is invisible and will retry"
    job, kwargs = recorded[0]
    assert job == worker.GARDEN_JOB_NAME
    assert kwargs["status"] == "error"
    assert "no key" in kwargs["error"]
