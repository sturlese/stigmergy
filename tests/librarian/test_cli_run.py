"""`stigmergy-librarian run` — the loop.

Two halves, deliberately in one file because they are two halves of one subcommand:

- **the loop itself, interrupted for real.** Anything that blocks gets interrupted in a test by a
  real signal, never by calling a handler. So these tests spawn the REAL console entry point
  as a separate OS process, wait for a line that proves it is running, and deliver a genuine
  SIGINT/SIGTERM. `test_worker_signals.py` proves the same contract one layer down, against
  `Worker` through a harness; what is added here is that the CLI wires it up correctly and exits 0
  when told to stop.
- **the configuration refusals**, driven in-process through `cli.main(argv)` because they never
  reach the loop: an explicit `--visibility-timeout 0` or `--poll-interval 0` must be refused out
  loud rather than silently replaced by a default. That is the carry-over defect this pass fixes.

The subprocess needs the REAL (MinIO) evidence store, for the same reason `_worker_harness.py`
does: `_cmd_run` builds its own `store_from_env()`, so nothing this test process wrote to an
in-process `MemoryEvidenceStore` would be visible to it.
"""
import builtins
import os
import pathlib
import signal
import subprocess
import sys
import time
import types

import pytest

from stigmergy.capture import queue, schema
from stigmergy.librarian import cli, config, worker
from tests import childwatch, testdb
from tests.librarian import support

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _librarian_command() -> list[str]:
    """The installed console script when there is one — the real entry point — else `python -m`
    so a bare source checkout still runs. Mirrors
    `tests/server/conftest.py::server_command`."""
    beside = os.path.join(os.path.dirname(sys.executable), "stigmergy-librarian")
    if os.path.exists(beside):
        return [beside]
    return [sys.executable, "-m", "stigmergy.librarian.cli"]


def _spawn(env, *extra: str) -> subprocess.Popen:
    return childwatch.spawn(
        [*_librarian_command(), "--dsn", testdb.dsn(), "--repo", env.repo,
         "run", "--poll-interval", "0.2", *extra],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _read_until(stream, needle: str, timeout: float = 25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = stream.readline()
        if not line:
            return None
        if needle in line:
            return line
    return None


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.communicate()


@pytest.fixture()
def run_rig(tmp_path, require_gitleaks, require_minio, clean_queue):
    """A real repo + bare remote and the real MinIO store the spawned process will resolve itself.
    Returns `(env, deps, conn)` — `deps` only so this test can submit through the same evidence
    store the child will read from."""
    env, deps = support.build_rig(tmp_path, evidence=require_minio)
    return env, deps, clean_queue


# ── the loop, with a real signal ──────────────────────────────────────────────────────────────────
def test_run_drains_the_queue_and_stops_cleanly_on_sigint(run_rig):
    env, deps, conn = run_rig
    support.submit(conn, deps, "A capture about Acme Corp, drained by the loop.")

    proc = _spawn(env)
    try:
        filed = _read_until(proc.stdout, "->")
        assert filed, "the loop never reported an item"
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=30)
    finally:
        _kill(proc)

    assert proc.returncode == 0, err
    assert "Traceback" not in out and "Traceback" not in err
    assert "finishing the item in flight, then stopping" in out
    assert "stopped after" in out
    # the item really reached a terminal state — the loop is the same `process_next` `once` uses
    statuses = {r["status"] for r in queue.list_all_submissions(conn)}
    assert statuses & {schema.FILED, schema.REJECTED, schema.TRIAGE, schema.FAILED}


def test_run_prints_the_same_preamble_once_prints_plus_its_loop_settings(run_rig):
    """An operator who has read "filing into … against origin/main@abc" from `once` must not have to
    learn a second layout to read it from `run` — it is the same `_preamble`. The loop adds the two
    numbers that are only meaningful for a loop."""
    env, _, _ = run_rig

    proc = _spawn(env)
    try:
        preamble = _read_until(proc.stdout, "filing into")
        settings_line = _read_until(proc.stdout, "polling every")
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=30)
    finally:
        _kill(proc)

    assert preamble and env.repo in preamble and "origin/main@" in preamble
    assert settings_line
    assert "polling every 0.2s" in settings_line
    assert (f"lease {worker.human_duration(config.DEFAULT_VISIBILITY_TIMEOUT_S)}"
            in settings_line)   # derived end to end — a retyped "(N min)" drifts (issue #113)
    assert "Ctrl-C stops after the item in flight" in settings_line


