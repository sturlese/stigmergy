"""The librarian worker's periodic view sweep: its own worktree, its own interval, its own
ceiling, and a fault that is recorded and swallowed.

Keyless and Postgres-free: `Worker.maybe_sweep_views` is exercised with an injected clock and an
injected pass (the interval is a timing contract, and a test that had to wait one out could only
prove it by sleeping), while `run_view_sweep` is driven for real against the suite's git rig with
the `job_runs` write standing in for a database it does not need — `tests/views/conftest.FakeConn`,
reused rather than copied, because the shape it records is the same shape.
"""
import os

import pytest

from stigmergy.librarian import config, gitcmd, worker
from stigmergy.librarian.errors import LibrarianConfigError
from stigmergy.views import regenerate as views_regenerate
from tests.librarian import support
from tests.views.conftest import FakeConn


@pytest.fixture(autouse=True)
def _offline_view_agent(monkeypatch):
    """The view synthesis runs offline here, exactly as it does in `tests/views/` — the sweep is
    what is under test, never the model behind one entity's synthesis."""
    monkeypatch.setenv("CLEAN_LLM", "fake")


class _Clock:
    """A hand-cranked monotonic clock. Injected rather than monkeypatched onto `time`, because the
    worker's `_sleep` reads the real one and a patched module clock would deadlock its slicing."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _CountingSweep:
    """A stand-in for `run_view_sweep`, counting calls and optionally faulting."""

    def __init__(self, *, result=None, raises=None):
        self.calls = 0
        self._result = result if result is not None else views_regenerate.RunResult()
        self._raises = raises

    def __call__(self, conn, deps, **_kw):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._result


def _worker(deps, *, sweeper, clock, interval=900.0, ceiling=10, on_output=lambda _m: None,
            conn=None):
    import dataclasses
    settings = dataclasses.replace(deps.settings, view_sweep_interval_s=interval,
                                   view_sweep_ceiling=ceiling)
    return worker.Worker(conn, dataclasses.replace(deps, settings=settings),
                         on_output=on_output, view_sweep=sweeper, now=clock)


# ── D5: the seam is the loop's IDLE branch ─────────────────────────────────────────────────────
def test_the_loop_sweeps_on_the_idle_branch(rig, monkeypatch):
    """"The queue is empty" is precisely where maintenance belongs — and the pass must sit BEFORE
    the sleep, so a worker that is about to wait a poll interval out has already done it."""
    env, deps = rig
    monkeypatch.setattr(worker.queue, "claim_next", lambda *a, **kw: None)
    monkeypatch.setattr(worker.queue, "release_expired", lambda *a, **kw: {})
    sweeper, clock = _CountingSweep(), _Clock()
    w = _worker(deps, sweeper=sweeper, clock=clock, conn=FakeConn())
    slept = []
    monkeypatch.setattr(w, "_sleep", lambda s: (slept.append(s), setattr(w, "stopping", True)))

    assert w.run() == 0                       # nothing was in the queue

    assert sweeper.calls == 1
    assert slept == [deps.settings.poll_interval_s]


# ── D7: its own interval, not every idle tick ──────────────────────────────────────────────────
def test_the_first_idle_tick_sweeps(rig):
    """A worker that has just started converges `views/` before it waits an interval out — the same
    posture `worker.sweep()` (the stranded-claim recovery) already takes at startup."""
    env, deps = rig
    sweeper, clock = _CountingSweep(), _Clock()
    w = _worker(deps, sweeper=sweeper, clock=clock)

    assert w.maybe_sweep_views() is True
    assert sweeper.calls == 1


def test_an_idle_worker_does_not_sweep_on_every_poll(rig):
    """The cost property at the worker level: an empty queue polls every few seconds, and a corpus
    parse plus a fresh worktree per poll is not free. The pass is SKIPPED, not blocked, so `_sleep`
    keeps slicing and a signal is still observed promptly."""
    env, deps = rig
    sweeper, clock = _CountingSweep(), _Clock()
    w = _worker(deps, sweeper=sweeper, clock=clock, interval=900.0)

    w.maybe_sweep_views()
    for _ in range(300):                      # 300 polls at the 3s default = 15 minutes of ticks
        clock.advance(3.0)
        if clock.t >= 1000.0 + 900.0:
            break
        assert w.maybe_sweep_views() is False
    assert sweeper.calls == 1

    clock.advance(1.0)                        # now past the interval
    assert w.maybe_sweep_views() is True
    assert sweeper.calls == 2


def test_a_zero_interval_turns_the_sweep_off(rig):
    """`0` is a real setting, not a broken one: it leaves the post-meeting hook and
    `stigmergy-views regenerate` as the only roads — what a deployment had before the pass
    existed."""
    env, deps = rig
    sweeper, clock = _CountingSweep(), _Clock()
    w = _worker(deps, sweeper=sweeper, clock=clock, interval=config.VIEW_SWEEP_OFF)

    for _ in range(5):
        assert w.maybe_sweep_views() is False
        clock.advance(10_000.0)
    assert sweeper.calls == 0


def test_a_stopping_or_releasing_worker_starts_no_new_sweep(rig):
    """A shutdown must not pick up a fresh multi-entity pass on its way out. Nothing can cancel one
    already running — same as an item in flight — so the guard is at the START."""
    env, deps = rig
    sweeper, clock = _CountingSweep(), _Clock()
    w = _worker(deps, sweeper=sweeper, clock=clock)
    w.stopping = True
    assert w.maybe_sweep_views() is False

    w.stopping, w.releasing = False, True
    assert w.maybe_sweep_views() is False
    assert sweeper.calls == 0


# ── D9: a fault is recorded and swallowed ──────────────────────────────────────────────────────
def test_a_sweep_fault_is_swallowed_so_the_queue_keeps_draining(rig, caplog):
    """Filing must never depend on a rollup — the post-meeting hook's posture, for the same reason.
    `views.regenerate.run` has already written its own `job_runs` error row by the time a fault
    reaches here (pinned in `tests/views/test_sweep.py`), so this half is about the WORKER."""
    env, deps = rig
    sweeper = _CountingSweep(raises=RuntimeError("the remote went away mid-sweep"))
    clock = _Clock()
    w = _worker(deps, sweeper=sweeper, clock=clock)

    with caplog.at_level("ERROR"):
        assert w.maybe_sweep_views() is True          # it RAN; it just did not succeed
    assert "view sweep failed" in caplog.text
    assert sweeper.calls == 1


def test_a_faulting_sweep_is_not_retried_on_every_idle_tick(rig):
    """The interval is scheduled BEFORE the pass runs. Without that, a persistently faulting sweep
    would re-attempt on every poll — a fresh worktree and a corpus parse every few seconds, which
    is the failure mode the interval exists to prevent, arriving through the error path."""
    env, deps = rig
    sweeper = _CountingSweep(raises=RuntimeError("still broken"))
    clock = _Clock()
    w = _worker(deps, sweeper=sweeper, clock=clock, interval=900.0)

    w.maybe_sweep_views()
    clock.advance(3.0)
    assert w.maybe_sweep_views() is False
    assert sweeper.calls == 1


# ── the operator-facing line ───────────────────────────────────────────────────────────────────
def test_a_converged_pass_says_nothing(rig):
    """A maintenance pass that printed "nothing changed" every interval would bury the passes that
    did change something — `swept_clause`'s own rule, for the same reason."""
    result = views_regenerate.RunResult(checked=3, population=3)
    result.outcomes = [views_regenerate.RegenOutcome(entity_id=f"e{i}", entity_name=f"E{i}",
                                                     action="unchanged") for i in range(3)]
    assert worker.view_sweep_clause(result) == ""


