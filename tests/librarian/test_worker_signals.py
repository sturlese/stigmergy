"""Real SIGINT/SIGTERM delivered to a real `Worker` subprocess — anything that blocks gets
interrupted by a real signal, never by calling its handler and assuming. `worker.py`'s own
shutdown contract: "SIGINT... finish the item in flight, then stop. A SECOND Ctrl-C releases it
immediately"; "SIGTERM... Release immediately, never wait".

Every test here spawns `_worker_harness.py` as a genuine OS process (never calls `_on_sigint`/
`_on_sigterm` directly — that would prove the handler runs, not that a real signal reaches it and
the process behaves as promised) and reads its stdout with a real timeout to know exactly when an
item is in flight before sending anything.
"""
import os
import pathlib
import select
import signal
import subprocess
import sys
import time

from stigmergy.capture import queue, schema
from stigmergy.librarian import gitcmd, worker
from tests import childwatch, testdb
from tests.librarian import support

# The repo root: invoked with `-m` (not as a bare script path) so the subprocess's `sys.path`
# picks up "." the same way pytest's own `pythonpath = ["src", "."]` does, and `import
# tests.librarian.support` resolves inside the harness exactly like it does in this test process.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SLOW_SECONDS = 3.0


def _rig_with_real_evidence(tmp_path, minio):
    """Like `support.build_rig`, but wired to the REAL (MinIO) evidence store — the worker
    subprocess builds its own `Deps` and cannot see anything the parent test wrote to an
    in-process `MemoryEvidenceStore` (see `_worker_harness.py`'s own comment on this)."""
    return support.build_rig(tmp_path, evidence=minio)


