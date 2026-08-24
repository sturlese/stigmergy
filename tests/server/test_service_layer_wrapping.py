"""`BrainService._call` / `.call_async` — the service-layer wrapper both transports share:
rate-limit check, then run the real call, then an audit write in a `finally` (so it
fires on success AND on failure). Pure unit tests against fake `rate_limiter`/`audit` doubles —
`_call`/`call_async` never touch `self.conn`, so no Postgres is needed here; both seams are
duck-typed and kept injectable for exactly this reason."""
import asyncio
import logging

import pytest

from stigmergy.server.errors import RateLimitError
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings


class FakeAudit:
    """Records every `.write(...)` call verbatim — the test's window into what `_call`/
    `call_async` attribute to the audit trail."""

    def __init__(self):
        self.rows: list[dict] = []

    def write(self, **kwargs) -> None:
        self.rows.append(kwargs)


class RaisingAudit:
    """An audit writer whose `.write` itself fails — the production `AuditWriter.write` swallows
    its own errors; THIS double intentionally does not, so a test can prove exactly where that
    guarantee has to live (see the test below)."""

    def write(self, **kwargs) -> None:
        raise RuntimeError("simulated audit backend outage")


class FakeRateLimiter:
    def __init__(self, refuse_after: int | None = None):
        self.calls: list[tuple[str, str]] = []
        self._refuse_after = refuse_after

    def check(self, identity: str, tool: str) -> None:
        self.calls.append((identity, tool))
        if self._refuse_after is not None and len(self.calls) > self._refuse_after:
            raise RateLimitError("rate limited: 1 requests/min exceeded — wait a moment and retry")


def make(rate_limiter=None, audit=None, identity="ana@example.com") -> BrainService:
    settings = Settings(identity=identity, identities_path="x")
    return BrainService(settings, conn=None, embedder=None, audiences=None, identity=identity,
                        rate_limiter=rate_limiter, audit=audit)


# ── sync wrapper (_call) ─────────────────────────────────────────────────────────────────────
def test_call_returns_the_wrapped_functions_result():
    svc = make()
    assert svc._call("search_brain", {"query": "q"}, lambda: {"hits": []}) == {"hits": []}


def test_call_writes_one_audit_row_on_success_with_full_args_and_ok_outcome():
    audit = FakeAudit()
    svc = make(audit=audit)
    svc._call("search_brain", {"query": "q", "max_results": 5}, lambda: {"hits": []})

    assert len(audit.rows) == 1
    row = audit.rows[0]
    assert row["identity"] == "ana@example.com"
    assert row["tool"] == "search_brain"
    assert row["args"] == {"query": "q", "max_results": 5}   # FULL args, not a summary
    assert row["outcome"] == "ok"
    assert row["error_class"] == ""
    assert row["duration_ms"] >= 0


def test_call_writes_an_audit_row_on_exception_and_still_reraises():
    """An audit row is written even when the tool returns an error payload. At the service layer
    that means: the exception still propagates (the MCP adapter turns it into the {"error": ...}
    JSON payload one layer up), but the audit row for THIS call was already written, with
    outcome=error and the real exception class name."""
    audit = FakeAudit()
    svc = make(audit=audit)

    def boom():
        raise ValueError("unknown filter: body")

    with pytest.raises(ValueError, match="unknown filter"):
        svc._call("search_brain", {"query": "q"}, boom)

    assert len(audit.rows) == 1
    row = audit.rows[0]
    assert row["outcome"] == "error"
    assert row["error_class"] == "ValueError"


def test_call_checks_the_rate_limiter_before_running_the_function():
    limiter = FakeRateLimiter(refuse_after=0)
    ran = []
    svc = make(rate_limiter=limiter)

    with pytest.raises(RateLimitError):
        svc._call("search_brain", {}, lambda: ran.append(1))

    assert ran == []                      # the wrapped function never ran
    assert limiter.calls == [("ana@example.com", "search_brain")]


def test_call_writes_an_audit_row_even_on_rate_limit_refusal():
    """The refusal itself is a call outcome worth auditing: EVERY tool call gets a row, and "a row
    even when the call errors" covers a rate-limit refusal exactly as it does any other error."""
    audit = FakeAudit()
    limiter = FakeRateLimiter(refuse_after=0)
    svc = make(rate_limiter=limiter, audit=audit)

    with pytest.raises(RateLimitError):
        svc._call("ask", {"question": "q"}, lambda: None)

    assert len(audit.rows) == 1
    assert audit.rows[0]["outcome"] == "error"
    assert audit.rows[0]["error_class"] == "RateLimitError"


def test_call_with_no_rate_limiter_or_audit_is_a_pure_noop_wrapper():
    """A caller that wires neither seam (`tests/server/conftest.py::make_service` without those
    kwargs) must keep working: both default to None, so `_call` becomes a bare passthrough."""
    svc = make(rate_limiter=None, audit=None)
    assert svc._call("search_brain", {"query": "q"}, lambda: 42) == 42


