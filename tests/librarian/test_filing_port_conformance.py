"""Every backend really does answer `filing_port.FilingAgent`, method for method and argument for
argument — keylessly, so the claim is checked on every run rather than on the one that has a key.

The seam used to be a CONVENTION: the first two backends and `processing.py` agreed about two
signatures and one envelope, and nothing stated it anywhere a THIRD implementation could read. ADR
032 wrote it down as a port; this file is what makes the writing load-bearing — and the port has
since outlived one of its implementations without `processing.py` changing a line.

**`isinstance` is not the test, it is the cheapest third of it.** A `runtime_checkable` Protocol
checks that the two methods are PRESENT and nothing else — not their argument names, not that they
are keyword-only, not their defaults. A backend whose `run_meeting` took `worktree` positionally,
or spelled it `work_tree`, or defaulted `corrective` to `None` instead of `""`, passes `isinstance`
and fails at the one call site in `processing.py`, mid-item, against a real queue row. So the
signatures are compared to the Protocol's own — the thing the port DOCUMENTS — and the twins below
prove each half of that comparison can actually fail.

**Keyless throughout.** The signature half constructs every backend and calls none of them (a
backend's `__init__` stores settings and little else). The envelope-semantics half at the bottom
does run the two calls — against an offline model, a scratch brief and a real scratch git repo,
because what a backend HANDS BACK cannot be read off a signature. No agent framework is
loaded by any of it: the `pydantic` backend imports its own inside the method. **Every
`pydantic` run below is driven through an injected
`model_factory`**, never through the configured id — a run that resolved the real model would
reach the network on any machine that happens to export a provider key, which is the one way a
keyless suite stops being keyless by accident rather than by decision.

**The port grew a third member in ADR 033 and a fourth in ADR 034**, and neither is a method:
`structured_ordinary` (which half of the outcome envelope is owed, and who writes the page) and
`wants_gathered` (whether the deterministic gatherer runs before the call). A `runtime_checkable`
Protocol counts an annotated attribute as a member, so `isinstance` checks both — which is why
every hand-written stand-in below declares them. That is not bookkeeping: a stand-in that satisfied
the port WITHOUT a declaration would be a backend the worker reads as `False` on both counts, and
the tests written around it would measure a shape nothing ships.

**They are two declarations because the two questions came apart.** M2's ordinary backend was
structured AND gathered-for; M4's writes its own page AND is gathered-for, because the gathered
block is the SEED its tools go further from. Deriving either from the other would have made "it
explores" mean "it starts from nothing" — a real backend running blind, scoring like a bad model.
"""
import dataclasses
import inspect
import math
import pathlib

import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import config, filing_port
from stigmergy.librarian.double import DoubleAgent
from stigmergy.librarian.errors import AgentError, LibrarianConfigError, OutcomeShapeError
from stigmergy.librarian.filing_port import AgentRun, FilingAgent
from stigmergy.librarian.pydantic_backend import (
    FilingAccount,
    OrdinaryAnchoring,
    OrdinaryPage,
    PydanticFilingAgent,
)
from tests.librarian import support

# The two calls `processing.py` makes on `Deps.agent`, and the only two it may make.
PORT_METHODS = ("run", "run_meeting")

# The port's non-method members (ADR 033, ADR 034) — asserted as part of the protocol's own
# attribute set below rather than assumed, because `isinstance` behaviour depends on them being
# annotations rather than comments.
PORT_DECLARATIONS = ("structured_ordinary", "wants_gathered")

# Every implementation the port claims, named by the backend id that dispatches to it. Derived from
# `agent.BACKENDS` in the test below rather than trusted here: a third backend added to that tuple
# and forgotten here would leave this whole file silently measuring two of three.
BACKEND_CLASSES = {
    "double": DoubleAgent,
    agent_module.PYDANTIC_BACKEND: PydanticFilingAgent,
}


def _settings() -> config.Settings:
    """A `Settings` nothing here runs against — the backends are constructed, never called."""
    return config.Settings(repo="/nonexistent/knowledge-repo", model="openai:gpt-5.6-terra")


@pytest.fixture(params=sorted(BACKEND_CLASSES), ids=sorted(BACKEND_CLASSES))
def backend(request):
    return BACKEND_CLASSES[request.param](_settings())


# ── the port covers every backend, and every backend the port covers still exists ──────────────
def test_the_conformance_set_is_exactly_the_backends_dispatch_knows_about():
    """The blindness guard, first: this file is a claim about EVERY implementation of the port, and
    a third backend added to `agent.BACKENDS` without a line here would make that claim false while
    every test below stayed green. Derived from the production tuple, never retyped."""
    assert set(BACKEND_CLASSES) == set(agent_module.BACKENDS)


@pytest.mark.parametrize("name", sorted(BACKEND_CLASSES))
def test_build_agent_returns_the_backend_this_file_conforms(name):
    """The other end of the same wire: the classes below are the ones `build_agent` actually hands
    `processing.Deps`. Conformance proven about a class nothing constructs would be conformance
    proven about nothing.

    The settings carry a PRICED, provider-prefixed model for every branch, not only the one that
    needs it: `PydanticFilingAgent.__init__` prices its configured id at construction (the backstop
    below `worker.startup_checks`), so a `Settings` carrying a bare model id would
    refuse here for a reason that has nothing to do with the port. The double ignores the field.
    """
    built = agent_module.build_agent(dataclasses.replace(_settings(), backend=name))
    assert isinstance(built, BACKEND_CLASSES[name])
    assert isinstance(built, FilingAgent)


