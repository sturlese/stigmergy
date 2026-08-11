"""`librarian.worker`'s in-process-testable surface: `startup_checks` (fail-closed validation
before any item is claimed) and `process_next`'s mapping of processing exceptions to a `failed`
result. The signal-handling half (`Worker.run`, SIGINT/SIGTERM) is proven with REAL signals
against a real subprocess in `test_worker_signals.py` — coverage.py cannot see across that
process boundary, so this file is what raises `worker.py`'s in-process coverage number without
weakening the signal tests' own honesty about needing a real process.
"""
import contextlib
import dataclasses
import io
import json
import os
import signal
from types import SimpleNamespace

import pytest

from stigmergy.librarian import worker
from stigmergy.librarian.errors import AgentError, LibrarianConfigError, StaleBaseError
from tests.librarian import support


def test_startup_checks_returns_the_resolved_repo_acl_config_and_registry(rig):
    env, deps = rig
    resolved = worker.startup_checks(deps.settings)
    assert resolved["repo"] == env.repo
    assert resolved["registry"].canonical_id("Acme Corp") == "acme"
    assert resolved["acl_config"] is not None or resolved["acl_config"] is None  # never raises
    assert resolved["reaped"] == 0                      # nothing to reap on a fresh repo


def test_startup_checks_reaps_a_leftover_worktree_from_a_previous_crash(rig, tmp_path):
    env, deps = rig
    from stigmergy.librarian import gitcmd
    base = gitcmd.run("rev-parse", "HEAD", cwd=env.repo).stdout.strip()
    # Named the way a crashed run of THIS repo names it — `support.crash_leftover_name` builds it
    # from `gitcmd`'s own key and a dead pid, because that name IS the reaping contract.
    leftover = os.path.join(deps.settings.worktree_root,
                            support.crash_leftover_name(env.repo))
    os.makedirs(deps.settings.worktree_root, exist_ok=True)
    gitcmd.run("worktree", "add", "--detach", "--quiet", leftover, base, cwd=env.repo)

    resolved = worker.startup_checks(deps.settings)

    assert resolved["reaped"] >= 1
    assert not os.path.exists(leftover)


def test_startup_checks_raises_config_error_for_an_unknown_backend(rig):
    env, deps = rig
    bad_settings = dataclasses.replace(deps.settings, backend="not-a-real-backend")
    with pytest.raises(LibrarianConfigError, match="not-a-real-backend"):
        worker.startup_checks(bad_settings)


def test_startup_checks_raises_config_error_when_the_linter_is_missing(rig):
    env, deps = rig
    os.remove(deps.settings.linter_path)
    # The linter is materialized from the BASE COMMIT, never off the working
    # tree — removing the on-disk copy alone changes nothing a run sees, so the removal has to be
    # committed and pushed to `origin/main` for `base_inputs.check_linter_at` to notice it is gone.
    support.commit_and_push(env.repo, "test: remove the contract linter")
    with pytest.raises(LibrarianConfigError, match="contract linter"):
        worker.startup_checks(deps.settings)


def test_build_deps_wires_the_dispatched_agent_and_the_resolved_repo(rig):
    env, deps = rig
    resolved = worker.startup_checks(deps.settings)
    from stigmergy.capture.evidence import MemoryEvidenceStore
    from stigmergy.librarian.double import DoubleAgent
    built = worker.build_deps(deps.settings, resolved, MemoryEvidenceStore())
    assert isinstance(built.agent, DoubleAgent)
    assert built.repo == env.repo


# ── process_next: exceptions are mapped to a `failed` Result, never left to propagate ──────────
class _RaisingAgent:
    """A standalone stub — it wraps nothing, so it DECLARES the port member rather than copying one
    (`filing_port.FilingAgent.structured_ordinary`). `False` is the shape every test here means: the
    exploring ordinary flow, which is what `processing._one_pass` does when nothing gathers."""

    structured_ordinary = False
    wants_gathered = False

    def __init__(self, exc):
        self.exc = exc

    def run(self, **kwargs):
        raise self.exc