def test_audit_write_failure_is_not_this_layers_job_to_swallow():
    """Documents the actual seam: `_call`'s `finally` block calls
    `self.audit.write(...)` UNGUARDED — the "never fails the serving call" guarantee is the
    production `AuditWriter.write`'s OWN responsibility (it catches and logs internally, see
    `tests/server/test_audit.py`), not something `_call` re-implements. A double that does not
    honor that contract (like this one) correctly breaks the call — proving the guarantee is not
    accidentally provided twice, and pinning down precisely which module owns it."""
    svc = make(audit=RaisingAudit())
    with pytest.raises(RuntimeError, match="simulated audit backend outage"):
        svc._call("search_brain", {"query": "q"}, lambda: {"hits": []})


# ── async wrapper (call_async) — the SAME contract, for `ask` ──────────────────────────────────
def test_call_async_returns_the_coroutines_result():
    svc = make()

    async def go():
        return await svc.call_async("ask", {"question": "q"}, lambda: _coro({"answer": "x"}))

    assert asyncio.run(go()) == {"answer": "x"}


async def _coro(value):
    return value


def test_call_async_writes_full_args_and_ok_outcome_on_success():
    audit = FakeAudit()
    svc = make(audit=audit)

    async def go():
        await svc.call_async("ask", {"question": "what is arr?"}, lambda: _coro({"answer": "x"}))

    asyncio.run(go())

    assert len(audit.rows) == 1
    row = audit.rows[0]
    assert row["tool"] == "ask"
    assert row["args"] == {"question": "what is arr?"}
    assert row["outcome"] == "ok"


def test_call_async_writes_an_audit_row_on_exception_and_reraises():
    audit = FakeAudit()
    svc = make(audit=audit)

    async def boom():
        raise RuntimeError("synthesizer blew up")

    async def go():
        await svc.call_async("ask", {"question": "q"}, boom)

    with pytest.raises(RuntimeError, match="synthesizer blew up"):
        asyncio.run(go())

    assert audit.rows[0]["outcome"] == "error"
    assert audit.rows[0]["error_class"] == "RuntimeError"


def test_call_async_external_cancellation_is_audited_as_an_error_and_reraised():
    audit = FakeAudit()
    svc = make(audit=audit)
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()

    async def go():
        task = asyncio.create_task(svc.call_async("ask", {"question": "q"}, blocked))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())

    assert audit.rows[0]["outcome"] == "error"
    assert audit.rows[0]["error_class"] == "CancelledError"


def test_call_async_checks_the_rate_limiter_before_awaiting():
    limiter = FakeRateLimiter(refuse_after=0)
    ran = []
    svc = make(rate_limiter=limiter)

    async def go():
        await svc.call_async("ask", {"question": "q"}, lambda: _coro(ran.append(1)))

    with pytest.raises(RateLimitError):
        asyncio.run(go())
    assert ran == []
    assert limiter.calls == [("ana@example.com", "ask")]


# ── `_audit_args`'s fail-safe — audit SHAPING must never surface through `_call`'s `finally`
# and clobber the caller's real result or real exception ───────────────────────────────────────
class BoomDict(dict):
    """A dict whose `.items()` raises — the depth guard in `_truncate_for_audit` cannot itself
    raise (it is a plain comparison), so this is an UNKNOWN way to make shaping fail, exactly the
    kind `_audit_args`'s broad `except Exception` exists for rather than the depth guard alone."""

    def items(self):
        raise RuntimeError("audit shaping blew up")


def test_call_still_returns_the_real_result_when_audit_arg_shaping_raises(caplog):
    audit = FakeAudit()
    svc = make(audit=audit)

    with caplog.at_level(logging.ERROR):
        result = svc._call("search_brain", {"query": BoomDict(a=1)}, lambda: {"hits": []})

    assert result == {"hits": []}                              # the real result survives, untouched
    assert len(audit.rows) == 1                                 # a row is still written
    assert audit.rows[0]["args"] == {"args_unavailable": "audit shaping failed"}
    assert any("audit arg shaping failed" in r.message for r in caplog.records)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_call_reraises_the_real_exception_even_when_audit_arg_shaping_also_fails(caplog):
    """The stronger case: the WRAPPED CALL fails for a real reason, and audit shaping fails too —
    the exception that reaches the caller must be the real one, never masked by the shaping
    failure (mirrors `tests/capture/test_ops_pg.py`'s
    `test_job_run_bookkeeping_failure_never_masks_the_original_exception` at the service layer)."""
    audit = FakeAudit()
    svc = make(audit=audit)

    def boom():
        raise ValueError("the real error the caller asked about")

    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match="the real error"):
        svc._call("search_brain", {"query": BoomDict(a=1)}, boom)

    assert audit.rows[0]["outcome"] == "error"
    assert audit.rows[0]["error_class"] == "ValueError"
    assert audit.rows[0]["args"] == {"args_unavailable": "audit shaping failed"}
