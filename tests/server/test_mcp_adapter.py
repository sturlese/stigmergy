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
import json
import types
from unittest.mock import create_autospec

import pytest

from stigmergy.capture.errors import CaptureError, EvidenceError, SubmissionRejected
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.server.errors import RateLimitError, RegistryError
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
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {
        # read
        "search_brain", "read_page", "list_entities", "describe_entity", "ask",
        # write
        "brain_submit", "brain_submissions", "brain_delete",
    }
    assert len(names) == 8


def test_search_brain_forwards_arguments_and_returns_the_service_payload(fake_service):
    fake_service.search.return_value = {
        "query": "q", "built_at": "2026-07-19T00:00:00", "embedding_model": "fake",
        "count": 0, "hits": []}
    mcp = build_mcp(fake_service)

    out = _call(mcp, "search_brain", query="q", filters={"entity": "acme"}, max_results=3)

    assert out == fake_service.search.return_value
    fake_service.search.assert_called_once_with(
        "q", filters={"entity": "acme"}, max_results=3)


@pytest.mark.parametrize("exc", [ValueError("unknown filter: body"), StigmergyIndexError("boom")])
def test_search_brain_maps_service_errors_to_a_clean_json_error(fake_service, exc):
    fake_service.search.side_effect = exc
    mcp = build_mcp(fake_service)

    out = _call(mcp, "search_brain", query="q")

    assert out == {"error": str(exc)}


def test_search_brain_does_not_echo_a_malformed_registrys_path(fake_service):
    """Registry failures never expose their filesystem origin."""
    fake_service.search.side_effect = RegistryError("the entity registry could not be read")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "search_brain", query="q")

    assert out == {"error": "search_brain failed (RegistryError)"}
    assert "/" not in json.dumps(out)


def test_read_page_forwards_the_path_and_returns_the_service_payload(fake_service):
    fake_service.read_page.return_value = {"error": "unknown page: wiki/x.md"}
    mcp = build_mcp(fake_service)

    out = _call(mcp, "read_page", path="wiki/x.md")

    assert out == {"error": "unknown page: wiki/x.md"}
    fake_service.read_page.assert_called_once_with("wiki/x.md")




# ── list_entities / describe_entity closures ───────────────────────────────────────────────────
def test_list_entities_takes_no_arguments_and_returns_the_service_payload(fake_service):
    fake_service.list_entities.return_value = {"count": 1, "entities": [{"id": "acme"}]}
    mcp = build_mcp(fake_service)

    out = _call(mcp, "list_entities")

    assert out == fake_service.list_entities.return_value
    fake_service.list_entities.assert_called_once_with()


def test_list_entities_maps_an_unanticipated_exception_to_class_name_only(fake_service):
    """A `ValueError` whose message can carry a filesystem path — the shape the registry loader
    itself raises before the service converts it (`RegistryError`) — is never echoed verbatim, the
    same posture as every other unanticipated exception. The conversion is the service's promise;
    this is the closure's own net under it."""
    fake_service.list_entities.side_effect = ValueError(
        "entity registry /repo/ops/entity-registry.json: top-level 'entities' object "
        "is required")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "list_entities")

    assert out == {"error": "list_entities failed (ValueError)"}
    assert "/repo" not in json.dumps(out)


def test_list_entities_maps_a_rate_limit_refusal_to_a_clean_json_error(fake_service):
    fake_service.list_entities.side_effect = RateLimitError(
        "rate limited: 30 requests/min exceeded — wait a moment and retry")
    mcp = build_mcp(fake_service)

    out = _call(mcp, "list_entities")

    assert out == {"error": "rate limited: 30 requests/min exceeded — wait a moment and retry"}


def test_describe_entity_forwards_the_input_and_returns_the_service_payload(fake_service):
    fake_service.describe_entity.return_value = {
        "entity": {"id": "acme", "name": "Acme", "type": "organization", "aliases": [],
                  "page": None},
        "view": None, "timeline": [], "timeline_note": "No anchored pages."}
    mcp = build_mcp(fake_service)

    out = _call(mcp, "describe_entity", entity="Acme Corp")

    assert out == fake_service.describe_entity.return_value
    fake_service.describe_entity.assert_called_once_with("Acme Corp")


def test_describe_entity_unknown_or_out_of_scope_returns_the_service_error_payload(fake_service):
    fake_service.describe_entity.return_value = {"error": "unknown entity: nope"}
    mcp = build_mcp(fake_service)

    out = _call(mcp, "describe_entity", entity="nope")

    assert out == {"error": "unknown entity: nope"}


def test_describe_entity_a_marker_valueerror_is_echoed_verbatim(fake_service):
    fake_service.describe_entity.side_effect = _marker_value_error(
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


# ── read_page/describe_entity/ask narrow their ValueError catch to ONLY check_arg_length's own
# rejection (marked `is_arg_length_error`) — any OTHER ValueError (e.g. a stray
# pydantic_core.ValidationError from the ask/answer stack, which genuinely subclasses ValueError
# and could carry untrusted LLM output or an internal field path) falls through to the
# class-name-only fallback, never echoing str(ex). search_brain is deliberately BROADER (it keeps
# catching ValueError wholesale — its own carve-out, see mcp_server.py's docstring) and is
# exercised by test_search_brain_maps_service_errors_to_a_clean_json_error above. ──────────────
def _marker_value_error(message: str) -> ValueError:
    """The exact shape `check_arg_length` raises — a plain ValueError with the marker attribute
    set — WITHOUT depending on the real length-checking logic, so this file stays scoped to
    mcp_server.py's own exception-to-JSON mapping (`tests/server/test_arg_length.py` owns the
    length-checking logic itself)."""
    ex = ValueError(message)
    ex.is_arg_length_error = True
    return ex



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