def _read_until(stream, needle: str, timeout: float = 20.0):
    """Read lines until one contains `needle`, or `timeout` elapses. Returns None on either
    timeout or EOF.

    **The `select` is the whole point and must not be simplified away.** This helper used to be a
    plain `while time.monotonic() < deadline: line = stream.readline()`, which reads like a bounded
    wait and is not one: `readline()` on a pipe blocks forever when the child stays alive and writes
    nothing, so the deadline is only consulted BETWEEN lines and never during the one that hangs.

    That cost real time. Nine CI runs died on GitHub's six-hour ceiling with an orphan `python`
    child, the whole suite frozen right here, and because a job-level kill names no test the gate
    just stopped reporting and everyone read "still running" as "probably fine" — two changes
    merged on a CI that had not actually passed in between. On a laptop the child always printed
    promptly, so the defect was invisible exactly where the suite was run.

    `select` bounds the WAIT rather than the loop, so a silent child produces a named failure in
    `timeout` seconds. "Anything that blocks gets interrupted in a test" applies to the test
    harness itself, and this is what it looks like when it does not.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if not select.select([stream], [], [], remaining)[0]:
            return None
        line = stream.readline()
        if not line:          # EOF — the child exited without saying it
            return None
        if needle in line:
            return line


def _spawn(env, worktree_root: str, sleep_seconds: float = SLOW_SECONDS) -> subprocess.Popen:
    return childwatch.spawn(
        [sys.executable, "-m", "tests.librarian._worker_harness", "--dsn", testdb.dsn(),
         "--repo", env.repo, "--bare", env.bare, "--worktree-root", worktree_root,
         "--sleep-seconds", str(sleep_seconds)],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.communicate()


def test_sigint_once_finishes_the_item_in_flight_then_stops(tmp_path, clean_queue,
                                                             require_gitleaks, require_minio):
    env, deps = _rig_with_real_evidence(tmp_path, require_minio)
    support.submit(clean_queue, deps, "First capture, about Acme Corp.")
    support.submit(clean_queue, deps, "Second capture, about Acme Corp.")

    proc = _spawn(env, str(tmp_path / "worktrees"))
    try:
        assert _read_until(proc.stdout, "PROCESSING-STARTED"), "worker never started an item"
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=SLOW_SECONDS + 15)
    finally:
        _kill(proc)

    assert "Traceback" not in out and "Traceback" not in err
    assert "finishing the item in flight, then stopping" in out
    # WORDING CHANGE (test-pass Finding B): the hint used to promise a second press would "release
    # it now instead". Nothing can abort a `process_item` already running — `releasing` only stops
    # the NEXT claim — so the second press does the same thing without waiting to poll, and the
    # hint now says that.
    assert "Press Ctrl-C again" in out
    assert "release it now instead" not in out

    rows = {r["status"] for r in queue.list_all_submissions(clean_queue)}
    # exactly one item was claimed and carried to a real terminal state; the second was never
    # even claimed, because `stopping` breaks the loop BEFORE it calls `process_next` again.
    assert schema.QUEUED in rows
    assert rows & {schema.FILED, schema.REJECTED, schema.FAILED}


def test_sigterm_is_acknowledged_immediately_without_waiting_for_the_item(
        tmp_path, clean_queue, require_gitleaks, require_minio):
    """"Release immediately, never wait" (worker.py module docstring) — checked here as PROMPTNESS
    of the acknowledgement itself: the process must not sit silent until the in-flight item's own
    (artificially slowed) work finishes before it even prints that it received the signal.

    This test deliberately stops at "acknowledged promptly" and does not assert what ultimately
    becomes of the item that was already in flight when SIGTERM arrived — see
    `test_a_hard_kill_mid_item_...` below for the property that IS asserted end to end (a
    genuine kill leaves the row recoverable, never abandoned, never double-filed). Whether a
    caught-but-not-killed SIGTERM must also abort a synchronous `process_item` call already
    running is genuinely undecided, not a settled contract — so there is no assertion here, which
    would only be guessing at the answer."""
    env, deps = _rig_with_real_evidence(tmp_path, require_minio)
    support.submit(clean_queue, deps, "A capture about Acme Corp.")

    proc = _spawn(env, str(tmp_path / "worktrees"))
    try:
        assert _read_until(proc.stdout, "PROCESSING-STARTED"), "worker never started an item"
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        # `_read_until` consumes the WHOLE matched line (mirrors `tests/capture/test_cli.py`'s own
        # caveat) — the acknowledgement and the "releasing..." wording are ONE printed sentence,
        # so it is captured here rather than re-searched for in what `communicate()` returns after.
        ack_line = _read_until(proc.stdout, "received SIGTERM", timeout=5.0)
        assert ack_line, "SIGTERM was not acknowledged promptly — it must never wait for the item"
        elapsed_to_ack = time.monotonic() - started
    finally:
        _kill(proc)

    # near-instant: well under the item's own 3s artificial delay, so this is really measuring
    # "did the handler run promptly", not "did the whole process happen to finish quickly".
    assert elapsed_to_ack < 1.0
    # The message used to say the item was being "released" and would "return to the queue". It is
    # not released: nothing can abort a `process_item` already running, and in a real run the
    # in-flight item finished and was FILED, with a commit, printed directly under that promise.
    # Rewording was the fix rather than building cancellation, so the message now states that the
    # item finishes, that it may be filed, and names the crash case as the only path where the row
    # does come back.
    assert "finishing the item in flight, then stopping" in ack_line
    assert "possibly filed, with a commit" in ack_line
    assert "if this process is killed before it finishes" in ack_line
    assert "press" not in ack_line.lower()     # SIGTERM prints no "press again" hint — nobody is there


def test_a_hard_kill_mid_item_leaves_the_row_recoverable_and_the_worktree_reaped(
        tmp_path, clean_queue, require_gitleaks, require_minio):
    """Criterion 15 with a REAL crash: `SIGKILL` is uncatchable — no handler, no `finally`, the
    same shape as an OOM kill or `docker kill -s KILL` — so this is the honest way to prove "the
    item in flight is finished or released, never abandoned silently" rather than assuming a
    caught signal generalizes to an uncatchable one."""
    env, deps = _rig_with_real_evidence(tmp_path, require_minio)
    worktree_root = str(tmp_path / "worktrees")
    item = support.submit(clean_queue, deps, "A capture about Acme Corp.")

    proc = _spawn(env, worktree_root)
    try:
        assert _read_until(proc.stdout, "PROCESSING-STARTED"), "worker never started an item"
        proc.kill()                                    # SIGKILL — the real crash case
        proc.wait(timeout=10)
    finally:
        _kill(proc)

    row = queue.get_submission_trace(clean_queue, item["id"])
    assert row["status"] == schema.CLAIMED
    assert row["attempts"] == 1
    assert row["finished_at"] is None

    # the leftover worktree directory is on disk — the crash skipped the `finally` that normally
    # tears it down ...
    leftover_before = [n for n in os.listdir(worktree_root)
                       if n.startswith(gitcmd.WORKTREE_PREFIX)]
    assert leftover_before, "expected a leftover worktree directory after a hard kill mid-item"

    # ... and `startup_checks`'s own reap step — what a restarted worker runs before claiming
    # anything — cleans it up rather than a future worker ever reusing it.
    reaped = gitcmd.reap(env.repo, worktree_root)
    assert reaped >= 1
    assert not [n for n in os.listdir(worktree_root) if n.startswith(gitcmd.WORKTREE_PREFIX)]

    # after the visibility timeout, the row comes back to the queue with `attempts` already
    # incremented (it counts deliveries, not failures — `queue.py`'s own docstring) ...
    queue.release_expired(clean_queue, visibility_timeout_s=0)
    row_after_sweep = queue.get_submission_trace(clean_queue, item["id"])
    assert row_after_sweep["status"] == schema.QUEUED

    # ... and a restarted worker (this test process, standing in for one) files it EXACTLY once:
    # not lost, and not filed twice.
    _, result = worker.process_next(clean_queue, deps)
    assert result.status in (schema.FILED, schema.REJECTED, schema.FAILED)
    row_final = queue.get_submission_trace(clean_queue, item["id"])
    assert row_final["attempts"] == 2               # the crash burned delivery 1; this is 2


def test_sigint_twice_the_second_press_sets_releasing_before_the_next_claim(
        tmp_path, clean_queue, require_gitleaks, require_minio):
    """What a REAL double-press against a currently-running item leaves behind, asserted rather
    than assumed — see the module docstring on why this must be a real signal, not a direct
    handler call.

    WORDING CHANGE (test-pass Finding B): the contract used to say a second Ctrl-C "releases it
    immediately... nothing was committed", and this test matched on "releasing the item in flight".
    Both were false — the in-flight item cannot be aborted and may be filed with a real commit —
    so the message was reworded and this test now matches the honest one. The PROPERTY it proves is
    unchanged and is the one that was always true: no third claim happens.

    Both presses arrive while `process_next` is still synchronously inside the ONE item it
    already claimed (the double is slowed on purpose, see `SLOW_SECONDS`), so `releasing` cannot
    abort that in-flight call — nothing in `Worker.run` re-checks the flag until the call
    returns. What it DOES do, provably, is stop a THIRD claim: `run()`'s own loop re-checks
    `self.releasing` before ever calling `process_next` again, so this test's second queued item
    must still be sitting `queued` once the process exits, whatever became of the first."""
    env, deps = _rig_with_real_evidence(tmp_path, require_minio)
    support.submit(clean_queue, deps, "First capture, about Acme Corp.")
    support.submit(clean_queue, deps, "Second capture, about Acme Corp.")

    proc = _spawn(env, str(tmp_path / "worktrees"))
    try:
        assert _read_until(proc.stdout, "PROCESSING-STARTED"), "worker never started an item"
        proc.send_signal(signal.SIGINT)
        assert _read_until(proc.stdout, "finishing the item in flight"), (
            "first SIGINT was not acknowledged")
        proc.send_signal(signal.SIGINT)
        assert _read_until(proc.stdout, "stopping as soon as the item in flight finishes"), (
            "second SIGINT was not acknowledged")
        out, err = proc.communicate(timeout=SLOW_SECONDS + 15)
    finally:
        _kill(proc)

    assert "Traceback" not in out and "Traceback" not in err
    rows = {r["status"] for r in queue.list_all_submissions(clean_queue)}
    assert schema.QUEUED in rows, (
        "a second item was claimed after 'releasing the item in flight' was printed")