def test_constructing_the_token_priced_backend_refuses_an_unpriced_model():
    """The construction backstop itself, stated where the port's other constructors are.

    `worker.startup_checks` is the loud road and stays the loud road; this is the one point that
    cannot be reached around, because it is the object that will spend the money. Without it a
    caller who builds `Deps` by hand — every test in this suite, and the eval rig — could run, pay,
    and only then discover the run cannot say what it cost.
    """
    with pytest.raises(LibrarianConfigError, match="openai:gpt-9"):
        PydanticFilingAgent(dataclasses.replace(_settings(), model="openai:gpt-9"))


@pytest.mark.parametrize("name", ["double"])
def test_the_backends_that_price_themselves_construct_whatever_the_model_says(name):
    """The specificity half: the price backstop belongs to the backend that computes cost from
    tokens, and only to it. The double spends nothing, so requiring a priced id of it would refuse
    a configuration nothing was going to use. A one-item parametrize on purpose: the retired
    backend was priced by its own harness and was the second arm here, and the next self-pricing
    backend joins this list rather than getting a test of its own."""
    built = agent_module.build_agent(
        dataclasses.replace(_settings(), backend=name, model="a-model-nothing-prices"))
    assert isinstance(built, BACKEND_CLASSES[name])


def test_an_unknown_backend_fails_fast_rather_than_falling_through_to_one_of_them():
    """The dispatch's own refusal, and it names them all so a typo is one line from fixed. A
    fall-through would silently pick the real path or the double — the two outcomes
    `build_agent`'s docstring exists to rule out."""
    with pytest.raises(LibrarianConfigError) as exc_info:
        agent_module.build_agent(config.Settings(repo="/nonexistent", backend="pydanitc"))
    message = str(exc_info.value)
    assert "pydanitc" in message
    for name in agent_module.BACKENDS:
        assert name in message


# ── isinstance: the two methods AND the one declaration are present ────────────────────────────
def test_every_backend_satisfies_the_runtime_checkable_port(backend):
    assert isinstance(backend, FilingAgent)


def test_the_port_really_does_carry_the_declaration_isinstance_now_depends_on():
    """The blindness guard for everything below it. `structured_ordinary` is an ANNOTATION on the
    Protocol, and a `runtime_checkable` Protocol counts annotated names among its members — which
    is the only reason the stand-ins below have to declare one. If it were ever demoted to a plain
    comment, `isinstance` would stop checking it, every stub here would still pass, and a backend
    that never declared its shape would sail through the port claiming conformance it does not
    have.
    """
    try:
        # 3.13+ only. CI pins 3.12 ("the interpreter the imported suites were green on",
        # `.github/workflows/ci.yml`) and `requires-python` is `>=3.12`, so the stdlib spelling
        # alone made this test a local-only check that could not run where it matters most.
        # `typing_extensions` is guaranteed present — pydantic requires it — and its
        # implementation is the one the stdlib adopted, returning the same member set.
        from typing import get_protocol_members
    except ImportError:                              # pragma: no cover — taken on 3.12, not here
        from typing_extensions import get_protocol_members

    members = get_protocol_members(FilingAgent)
    assert set(PORT_METHODS) | set(PORT_DECLARATIONS) == members


@pytest.mark.parametrize("name", sorted(BACKEND_CLASSES))
def test_every_backend_declares_which_shape_of_the_ordinary_flow_it_answers(name):
    """The declarations are read off the backend by `processing._one_pass`, and each one selects a
    road: `structured_ordinary` decides whether CODE writes the page and which half of the outcome
    envelope is owed, `wants_gathered` decides whether the gatherer runs at all. Both are REFUSED
    when absent rather than defaulted, which is exactly why the VALUES have to be pinned per
    backend here.

    **The pair, not each one alone, is the claim worth pinning.** Both shipped backends write their
    own page today, and they differ on the other axis: the pydantic-ai run is seeded with a
    gathered context and then goes further with its tools (ADR 034), while the offline double is
    directive-driven and is handed none. A test asserting only `structured_ordinary` would go green
    on a backend that had quietly lost its seed.
    """
    expected = {"double": (False, False),
                agent_module.PYDANTIC_BACKEND: (False, True)}[name]
    cls = BACKEND_CLASSES[name]
    declared = (cls.structured_ordinary, cls.wants_gathered)
    assert declared == expected, (
        f"{cls.__name__} declares (structured_ordinary, wants_gathered)={declared!r} — `processing` "
        f"branches the whole ordinary flow on these two booleans")


# ── the WRAPPERS, which is where a declared port member actually gets swallowed ────────────────
# A backend is written once and read by everybody; a wrapper is written per test file, per rig and
# per instrument, by somebody thinking about the one thing they are counting. That asymmetry is why
# the port's newest member went missing in ten places at once and in the eval rig — and why the
# refusal in `processing._one_pass` is a REFUSAL rather than a `getattr(..., False)`: a default
# would have made every one of those wrappers report the exploring shape while wrapping a
# structured backend, and the paid golden would have scored that as filing quality.
#
# The two wrappers that ship OUTSIDE a test file are pinned here, as objects, against the real port.
_WRAPPERS = ("evals.run_filing.CountingAgent", "tests.librarian.support.DelayedAgent")