def test_a_pass_that_moved_something_reports_what_and_how_much(rig):
    result = views_regenerate.RunResult(checked=2, population=5)
    result.outcomes = [
        views_regenerate.RegenOutcome(entity_id="a", entity_name="A", action="written"),
        views_regenerate.RegenOutcome(entity_id="b", entity_name="B", action="unchanged")]
    result.skip_reasons = [views_regenerate.RUN_CEILING_REASON.format(ceiling=1, deferred=3)]

    line = worker.view_sweep_clause(result)

    assert "2 of 5" in line
    assert "1 regenerated" in line
    assert "run-ceiling-reached(1)" in line


# ── D6: the pass materializes its OWN worktree, which is what keeps guarded=False honest ───────
def test_run_view_sweep_works_on_a_fresh_worktree_and_leaves_the_checkout_alone(rig, tmp_path):
    """The post-meeting hook BORROWS the capture's worktree, so it inherits the "always a fresh,
    detached checkout" justification `regenerate_entity` states for skipping the steward guards. An
    idle pass has none to borrow, so it builds one — and the justification stays literally true
    rather than quietly becoming a claim nobody checks.

    Driven against a real repo and a real bare remote: the view lands on the REMOTE, and the
    worker's own checkout is left untouched, which is only possible if the write happened somewhere
    else.
    """
    env, deps = rig
    _anchor_a_page(env, "acme")

    result = worker.run_view_sweep(FakeConn(), deps)

    assert result.stats["written"] == 1
    assert "views/acme.md" in _remote_files(env)
    assert not os.path.exists(os.path.join(env.repo, "views", "acme.md"))
    # And no worktree survived the pass: `ephemeral_worktree` reaps its own on the way out.
    assert gitcmd.reap(env.repo, deps.settings.worktree_root) == 0


