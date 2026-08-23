"""Anthropic prompt caching on the ORDINARY run — the settings-dict helper, pure; the
wire shape it produces, proven against a real framework call; the ORDINARY flow's `Agent(...)` call
actually wired to the helper's result; and the meeting flow's exclusion — both wiring facts pinned
as the decisions they are rather than left to be rediscovered from the source.

**Why messages-caching matters, in one sentence** (the fuller account is
`pydantic_backend.prompt_cache_settings`'s own docstring): an ordinary capture ITERATES — up to
`max_turns` model requests — and every one of those requests resends the same system prompt, the
same five tool schemas and the same gathered seed underneath a growing conversation, so a cache
READ at roughly 0.1x the input rate is where the bill lives past the first turn.

**The wire-shape tests are the one place in this file that touch the agent framework's Anthropic
path for real** (`pydantic_ai.models.anthropic.AnthropicModel` / `providers.anthropic.
AnthropicProvider`) — offline only because the TRANSPORT is an `httpx.MockTransport`, never because
anything is faked: this repo's own testing doctrine ("never fake what you are claiming to prove")
is exactly why a plain dict shaped like the framework's field names is not, on its own, proof that
the framework turns it into `cache_control` blocks on the wire. `pydantic_ai` is imported inside a
function body only, reached exclusively from these two tests, so importing this MODULE never touches
the framework's Anthropic submodule at all.

No test here needs a network call or a real API key: every request is answered by a `MockTransport`
closure, and an explicit fake `api_key` is passed so no ambient credential is read; the package's
autouse `no_ambient_agent_credential` fixture (`conftest.py`) clears the provider key variables
besides.
"""
import json

import pytest

from stigmergy.librarian import config, pydantic_backend
from stigmergy.librarian.errors import AgentError
from tests.librarian import support

ANTHROPIC_MODEL = "anthropic:claude-sonnet-5"


# ── the pure helper: (model id, prompt_cache setting) -> dict | None ───────────────────────────
@pytest.mark.parametrize("ttl", ["5m", "1h"])
def test_an_anthropic_model_with_a_valid_ttl_returns_the_exact_three_field_dict(ttl):
    assert pydantic_backend.prompt_cache_settings(ANTHROPIC_MODEL, ttl) == {
        "anthropic_cache_instructions": ttl,
        "anthropic_cache_tool_definitions": ttl,
        "anthropic_cache_messages": ttl,
    }


def test_off_returns_none_even_for_an_anthropic_model():
    """The escape hatch: `prompt_cache="off"` must produce nothing at all, on the one model family
    that could otherwise take it."""
    assert pydantic_backend.prompt_cache_settings(ANTHROPIC_MODEL, "off") is None


@pytest.mark.parametrize("model", [
    "openai:gpt-5.6-terra",
    "google-gla:gemini-3.6-flash",
    "claude-sonnet-5",     # the bare, unprefixed spelling — reads as OpenAI to pydantic-ai
    "",
])
@pytest.mark.parametrize("ttl", ["5m", "1h"])
def test_a_non_anthropic_model_returns_none_however_the_ttl_is_set(model, ttl):
    """**The benign twin the feature's whole safety depends on: it must NOT fire where it must
    not.** A Gemini or OpenAI model has no `anthropic_cache_*` field, and this helper must never
    hand one a dict shaped for a different provider's model settings."""
    assert pydantic_backend.prompt_cache_settings(model, ttl) is None


def test_an_unrecognized_setting_returns_none_defensively():
    """`config.resolved_prompt_cache` is what actually refuses a value outside `off|5m|1h`, before
    a worker ever boots on one — this is the defensive floor underneath that refusal, not a second
    copy of it: a spelling neither `config.py` validated nor this function recognizes fails SAFE
    (no caching) rather than raising mid-run or caching under a TTL Anthropic never agreed to."""
    assert pydantic_backend.prompt_cache_settings(ANTHROPIC_MODEL, "bogus") is None