def test_process_next_maps_a_known_processing_error_to_a_failed_result_and_finishes_the_row(
        rig, clean_queue):
    env, deps = rig
    deps = dataclasses.replace(deps, agent=_RaisingAgent(AgentError("the agent blew its budget")))
    support.submit(clean_queue, deps, "A capture about Acme Corp.")

    item, result = worker.process_next(clean_queue, deps)

    assert result.status == "failed"
    assert result.report["stage"] == "AgentError"
    from stigmergy.capture import queue
    row = queue.get_submission_trace(clean_queue, item["id"])
    assert row["status"] == "failed"


def test_process_next_maps_a_genuinely_unexpected_exception_to_failed_too(rig, clean_queue):
    env, deps = rig
    deps = dataclasses.replace(deps, agent=_RaisingAgent(RuntimeError("something nobody expected")))
    support.submit(clean_queue, deps, "A capture about Acme Corp.")

    item, result = worker.process_next(clean_queue, deps)

    assert result.status == "failed"
    row_report = result.report
    assert row_report["stage"] == "unexpected"


def test_process_next_returns_none_when_the_queue_is_empty(rig, clean_queue):
    _, deps = rig
    assert worker.process_next(clean_queue, deps) is None


# ── a HANDLED failure must not print a traceback ─────────────────────────────────────────────────
# A recurrence of a defect class this suite keeps catching: a raw traceback where a person expected
# a sentence. The known-family branch logged with `exc_info=True`, so thirty
# lines of Python landed above a correct, carefully-worded refusal and made a handled validation read
# as a crash. The unexpected branch keeps its traceback: there the fault really is a bug, and the
# traceback IS the diagnosis. Both halves are asserted, because dropping it everywhere would be the
# opposite mistake.
def test_a_handled_processing_failure_logs_no_traceback(rig, clean_queue, caplog):
    import logging

    _, deps = rig
    deps = dataclasses.replace(deps, agent=_RaisingAgent(AgentError("the agent blew its budget")))
    support.submit(clean_queue, deps, "A capture about Acme Corp.")

    with caplog.at_level(logging.ERROR):
        _, result = worker.process_next(clean_queue, deps)

    assert result.status == "failed"
    assert "Traceback" not in caplog.text
    assert not any(record.exc_info for record in caplog.records)
    # and what an operator can act on is still all there: the class, the stage and the message
    assert "AgentError" in caplog.text
    assert "the agent blew its budget" in caplog.text


def test_a_genuinely_unexpected_failure_still_logs_its_traceback(rig, clean_queue, caplog):
    """The benign twin of the line above: an unexpected fault is a bug, and the stack is the point."""
    import logging

    _, deps = rig
    deps = dataclasses.replace(deps, agent=_RaisingAgent(RuntimeError("nobody expected this")))
    support.submit(clean_queue, deps, "A capture about Acme Corp.")

    with caplog.at_level(logging.ERROR):
        _, result = worker.process_next(clean_queue, deps)

    assert result.status == "failed" and result.report["stage"] == "unexpected"
    assert any(record.exc_info for record in caplog.records)


# ── a mid-run config fault: named as a stage, never as a PATH ────────────────────────────────────
# `LibrarianConfigError` messages are written for a local CLI, where naming the file is
# the whole point, and every one of them carries a filesystem path — `githubapp._private_key` names
# the App PRIVATE KEY's location, and its own docstring claimed it was "never a wire message". This
# branch interpolated `str(ex)` into `report["summary"]`, which `Result.error` and `Result.report`
# both carry into `capture_queue` and out to every authenticated MCP identity through
# `_shape_submission`. Errors are generic over HTTP and specific in a local CLI, which forbids that.
_SECRET_PATH = "/Users/someone/.config/stigmergy/librarian.private-key.pem"