def _wrapped(name: str, inner):
    from evals.run_filing import CountingAgent
    from tests.librarian.support import DelayedAgent

    return {"evals.run_filing.CountingAgent": lambda: CountingAgent(inner),
            "tests.librarian.support.DelayedAgent": lambda: DelayedAgent(inner, 0.0)}[name]()


@pytest.mark.parametrize("name", _WRAPPERS)
@pytest.mark.parametrize("declares", [True, False], ids=["structured", "exploring"])
def test_a_shipped_wrapper_forwards_the_shape_its_inner_backend_declares(name, declares):
    """**Forwarded, not defaulted, and asserted for BOTH values of BOTH members.** A wrapper that
    hardcoded `False` would pass a test written only against the double — which is the shape almost
    every rig wraps — and would silently take the wrong branch behind a real backend. So each
    wrapper is built over an inner that declares `True` and over one that declares `False`, on each
    member, and the values have to come back out.

    The two are varied TOGETHER and inverted against each other on purpose: a wrapper that
    forwarded one member into both attributes would pass a same-value test and is precisely the
    copy-paste this file exists to catch.
    """
    class Inner:
        structured_ordinary = declares
        wants_gathered = not declares

        def run(self, **kwargs):
            return AgentRun()

        def run_meeting(self, **kwargs):
            return AgentRun()

    wrapped = _wrapped(name, Inner())
    assert wrapped.structured_ordinary is declares
    assert wrapped.wants_gathered is (not declares)


@pytest.mark.parametrize("name", _WRAPPERS)
def test_a_shipped_wrapper_around_a_real_backend_is_itself_a_filing_agent(name):
    """The wrapper is what `processing.Deps.agent` actually holds, so the port claim has to be true
    of the WRAPPER and not only of what it wraps. `isinstance` covers all three members now — two
    methods and the declaration — which is exactly why this is worth asserting on the object a
    rig builds rather than on the class it wraps."""
    wrapped = _wrapped(name, DoubleAgent(_settings()))

    assert isinstance(wrapped, FilingAgent)
    assert wrapped.structured_ordinary is False


@pytest.mark.parametrize("name", _WRAPPERS)
def test_a_wrapper_around_a_backend_that_declares_nothing_fails_at_CONSTRUCTION(name):
    """**Where the loud failure has to land, and the whole argument for plain attribute access.**

    `getattr(inner, "structured_ordinary", False)` would build the wrapper happily and hand back a
    lie — the exploring shape, for a backend that never said so. The wrapper reads the attribute
    directly, so a non-conforming inner raises `AttributeError` in the line that builds the rig,
    with the test or the eval run that built it in the traceback, before a single capture is
    claimed.
    """
    class Undeclared:
        def run(self, **kwargs):
            return AgentRun()

        def run_meeting(self, **kwargs):
            return AgentRun()

    with pytest.raises(AttributeError, match="structured_ordinary"):
        _wrapped(name, Undeclared())


def test_a_class_missing_run_meeting_is_not_a_filing_agent():
    """The benign twin's sharp half: `isinstance` is worth running because it can fail. A backend
    that only implements the ordinary flow is exactly the half-backend the meeting dispatch would
    meet with an `AttributeError` mid-item.

    It declares `structured_ordinary` so the ONE thing missing is the method — a stand-in short of
    two members would fail this assertion whichever one the port stopped caring about, and prove
    nothing about either.
    """
    class OrdinaryOnly:
        structured_ordinary = False
        wants_gathered = False

        def run(self, *, worktree, material, hints, submitted_by,
                corrective="", flow_note="", gathered=""):
            return AgentRun()

    assert not isinstance(OrdinaryOnly(), FilingAgent)


def test_a_class_answering_both_calls_but_declaring_no_shape_is_not_one_either():
    """The declaration's own sharp half, and the failure it exists to catch: this stand-in answers
    every call `processing.py` makes and would run — taking the exploring branch by default, which
    is a shape it never claimed. The port refuses it at `isinstance` instead."""
    class Undeclared:
        def run(self, *, worktree, material, hints, submitted_by,
                corrective="", flow_note="", gathered=""):
            return AgentRun()

        def run_meeting(self, *, worktree, material, meeting_meta, registry,
                        source_page_path, corrective=""):
            return AgentRun()

    assert not isinstance(Undeclared(), FilingAgent)


def test_a_class_answering_both_calls_is_a_filing_agent_however_it_was_written():
    """...and its benign half: conformance is STRUCTURAL. Nothing inherits, nothing registers, and
    a class written by somebody who never read `filing_port.py` conforms the moment it answers the
    two calls and declares its shape — which is what lets the test doubles this suite is built on
    exercise the same contract a live backend does."""
    class HandWritten:
        structured_ordinary = False
        wants_gathered = False

        def run(self, *, worktree, material, hints, submitted_by,
                corrective="", flow_note="", gathered=""):
            return AgentRun()

        def run_meeting(self, *, worktree, material, meeting_meta, registry,
                        source_page_path, corrective="", gathered=""):
            return AgentRun()

    assert isinstance(HandWritten(), FilingAgent)


# ── the signatures: what `isinstance` cannot see ───────────────────────────────────────────────
@pytest.mark.parametrize("method", PORT_METHODS)
def test_each_backend_matches_the_ports_signature_exactly(backend, method):
    """Names, kinds, defaults and annotations, in one equality — including the return annotation,
    because a backend returning something other than an `AgentRun` is the one defect
    `processing.py` cannot recover from."""
    assert (inspect.signature(getattr(type(backend), method))
            == inspect.signature(getattr(FilingAgent, method))), (
        f"{type(backend).__name__}.{method} has drifted from the port it claims to implement")


