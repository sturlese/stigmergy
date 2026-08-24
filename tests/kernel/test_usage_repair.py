"""`kernel.usage_repair`: the shim for pydantic-ai 2.13.0's silently-ZERO token extraction.

A paid trial found this on the first real measurement. `RequestUsage.extract` asks genai-prices for
the counts and constructs its dataclass from whatever came back — and for any OpenAI model
reporting `output_tokens_details.reasoning_tokens`, genai-prices returns an
`output_reasoning_tokens` field `RequestUsage` does not declare. The `TypeError` lands inside the
method's own `except Exception: pass` provider-fallback loop, all three candidates fail identically,
and the method returns a `RequestUsage` with every count at zero. No warning, no log line, no
exception. A real, paid response reports as if it had cost nothing.

That is the exact failure `librarian.pricing` was built to prevent — a zero that reads as free —
arrived at from below, and it also zeroes `audit_log.result.usage`, the counters the suppression-gated retry put
there so a model-policy decision starts from recorded numbers.

**Two halves, and the first one is a tripwire rather than a test of our code.**

The reproduction runs in a FRESH INTERPRETER, in a subprocess, and asserts that the unpatched
framework really does extract zero. It has to: the shim replaces `RequestUsage.extract` on the
class and keeps the original only in a closure, so once anything in a pytest process has installed
it there is no way back — and whether that has happened depends on which suites ran first, which is
exactly the order-dependence a test must not have. A subprocess makes the answer the same however
this file is invoked. `tests/librarian/test_worker_signals.py` drives a real separate OS process
for the same reason: some properties are not observable from inside.

**When that half fails, the framework has fixed itself and this whole shim retires.** That is the
removal condition from `usage_repair`'s docstring, made executable — the one form in which a
"delete this when upstream fixes it" note does not quietly outlive its reason.

Keyless throughout, and no network: genai-prices ships its price snapshot in the package, and every
payload here is a canned real one.
"""
import dataclasses
import json
import pathlib
import subprocess
import sys

import pytest

from stigmergy.kernel import usage_repair
from stigmergy.kernel.usage_repair import ensure_usage_extraction_repaired

ROOT = pathlib.Path(__file__).resolve().parents[2]

# ── the payloads, in the shape the framework's own call site builds ────────────────────────────
# `models/openai.py::_map_usage` builds `dict(model=…, usage=<usage.model_dump(exclude_none=True)>)`
# and passes `details` alongside. Both are reproduced exactly; a payload of a shape the real caller
# never produces would be testing a scenario nobody can reach.
#
# The counts are a real meeting distillation's order of magnitude rather than a toy: the arithmetic
# test below turns them into dollars, and a two-token payload would round to nothing and prove
# nothing about the failure being prevented.
CRASH_PAYLOAD = {
    "model": "gpt-5.6-terra",
    "usage": {"input_tokens": 1081, "input_tokens_details": {"cached_tokens": 400},
              "output_tokens": 1250, "output_tokens_details": {"reasoning_tokens": 384},
              "total_tokens": 2331},
}
# What the framework itself passes as `details` for a Responses call — its own curated view.
CRASH_DETAILS = {"reasoning_tokens": 384}