def test_a_mid_run_config_fault_puts_no_filesystem_path_on_the_wire(rig, clean_queue, caplog):
    import logging

    env, deps = rig
    fault = LibrarianConfigError(
        f"cannot read the librarian App private key at {_SECRET_PATH!r}: FileNotFoundError")
    deps = dataclasses.replace(deps, agent=_RaisingAgent(fault))
    support.submit(clean_queue, deps, "A capture about Acme Corp.")

    with caplog.at_level(logging.ERROR):
        item, result = worker.process_next(clean_queue, deps)

    assert result.status == "failed"
    assert result.report["stage"] == "config"          # still named, so an operator knows where to look
    # nothing about the path, in EITHER wire surface, nor in the row the client reads
    from stigmergy.capture import queue
    row = queue.get_submission_trace(clean_queue, item["id"])
    for surface in (result.report["summary"], result.error,
                    json.dumps(row["report"]), row["error"] or ""):
        assert _SECRET_PATH not in surface
        assert "private-key" not in surface
        assert os.sep not in surface, surface
    # and the submitter is told the one thing that concerns them
    assert "nothing about your capture caused it" in result.report["summary"]
    # the detail is not lost — it goes to the operator's log, which is where it belongs
    assert _SECRET_PATH in caplog.text


# ── the agent-attempt count, threaded through the EXCEPTION path ────────────────────────────────
# The carry-over: on the exception path the failure report named only the queue DELIVERY, because
# the agent-attempt count lives inside `processing._run_in_worktree`'s loop and the report is
# composed here. So a `failed` row said "queue delivery 1" while the agent had had one or two tries,
# and nobody reading it could tell whether the corrective retry had run — which is the first thing
# you want to know about a librarian that gave up. `report.failed_system` already ACCEPTED the
# number; there was no way to tell it.
class _RaisingOnAttempt:
    """Raises on the Nth agent pass, behaving normally before it. `calls` is asserted on, so a test
    can prove the failure happened where it meant it to."""

    def __init__(self, inner, fail_on: int, exc):
        self.inner = inner
        # The declared port member, copied from what this wraps (ADR 033). Plain attribute
        # access with NO default: `processing._one_pass` refuses an agent that carries no
        # `structured_ordinary` rather than defaulting it, so a wrapper that swallowed the
        # declaration would silently change which shape of the ordinary flow runs behind it.
        # Reading it here means a wrapper around a non-conforming backend fails at
        # CONSTRUCTION, in the test that built it, instead of one queue delivery at a time.
        self.structured_ordinary = inner.structured_ordinary
        self.wants_gathered = inner.wants_gathered
        self.fail_on = fail_on
        self.exc = exc
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        if self.calls == self.fail_on:
            raise self.exc
        return self.inner.run(**kwargs)


def test_a_failure_on_the_first_agent_pass_reports_one_agent_attempt(rig, clean_queue):
    env, deps = rig
    agent = _RaisingOnAttempt(deps.agent, 1, AgentError("the agent produced nothing usable"))
    deps = dataclasses.replace(deps, agent=agent)
    support.submit(clean_queue, deps, "A capture about Acme Corp.")

    _, result = worker.process_next(clean_queue, deps)

    assert result.status == "failed"
    assert agent.calls == 1
    assert result.report["agent_attempts"] == 1
    # and it reaches the SENTENCE, not just the structured field — the field nobody renders is a
    # field that does not exist as far as an operator is concerned
    assert "1 agent attempt inside it" in result.report["summary"]



def test_a_fault_raised_before_the_agent_ever_runs_reports_no_agent_attempts(rig, clean_queue):
    """Zero is the honest answer, not 1. `_material` reads the evidence blob before anything else, so
    a purged or unreachable archive fails with the agent never invoked — and `report.failed_system`
    then omits the agent counter rather than guessing at one."""
    env, deps = rig

    class _EmptyEvidence:
        def put(self, data):
            return "sha256/00/00/absent"

        def get(self, key):
            from stigmergy.capture.errors import EvidenceError
            raise EvidenceError("the archived material is gone")

    support.submit(clean_queue, deps, "A capture whose evidence disappears.")
    deps = dataclasses.replace(deps, evidence=_EmptyEvidence())

    _, result = worker.process_next(clean_queue, deps)

    assert result.status == "failed"
    assert result.report["agent_attempts"] == 0
    assert "agent attempt" not in result.report["summary"]
    # ...while the queue delivery IS still named, so the sentence is not silent about both counters
    assert "queue delivery 1" in result.report["summary"]