@pytest.mark.parametrize("method", PORT_METHODS)
def test_every_argument_of_every_backend_call_is_keyword_only(backend, method):
    """Stated separately from the equality above because it is the property with a REASON: the
    argument lists are long and half of them are strings, so a positional call site is a defect
    waiting for somebody to swap two of them. An equality alone would pin it as a coincidence; this
    pins it as the rule.
    """
    params = list(inspect.signature(getattr(type(backend), method)).parameters.values())
    assert params[0].name == "self"
    positional = [p.name for p in params[1:] if p.kind is not inspect.Parameter.KEYWORD_ONLY]
    assert not positional, (
        f"{type(backend).__name__}.{method} accepts {positional} positionally — the port is "
        f"keyword-only throughout")


@pytest.mark.parametrize("method", PORT_METHODS)
def test_the_optional_arguments_default_the_same_way_everywhere(backend, method):
    """The defaults are the contract too: `processing.py` omits `corrective`/`flow_note` on
    a first, unattached pass, so a backend defaulting one of them to `None` would meet a `None`
    where every other backend meets `""` — and the difference surfaces as a prompt containing the
    word "None"."""
    port = inspect.signature(getattr(FilingAgent, method)).parameters
    mine = inspect.signature(getattr(type(backend), method)).parameters
    expected = {name: p.default for name, p in port.items()
                if p.default is not inspect.Parameter.empty}
    assert expected, "the port declares no optional argument — this test has gone blind"
    assert {name: mine[name].default for name in expected} == expected


def test_the_signature_check_catches_a_positional_worktree_that_isinstance_waves_through():
    """**The sabotage twin, and the whole argument for comparing signatures at all.** This backend
    answers both calls and declares its shape, so `isinstance` says yes — and its `worktree` is
    positional, which is precisely the drift a Protocol cannot see. If the comparison above ever
    stops being able to fail, this test goes red first.

    **Everything else mirrors the port exactly, annotations included**, so the inequality below is
    caused by the sabotage and by nothing else. A stand-in written in shorthand would differ from
    the port for three or four reasons at once and would keep "differing" long after the positional
    argument was the least of them — a sabotage twin that cannot attribute its own failure is a
    green light wearing a warning's clothes.
    """
    class PositionalWorktree:
        structured_ordinary = False
        wants_gathered = False

        def run(self, worktree: str, *, material: str, hints: dict, submitted_by: str,
                corrective: str = "", flow_note: str = "",
                gathered: str = "") -> AgentRun:
            return AgentRun()

        def run_meeting(self, worktree: str, *, material: str, meeting_meta: dict, registry,
                        source_page_path: str, corrective: str = "",
                        gathered: str = "") -> AgentRun:
            return AgentRun()

    assert isinstance(PositionalWorktree(), FilingAgent), (
        "isinstance must still accept it — that is the gap this test exists to cover")
    for method in PORT_METHODS:
        mine = inspect.signature(getattr(PositionalWorktree, method))
        port = inspect.signature(getattr(FilingAgent, method))
        assert mine != port
        # ...and the difference is the KIND of exactly one parameter, nothing else
        assert [p.name for p in mine.parameters.values()] == [p.name for p in port.parameters
                                                              .values()]
        assert [p.name for p in mine.parameters.values()
                if p.kind is not port.parameters[p.name].kind] == ["worktree"]


def test_the_signature_check_catches_a_renamed_argument_too():
    """The second shape the same gap takes, and the likelier one: a backend written from memory
    spells `meeting_meta` `meta`. Keyword-only throughout means the call site raises `TypeError` on
    a real item — this catches it with no key, no queue and no model.

    Its `run` mirrors the port exactly, which is the control: one method drifted, one did not, and
    the assertions below say which is which.
    """
    class Renamed:
        structured_ordinary = False
        wants_gathered = False

        def run(self, *, worktree: str, material: str, hints: dict, submitted_by: str,
                corrective: str = "", flow_note: str = "",
                gathered: str = "") -> AgentRun:
            return AgentRun()

        def run_meeting(self, *, worktree: str, material: str, meta: dict, registry,
                        source_page_path: str, corrective: str = "",
                        gathered: str = "") -> AgentRun:
            return AgentRun()

    assert isinstance(Renamed(), FilingAgent)
    assert (inspect.signature(Renamed.run_meeting)
            != inspect.signature(FilingAgent.run_meeting))
    assert (inspect.signature(Renamed.run)
            == inspect.signature(FilingAgent.run)), (
        "the undrifted method must still match — otherwise this twin proves the comparison is "
        "noisy rather than that it is sharp")