def test_run_view_sweep_on_a_converged_corpus_commits_nothing(rig):
    """The benign twin at the worker level. The librarian fixture repo anchors no entity at all, so
    this is also the shape of the very first pass on a brand-new deployment: a population of zero,
    one parse, no commit."""
    env, deps = rig
    before = _remote_log(env)

    result = worker.run_view_sweep(FakeConn(), deps)

    assert result.stats["written"] == 0 and result.stats["removed"] == 0
    assert _remote_log(env) == before
    assert worker.view_sweep_clause(result) == ""


def test_run_view_sweep_honours_the_configured_ceiling(rig, monkeypatch):
    """The settings value really reaches `regenerate.run` — asserted at the seam rather than
    trusted, because a ceiling that silently stayed `None` is invisible until the bill arrives."""
    env, deps = rig
    seen = {}

    async def spy(repo, conn, **kw):
        seen.update(kw)
        return views_regenerate.RunResult()

    monkeypatch.setattr(worker.views_regenerate, "sweep", spy)
    import dataclasses
    deps = dataclasses.replace(deps, settings=dataclasses.replace(deps.settings,
                                                                  view_sweep_ceiling=4))

    worker.run_view_sweep(FakeConn(), deps)

    assert seen["max_changes"] == 4
    assert seen["guarded"] is False


def test_run_view_sweep_reads_the_registry_at_its_own_base_not_at_startup(rig, monkeypatch):
    """A de-registration pushed since the worker booted is exactly the input that turns an orphaned
    view into a removal. A startup pre-flight copy would miss it until the next restart — the same
    reason `processing.process_item` re-reads the registry per item."""
    env, deps = rig
    _anchor_a_page(env, "acme")
    worker.run_view_sweep(FakeConn(), deps)
    assert "views/acme.md" in _remote_files(env)

    # The sweep pushed the view from its OWN worktree, so this checkout is behind the remote now —
    # which is itself the proof that the write did not happen here.
    gitcmd.run("pull", "--quiet", "--rebase", "origin", "main", cwd=env.repo)
    with open(os.path.join(env.repo, "ops", "entity-registry.json"), "w") as f:
        f.write('{"entities": {}}\n')
    support.commit_and_push(env.repo, "chore: de-register every entity")

    result = worker.run_view_sweep(FakeConn(), deps)

    assert result.stats["removed"] == 1
    assert "views/acme.md" not in _remote_files(env)


# ── the settings' own domain ───────────────────────────────────────────────────────────────────
def test_a_negative_interval_is_refused_and_zero_is_not(rig):
    """Zero is the documented off switch; negative would make every idle tick "due"."""
    import dataclasses
    env, deps = rig
    dataclasses.replace(deps.settings, view_sweep_interval_s=0).check_domains()   # legal
    with pytest.raises(LibrarianConfigError, match="view_sweep_interval_s"):
        dataclasses.replace(deps.settings, view_sweep_interval_s=-1).check_domains()


def test_a_ceiling_below_one_is_refused(rig):
    """A zero ceiling defers every entity on every pass — a loop that runs forever and converges
    nothing, which reads as working."""
    import dataclasses
    env, deps = rig
    with pytest.raises(LibrarianConfigError, match="view_sweep_ceiling"):
        dataclasses.replace(deps.settings, view_sweep_ceiling=0).check_domains()


def test_both_knobs_are_env_resolved_through_from_args(monkeypatch):
    """`Settings.from_args` is the ONE place this package reads the environment, and an operator
    who sets a variable that nothing reads has been silently ignored."""
    from types import SimpleNamespace
    monkeypatch.setenv(config.VIEW_SWEEP_INTERVAL_ENV, "60")
    monkeypatch.setenv(config.VIEW_SWEEP_CEILING_ENV, "3")
    settings = config.Settings.from_args(SimpleNamespace())
    assert settings.view_sweep_interval_s == 60.0
    assert settings.view_sweep_ceiling == 3


