"""In-process adapter test for `build_mcp`.

`tests/server/test_mcp_harness.py` already proves the real contract end to end: a real subprocess
speaking the real MCP protocol against a real `BrainService` over postgres. What it CANNOT prove
is coverage of `mcp_server.py`'s own tool closures — they run inside a child process, so
`coverage.py` in the parent pytest run never sees those lines execute (58% on this module, all of
it the closure bodies), even though the harness genuinely exercises them.

This file closes that instrumentation gap without giving up anything the harness already covers:
it calls the real `build_mcp()` and the real closures in-process (via FastMCP's own `call_tool`,
no subprocess) against a `create_autospec(BrainService)` double. The double is spec'd against the
real class — a signature drift in `BrainService` fails this test loudly instead of the mock
silently accepting a call the real service could never satisfy.

Scope: this test is about `mcp_server.py`'s OWN logic (argument wiring, JSON envelope, exception
-> {"error": ...} mapping) — never about ACL enforcement or ranking, which stay proven for real
elsewhere (`test_mcp_harness.py`, `test_service_acl.py`)."""
import asyncio
import datetime as dt
import json
import types
from unittest.mock import create_autospec

import pytest

from stigmergy.capture import queue
from stigmergy.capture.errors import CaptureError, EvidenceError, SubmissionRejected
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.server import identity
from stigmergy.server.errors import ArgumentLengthError, InvalidArgumentError, RateLimitError
from stigmergy.server.mcp_server import build_mcp
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings


def _call(mcp, tool: str, **args) -> dict:
    """`tool`, not `name` — a tool argument called `name` once shadowed it, and every call site
    here passes the tool NAME positionally."""
    blocks, _ = asyncio.run(mcp.call_tool(tool, args))
    return json.loads(blocks[0].text)


@pytest.fixture()
def fake_service():
    service = create_autospec(BrainService, instance=True)
    # `create_autospec` specs against `dir(BrainService)` — the CLASS's methods and any
    # class-level attribute — never a plain instance attribute `__init__` only ever assigns via
    # `self.x = ...` (`settings`, `conn`, `identity`, ... are exactly that kind of attribute).
    # Reading one off a bare `create_autospec(..., instance=True)` raises `AttributeError:
    # Mock object has no attribute 'x'` immediately, the same way it would for a genuine typo.
    # Setting the three a real `BrainService` always carries keeps the double a faithful
    # stand-in for any closure that reaches for them, instead of failing on the attribute lookup
    # before it ever reaches the assertion under test.
    service.settings = Settings()
    service.conn = None
    service.identity = None
    return service


def test_the_mounted_tool_list_is_exactly_the_eight_supported_tools(fake_service):
    """Pin the supported MCP product surface."""
    mcp = build_mcp(fake_service)
    assert mcp.name == "stigmergy"
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {
        # read
        "search_brain", "read_page", "list_pages", "describe_entity", "ask",
        # write
        "brain_submit", "brain_submissions", "brain_delete",
    }
    assert len(names) == 8


def test_search_brain_forwards_arguments_and_returns_the_service_payload(fake_service):
    fake_service.search.return_value = {
        "query": "q", "built_at": "2026-07-19T00:00:00", "embedding_model": "fake",
        "count": 0, "hits": []}
    mcp = build_mcp(fake_service)

    out = _call(mcp, "search_brain", query="q", filters={"entity": "wiki/entities/Acme.md"},
                max_results=3)

    assert out == fake_service.search.return_value
    fake_service.search.assert_called_once_with(
        "q", filters={"entity": "wiki/entities/Acme.md"}, max_results=3)


@pytest.mark.parametrize("exc", [ValueError("unknown filter: body"), StigmergyIndexError("boom")])
def test_search_brain_maps_service_errors_to_a_clean_json_error(fake_service, exc):
    fake_service.search.side_effect = exc
    mcp = build_mcp(fake_service)

    out = _call(mcp, "search_brain", query="q")

    assert out == {"error": str(exc)}


