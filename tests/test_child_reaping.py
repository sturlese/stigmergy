"""The accounting that makes a leaked child process fail its own test.

`tests/childwatch.py` explains why this matters: a surviving `stigmergy-librarian` or
`stigmergy-queue` claims rows from `capture_queue` while a later test's fixture truncates it, and
the resulting `LeaseLostError` storm lands in packages that never spawned anything. The point of
the registry is attribution — moving the failure from wherever it surfaced back to whoever caused
it.

The structural test at the bottom is what keeps that true tomorrow. `spawn()` cannot register a
child it never saw, so a registry any new test file may bypass with a plain `subprocess.Popen`
answers only for the files written before the rule existed — the same "one file remembering is not
a control" this suite's root conftest already argues twice.
"""
import pathlib
import subprocess
import sys

from tests import childwatch

TESTS = pathlib.Path(__file__).resolve().parent

# A child that will not exit on its own inside a test's lifetime, so "still running" is a fact
# rather than a race. `sys.executable` because every environment that can run this suite has it.
SLEEPER = [sys.executable, "-c", "import time; time.sleep(300)"]


def test_a_child_still_running_is_reported_as_a_stray():
    """The sensitivity half: the registry actually notices."""
    proc = childwatch.spawn(SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert proc in childwatch.strays()
    finally:
        childwatch.reap([proc])


def test_a_child_that_its_test_reaped_is_not_a_stray():
    """The benign twin, and the one that decides whether this guard is usable at all.

    Every well-behaved spawning test in the suite goes through exactly this shape — spawn, kill in
    a `finally` — and there are a dozen of them. A registry that reported those as leaks would
    have to be turned off within a day, which is the ordinary fate of a check with no specificity.
    """
    proc = childwatch.spawn(SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.kill()
    proc.communicate()

    assert childwatch.strays() == []


def test_reaping_kills_the_stray_rather_than_only_naming_it():
    """A report that left the process running would be accurate and useless: the test that leaked
    would go red, and the survivor would carry on failing later suites exactly as before."""
    proc = childwatch.spawn(SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    described = childwatch.reap(childwatch.strays())

    assert proc.poll() is not None, "reap named the stray but left it running"
    assert len(described) == 1 and f"pid {proc.pid}" in described[0]
    assert "time.sleep" in described[0], "the report must show WHAT was left running, not only a pid"


def test_forgetting_starts_a_new_tally_so_a_leak_is_blamed_on_one_test():
    """Why the autouse fixture clears at BOTH ends. Without the second clear, a test that failed
    while holding a child would hand it to the next test, which would then be reported as the
    leaker — attribution pointing one test to the right of the truth is worse than none, because
    it is confidently wrong."""
    proc = childwatch.spawn(SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        childwatch.forget()
        assert childwatch.strays() == [], "a forgotten child was still charged to the next test"
    finally:
        proc.kill()
        proc.communicate()


def test_no_test_spawns_a_process_without_registering_it():
    """The structural half. `childwatch.py` is the only file allowed to call `Popen` directly —
    it is the one doing the registering.

    Reverting any single call site under `tests/librarian/` or `tests/capture/` to a plain
    unregistered spawn turns this red and leaves every other test in the suite green — which is
    exactly the invisibility this file exists to remove.
    """
    # Assembled rather than written out, so this file does not match its own needle. Spelling it
    # whole here would make the guard permanently red for a reason that has nothing to do with the
    # property, and the obvious repair — exempting this file too — would blind it.
    needle = "subprocess." + "Popen("
    offenders = sorted(str(p.relative_to(TESTS)) for p in TESTS.rglob("*.py")
                       if p.name != "childwatch.py" and needle in p.read_text())
    assert not offenders, (
        f"{offenders} start a child process with subprocess.Popen directly, so nothing knows the "
        f"process exists. Use `childwatch.spawn(...)` — same signature — or a leak there will "
        f"surface as unattributable LeaseLostError failures in some other package.")
