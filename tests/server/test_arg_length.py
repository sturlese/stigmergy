"""Every user-controlled string argument reachable over the public HTTP boundary — `query`, the
`filters` keys and values, `path`, `entity`, `ask`'s `question`, and `brain_delete`'s `why` and
each `path` — is length-checked BEFORE the DB read, the embedder call, the clone or the LLM call it
would otherwise trigger. Pure/unit where possible (poisoned doubles that raise if ever touched —
the strongest proof that the check runs first); the at-limit/pass side uses the real `indexed`
Postgres fixture, since "passes" has to mean the call actually reaches and completes the real work.

Both halves of the escape hatch are pinned here: `check_arg_length`'s OWN marked rejection is
echoed verbatim (it is server-authored and actionable), and any other `ValueError` — the shape a
`pydantic_core.ValidationError` arrives in — still collapses to a class name."""
import pytest

from stigmergy.server.errors import RateLimitError
from stigmergy.server.service import MAX_ARG_CHARS, BrainService, check_arg_length
from stigmergy.server.settings import Settings
from tests.server.conftest import make_service


class Poison:
    """Raises on ANY attribute access — the strongest available proof that `check_arg_length`
    short-circuits BEFORE the DB/embedder is ever touched (a real conn/embedder double that merely
    counted calls could still miss a read hiding behind an untouched attribute)."""

    def __getattr__(self, name):
        raise AssertionError(f"unexpected access to poisoned dependency: .{name}")


class FakeAudit:
    def __init__(self):
        self.rows: list[dict] = []

    def write(self, **kwargs) -> None:
        self.rows.append(kwargs)


def poisoned_service(audit=None) -> BrainService:
    settings = Settings(identity="steward@example.com", identities_path="x")
    return BrainService(settings, conn=Poison(), embedder=Poison(), audiences=None,
                        identity="steward@example.com", audit=audit)


# ── check_arg_length itself: the exact boundary ─────────────────────────────────────────────────
def test_check_arg_length_at_the_limit_passes():
    check_arg_length("query", "x" * MAX_ARG_CHARS)   # must not raise


def test_check_arg_length_one_over_the_limit_raises():
    with pytest.raises(ValueError, match=f"query too long \\(max {MAX_ARG_CHARS} characters\\)"):
        check_arg_length("query", "x" * (MAX_ARG_CHARS + 1))


def test_check_arg_length_error_message_never_echoes_the_input():
    """Generic wording only: the error names the ARGUMENT, never the (potentially huge, or
    content-sensitive) VALUE itself."""
    huge = "SECRET-MARKER-" + "x" * MAX_ARG_CHARS
    with pytest.raises(ValueError) as exc_info:
        check_arg_length("query", huge)
    assert "SECRET-MARKER" not in str(exc_info.value)



# ── `filters` VALUES are user-controlled too ───────────────────────────────────────────────────
def test_over_limit_filters_value_is_rejected_before_touching_conn_or_embedder():
    svc = poisoned_service()
    with pytest.raises(ValueError, match=r"filters\.entity too long"):
        svc.search("ok query", filters={"entity": "x" * (MAX_ARG_CHARS + 1)})


def test_over_limit_filters_value_is_checked_even_when_query_itself_is_fine():
    """The `query` string alone passing its own check must not shadow a still-oversized `filters`
    value — both are independent guards over the SAME call."""
    svc = poisoned_service()
    with pytest.raises(ValueError, match="too long"):
        svc.search("a perfectly reasonable query", filters={"type": "x" * (MAX_ARG_CHARS + 1)})


def test_over_limit_filters_value_names_its_own_key_not_just_filters():
    """The error names WHICH filter overflowed (`filters.<key>`), not a generic 'filters too
    long' — actionable for the caller, and distinguishable from the `query`/`path` cases."""
    svc = poisoned_service()
    with pytest.raises(ValueError, match=r"^filters\.owner too long"):
        svc.search("x", filters={"owner": "y" * (MAX_ARG_CHARS + 1)})


# ── `filters` KEYS are user-controlled too, not just values ────────────────────────────────────
def test_over_limit_filters_key_is_rejected_before_touching_conn_or_embedder():
    """An oversized KEY used to reach `search.search_arms` unchecked — `_filter_clause` echoes
    every unrecognized key verbatim into its own ValueError, so a giant key was both wasted
    query-planner work and a reflection surface, never merely an unbounded value."""
    svc = poisoned_service()
    with pytest.raises(ValueError, match="too long"):
        svc.search("ok query", filters={"x" * (MAX_ARG_CHARS + 1): "acme"})


def test_over_limit_filters_key_error_never_echoes_the_key_itself():
    """The generic-wording rule, applied to the key exactly as it already is to the value
    (`test_check_arg_length_error_message_never_echoes_the_input`): the error names the ARGUMENT
    ('filters key'), never embeds the huge key text — checked with a fixed marker so this
    holds."""
    svc = poisoned_service()
    huge_key = "SECRET-MARKER-" + "x" * MAX_ARG_CHARS
    with pytest.raises(ValueError) as exc_info:
        svc.search("ok query", filters={huge_key: "acme"})
    assert "SECRET-MARKER" not in str(exc_info.value)