def test_every_value_config_accepts_agrees_with_the_helper_on_which_ones_cache():
    """**The contract, checked rather than assumed.** `config.resolved_prompt_cache`'s own
    docstring promises `from_args` and this helper "can never disagree about which values are
    valid" — a promise `config.PROMPT_CACHE_TTLS` is what makes true: both modules read the SAME
    tuple instead of two hand-copied ones. Swept over `config.PROMPT_CACHE_VALUES` itself, never a
    retyped `["off", "5m", "1h"]`, so a third TTL added to one side and not the other fails HERE —
    where the mismatch is a one-line diff to read — rather than in a worker that boots on a value
    this function silently declines to honor.
    """
    for value in config.PROMPT_CACHE_VALUES:
        result = pydantic_backend.prompt_cache_settings(ANTHROPIC_MODEL, value)
        assert (result is None) == (value == "off"), (
            f"prompt_cache_settings({ANTHROPIC_MODEL!r}, {value!r}) returned {result!r} — every "
            f"value config.py accepts must produce a dict except 'off'")


def test_the_helper_needs_no_agent_framework_import(monkeypatch):
    """Pure means pure: this function's whole body must run with `pydantic_ai` never imported —
    the `index.md` "Avoid" rule this package holds everywhere else, applied to a helper the module
    docstring says needs no framework import at all."""
    import ast
    import inspect

    source = inspect.getsource(pydantic_backend.prompt_cache_settings)
    tree = ast.parse(source)
    names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    names |= {alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names}
    assert not [n for n in names if n == "pydantic_ai" or n.startswith("pydantic_ai.")]