def test_search_brain_maps_an_unanticipated_exception_to_class_name_only(fake_service):
    """An unanticipated failure never exposes a path, a DSN or another internal detail."""
    fake_service.search.side_effect = RuntimeError("could not read /repo/wiki/entities/Acme.md")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "search_brain", query="q")

    assert out == {"error": "search_brain failed (RuntimeError)"}
    assert "/" not in json.dumps(out)


def test_read_page_forwards_the_path_and_returns_the_service_payload(fake_service):
    fake_service.read_page.return_value = {"error": "unknown page: wiki/x.md"}
    mcp = build_mcp(fake_service)

    out = _call(mcp, "read_page", path="wiki/x.md")

    assert out == {"error": "unknown page: wiki/x.md"}
    fake_service.read_page.assert_called_once_with("wiki/x.md")




# ── list_pages / describe_entity closures ──────────────────────────────────────────────────────
def test_list_pages_forwards_every_filter_and_returns_the_service_payload(fake_service):
    fake_service.list_pages.return_value = {"total": 1, "count": 1, "next": None, "layers": [],
                                            "pages": [{"path": "wiki/entities/Acme.md"}]}
    mcp = build_mcp(fake_service)

    out = _call(mcp, "list_pages", type="entity", links_to="Acme", since="2026-09-01")

    assert out == fake_service.list_pages.return_value
    fake_service.list_pages.assert_called_once_with(
        type="entity", entity_type=None, layer=None, since="2026-09-01", until=None,
        links_to="Acme", sort="recent", cursor=None)


def test_list_pages_echoes_its_own_argument_refusals(fake_service):
    fake_service.list_pages.side_effect = InvalidArgumentError("sort must be one of recent, oldest, title")
    mcp = build_mcp(fake_service)

    assert _call(mcp, "list_pages", sort="random") == {
        "error": "sort must be one of recent, oldest, title"}


def test_list_pages_maps_an_unanticipated_value_error_to_class_name_only(fake_service):
    """A `ValueError` that is not the service's own refusal can carry a filesystem path, so it is
    never echoed verbatim."""
    fake_service.list_pages.side_effect = ValueError(
        "/repo/wiki/entities/Acme.md: aliases must be a string list")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "list_pages")

    assert out == {"error": "list_pages failed (ValueError)"}
    assert "/repo" not in json.dumps(out)


def test_list_pages_maps_a_rate_limit_refusal_to_a_clean_json_error(fake_service):
    fake_service.list_pages.side_effect = RateLimitError(
        "rate limited: 30 requests/min exceeded — wait a moment and retry")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "list_pages")

    assert out == {"error": "rate limited: 30 requests/min exceeded — wait a moment and retry"}


def test_describe_entity_forwards_the_input_and_returns_the_service_payload(fake_service):
    fake_service.describe_entity.return_value = {
        "found": True,
        "entity": {"path": "wiki/entities/Acme Corp.md", "title": "Acme Corp",
                   "entity_type": "organization", "aliases": ["Acme"], "status": "developing",
                   "updated": "2026-08-24", "excerpt": "A customer.", "excerpt_truncated": False},
        "knowledge": [], "knowledge_note": "No pages link this entity."}
    mcp = build_mcp(fake_service)

    out = _call(mcp, "describe_entity", entity="Acme Corp")

    assert out == fake_service.describe_entity.return_value
    fake_service.describe_entity.assert_called_once_with("Acme Corp")


def test_describe_entity_unknown_or_out_of_scope_returns_the_service_absence_payload(fake_service):
    fake_service.describe_entity.return_value = {"found": False, "entity": None, "knowledge": []}
    mcp = build_mcp(fake_service)

    out = _call(mcp, "describe_entity", entity="nope")

    assert out == {"found": False, "entity": None, "knowledge": []}


def test_describe_entity_an_argument_length_error_is_echoed_verbatim(fake_service):
    fake_service.describe_entity.side_effect = ArgumentLengthError(
        "entity too long (max 8192 characters)")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "describe_entity", entity="x")

    assert out == {"error": "entity too long (max 8192 characters)"}