def test_the_signature_check_catches_a_backend_that_never_learned_about_the_gathered_context():
    """ADR 033's own drift shape, and the one this milestone made possible: a backend written
    against the M1 port answers both calls, declares its shape, and has no `gathered` parameter at
    all. `processing._one_pass` passes `gathered=` on EVERY ordinary call — empty for an exploring
    backend — so this one raises `TypeError` on the first item it claims, against a real queue row.
    The equality above is what catches it here instead.

    **BOTH calls now, since ADR-038.** `_one_meeting_pass` passes `gathered=` on every meeting call
    too — unconditionally, because no backend on that flow holds a tool — so a backend predating
    that change is broken on the meeting road exactly as it is on the ordinary one, and asserting
    only the ordinary half would leave the newer of the two drifts uncovered.
    """
    class PreGatherer:
        structured_ordinary = False
        wants_gathered = False

        def run(self, *, worktree: str, material: str, hints: dict, submitted_by: str,
                corrective: str = "", flow_note: str = "") -> AgentRun:
            return AgentRun()

        def run_meeting(self, *, worktree: str, material: str, meeting_meta: dict, registry,
                        source_page_path: str, corrective: str = "") -> AgentRun:
            return AgentRun()

    assert isinstance(PreGatherer(), FilingAgent)
    for method in PORT_METHODS:
        assert (inspect.signature(getattr(PreGatherer, method))
                != inspect.signature(getattr(FilingAgent, method))), method
    with pytest.raises(TypeError):
        PreGatherer().run(worktree="/x", material="m", hints={}, submitted_by="a@b.test",
                          gathered="context")
    with pytest.raises(TypeError):
        PreGatherer().run_meeting(worktree="/x", material="m", meeting_meta={}, registry={},
                                  source_page_path="sources/meetings/x.md", gathered="context")


# ── the envelope and the fault contract, which moved with the port ─────────────────────────────
def test_the_envelope_the_port_owns_is_the_one_every_importer_already_had():
    """`AgentRun` moved from `agent.py` to `filing_port.py` and `agent.AgentRun` stayed a
    re-export. IDENTITY, not equality: a second class with the same fields would make
    `isinstance(run, agent.AgentRun)` false somewhere downstream for no visible reason."""
    assert agent_module.AgentRun is filing_port.AgentRun
    assert agent_module._priced is filing_port.priced


def test_the_envelopes_defaults_are_the_zeroes_the_port_calls_legitimate():
    """A structured backend has no conversational loop and no tool to call, so it reports `0` for
    both rather than inventing a `1` — which is only honest if zero is what the envelope means by
    "not measured here". `cost_usd` defaults to `0.0` for the same reason: an offline double and a
    park re-file both spend nothing, and that is a real answer."""
    run = AgentRun()
    assert (run.outcome, run.turns, run.tool_calls, run.cost_usd, run.stop_reason) == (
        None, 0, 0, 0.0, "")


def test_priced_attaches_the_attempts_spend_to_the_fault_and_hands_the_exception_back():
    """The fault contract: most agent faults fire AFTER the run was priced, and `processing` banks
    the figure off the exception because no `AgentRun` ever returned. Returning the exception is
    what lets a raise site read as one expression."""
    run = AgentRun(cost_usd=0.31)
    ex = AgentError("the meeting agent exceeded its 900s budget")

    returned = filing_port.priced(run, ex)

    assert returned is ex
    assert ex.run_cost_usd == 0.31


def test_a_fault_priced_at_zero_still_carries_the_field():
    """`0.0` is the honest figure for a timeout that never reached a priced response — and the
    FIELD must be present either way, or `processing`'s `getattr(ex, "run_cost_usd", 0.0)` cannot
    tell "nothing was spent" from "nobody attached it"."""
    ex = filing_port.priced(AgentRun(), AgentError("nothing ever arrived"))
    assert ex.run_cost_usd == 0.0