# ── the two refusals in front of the one unattended writer ────────────────────────────────────
def test_a_registry_absent_at_the_base_refuses_the_pass_and_deletes_nothing(rig):
    """The defense, made to FIRE. Every existing sweep test runs with the registry present, which
    proves the pass works and says nothing about the guard in front of it — and this guard is what
    stands between a fetch that raced a force-push (or a corrupt object, or a botched merge that
    dropped `ops/`) and a pass whose answer to every orphaned view is to DELETE it, ceiling per
    interval, for as long as the worker runs."""
    env, deps = rig
    _anchor_a_page(env, "acme")
    worker.run_view_sweep(FakeConn(), deps)
    assert "views/acme.md" in _remote_files(env)

    gitcmd.run("pull", "--quiet", "--rebase", "origin", "main", cwd=env.repo)
    gitcmd.run("rm", "--quiet", os.path.join("ops", "entity-registry.json"), cwd=env.repo)
    support.commit_and_push(env.repo, "chore: drop the registry from the repo")
    conn = FakeConn()

    result = worker.run_view_sweep(conn, deps)

    (reason,) = result.skip_reasons
    assert reason.startswith("refusing to converge views/")
    assert "views/acme.md" in _remote_files(env), "the refusal exists so this file survives"
    # The refusal is not only a log line: `job_runs` is the one operator surface an unattended
    # pass has, and the row names the exception the FILING path raises for the same fault.
    assert any("job_runs" in sql and LibrarianConfigError.__name__ in tuple(params)
               for sql, params in conn.executed)


def test_a_registry_that_is_present_and_empty_is_honoured_not_refused(rig):
    """The benign twin, and the line the refusal's own wording draws: a registry that is PRESENT
    and declares no entities is a committed, reviewable statement — de-registration is exactly the
    input that turns a view into a removal, and the guard must not eat it. (The end-to-end version
    of this twin already exists: `test_run_view_sweep_reads_the_registry_at_its_own_base_not_at_
    startup` drives the removal itself.)"""
    env, deps = rig
    gitcmd.run("pull", "--quiet", "--rebase", "origin", "main", cwd=env.repo)
    with open(os.path.join(env.repo, "ops", "entity-registry.json"), "w") as f:
        f.write('{"entities": {}}\n')
    support.commit_and_push(env.repo, "chore: de-register everything, on purpose")

    result = worker.run_view_sweep(FakeConn(), deps)

    assert result.skip_reasons == []


def test_a_local_only_base_refuses_the_pass_when_the_remote_base_is_required(rig):
    """The other refusal: a deployed worker whose fetch failed is sitting on whatever it was
    cloned at, and a pass off that base re-derives every view from an OLD member set — replaying
    an older, potentially wider acl over the current one. Same rule as the filing path's
    `_resolve_filing_base`, asserted on this caller because this one has no operator in front of
    it."""
    import dataclasses
    env, deps = rig
    gitcmd.run("remote", "remove", "origin", cwd=env.repo)
    deps = dataclasses.replace(deps, settings=dataclasses.replace(deps.settings,
                                                                  require_remote_base=True))
    conn = FakeConn()

    result = worker.run_view_sweep(conn, deps)

    (reason,) = result.skip_reasons
    assert reason.startswith("refusing to converge views/")
    assert "origin/main" in reason
    assert any("job_runs" in sql and "StaleBaseError" in tuple(params)
               for sql, params in conn.executed)


def test_a_reachable_remote_base_satisfies_the_requirement(rig):
    """The benign twin: `require_remote_base=True` is the DEPLOYED shape, so the guard must pass
    every healthy interval, not only fail the broken one."""
    import dataclasses
    env, deps = rig
    deps = dataclasses.replace(deps, settings=dataclasses.replace(deps.settings,
                                                                  require_remote_base=True))

    result = worker.run_view_sweep(FakeConn(), deps)

    assert result.skip_reasons == []


# ── helpers ────────────────────────────────────────────────────────────────────────────────────
def _anchor_a_page(env, entity_id: str) -> None:
    """A page anchored to a registered entity, committed and pushed the way ANY door leaves one —
    no view hook involved, which is the whole point of a state-based pass."""
    path = os.path.join(env.repo, "wiki", "notes", "Sweep Fixture.md")
    with open(path, "w") as f:
        f.write(f'---\ntype: note\ntitle: "Sweep Fixture"\nentity: [{entity_id}]\n'
                f'as_of: "2026-08-17"\ncreated: "2026-08-17"\nupdated: "2026-08-17"\n'
                f'status: developing\ntags: [note]\n---\n\n# Sweep Fixture\n\nA page.\n')
    # The entity's OWN page has to anchor itself too, or the view has no `type: entity` member and
    # degrades to the raw id — the shape governed entity birth actually produces.
    entity_md = os.path.join(env.repo, "wiki", "entities", "Acme Corp.md")
    with open(entity_md) as f:
        text = f.read()
    with open(entity_md, "w") as f:
        f.write(text.replace("type: entity\n", f"type: entity\nentity: [{entity_id}]\n", 1))
    support.commit_and_push(env.repo, "feat: a page anchored to acme")


def _remote_files(env) -> list[str]:
    return gitcmd.run("ls-tree", "-r", "--name-only", "main", cwd=env.bare).stdout.splitlines()


def _remote_log(env) -> str:
    return gitcmd.run("log", "--oneline", "main", cwd=env.bare).stdout