def test_the_signal_handlers_are_installed_before_the_first_line_is_printed(run_rig, monkeypatch):
    """Old behaviour: `_cmd_run` printed the preamble and the settings line, and only THEN
    installed its handlers. Everything that waits on this loop waits for a printed line and then
    signals, so that window is one in which SIGTERM meets the default disposition and kills the
    process with 143 — CI run 30895512061, `assert -15 == 0`.

    Driven in-process rather than by racing a real subprocess, because the window is microseconds
    wide and a test that has to win a race to fail is a test that goes quietly green. What is
    asserted is the ORDER, which is the property: by the time anything is on stdout, SIGTERM is
    no longer `SIG_DFL`. The real-signal half stays in the two subprocess tests around it.
    """
    env, _, conn = run_rig
    dispositions = []
    real_print = builtins.print

    def recording_print(*args, **kwargs):
        dispositions.append(signal.getsignal(signal.SIGTERM))
        return real_print(*args, **kwargs)

    # The REAL `_cmd_run` runs; only its expensive collaborators are stood in for, and none of
    # them can affect the ordering under test. `run` returns immediately because this test is
    # about what happened BEFORE the loop starts.
    settings = support.build_settings(env, worktree_root=str(env.repo), poll_interval_s=0.2)
    loop = worker.Worker(conn, None, on_output=lambda line: None)
    base = types.SimpleNamespace(describe=lambda: "origin/main@abc1234")
    monkeypatch.setattr(builtins, "print", recording_print)
    monkeypatch.setattr(cli.worker, "startup_checks", lambda _s: {"base": base, "reaped": 0})
    monkeypatch.setattr(cli.worker, "build_deps", lambda *a, **k: None)
    monkeypatch.setattr(cli.evidence_plane, "store_from_env", lambda: None)
    monkeypatch.setattr(cli.worker, "Worker", lambda *a, **k: loop)
    monkeypatch.setattr(loop, "run", lambda: 0)
    saved = signal.getsignal(signal.SIGTERM)
    try:
        cli._cmd_run(conn, None, settings)
    finally:
        signal.signal(signal.SIGTERM, saved)

    assert dispositions, "the loop printed nothing at all — the test is not exercising _cmd_run"
    assert dispositions[0] is not signal.SIG_DFL, (
        "SIGTERM was still on its default disposition when the first line was printed — anything "
        "signalling on that line would kill this process with 143 instead of draining")


def test_run_stops_cleanly_on_sigterm_too(run_rig):
    """`docker stop` and every supervisor send SIGTERM, and a loop that exited non-zero when told to
    stop would fail every restart policy it runs under."""
    env, _, _ = run_rig

    proc = _spawn(env)
    try:
        assert _read_until(proc.stdout, "polling every"), "the loop never started"
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    finally:
        _kill(proc)

    assert proc.returncode == 0, err
    assert "received SIGTERM" in out
    assert "stopped after 0 item(s)" in out
    assert "Traceback" not in out and "Traceback" not in err


# ── the configuration refusals (no loop is ever entered) ──────────────────────────────────────────
def _main(capsys, *argv):
    exit_code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return exit_code, out, err


def test_an_explicit_visibility_timeout_of_zero_is_refused_with_the_arithmetic(capsys, run_rig):
    """**The carry-over defect.** `--visibility-timeout 0` used to be discarded in silence
    (`args.x or default`, and `0` is falsy), so the run proceeded on the 900s default and reported
    900 back. Now the zero survives resolution and `worker.startup_checks` refuses it, naming the
    worst case it has to exceed rather than just saying no."""
    env, _, _ = run_rig

    exit_code, out, err = _main(capsys, "--dsn", testdb.dsn(), "--repo", env.repo,
                                "run", "--visibility-timeout", "0")

    assert exit_code == cli.EXIT_CONFIG
    assert "visibility_timeout_s is 0s" in err
    assert f"{config.MAX_AGENT_ATTEMPTS} agent attempts" in err
    assert str(config.minimum_visibility_timeout_s()) in err
    assert "Traceback" not in err


def test_the_same_zero_is_refused_on_once_not_only_on_run(capsys, run_rig):
    """One resolution path, so the fix cannot hold for one subcommand and not the other."""
    env, _, _ = run_rig
    exit_code, _, err = _main(capsys, "--dsn", testdb.dsn(), "--repo", env.repo,
                              "once", "--visibility-timeout", "0")
    assert exit_code == cli.EXIT_CONFIG
    assert "visibility_timeout_s is 0s" in err


def test_a_poll_interval_of_zero_is_refused_rather_than_becoming_a_busy_loop(capsys, run_rig):
    """The other half of honoring an explicit zero: once it survives resolution it has to be
    ANSWERED. A zero poll interval hammers Postgres with claims in a tight loop, so the refusal is
    the point rather than the acceptance."""
    env, _, _ = run_rig

    exit_code, out, err = _main(capsys, "--dsn", testdb.dsn(), "--repo", env.repo,
                                "run", "--poll-interval", "0")

    assert exit_code == cli.EXIT_CONFIG
    assert "poll_interval_s is 0" in err and "tight loop" in err
    assert str(config.DEFAULT_POLL_INTERVAL_S) in err
    assert "Traceback" not in err


def test_max_attempts_zero_is_refused_because_every_delivery_would_start_exhausted(capsys, run_rig):
    env, _, _ = run_rig

    exit_code, out, err = _main(capsys, "--dsn", testdb.dsn(), "--repo", env.repo,
                                "run", "--max-attempts", "0")

    assert exit_code == cli.EXIT_CONFIG
    assert "max_attempts is 0" in err
    assert "Traceback" not in err


def test_a_non_zero_flag_still_takes_effect_which_is_the_benign_twin(capsys, run_rig):
    """The benign twin. The fix must not have turned "honor the flag" into "refuse the flag": an
    ordinary explicit value has to be used, and `--visibility-timeout 1200` is above the minimum, so
    `startup_checks` lets it through and the loop runs with it."""
    env, _, _ = run_rig

    proc = childwatch.spawn(
        [*_librarian_command(), "--dsn", testdb.dsn(), "--repo", env.repo,
         "run", "--poll-interval", "0.2", "--visibility-timeout", "1200"],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = _read_until(proc.stdout, "polling every")
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
    finally:
        _kill(proc)

    assert line and "lease 1200s (20 min)" in line