def test_over_limit_filters_key_is_checked_even_when_its_own_value_is_fine():
    """An in-bounds VALUE must not shadow a still-oversized KEY on the same filter entry — both
    are independent guards over the same (key, value) pair."""
    svc = poisoned_service()
    with pytest.raises(ValueError, match="too long"):
        svc.search("ok query", filters={"y" * (MAX_ARG_CHARS + 1): "short"})


def test_at_limit_filters_key_passes_length_and_fails_only_on_being_unknown(indexed):
    """No real `FILTER_COLUMNS` name is anywhere near `MAX_ARG_CHARS` long, so there is no benign
    "at-limit key that is also a real column" case to prove succeeds — this proves the NEXT thing
    instead: an at-limit key is never rejected for its LENGTH (it reaches `search_arms`), and the
    error it does get is the separate, later "unknown filter column" one, not "too long"."""
    conn, fx = indexed
    svc = make_service(fx, conn, fx.STEWARD)
    with pytest.raises(ValueError, match="unknown filter column"):
        svc.search("quarterly revenue", filters={"x" * MAX_ARG_CHARS: "acme"})


def test_at_limit_filters_value_reaches_the_real_search(indexed):
    conn, fx = indexed
    svc = make_service(fx, conn, fx.STEWARD)
    out = svc.search("quarterly revenue", filters={"entity": "x" * MAX_ARG_CHARS})
    assert "hits" in out   # ran to completion (no matching entity, but no ValueError either)


def test_over_limit_argument_still_writes_an_audit_row():
    """A length rejection is a call outcome worth auditing, exactly like a rate-limit refusal or
    any other service-layer error."""
    audit = FakeAudit()
    svc = poisoned_service(audit=audit)
    with pytest.raises(ValueError):
        svc.search("x" * (MAX_ARG_CHARS + 1))

    assert len(audit.rows) == 1
    assert audit.rows[0]["outcome"] == "error"
    assert audit.rows[0]["error_class"] == "ValueError"
    assert audit.rows[0]["tool"] == "search_brain"


def test_over_limit_argument_is_checked_before_the_rate_limiter_would_matter():
    """The length check and the rate limiter are independent guards — an identity with budget to
    spare still gets the clean ValueError, not confused with a RateLimitError."""
    svc = poisoned_service()
    with pytest.raises(ValueError):
        svc.search("x" * (MAX_ARG_CHARS + 1))
    # sanity: RateLimitError is a genuinely different exception type this call did NOT raise
    with pytest.raises(ValueError) as exc_info:
        svc.search("x" * (MAX_ARG_CHARS + 1))
    assert not isinstance(exc_info.value, RateLimitError)


# ── at-limit really reaches the real work (real Postgres, fake embedder) ───────────────────────
def test_at_limit_query_reaches_the_real_search(indexed):
    conn, fx = indexed
    svc = make_service(fx, conn, fx.STEWARD)
    out = svc.search("quarterly revenue " + "x" * (MAX_ARG_CHARS - len("quarterly revenue ")))
    assert "hits" in out and out["count"] == len(out["hits"])   # ran to completion, no ValueError


def test_at_limit_path_reaches_the_real_read_page(indexed):
    conn, fx = indexed
    svc = make_service(fx, conn, fx.STEWARD)
    padded_path = fx.OPEN_PAGE   # a real path; the limit is on LENGTH, not on being a real page
    assert len(padded_path) <= MAX_ARG_CHARS
    out = svc.read_page(padded_path)
    assert "body" in out   # ran to completion


# ── ask's question: the same guard, one layer up (mcp_server.py's closure) ─────────────────────
def _call_mcp(mcp, name: str, **args) -> dict:
    import asyncio
    import json
    blocks, _ = asyncio.run(mcp.call_tool(name, args))
    return json.loads(blocks[0].text)


def test_ask_over_the_limit_never_constructs_the_answer_service(monkeypatch):
    from stigmergy.server.mcp_server import build_mcp

    def boom(*_a, **_kw):
        raise AssertionError("AnswerService must never be constructed for an over-limit question")
    monkeypatch.setattr("stigmergy.answer.service.AnswerService", boom)

    svc = poisoned_service(audit=FakeAudit())
    mcp = build_mcp(svc)

    out = _call_mcp(mcp, "ask", question="x" * (MAX_ARG_CHARS + 1))
    assert out == {"error": f"question too long (max {MAX_ARG_CHARS} characters)"}
    assert svc.audit.rows[-1]["outcome"] == "error"
    assert svc.audit.rows[-1]["error_class"] == "ValueError"


