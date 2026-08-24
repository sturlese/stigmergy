"""**The write path starts without the read path's key.**

With no `OPENAI_API_KEY`, `stigmergy-server` starts and `brain_submit` / `brain_submissions` /
`read_page` work, while `search_brain` / `ask` fail with an honest message naming the missing
capability. That is the whole property, and it is asserted here at TWO levels:

- through the MCP tool closures (`build_mcp`), because the message a person actually reads is
  shaped there — a `CapabilityUnavailableError` that the generic handler collapsed to
  `search_brain failed (CapabilityUnavailableError)` would satisfy "fails" and fail "honest";
- through `BrainService.require_embedder`, the backend guard, because a tool that only *happened*
  to fail because `.embed()` blew up downstream would be one refactor away from doing the
  expensive work first.

The keyless condition is built explicitly (an `UnavailableEmbedder` injected into the service)
rather than by deleting the environment variable and hoping the fixture chain notices: this suite's
index is built with the FAKE embedder, so an absent key changes nothing about it. What is under
test is the degraded path, not how the degradation is detected — `tests/server/test_startup.py`
owns that half.
"""
import asyncio
import json

import pytest

from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.index.backends.embedder import MISSING_KEY_MESSAGE
from stigmergy.server.errors import CapabilityUnavailableError
from stigmergy.server.mcp_server import build_mcp
from stigmergy.server.service import UnavailableEmbedder, missing_embedder_reason
from tests.server.conftest import make_service

# The reason a real keyless start produces: `OpenAIEmbedder`'s own refusal, wrapped by
# `missing_embedder_reason`. Built from the embedder's OWN message constant (imported above) — a
# retyped copy of the sentence drifted once (audit T4) and left this suite permanently green
# about a message that no longer existed.
KEYLESS_REASON = missing_embedder_reason(MISSING_KEY_MESSAGE)


@pytest.fixture()
def keyless(indexed):
    """A service whose query embedder is unavailable — everything else real."""
    conn, fx = indexed
    svc = make_service(fx, conn, fx.STEWARD, evidence=MemoryEvidenceStore())
    svc.embedder = UnavailableEmbedder(KEYLESS_REASON)
    return svc, fx


def _tools(service):
    """The tool callables out of a real `FastMCP`, so the closures under test are the ones the
    protocol would invoke rather than reimplementations of them."""
    mcp = build_mcp(service)
    return {name: fn.fn for name, fn in mcp._tool_manager._tools.items()}


# ── the half that must keep working ──────────────────────────────────────────────────────────────
# `brain_submit` is an `async` closure — it uploads to the evidence store, which must not run on
# the event loop (#136) — so it is driven through `asyncio.run` here, for the reason the `ask` test
# below states: this suite has no async plugin, and the closure under test is the real one.
def test_brain_submit_works_with_no_embedder(keyless):
    svc, _ = keyless
    ack = json.loads(asyncio.run(_tools(svc)["brain_submit"](
        text="a capture made while the embedding key was expired")))
    assert ack["status"] == "queued" and isinstance(ack["id"], str)
    assert "error" not in ack


def test_brain_submissions_works_with_no_embedder(keyless):
    svc, _ = keyless
    tools = _tools(svc)
    asyncio.run(tools["brain_submit"](text="something to read back keyless"))
    out = json.loads(tools["brain_submissions"]())
    assert "error" not in out and out["submissions"]


def test_read_page_works_with_no_embedder(keyless):
    """`read_page` fetches by PATH — it never embeds anything. This is the assertion that keeps a
    future 'require the embedder in the service constructor' shortcut from taking it down."""
    svc, _ = keyless
    out = json.loads(_tools(svc)["read_page"](path="wiki/notes/initech-kpi.md"))
    assert "error" not in out
    assert out["path"] == "wiki/notes/initech-kpi.md"



# ── the entity-navigation tools need no embedder either ───────────────────────────────────────
def test_list_entities_works_with_no_embedder(keyless):
    """`list_entities` reads the registry file plus `pages_index.entity` by membership — it never
    embeds anything, same reasoning as `read_page`."""
    svc, _ = keyless
    out = json.loads(_tools(svc)["list_entities"]())
    assert "error" not in out
    assert isinstance(out["entities"], list)


def test_describe_entity_works_with_no_embedder(keyless):
    svc, _ = keyless
    out = json.loads(_tools(svc)["describe_entity"](entity="initech"))
    assert "error" not in out
    assert out["found"] is False


# ── the half that must refuse, honestly ──────────────────────────────────────────────────────────
def test_search_brain_refuses_and_names_the_missing_capability(keyless):
    svc, _ = keyless
    out = json.loads(_tools(svc)["search_brain"](query="anything at all"))

    error = out["error"]
    assert "OPENAI_API_KEY" in error                 # WHICH thing is missing
    assert "cannot search" in error                  # which CAPABILITY is gone
    assert "brain_submit" in error                   # and that capture still works
    # NOT the class-name-only fallback: that is the shape this test exists to prevent
    assert "CapabilityUnavailableError" not in error


def test_ask_refuses_and_names_the_missing_capability(keyless):
    """`ask` searches, so it needs the same capability — and it must refuse BEFORE the agent, which
    is why `mcp_server`'s closure calls `require_embedder` itself rather than waiting for the
    evidence-gathering run to fail somewhere inside.

    `asyncio.run` rather than a pytest-asyncio marker: this suite has no async plugin and every
    other async assertion in `tests/server` drives the coroutine directly."""
    svc, _ = keyless
    out = json.loads(asyncio.run(_tools(svc)["ask"](question="what do we know about Initech?")))

    error = out["error"]
    assert "OPENAI_API_KEY" in error and "cannot search" in error
    assert "CapabilityUnavailableError" not in error


def test_the_refusal_message_names_no_key_no_path_and_no_dsn(keyless):
    """This message crosses the HTTP boundary verbatim, so it may name an environment VARIABLE
    and must never name its value, a filesystem path or a DSN."""
    svc, _ = keyless
    error = json.loads(_tools(svc)["search_brain"](query="x"))["error"]
    assert "sk-" not in error
    assert "postgresql://" not in error and "/Users/" not in error and "@" not in error


# ── the backend guard itself ──────────────────────────────────────────────────────────────────────
def test_require_embedder_is_silent_when_the_embedder_is_real(indexed):
    conn, fx = indexed
    make_service(fx, conn, fx.STEWARD).require_embedder()      # must not raise


def test_require_embedder_refuses_on_the_unavailable_one(keyless):
    svc, _ = keyless
    with pytest.raises(CapabilityUnavailableError, match="OPENAI_API_KEY"):
        svc.require_embedder()


def test_the_unavailable_embedder_raises_rather_than_returning_a_vector():
    """The defence-in-depth half. A degraded embedder that returned SOMETHING would reproduce the
    `--embedder fake` hazard: a query embedded into a space the index was not built in returns
    unrelated results and reports success, which is the one failure worse than an error."""
    embedder = UnavailableEmbedder(KEYLESS_REASON)
    with pytest.raises(CapabilityUnavailableError):
        embedder.embed(["a query"])