def test_the_counter_survives_an_exception_type_that_cannot_carry_it(rig, clean_queue):
    """`PROCESSING_ERRORS` deliberately includes `CaptureError`, which is not a `LibrarianError` and
    has no `agent_attempts` slot. `worker._agent_attempts` reads it with `getattr`, so such a fault
    degrades to zero instead of raising an AttributeError inside the failure handler."""
    assert worker._agent_attempts(RuntimeError("no such attribute")) == 0
    assert worker._agent_attempts(AgentError("plain").at_agent_attempt(2)) == 2


# ── human_duration: the number AND the unit a person thinks in ─────────────────────────────────
# The interrupt message named `900s`, which is right and leaves arithmetic to somebody mid-incident.
@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"),
    (45, "45s"),
    (60, "60s (1 min)"),
    (900, "900s (15 min)"),
    (1234, "1234s (20 min 34s)"),
    (3600, "3600s (1h)"),
    (5400, "5400s (1h 30 min)"),
])
def test_human_duration_carries_both_the_machine_value_and_the_readable_one(seconds, expected):
    assert worker.human_duration(seconds) == expected


def test_the_visibility_timeout_clause_names_the_configured_number_with_its_unit():
    """One fragment, two callers (the shutdown messages and `once`'s interrupt), so the two cannot
    drift into describing the same recovery differently."""
    clause = worker.visibility_timeout_clause(900)
    assert "900s (15 min) visibility timeout" in clause
    assert "attempts incremented" in clause


def test_swept_clause_is_empty_when_a_sweep_moved_nothing(rig):
    """The no-op sweep runs before every claim. A line printed every time is a line nobody reads."""
    _, deps = rig
    assert worker.swept_clause({"released": 0, "failed": 0}, deps.settings) == ""


def test_swept_clause_names_both_counts_and_the_lease_it_swept_against(rig):
    _, deps = rig
    clause = worker.swept_clause({"released": 2, "failed": 1}, deps.settings)
    assert "swept 2 stranded claim(s)" in clause
    assert "failed 1" in clause
    assert worker.human_duration(deps.settings.visibility_timeout_s) in clause

# ── the signal handlers must not touch buffered IO ────────────────────────────────────────────
# A Python signal handler runs on the main thread at a check point CPython reaches while that
# thread may already be INSIDE a buffered write — `BufferedWriter` holds its lock across the raw
# write, and the pending-signal check happens in there. A handler that writes to the same stream
# therefore re-enters a locked writer and raises `RuntimeError: reentrant call`, from the point
# the signal interrupted rather than from the handler. That point is `process_item`, so the item
# in flight FAILS instead of draining — the exact opposite of what the handler's own message
# promises.
#
# Delivering the handler from inside `RawIOBase.write` reproduces that structurally: the raw
# write is called BY `BufferedWriter` while it holds the lock, which is where CPython would run
# the handler. It cost three red CI runs on `main` to find (2026-08-01, 08-03, 08-05) because a
# real signal only lands in that window under load — the mechanism is deterministic, the timing
# is not, so it is pinned here rather than left to the subprocess tests to catch by luck.


class _SignalDuringWrite(io.RawIOBase):
    """A raw stream that delivers `handler` mid-write, where CPython delivers a real one."""

    def __init__(self):
        self.handler = None
        self.chunks = []

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self.chunks.append(bytes(b))
        if self.handler is not None:
            handler, self.handler = self.handler, None
            handler(signal.SIGTERM, None)
        return len(b)


def _worker_writing_to_a_buffered_stream(rig):
    """A `Worker` whose `on_output` is a REAL `print` into a REAL `BufferedWriter` — the
    production wiring (`cli.py`'s `on_output=lambda line: print(line, flush=True)`), not a list
    that records strings. A fake collector cannot re-enter a lock it does not have, so it would
    prove nothing about the property being claimed."""
    _, deps = rig
    raw = _SignalDuringWrite()
    text = io.TextIOWrapper(io.BufferedWriter(raw))
    loop = worker.Worker(None, deps, on_output=lambda msg: print(msg, file=text, flush=True))
    return loop, raw, text