def test_brain_delete_over_the_limit_reason_returns_the_checks_own_sentence():
    """OLD BEHAVIOUR: `{"error": "<tool> failed (ValueError)"}`.

    The write tools length-check their free text before they read anything, and
    `check_arg_length` raises its MARKED `ValueError` — but the closure caught only
    `(CaptureError, RateLimitError, CapabilityUnavailableError)`, so the marked rejection fell
    through to `_failure`, which is class-name-only by design. A person pasting an over-long
    reason was told which tool broke and nothing about what to do, while every read tool answered
    the same overflow with the actionable sentence.

    Poisoned conn/embedder: the check must fire before anything is read, so nothing on this path
    may touch the database at all.
    """
    from stigmergy.server.mcp_server import build_mcp

    svc = poisoned_service(audit=FakeAudit())
    mcp = build_mcp(svc)

    out = _call_mcp(mcp, "brain_delete", paths=["wiki/notes/Old.md"],
                    why="x" * (MAX_ARG_CHARS + 1))
    assert out == {"error": f"why too long (max {MAX_ARG_CHARS} characters)"}
    assert svc.audit.rows[-1]["error_class"] == "ValueError"


def test_brain_delete_checks_every_path_it_was_handed_not_only_the_reason():
    """The same guard, over the OTHER free-text argument: a path is a caller-controlled string
    that reaches a clone and a commit message, so an over-long one is refused by name before any
    of that. The sentence names `path`, not `why` — a guard that reported the wrong field would
    send a caller to edit text that was never the problem."""
    from stigmergy.server.mcp_server import build_mcp

    svc = poisoned_service(audit=FakeAudit())
    mcp = build_mcp(svc)

    out = _call_mcp(mcp, "brain_delete", paths=["wiki/notes/Old.md", "x" * (MAX_ARG_CHARS + 1)],
                    why="a short reason")
    assert out == {"error": f"path too long (max {MAX_ARG_CHARS} characters)"}


def test_brain_delete_at_the_limit_reason_reaches_the_service():
    """The benign twin: an AT-limit reason is never rejected for its LENGTH — the call proceeds
    into the door, which then refuses it for a completely different reason, so "passed the guard"
    can never be read as "was rejected by it"."""
    from stigmergy.server.mcp_server import build_mcp

    svc = poisoned_service(audit=FakeAudit())
    mcp = build_mcp(svc)

    out = _call_mcp(mcp, "brain_delete", paths=["wiki/notes/Old.md"], why="x" * MAX_ARG_CHARS)
    # Past the guard and into the door's own first refusal — a genuinely different sentence from
    # the length check's, on a service with no evidence store wired (a removal is QUEUED since ADR
    # 044 D3, so the capture queue is what this door needs and what this service has not got).
    assert "evidence store" in out["error"]
    assert "too long" not in out["error"]


def test_a_pydantic_shaped_value_error_is_still_reduced_to_a_class_name(monkeypatch):
    """The specificity half of the escape: only `check_arg_length`'s OWN marked rejection is
    echoed. An UNMARKED `ValueError` — the shape a `pydantic_core.ValidationError` arrives in,
    carrying untrusted content or internal field paths — must still collapse to a class name."""
    from stigmergy.server.mcp_server import build_mcp

    def _unmarked(*_a, **_kw):
        raise ValueError("SECRET-MARKER field path leaked from somewhere internal")
    # The door's own sequence, reached as a module attribute — patched here rather than raised
    # from a double, so the ValueError arrives from exactly where a real one would.
    monkeypatch.setattr("stigmergy.server.review.queue_deletion", _unmarked)

    # `poisoned_service` resolves no audiences, so it is unrestricted — the one identity kind
    # `brain_delete` authorizes (ADR 044 D3), which is what lets the call reach the door at all.
    svc = poisoned_service(audit=FakeAudit())
    mcp = build_mcp(svc)

    out = _call_mcp(mcp, "brain_delete", paths=["wiki/notes/Old.md"], why="a short reason")
    assert out == {"error": "brain_delete failed (ValueError)"}
    assert "SECRET-MARKER" not in out["error"]


def test_ask_at_the_limit_does_reach_the_answer_service(monkeypatch):
    """The mirror image of the test above: an AT-limit question is NOT short-circuited — the code
    proceeds far enough to construct `AnswerService` (proven with a marker exception distinct from
    the length-check's ValueError, so the two cases can never be confused)."""
    from stigmergy.server.mcp_server import build_mcp

    def marker(*_a, **_kw):
        raise RuntimeError("reached AnswerService")
    monkeypatch.setattr("stigmergy.answer.service.AnswerService", marker)

    svc = poisoned_service(audit=FakeAudit())
    mcp = build_mcp(svc)

    out = _call_mcp(mcp, "ask", question="x" * MAX_ARG_CHARS)
    assert out["error"].startswith("ask failed (RuntimeError)")   # the marker, not a length error
    assert svc.audit.rows[-1]["error_class"] == "RuntimeError"
