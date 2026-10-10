"""Every child process a test spawns, and the proof that none of them outlived the test.

Several suites here drive a real OS process rather than a monkeypatched one, and they are right
to: only a genuinely separate process can be sent a real `SIGINT`, and only a real
`stigmergy-librarian` proves the loop claims and releases rows the way a deployment's would. The
cost is that those children reach into `capture_queue` — the one table every Postgres fixture
`DELETE`s at setup.

A child that survives its own test therefore does not fail that test. It fails a LATER one, in
another package, by claiming a row a fixture is about to delete, and the failure it produces is
`LeaseLostError` / "submission N does not exist" — the exact storm this module's issue was filed
about, with no thread back to the test that leaked. Sequential suites make that shape look
impossible, which is why it went unattributed for so long.

So the accounting is structural rather than remembered. `spawn()` registers, the root conftest's
autouse fixture asks `strays()` after every single test in the tree, and
`tests/test_child_reaping.py` refuses a direct `subprocess.Popen` anywhere under `tests/` — because
a registry that a new test file can silently opt out of is a registry that answers for whichever
files happened to be written before the rule.

Named `childwatch.py`, not `test_childwatch.py`: pytest collects `test_*.py`, and this is support
code — the same reason `testdb.py` is spelled the way it is.
"""
import subprocess

# Children spawned since the current test started. Cleared at both ends of the autouse fixture, so
# a test that fails mid-flight does not hand its strays to the next one and make IT the culprit.
_SPAWNED: list[subprocess.Popen] = []


def spawn(argv, **kwargs) -> subprocess.Popen:
    """`subprocess.Popen(argv, **kwargs)`, registered — the ONE way a test starts a process.

    Deliberately a thin pass-through with no defaults of its own: a helper that also decided pipes,
    text mode or cwd would be a second thing to reason about at every call site, and the tests that
    read a child's stdout line by line are particular about all three.
    """
    proc = subprocess.Popen(argv, **kwargs)
    _SPAWNED.append(proc)
    return proc


def forget() -> None:
    """Start a new tally. Called before and after each test."""
    _SPAWNED.clear()


def strays() -> list[subprocess.Popen]:
    """The children of the current test that are still running.

    `poll()` rather than `wait()`: this is asked after every test in the suite, including the
    thousands that spawn nothing, so it must never block on anything.
    """
    return [p for p in _SPAWNED if p.poll() is None]


def reap(procs) -> list[str]:
    """Kill and collect `procs`, returning one `pid N (argv…)` line each for the failure message.

    Reaping BEFORE asserting is what keeps a leak from cascading: a stray reported and left
    running would fail its own test and then go on causing the unattributable failures elsewhere
    that this module exists to prevent — the report would be accurate and the damage would
    continue anyway.
    """
    described = []
    for proc in procs:
        described.append(f"pid {proc.pid} ({' '.join(str(a) for a in proc.args)})")
        proc.kill()
        proc.communicate()
    return described