def test_describe_entity_a_non_marker_valueerror_is_never_echoed(fake_service):
    fake_service.describe_entity.side_effect = ValueError("leaked: /etc/secret internal detail")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "describe_entity", entity="x")

    assert out == {"error": "describe_entity failed (ValueError)"}
    assert "leaked" not in json.dumps(out) and "/etc/secret" not in json.dumps(out)


def test_describe_entity_maps_a_rate_limit_refusal_to_a_clean_json_error(fake_service):
    fake_service.describe_entity.side_effect = RateLimitError(
        "rate limited: 30 requests/min exceeded — wait a moment and retry")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "describe_entity", entity="acme")

    assert out == {"error": "rate limited: 30 requests/min exceeded — wait a moment and retry"}


# ── brain_submit / brain_submissions closures ──────────────────────────────────────────────────
def test_brain_submit_forwards_arguments_and_returns_the_service_payload(fake_service):
    fake_service.submit.return_value = {
        "id": "capture-7",
        "status": "queued",
        "submitted_by": "steward@example.com",
    }
    mcp = build_mcp(fake_service)

    out = _call(
        mcp,
        "brain_submit",
        text="a decision",
        title="Decision",
        occurred_at="2026-08-24",
        audience=["finance"],
        resolution_of="con_00000000-0000-4000-8000-000000000001",
    )

    assert out == fake_service.submit.return_value
    fake_service.submit.assert_called_once_with(
        text="a decision",
        path=None,
        url=None,
        title="Decision",
        occurred_at="2026-08-24",
        audience=["finance"],
        resolution_of="con_00000000-0000-4000-8000-000000000001",
    )


@pytest.mark.parametrize("exc", [SubmissionRejected("material is empty — there is nothing to capture"),
                                 EvidenceError("evidence store unavailable (ClientError)")])
def test_brain_submit_maps_capture_errors_to_a_clean_json_error_echoed_verbatim(fake_service, exc):
    """Capture-layer messages are value-free public refusals."""
    fake_service.submit.side_effect = exc
    mcp = build_mcp(fake_service)

    out = _call(mcp, "brain_submit", text="x")

    assert out == {"error": str(exc)}


def test_brain_submit_maps_a_rate_limit_refusal_to_a_clean_json_error(fake_service):
    fake_service.submit.side_effect = RateLimitError(
        "rate limited: 30 requests/min exceeded — wait a moment and retry")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "brain_submit", text="x")

    assert out == {"error": "rate limited: 30 requests/min exceeded — wait a moment and retry"}


def test_brain_submit_maps_an_unanticipated_exception_to_class_name_only(fake_service):
    fake_service.submit.side_effect = RuntimeError("connection to 10.0.0.5 refused, key AKIA123")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "brain_submit", text="x")

    assert out == {"error": "brain_submit failed (RuntimeError)"}
    assert "10.0.0.5" not in json.dumps(out) and "AKIA123" not in json.dumps(out)


def test_brain_submit_normalizes_occurred_at_and_refuses_naive_timestamps(monkeypatch):
    """MCP matches admin's date and timezone contract before a capture reaches the queue."""
    captured = []
    receipt = {
        "id": "capture-7",
        "status": "queued",
        "submitted_by": "steward@example.com",
        "created_at": "2026-08-24T00:00:00Z",
        "created": True,
        "acl": None,
        "layer_decision": "default",
        "layer_probabilities": None,
    }

    def existing_capture(_conn, envelope):
        captured.append(envelope)
        return receipt

    monkeypatch.setattr("stigmergy.capture.service.tenancy.require_tenant",
                        lambda _conn: "00000000-0000-4000-8000-0000000000aa")

    monkeypatch.setattr(queue, "find_capture", existing_capture)
    service = BrainService(
        Settings(),
        object(),
        None,
        None,
        identity="steward@example.com",
        evidence=object(),
        principal=identity.Principal(
            subject="steward@example.com",
            display_name="Steward",
            groups=(),
            default_audience=("public",),
        ),
    )
    mcp = build_mcp(service)

    assert _call(mcp, "brain_submit", text="bare date", occurred_at="2026-08-24")["status"] == "queued"
    assert _call(
        mcp,
        "brain_submit",
        text="aware timestamp",
        occurred_at="2026-08-24T15:30:00+02:00",
    )["status"] == "queued"
    naive = _call(
        mcp,
        "brain_submit",
        text="naive timestamp",
        occurred_at="2026-08-24T15:30:00",
    )

    assert captured[0].origin.occurred_at == dt.date(2026, 8, 24)
    assert captured[1].origin.occurred_at == dt.datetime(2026, 8, 24, 13, 30, tzinfo=dt.UTC)
    assert naive == {"error": "occurred_at must be an ISO date or timezone-aware timestamp"}
    assert len(captured) == 2