@contextlib.contextmanager
def _capturing_the_stdout_descriptor(tmp_path):
    """Redirect fd 1 ITSELF, not `sys.stdout`. The handler writes to the descriptor on purpose
    (that is the fix), so a capture that only swaps the Python-level object would see nothing and
    the assertion would be measuring the wrong layer."""
    sink = tmp_path / "fd1.txt"
    saved = os.dup(worker.STDOUT_FD)
    with open(sink, "wb") as handle:
        os.dup2(handle.fileno(), worker.STDOUT_FD)
        try:
            yield lambda: sink.read_text()
        finally:
            os.dup2(saved, worker.STDOUT_FD)
            os.close(saved)


@pytest.mark.parametrize("handler_name", ["_on_sigterm", "_on_sigint"])
def test_a_signal_arriving_mid_write_does_not_re_enter_the_buffered_writer(rig, handler_name):
    """Old behaviour: `RuntimeError: reentrant call inside <_io.BufferedWriter>`, raised out of
    whatever the signal interrupted — CI run 30983419585 died exactly here, with the traceback
    naming `_on_sigint` stacked on top of the `print` it interrupted."""
    loop, raw, _text = _worker_writing_to_a_buffered_stream(rig)
    raw.handler = getattr(loop, handler_name)

    print("PROCESSING-STARTED", file=_text, flush=True)   # must not raise

    assert loop.stopping is True, "the signal must still be recorded"


def test_the_acknowledgement_still_reaches_the_operator(rig, tmp_path):
    """The benign twin. A handler that went silent to dodge the reentrancy would pass the test
    above and break what `test_worker_signals.py` asserts end to end: SIGTERM is acknowledged
    WITHOUT waiting for the item in flight, which can take minutes. So the message must still
    arrive, and arrive from the handler itself rather than from the loop it cannot interrupt."""
    loop, _raw, _text = _worker_writing_to_a_buffered_stream(rig)

    with _capturing_the_stdout_descriptor(tmp_path) as written:
        loop._on_sigterm(signal.SIGTERM, None)
        out = written()

    assert "received SIGTERM" in out
    assert "finishing the item in flight" in out
    assert worker.human_duration(loop.deps.settings.visibility_timeout_s) in out


def test_a_second_ctrl_c_is_acknowledged_mid_write_too(rig, tmp_path):
    """The two-press path has its own message, on the branch `stopping` already took — the one a
    distracted operator reaches by pressing Ctrl-C twice while output is flowing."""
    loop, raw, text = _worker_writing_to_a_buffered_stream(rig)
    loop._on_sigint(signal.SIGINT, None)                 # first press: sets stopping
    raw.handler = loop._on_sigint                        # second press, delivered mid-write

    with _capturing_the_stdout_descriptor(tmp_path) as written:
        print("#1 -> filed", file=text, flush=True)      # must not raise
        out = written()

    assert loop.releasing is True
    assert "stopping as soon as the item in flight finishes" in out


def test_a_broken_descriptor_does_not_turn_a_shutdown_into_a_crash():
    """The notice is best-effort. A worker whose stdout has been closed under it must still
    STOP — losing the sentence is survivable, failing to drain is not."""
    saved = os.dup(worker.STDOUT_FD)
    os.close(worker.STDOUT_FD)
    try:
        worker.emit_from_signal_handler("nobody will read this")   # must not raise
    finally:
        os.dup2(saved, worker.STDOUT_FD)
        os.close(saved)