# ── the wire shape: a real AnthropicModel, offline through a MockTransport ─────────────────────
def _drive_anthropic_agent(model_settings):
    """A REAL pydantic-ai `Agent` over a REAL `AnthropicModel`, answered by a `MockTransport`
    closure — returns the JSON body the transport actually received. `pydantic_ai` is imported
    HERE, inside this function body, never at module scope.
    """
    import httpx
    from pydantic_ai import Agent
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-5", "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        })

    provider = AnthropicProvider(
        api_key="sk-test-fake",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    model = AnthropicModel("claude-sonnet-4-5", provider=provider)
    agent = Agent(model, instructions="be terse", model_settings=model_settings)

    @agent.tool_plain
    def search_pages(query: str) -> str:
        """A stand-in tool, so tool-DEFINITION caching has a tool to attach to — the ordinary run
        always registers five real ones."""
        return "ok"

    agent.run_sync("hi there")
    return seen["body"]


def test_the_helpers_dict_really_puts_cache_control_on_the_wire():
    """**Never fake what you are claiming to prove.** The settings dict `prompt_cache_settings`
    builds is exercised through a REAL `AnthropicModel`/`Agent`, so this proves pydantic-ai's own
    request-building code turns it into `cache_control` blocks — not merely that the dict is shaped
    like the framework's field names."""
    settings = pydantic_backend.prompt_cache_settings(ANTHROPIC_MODEL, "5m")

    body = _drive_anthropic_agent(settings)

    # one block each: the system prompt, the one registered tool, the one user message
    assert json.dumps(body).count("cache_control") == 3, body
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


def test_benign_twin_off_reaches_the_wire_with_no_cache_control_at_all():
    """The specificity half: `off` (`prompt_cache_settings` returning `None`, the same `None`
    `Agent(model_settings=...)` defaults to on its own) must produce a body with NO `cache_control`
    anywhere — not one block left over from some default the framework applies unasked."""
    settings = pydantic_backend.prompt_cache_settings(ANTHROPIC_MODEL, "off")
    assert settings is None

    body = _drive_anthropic_agent(settings)

    assert "cache_control" not in json.dumps(body)


# ── the ordinary flow's Agent(...) call actually receives the helper's result ──────────────────
def test_run_wires_the_pure_helpers_result_into_its_own_agent_as_model_settings(tmp_path):
    """**The positive counterpart to the meeting flow's exclusion pin below.** Every other test in
    this file proves `prompt_cache_settings` computes the right dict in isolation, or that
    pydantic-ai turns THAT dict into real `cache_control` blocks — none of them proves `_run`
    actually calls it with `self.settings`' own two fields and hands the result to its own
    `Agent(...)` as `model_settings=`. A refactor that dropped that keyword (or hard-coded `None`,
    or read a stale copy of the settings) would leave every other test in this file green — the
    pure-helper tests build no `Agent` at all, and the wire-shape tests build their OWN, never
    through `_run` — and only this test would catch it.

    Same technique as the meeting flow's own pin: `pydantic_ai.Agent` monkeypatched at its own
    module attribute, so `_run`'s `from pydantic_ai import Agent` (re-read at call time) resolves
    to a double that records its construction kwargs and raises before any model is actually
    called.
    """
    import pydantic_ai

    captured = {}

    class _RecordingAgent:
        def __init__(self, model, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop after recording the construction kwargs")

    original_agent = pydantic_ai.Agent
    pydantic_ai.Agent = _RecordingAgent
    try:
        env = support.build_repo(str(tmp_path / "git"))
        settings = support.build_settings(env, worktree_root=str(tmp_path / "worktrees"),
                                          backend="pydantic", model=ANTHROPIC_MODEL,
                                          prompt_cache="5m")
        agent = pydantic_backend.PydanticFilingAgent(settings, model_factory=lambda: object())

        with pytest.raises(AgentError):
            agent.run(worktree=env.repo, material="a captured note, padded well past nothing",
                      hints={}, submitted_by=support.DEFAULT_SUBMITTER)
    finally:
        pydantic_ai.Agent = original_agent

    assert captured.get("model_settings") == {
        "anthropic_cache_instructions": "5m",
        "anthropic_cache_tool_definitions": "5m",
        "anthropic_cache_messages": "5m",
    }, (
        f"the ordinary flow's Agent(...) call carried model_settings={captured.get('model_settings')!r} "
        f"— prompt caching wires the pure helper's result here, and this pins that wiring")


# ── the meeting flow's exclusion is a DECISION, and decisions get a pin ────────────────────────
def test_run_meeting_builds_its_agent_with_no_model_settings_at_all(tmp_path):
    """`_run_meeting` is untouched by prompt caching on purpose: one request per capture means a cache
    WRITE with no read ever — a pure surcharge (Anthropic charges 1.25x base input to write a
    cache entry) — so caching this flow would only ever cost money. Pinned on the actual
    construction call, not on reading the source, the same way any other backend decision here
    earns a test rather than a comment nothing runs.

    `pydantic_ai.Agent` is monkeypatched at its OWN module attribute: `_run_meeting`'s
    `from pydantic_ai import Agent` re-reads that attribute at call time, so patching it there is
    equivalent to patching the call site without touching `pydantic_backend.py` itself.
    """
    import pydantic_ai

    captured = {}

    class _RecordingAgent:
        def __init__(self, model, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop after recording the construction kwargs")

    import_patch_target = pydantic_ai
    original_agent = import_patch_target.Agent
    import_patch_target.Agent = _RecordingAgent
    try:
        env = support.build_repo(str(tmp_path / "git"))
        settings = support.build_settings(env, worktree_root=str(tmp_path / "worktrees"),
                                          backend="pydantic", model=ANTHROPIC_MODEL,
                                          prompt_cache="5m")
        deps = support.build_deps(env, settings)
        agent = pydantic_backend.PydanticFilingAgent(settings, model_factory=lambda: object())

        with pytest.raises(AgentError):
            agent.run_meeting(worktree=env.repo, material="a transcript, padded past nothing",
                              meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                              registry=deps.registry,
                              source_page_path="sources/meetings/q3-sync.md")
    finally:
        import_patch_target.Agent = original_agent

    assert "model_settings" not in captured, (
        f"the meeting flow's Agent(...) call carried model_settings={captured.get('model_settings')!r} "
        f"— prompt caching excludes this flow deliberately, and this pins that exclusion")