# The Chat-Completions shape, which carries no `output_tokens_details` and therefore never trips
# the defect. This is the control: the shim must not change it by a single field.
HEALTHY_PAYLOAD = {"model": "gpt-4o",
                   "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}

RESPONSES = dict(provider="openai", provider_url="https://api.openai.com/v1",
                 provider_fallback="openai", api_flavor="responses")
CHAT = dict(provider="openai", provider_url="https://api.openai.com/v1",
            provider_fallback="openai", api_flavor="chat")
NO_PROVIDER = dict(provider="nothing-known", provider_url="https://nowhere.invalid",
                   provider_fallback="nothing-known", api_flavor="responses")

# The counts the provider really reported, read off the payload rather than retyped — the
# arithmetic below derives from these, so a payload edit cannot leave a stale expectation behind.
REPORTED_INPUT = CRASH_PAYLOAD["usage"]["input_tokens"]
REPORTED_CACHED = CRASH_PAYLOAD["usage"]["input_tokens_details"]["cached_tokens"]
REPORTED_OUTPUT = CRASH_PAYLOAD["usage"]["output_tokens"]
REPORTED_REASONING = CRASH_PAYLOAD["usage"]["output_tokens_details"]["reasoning_tokens"]


# ── the tripwire: what the UNPATCHED framework does ────────────────────────────────────────────
_ORIGINAL_PROBE = """
import json, sys
from pydantic_ai.usage import RequestUsage

CRASH = json.loads(sys.argv[1])
DETAILS = json.loads(sys.argv[2])
HEALTHY = json.loads(sys.argv[3])


def counts(usage):
    return {name: getattr(usage, name) for name in
            ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")}


out = {}
crash = RequestUsage.extract(CRASH, provider="openai",
                             provider_url="https://api.openai.com/v1",
                             provider_fallback="openai", api_flavor="responses",
                             details=dict(DETAILS))
out["crash_counts"] = counts(crash)
out["crash_details"] = crash.details
healthy = RequestUsage.extract(HEALTHY, provider="openai",
                               provider_url="https://api.openai.com/v1",
                               provider_fallback="openai", api_flavor="chat")
out["healthy_counts"] = counts(healthy)
out["healthy_details"] = healthy.details
none = RequestUsage.extract(CRASH, provider="nothing-known",
                            provider_url="https://nowhere.invalid",
                            provider_fallback="nothing-known", api_flavor="responses")
out["no_provider_counts"] = counts(none)
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def original():
    """What `RequestUsage.extract` answers with the shim NOT installed, from a fresh interpreter.

    Module-scoped: one subprocess for the whole file. `sys.executable` rather than a hardcoded
    path, so this runs against the same virtualenv pytest is in — a probe against a different
    interpreter would be asking a different installation's question.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _ORIGINAL_PROBE, json.dumps(CRASH_PAYLOAD),
         json.dumps(CRASH_DETAILS), json.dumps(HEALTHY_PAYLOAD)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"the unpatched probe did not run:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


@pytest.fixture()
def installed():
    """The shim, installed. Idempotent, so this is safe however many tests ask for it — and it is
    never uninstalled, because the wrapper defers to the original and cannot worsen an answer."""
    ensure_usage_extraction_repaired()
    from pydantic_ai.usage import RequestUsage
    return RequestUsage


def test_the_unpatched_framework_extracts_zero_for_a_reasoning_response(original):
    """**THE DEFECT, in the shape it was found in — and the shim's removal condition.**

    Every count zero, no exception raised, and the caller's own `details` passed through intact so
    nothing downstream looks wrong. A run that cost real money reports as free.

    **If this test fails, pydantic-ai has fixed the kwarg mismatch and `kernel/usage_repair.py`
    should be DELETED along with its three call sites** — not repaired, not adjusted. That is the
    whole reason it is written as an assertion about the framework rather than a comment.
    """
    assert original["crash_counts"] == {"input_tokens": 0, "cache_read_tokens": 0,
                                        "cache_write_tokens": 0, "output_tokens": 0}
    assert original["crash_details"] == CRASH_DETAILS, (
        "the details survived, which is what makes the zeros so easy to miss: the object looks "
        "populated")


def test_genai_prices_reports_the_field_the_framework_cannot_construct():
    """The cause, named rather than inferred. genai-prices extracts a field `RequestUsage` does not
    declare, and the framework's `cls(**extracted)` raises `TypeError` on it inside its own
    swallowing loop. This asserts the raw extraction really does carry that field and really does
    carry the counts — so the zeros above are a construction failure, not missing data.

    Not affected by the shim: it wraps `RequestUsage.extract`, never genai-prices.
    """
    from genai_prices.data_snapshot import get_snapshot

    provider = get_snapshot().find_provider(None, None, RESPONSES["provider_url"])
    _model_ref, extracted = provider.extract_usage(CRASH_PAYLOAD, api_flavor="responses")
    values = vars(extracted)

    assert values["input_tokens"] == REPORTED_INPUT
    assert values["output_tokens"] == REPORTED_OUTPUT
    assert "output_reasoning_tokens" in values, (
        "genai-prices no longer reports the field the framework cannot accept — re-read the "
        "defect before trusting this shim")

    from pydantic_ai.usage import RequestUsage
    declared = {field.name for field in dataclasses.fields(RequestUsage)}
    assert "output_reasoning_tokens" not in declared, (
        "RequestUsage grew the field — the framework fixed itself and the shim retires")


# ── what the repair recovers ───────────────────────────────────────────────────────────────────
def test_the_repair_recovers_the_counts_the_provider_actually_reported(installed):
    """The fix, at the seam. These are the numbers the response carried, and after the shim they
    are the numbers the framework hands back."""
    usage = installed.extract(CRASH_PAYLOAD, details=dict(CRASH_DETAILS), **RESPONSES)

    assert usage.input_tokens == REPORTED_INPUT
    assert usage.output_tokens == REPORTED_OUTPUT
    assert usage.cache_read_tokens == REPORTED_CACHED


def test_the_field_the_framework_could_not_hold_is_kept_rather_than_dropped(installed):
    """`output_reasoning_tokens` is why the construction failed, and dropping it to make the
    construction succeed would trade one silent loss for another. It goes to `details`, which is
    where the dataclass keeps everything it does not declare a field for."""
    usage = installed.extract(CRASH_PAYLOAD, details=dict(CRASH_DETAILS), **RESPONSES)

    assert usage.details["output_reasoning_tokens"] == REPORTED_REASONING
    # ...and the framework's own curated detail is still there beside it
    assert usage.details["reasoning_tokens"] == CRASH_DETAILS["reasoning_tokens"]


def test_the_callers_own_details_win_a_key_collision(installed):
    """The caller's `details` is the framework's curated view of the response; ours is whatever the
    provider object happened to carry under the same name. A shim that overwrote the caller would
    be changing an answer it was only supposed to complete."""
    theirs = {"output_reasoning_tokens": "the caller's own value", "reasoning_tokens": 384}

    usage = installed.extract(CRASH_PAYLOAD, details=dict(theirs), **RESPONSES)

    assert usage.details["output_reasoning_tokens"] == "the caller's own value"
    assert usage.input_tokens == REPORTED_INPUT, "recovering the counts is unaffected either way"


# ── what the repair must NOT change ────────────────────────────────────────────────────────────
def test_a_healthy_payload_comes_back_exactly_as_the_original_answered_it(installed, original):
    """**The benign twin, and the one that decides whether this shim is safe to install
    process-wide.** A Chat-Completions response never trips the defect, so the original's own
    extraction works — and the wrapper must return that answer untouched, field for field, rather
    than substituting its own.

    Compared against the ORIGINAL's real answer from the fresh interpreter, not against a
    hand-typed expectation that could agree with a broken shim.
    """
    usage = installed.extract(HEALTHY_PAYLOAD, **CHAT)

    assert {name: getattr(usage, name) for name in original["healthy_counts"]} == \
        original["healthy_counts"]
    assert usage.details == original["healthy_details"]
    assert usage.input_tokens > 0, "the control payload must actually extract something"


def test_a_payload_no_provider_matches_returns_the_originals_answer_and_does_not_raise(
        installed, original):
    """The repair runs the same three-candidate provider loop, so a payload no candidate matches
    reaches the end of it. That must be the original's own answer and an ordinary return — a shim
    that raised here would turn a successful, already-paid model call into a failed item over
    telemetry."""
    usage = installed.extract(CRASH_PAYLOAD, **NO_PROVIDER)

    assert {name: getattr(usage, name) for name in original["no_provider_counts"]} == \
        original["no_provider_counts"]


def test_a_repair_path_that_blows_up_leaves_the_originals_answer_standing(installed, monkeypatch):
    """Every exception on the repair road is swallowed, and the docstring's reason is the one that
    matters: this can improve a figure and must never fail a paid call. Forced by making the
    snapshot lookup itself raise — the one dependency the repair has.

    The import is inside `_repair`, so patching the module attribute is what the real call
    resolves.
    """
    import genai_prices.data_snapshot as snapshot

    def _explode():
        raise RuntimeError("the price snapshot is unreadable")

    monkeypatch.setattr(snapshot, "get_snapshot", _explode)

    usage = installed.extract(CRASH_PAYLOAD, details=dict(CRASH_DETAILS), **RESPONSES)

    assert usage.input_tokens == 0, "with the repair unable to run, the original's answer stands"
    assert usage.details == CRASH_DETAILS


def test_a_genuinely_empty_response_stays_empty_rather_than_being_invented(installed):
    """The other reason every count can be zero, and the shim must not confuse the two: a response
    that really did report nothing. Every provider candidate matches and every one of them extracts
    zeros, so the loop exhausts and the original's answer stands — an all-zero `RequestUsage`,
    which is the truth here.

    This is what keeps the repair from being a source of numbers: it only ever hands back counts a
    provider actually reported.
    """
    empty = {"model": "gpt-5.6-terra",
             "usage": {"input_tokens": 0, "input_tokens_details": {"cached_tokens": 0},
                       "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0},
                       "total_tokens": 0}}

    usage = installed.extract(empty, details={"reasoning_tokens": 0}, **RESPONSES)

    assert usage.input_tokens == 0 and usage.output_tokens == 0
    assert usage.details == {"reasoning_tokens": 0}, (
        "an empty response must not acquire fields the repair went looking for")


def test_the_repair_does_not_require_genai_prices_to_be_importable(installed, monkeypatch):
    """The outermost guard, and the scenario it is for: the price snapshot package unavailable or
    broken at the moment a paid call returns.

    `pydantic-ai` can be installed without a usable `genai_prices`, and the shim reaches for it
    directly. If that import could escape, a shim that exists to IMPROVE a cost figure would
    instead turn every successful model call into a crash — strictly worse than the zeros it
    repairs. Forced with the real import machinery (`None` in `sys.modules` is what Python itself
    raises `ImportError` on), then restored, with the recovery re-asserted afterwards so this test
    cannot leave the module poisoned for its neighbours.
    """
    monkeypatch.setitem(sys.modules, "genai_prices.data_snapshot", None)

    usage = installed.extract(CRASH_PAYLOAD, details=dict(CRASH_DETAILS), **RESPONSES)

    assert usage.input_tokens == 0, "with no snapshot to consult, the original's answer stands"
    assert usage.details == CRASH_DETAILS

    monkeypatch.undo()
    recovered = installed.extract(CRASH_PAYLOAD, details=dict(CRASH_DETAILS), **RESPONSES)
    assert recovered.input_tokens == REPORTED_INPUT, (
        "the repair did not come back after the import was restored")


# ── idempotency ────────────────────────────────────────────────────────────────────────────────
def test_installing_twice_leaves_one_wrapper_and_not_two(installed):
    """Three call sites reach for this repair and any of them may run first, so a second call must
    be a no-op rather than a wrapper around a wrapper — which would double every extraction and, on
    a fixed framework, hide the deferral this shim retires by.

    Asserted on the FUNCTION IDENTITY, not on behaviour: two stacked wrappers would still produce
    the right numbers, so a behavioural check would pass while the layering was wrong.
    """
    from pydantic_ai.usage import RequestUsage

    before = RequestUsage.__dict__["extract"].__func__
    assert ensure_usage_extraction_repaired() is False, (
        "the `installed` fixture already installed it; a second call must report no install")
    after = RequestUsage.__dict__["extract"].__func__

    assert before is after
    assert getattr(RequestUsage.extract, usage_repair._REPAIR_MARKER, False) is True


def test_the_marker_is_what_makes_the_check_cheap_and_it_is_on_the_wrapper(installed):
    """The guard is one `getattr` on a bound method, so calling `ensure` on every agent
    construction costs nothing. That only holds if the marker is on the installed function itself
    rather than tracked in module state, which a second import of this module would not see."""
    from pydantic_ai.usage import RequestUsage

    wrapper = RequestUsage.__dict__["extract"].__func__
    assert getattr(wrapper, usage_repair._REPAIR_MARKER, False) is True
    assert wrapper.__closure__, "the original must be kept, or the wrapper cannot defer to it"


# ── the three call sites really call it ────────────────────────────────────────────────────────
# Each site imports the function INSIDE its own function body (the librarian's is inside a lazily
# imported block the architecture test polices), so patching the module attribute is exactly what
# the real call resolves — a spy, not a stand-in for the repair.
@pytest.fixture()
def spy(monkeypatch):
    calls = []
    monkeypatch.setattr(usage_repair, "ensure_usage_extraction_repaired",
                        lambda: calls.append(1) or False)
    return calls


def test_the_kernel_model_builder_installs_the_repair(spy, monkeypatch):
    from stigmergy.kernel import llm

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    llm.build_model(llm.ANSWER_MODEL)

    assert spy == [1]


def test_the_kernel_model_builder_refuses_before_framework_setup(spy, monkeypatch):
    from stigmergy.kernel import llm

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        llm.build_model(llm.ANSWER_MODEL)

    assert spy == []


def test_the_answer_synthesizer_installs_the_repair(spy, monkeypatch):
    import types

    from stigmergy.answer import synthesize

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    settings = types.SimpleNamespace(
        llm="openrouter", model="openrouter:z-ai/glm-5.2"
    )

    synthesize.build_synthesizer(settings)

    assert spy


def test_the_offline_answer_path_installs_nothing(spy):
    """The specificity half: `ANSWER_LLM=fake` builds no agent and touches no framework, so it must
    not drag a framework patch into a keyless process. A shim installed by the fake path would make
    the whole suite's import graph claim a dependency the offline path does not have."""
    import types

    from stigmergy.answer import synthesize

    synthesize.build_synthesizer(types.SimpleNamespace(llm="fake"))

    assert spy == []


def test_the_knowledge_planner_installs_the_repair(spy, tmp_path):
    from pydantic_ai.models.test import TestModel

    from stigmergy.knowledge.planner import PydanticPlanner

    brief = pathlib.Path(tmp_path, ".claude", "skills", "librarian", "SKILL.md")
    brief.parent.mkdir(parents=True)
    frozen = (ROOT / "tests" / "librarian" / "fixtures" / "repo" / ".claude" / "skills"
              / "librarian" / "SKILL.md")
    brief.write_text(frozen.read_text(encoding="utf-8"), encoding="utf-8")

    settings = type("Settings", (), {"model": "openrouter:deepseek/deepseek-v4-flash", "timeout_s": 5,
                                     "max_turns": 1})()
    planner = PydanticPlanner(settings, model_factory=lambda: TestModel())
    try:
        planner.repair(worktree=str(tmp_path), violations=())
    except Exception:  # noqa: BLE001
        pass

    assert spy == [1]


def test_every_module_that_builds_a_pydantic_ai_agent_installs_the_repair():
    """**The blindness guard.** One classmethod is shared by every consumer in the process, so a
    FOURTH agent-construction site added later would work perfectly and quietly report zeros
    whenever it happened to run first.

    Derived from the source: the modules importing the `Agent` symbol are exactly the modules
    importing the repair. A module that merely catches a framework exception or reads
    `UsageLimits` (`answer.service` among them) builds nothing and is
    correctly absent from both sides.
    """
    import re

    src = ROOT / "src" / "stigmergy"
    builders, repairers = set(), set()
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*from pydantic_ai import .*\bAgent\b", text, re.M) or \
                re.search(r"^\s*from pydantic_ai\.models\.\w+ import", text, re.M):
            builders.add(path.name)
        if "ensure_usage_extraction_repaired" in text and path.name != "usage_repair.py":
            repairers.add(path.name)

    assert builders, "no module builds a pydantic-ai agent — this guard has gone blind"
    assert builders == repairers, (
        f"these build a pydantic-ai agent and do not install the usage repair: "
        f"{sorted(builders - repairers)}; these install it and build nothing: "
        f"{sorted(repairers - builders)}")