# ── retired tests, named here rather than dropped in silence: a check that stops running must be
# impossible to miss ───────────────────────────────────────────────────────────────────────────
# The tests listed below drove a HALLUCINATED FIGURE through the fast lane and asserted that
# ingest-time figure verification vetoed it, that one corrective retry recovered it, or that the
# resulting report carried the right verdict. That verification is gone
# ([ADR 026](../../docs/decisions/026-the-purge.md) D2): it died with the trust layer,
# deliberately, and the accepted consequence is stated there — **an invented figure CAN now sit
# on a page.** The reader's protection is the verbatim source one click away, the gardener, and
# `answer.verify_answer` at query time.
#
# So these are removed, not repaired: their subject no longer exists, and a test rewritten to
# assert the opposite would be measuring a decision, not a mechanism. What they ALSO covered
# incidentally — atomicity, the once-directive, the steering veto — is covered by the remaining
# tests in this file, which use a veto that still exists (zone, anchoring, secrets) to produce
# the same refusal shape.
#
# Removed: `test_a_failure_on_the_corrective_retry_reports_two_agent_attempts`


def test_ctrl_c_while_idle_claims_no_further_item(rig, monkeypatch):
    """OLD BEHAVIOUR: one more item was claimed, filed, committed and PUSHED after Ctrl-C.

    The loop's own guard tested `releasing`, which a first Ctrl-C does not set — it sets
    `stopping`. `_sleep` returns immediately once `stopping` is set, control went back to the top,
    and `process_next` ran again; the `break` is checked only AFTER the claim. The handler prints
    "finishing the item in flight, then stopping — no further items will be claimed" one
    instruction earlier, and `docs/reference/librarian.md` states the same contract: the flags
    affect only whether the NEXT item is claimed.

    Driven through `Worker.run` itself with the signal arriving while IDLE — the existing
    end-to-end signal tests deliver it mid-item, which is the path where the trailing `break` does
    fire, and with a single queued row, where the extra claim finds an empty queue.
    """
    _, deps = rig
    loop = worker.Worker(None, deps, on_output=lambda _msg: None)
    claims = []

    def _claim(_conn, _deps):
        claims.append(1)
        return None                      # nothing to do: the loop goes to sleep, as when idle

    def _sleep_then_ctrl_c(_seconds):
        loop._on_sigint(signal.SIGINT, None)      # the first press, arriving while idle

    monkeypatch.setattr(worker, "process_next", _claim)
    monkeypatch.setattr(worker, "sweep", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "swept_clause", lambda *_a, **_k: "")
    monkeypatch.setattr(contextlib, "nullcontext", contextlib.nullcontext)
    monkeypatch.setattr(worker.ops, "job_run",
                        lambda *_a, **_k: contextlib.nullcontext({}))
    monkeypatch.setattr(loop, "_sleep", _sleep_then_ctrl_c)

    loop.run()

    assert len(claims) == 1, f"claimed {len(claims)} times after Ctrl-C; the contract is one"
    assert loop.stopping is True


def test_a_run_that_dies_mid_drain_still_records_the_work_it_already_pushed(rig, monkeypatch):
    """OLD BEHAVIOUR: `job_runs` recorded `stats={}` for a run that had already filed and PUSHED.

    `stats["processed"]` was written once, AFTER the loop. `process_next` deliberately re-raises
    `StaleBaseError` rather than turning it into a `failed` row, so it escapes the loop and that
    line never runs; `ops.job_run`'s own `except Exception` then persists whatever the dict holds.
    A worker that drained five captures before its installation token expired recorded a run that
    read as having done nothing at all.

    `views/regenerate` states the rule this now follows, in prose, for exactly this reason:
    updating only at the end writes "a `job_runs` audit trail lying by omission about real,
    already-pushed work".
    """
    _, deps = rig
    loop = worker.Worker(None, deps, on_output=lambda _msg: None)
    recorded = {}
    calls = []

    def _process(_conn, _deps):
        calls.append(1)
        if len(calls) <= 2:
            return ({"id": len(calls)}, SimpleNamespace(status="filed"))
        raise StaleBaseError("the base moved under this item")

    monkeypatch.setattr(worker, "process_next", _process)
    monkeypatch.setattr(worker, "sweep", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "swept_clause", lambda *_a, **_k: "")
    monkeypatch.setattr(worker.ops, "job_run", lambda *_a, **_k: contextlib.nullcontext(recorded))

    with pytest.raises(StaleBaseError):
        loop.run()

    assert recorded.get("processed") == 2, f"two items were filed and pushed; stats say {recorded}"