def test_brain_submissions_forwards_blank_status_as_none(fake_service):
    fake_service.submissions.return_value = {"identity": "steward@example.com", "scope": "own",
                                             "count": 0, "submissions": []}
    mcp = build_mcp(fake_service)

    _call(mcp, "brain_submissions", limit=10, status="")

    fake_service.submissions.assert_called_once_with(limit=10, status=None)


def test_brain_submissions_returns_the_service_payload_verbatim(fake_service):
    fake_service.submissions.return_value = {"identity": "steward@example.com", "scope": "all",
                                             "count": 1, "submissions": [{"id": 1, "mine": True}]}
    mcp = build_mcp(fake_service)

    out = _call(mcp, "brain_submissions")

    assert out == fake_service.submissions.return_value


@pytest.mark.parametrize("exc", [ValueError("unknown status: bogus (allowed: queued, processing, "
                                            "landed, failed)"),
                                 CaptureError("some capture-layer refusal")])
def test_brain_submissions_maps_value_and_capture_errors_to_a_clean_json_error(fake_service, exc):
    fake_service.submissions.side_effect = exc
    mcp = build_mcp(fake_service)

    out = _call(mcp, "brain_submissions", status="bogus")

    assert out == {"error": str(exc)}


def test_brain_submissions_maps_an_unanticipated_exception_to_class_name_only(fake_service):
    fake_service.submissions.side_effect = RuntimeError("boom")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "brain_submissions")

    assert out == {"error": "brain_submissions failed (RuntimeError)"}


# ── blocking write tools ──────────────────────────────────────────────────────────────────────
def _on_the_event_loop_probe(service, method: str):
    """Point one `BrainService` method at a probe reporting whether it was called ON the event
    loop. It answers a fact about its CALLER, so it reads the same for a closure that calls the
    service inline and for one that hands it to a worker thread."""
    def probe(*_a, **_k):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return {"on_the_event_loop": False}
        return {"on_the_event_loop": True}

    setattr(service, method, probe)


@pytest.mark.parametrize("tool, method, args", [
    ("brain_submit", "submit", {"text": "something worth keeping"}),
    ("brain_delete", "delete_pages", {"paths": ["wiki/notes/Old.md"], "why": "stale"}),
])
def test_a_write_tool_never_runs_on_the_event_loop(fake_service, tool, method, args):
    """Blocking writes run in a worker thread without changing their response."""
    _on_the_event_loop_probe(fake_service, method)

    out = _call(build_mcp(fake_service), tool, **args)

    assert out == {"on_the_event_loop": False}


def test_the_probe_can_actually_see_the_event_loop():
    """The instrument's own specificity: a probe that always answered `False` would make the two
    tests above green for a tool that never left the loop at all. Called directly from inside a
    coroutine, it must say so."""
    holder = types.SimpleNamespace()
    _on_the_event_loop_probe(holder, "submit")

    async def go():
        return holder.submit()

    assert asyncio.run(go()) == {"on_the_event_loop": True}
    assert holder.submit() == {"on_the_event_loop": False}   # and off it, from an ordinary call