# ── envelope SEMANTICS, which the signatures cannot express ────────────────────────────────────
# Matching signatures make a backend callable; they say nothing about what it may hand back. Two
# properties carry every downstream figure and neither is visible in an `inspect.signature`:
#
#   * a returned `cost_usd` is a real, finite, non-negative dollar amount — `AgentPasses` SUMS these
#     and `_stamp_cost` rounds the sum onto a row that is serialized into a `jsonb` column, where a
#     `NaN` is not valid JSON at all and a negative would credit an operator for filing;
#   * a fault ALWAYS carries `run_cost_usd`, because `processing` banks the spend off the exception
#     and `getattr(ex, "run_cost_usd", 0.0)` cannot tell "spent nothing" from "nobody attached it".
#
# Checked per backend against the faults each one can be made to raise offline, rather than as a
# claim about the port in the abstract.
def _finite_dollars(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def _frozen_brief(*parts: str) -> pathlib.Path:
    return pathlib.Path(__file__).parent.joinpath("fixtures", "repo", ".claude", "skills", *parts)


def _brief_worktree(tmp_path) -> str:
    """A directory carrying just the frozen meeting brief — what `read_meeting_brief` needs and
    nothing else. No git: this file is keyless AND repo-free, and the brief read is an ordinary
    `open()` at a known relative path."""
    target = tmp_path / ".claude" / "skills" / "meeting-distiller"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        _frozen_brief("meeting-distiller", "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8")
    return str(tmp_path)


def _skill_worktree(tmp_path) -> str:
    """Its ordinary-flow twin: a directory carrying just the frozen librarian brief, which is what
    `agent.read_skill` reads out of an item's worktree (`agent.SKILL_RELPATH`).

    The real file, never a stub string: `build_system_prompt` splits its frontmatter off and the
    backend refuses an empty one, so a placeholder would exercise a shorter path than production's.
    """
    target = tmp_path / ".claude" / "skills" / "librarian"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        _frozen_brief("librarian", "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")
    return str(tmp_path)


def _filing_account() -> FilingAccount:
    """A well-formed STRUCTURED ordinary account — the shape a `structured_ordinary = True` backend
    returns in its envelope and `processing._write_ordinary_page` writes a page from.

    No shipped backend composes this schema on the ordinary flow any more (ADR 034 gave that flow
    tools and an outcome FILE), and it is kept here rather than deleted because the schema and the
    branch that reads it both survive: `MeetingAccount` below is its twin on a flow that still works
    exactly this way, and a `structured_ordinary = True` stub is how the content-carrying road is
    exercised in `test_structured_processing_pg.py`.
    """
    return FilingAccount(
        decision="file",
        page=OrdinaryPage(title="Acme Corp Renewal Window", page_type="note",
                          body="## Context\n\nThe renewal window was confirmed."),
        anchoring=OrdinaryAnchoring(kind="entity", entities=["Acme Corp"]),
        summary="filed the renewal note")


def _test_model(account):
    """pydantic-ai's own offline model, answering with `account`. Imported inside the helper for
    this file's standing rule: neither agent framework may be loaded by the signature half."""
    from pydantic_ai.models.test import TestModel

    return TestModel(custom_output_args=account.model_dump())


# ── the ORDINARY flow's own offline model: one that USES ITS TOOLS (ADR 034) ───────────────────
# The ordinary run holds five tools and returns its account by WRITING `.librarian-outcome.json`
# with one of them, so a `TestModel` returning a typed object cannot exercise it at all — there is
# no output schema to fill and nothing would ever be written. A `FunctionModel` scripts the two
# steps that road really takes: call `write_page`, then stop.
#
# It writes the ACCOUNT only and no page, which is enough here and deliberately so: this file is
# about the ENVELOPE (what the backend hands back, what it costs, what it counts), and what the
# worker does with the account afterwards — the page, the diff, the gates — is
# `test_processing_pg.py`'s and the golden's.
_AGENTIC_ACCOUNT = {
    "decision": "file",
    "page_path": "wiki/notes/Acme Corp Renewal Window.md",
    "page_type": "note",
    "title": "Acme Corp Renewal Window",
    "anchoring": {"kind": "entity", "entities": ["Acme Corp"], "reason": ""},
    "links_created": [],
    "overlaps": [],
    "edits": [],
    "findings": [],
    "summary": "filed the renewal note",
}


def _writing_model(account: dict, *, searches: int = 0):
    """A `FunctionModel` that optionally searches, then writes `account` and stops.

    `searches` exists so a test can put REAL tool calls in the loop before the write: the envelope's
    `turns` and `tool_calls` are only worth asserting against a run that made more than one of each,
    and a scripted single call would let a hardcoded `1` pass.
    """
    import json as _json

    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    def _script(messages, info):
        # One request per model turn: the first `searches` of them look, the next writes the
        # account, and the last says something the backend ignores on purpose.
        turn = len([m for m in messages if m.kind == "request"])
        if turn <= searches:
            return ModelResponse(parts=[ToolCallPart("search_pages", {"query": "renewal"})])
        if turn == searches + 1:
            return ModelResponse(parts=[ToolCallPart(
                "write_page", {"path": agent_module.OUTCOME_FILENAME,
                               "content": _json.dumps(account)})])
        return ModelResponse(parts=[TextPart("filed it")])

    return FunctionModel(_script)


def _raises():
    raise RuntimeError("no model here")


# The ORDINARY flow, per backend, over every offline outcome each one actually supports.
# What each supports is a fact about the backend, not a choice, and it is worth writing down:
#
#   * `pydantic` — **both roads, and it used to have neither.** M1's `run` was a refusal priced at
#     `0.0`; it is now a real ITERATING run (ADR 034), so the returning half is a real framework
#     loop against a `FunctionModel` that calls the confined `write_page` tool (its `cost_usd` is
#     computed from real token counts and must be positive) and the faulting half is a model that
#     cannot be built (priced at `0.0`, honestly: nothing was spent). Driven through
#     `model_factory`, so no configured provider is ever resolved and nothing here can reach a
#     network — and over a REAL git checkout, because a run that writes needs one: the tools read
#     `git ls-files` to know which pages already exist.
#   * `double` — both roads. A well-formed note FILES, which exercises the returning half (an
#     envelope whose `cost_usd` is a real `0.0`: an offline pass spends nothing, and that is an
#     answer rather than a gap); `DOUBLE:bad-shape` drives its account through the same
#     `parse_outcome` every backend's account goes through, and is refused by it, which exercises
#     the faulting half.
#
# A THIRD backend was scoped out of this table rather than faked, and the rule that kept it out
# outlives it: past its skill read, its `run` went straight into a vendor SDK, so the only fault it
# could raise with no key was a `LibrarianConfigError` for a missing skill — the WORKER's config
# road, which the port deliberately does not price (see the boundary test below). Faking a fault by
# stubbing that SDK would have been asserting the stub. **A backend whose envelope semantics can
# only be exercised against its provider does not get a fake here; it gets covered where it is
# real, in the golden filing eval.**
def _ordinary_flow_cases(tmp_path):
    settings = _settings()
    env = support.build_repo(str(tmp_path / "git"))
    other = support.build_repo(str(tmp_path / "git-pydantic"))
    double = DoubleAgent(dataclasses.replace(settings, repo=env.repo))
    return {
        "pydantic-files": (PydanticFilingAgent(
            settings, model_factory=lambda: _writing_model(_AGENTIC_ACCOUNT)),
            other.repo, "A note about Acme Corp."),
        "pydantic-fault": (PydanticFilingAgent(settings, model_factory=_raises),
                           _skill_worktree(tmp_path / "faulting"), "A note about Acme Corp."),
        "double-files": (double, env.repo, "A note about Acme Corp."),
        "double-bad-shape": (double, env.repo, "DOUBLE:bad-shape\nA note about Acme Corp."),
    }


@pytest.mark.parametrize("name", ["pydantic-files", "pydantic-fault", "double-files",
                                  "double-bad-shape"])
def test_every_reachable_ordinary_flow_outcome_carries_a_usable_spend(tmp_path, name):
    """**The contract that decides what a `failed` row says it cost**, on whichever road the flow
    takes.

    `processing` banks a returning pass off `AgentRun.cost_usd` and a faulting one off the
    exception's `run_cost_usd`, and it reads the second with `getattr(ex, "run_cost_usd", 0.0)` —
    so an absent field and an honest zero are indistinguishable downstream. The port's docstring
    therefore requires the field on every fault, not merely a correct number.

    The double's faulting road is the one that closed last: it used to parse its own account
    without the attach, which meant the backend the whole offline suite stands on was exercising a
    SHORTER contract than the one it stands in for.
    """
    backend, worktree, material = _ordinary_flow_cases(tmp_path)[name]

    try:
        run = backend.run(worktree=worktree, material=material, hints={},
                          submitted_by="a@b.test")
    except AgentError as ex:
        spend = getattr(ex, "run_cost_usd", None)
        assert _finite_dollars(spend), (
            f"{name}: an agent fault carried run_cost_usd={spend!r} — `processing` banks that "
            f"figure onto a `failed` row, and absent is not the same claim as zero")
    else:
        assert _finite_dollars(run.cost_usd), f"{name}: returned cost_usd={run.cost_usd!r}"


def test_the_doubles_shape_road_and_its_structural_road_are_both_priced():
    """**Why the catch is `AgentError` and not `OutcomeShapeError`**, which is the half a
    directive cannot reach.

    `parse_outcome` refuses on two roads: a SHAPE problem (`OutcomeShapeError`, correctable on the
    one retry) and a STRUCTURAL one (a bare `AgentError` — a resource bound like the nesting
    ceiling, which no brief can talk an agent out of). Both leave a pass that must still say what
    it cost, so `_priced_parse` catches the parent. A handler narrowed to the subclass would look
    correct, pass every directive-driven test, and drop the field on exactly the road nothing
    exercises.

    Driven at the helper because the double CANNOT stage over-deep nesting through `run`: it builds
    its own outcome dict and no `DOUBLE:` directive injects arbitrary nesting, so the structural
    road has no directive at all. Nothing is faked to get here — the real `parse_outcome`, the real
    helper, and a payload nested past the real `MAX_OUTCOME_DEPTH`.
    """
    deep = {"decision": "file"}
    node = deep
    for _ in range(agent_module.MAX_OUTCOME_DEPTH + 2):
        node["nest"] = {}
        node = node["nest"]

    run = AgentRun()
    with pytest.raises(AgentError) as exc_info:
        DoubleAgent._priced_parse(run, agent_module.parse_outcome, deep)

    assert type(exc_info.value) is AgentError, (
        "the nesting ceiling is a resource bound, not a shape the corrective retry can repair — "
        "routing it to the retry would spend a second pass on the same answer")
    assert exc_info.value.run_cost_usd == 0.0
    # ...and the shape road through the same helper, which IS the subclass
    with pytest.raises(AgentError) as shape_info:
        DoubleAgent._priced_parse(run, agent_module.parse_outcome, {"decision": "publish"})
    assert isinstance(shape_info.value, OutcomeShapeError)
    assert shape_info.value.run_cost_usd == 0.0


def test_the_iterating_ordinary_pass_is_priced_and_counted_like_the_loop_it_now_is(tmp_path):
    """**The case that replaced M1's refusal, re-aimed at the run that replaced the one-shot.**

    M1's `run` raised, priced at `0.0`. ADR 033 made it one structured call. ADR 034 made it an
    iterating one, and the envelope's claim changed WITH it: a real `Agent.run` loop against a
    `FunctionModel` reports real token counts, `pricing.compute_cost_usd` multiplies them by the
    CONFIGURED model's rates, and `AgentPasses` sums the figure onto the row a person reads.

    `> 0` is the assertion that can catch the failure worth catching: a backend that priced a real
    pass at nothing would report every filing as free, and free is the one direction this instrument
    must never be wrong in.

    **`turns` and `tool_calls` are asserted as REAL numbers, and against a run scripted to make
    more than one of each** — the port's own semantics note (zero means "this shape has no loop",
    never "nobody counted"). A run with a single search would let a hardcoded `1` pass; two
    searches plus a write cannot.

    The account arrives as the outcome FILE the model wrote with its own confined tool, which is
    the whole channel change: nothing here fills an output schema, and the model's final message is
    ignored by design.
    """
    env = support.build_repo(str(tmp_path / "git"))
    backend = PydanticFilingAgent(
        _settings(), model_factory=lambda: _writing_model(_AGENTIC_ACCOUNT, searches=2))

    run = backend.run(worktree=env.repo, material="A note about Acme Corp.",
                      hints={}, submitted_by="a@b.test", gathered="")

    assert isinstance(run, AgentRun)
    assert run.outcome.decision == "file"
    # the LEGACY half of the envelope: the agent names the path it wrote, and carries no page text
    assert run.outcome.page_path == "wiki/notes/Acme Corp Renewal Window.md"
    assert run.outcome.page is None
    assert run.outcome.title == "Acme Corp Renewal Window"
    assert _finite_dollars(run.cost_usd) and run.cost_usd > 0, (
        "a real framework run priced at nothing — a silent zero reads as free")
    assert run.turns >= 3 and run.tool_calls == 3, (
        f"the loop reported turns={run.turns}, tool_calls={run.tool_calls} — this run searched "
        f"twice and wrote once")
    # ...and the channel is drained on the way out: `read_outcome` deletes the file it parsed, so
    # the account can never reach the diff `processing` is about to take.
    assert not pathlib.Path(env.repo, agent_module.OUTCOME_FILENAME).exists()


def test_the_ordinary_faults_carry_the_spend_the_same_way(tmp_path):
    """The other half of the same contract, on the road that raises. A model that cannot be built
    fails before a token is spent, so `0.0` is the honest figure — and the FIELD has to be there,
    because `processing` reads `getattr(ex, "run_cost_usd", 0.0)` and cannot otherwise tell "spent
    nothing" from "nobody attached it".

    A bare skill directory is enough here and is the point: this fault fires at model RESOLUTION,
    before the toolbox is built and before anything reads the checkout, so it is reachable without
    a git repo at all.
    """
    backend = PydanticFilingAgent(_settings(), model_factory=_raises)

    with pytest.raises(AgentError) as exc_info:
        backend.run(worktree=_skill_worktree(tmp_path), material="an ordinary note", hints={},
                    submitted_by="a@b.test")

    assert hasattr(exc_info.value, "run_cost_usd"), (
        "the ordinary-flow fault carries no run_cost_usd at all")
    assert exc_info.value.run_cost_usd == 0.0
    # the configuration fault names the setting an operator would change, never the provider's text
    assert "$STIGMERGY_LIBRARIAN_MODEL" in str(exc_info.value)


def test_the_ordinary_flow_no_longer_refuses_this_backend_at_all(tmp_path):
    """The retired refusal, pinned as retired.

    M1's `run` raised "serves the meeting flow only in this milestone" for every ordinary capture,
    and `worker.startup_checks` refused the backend outright so nobody met it. Both are gone with
    the limitation (ADR 033 D5). A sentence that came back — here, or in a message a `failed` row
    carries — would mean the ordinary path had been reverted while the worker still dispatched to
    it, which is the one regression this file is placed to notice.
    """
    env = support.build_repo(str(tmp_path / "git"))
    backend = PydanticFilingAgent(
        _settings(), model_factory=lambda: _writing_model(_AGENTIC_ACCOUNT))

    run = backend.run(worktree=env.repo, material="an ordinary note", hints={},
                      submitted_by="a@b.test")

    assert run.outcome is not None
    source = inspect.getsource(type(backend))
    assert "meeting flow only" not in source


@pytest.mark.parametrize("factory, why", [
    (_raises, "a model that cannot be built"),
    (lambda: "nosuchprovider:whatever", "a provider pydantic-ai does not know"),
])
def test_the_priced_backends_meeting_faults_carry_a_usable_spend(tmp_path, factory, why):
    """The property on the flow that actually costs money, for the one backend that computes its own
    price — over the two faults that fire before a single token is spent.

    `0.0` is the right figure for both, and the FIELD being present is the point: `processing` reads
    `getattr(ex, "run_cost_usd", 0.0)`, so an absent field and an honest zero are indistinguishable
    downstream, and the port's docstring says the field must be there either way.
    """
    backend = PydanticFilingAgent(_settings(), model_factory=factory)

    with pytest.raises(AgentError) as exc_info:
        backend.run_meeting(worktree=_brief_worktree(tmp_path), material="t", meeting_meta={},
                            registry=None, source_page_path="sources/meetings/x.md")

    assert _finite_dollars(getattr(exc_info.value, "run_cost_usd", None)), (
        f"{why}: the fault carried run_cost_usd="
        f"{getattr(exc_info.value, 'run_cost_usd', None)!r}")


def test_a_config_fault_stays_off_the_priced_road_deliberately(tmp_path):
    """The specificity half, and it is a boundary rather than an omission: a missing meeting brief is
    the WORKER's configuration road (`LibrarianConfigError`, which `process_next` names on its own
    terms), not an agent attempt that cost something. Pricing it would invent a figure for a fault
    that pre-dates the model call."""
    backend = PydanticFilingAgent(_settings(), model_factory=_raises)

    with pytest.raises(LibrarianConfigError):
        backend.run_meeting(worktree=str(tmp_path), material="t", meeting_meta={}, registry=None,
                            source_page_path="sources/meetings/x.md")


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -0.01])
def test_a_backend_handing_back_an_unusable_cost_is_what_this_contract_forbids(cost):
    """The sabotage twin, and the reason the two assertions above are worth running: a `NaN` cost
    survives every signature check, sums cleanly through `AgentPasses`, rounds without complaint,
    and only fails at the `jsonb` write — after the commit and the push. The predicate has to be
    able to say no."""
    assert not _finite_dollars(cost)
    assert not _finite_dollars(None)
    assert _finite_dollars(0.0) and _finite_dollars(0.31)
