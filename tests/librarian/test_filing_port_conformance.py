"""Every backend really does answer `filing_port.FilingAgent`, method for method and argument for
argument — keylessly, so the claim is checked on every run rather than on the one that has a key.

The seam used to be a CONVENTION: `SdkAgent`, `DoubleAgent` and `processing.py` agreed about two
signatures and one envelope, and nothing stated it anywhere a THIRD implementation could read. ADR
032 wrote it down as a port; this file is what makes the writing load-bearing.

**`isinstance` is not the test, it is the cheapest third of it.** A `runtime_checkable` Protocol
checks that the two methods are PRESENT and nothing else — not their argument names, not that they
are keyword-only, not their defaults. A backend whose `run_meeting` took `worktree` positionally,
or spelled it `work_tree`, or defaulted `corrective` to `None` instead of `""`, passes `isinstance`
and fails at the one call site in `processing.py`, mid-item, against a real queue row. So the
signatures are compared to the Protocol's own — the thing the port DOCUMENTS — and the twins below
prove each half of that comparison can actually fail.

**Keyless throughout.** The signature half constructs every backend and calls none of them
(`SdkAgent.__init__` stores settings and nothing else). The envelope-semantics half at the bottom
does run the two calls — against an offline model, a scratch brief and a real scratch git repo,
because what a backend HANDS BACK cannot be read off a signature. Neither agent framework is
loaded by any of it: the `pydantic` backend imports its own inside the method, and no test here
reaches the SDK branch.
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
from stigmergy.librarian.pydantic_backend import PydanticMeetingAgent
from tests.librarian import support

# The two calls `processing.py` makes on `Deps.agent`, and the only two it may make.
PORT_METHODS = ("run", "run_meeting")

# Every implementation the port claims, named by the backend id that dispatches to it. Derived from
# `agent.BACKENDS` in the test below rather than trusted here: a fourth backend added to that tuple
# and forgotten here would leave this whole file silently measuring three of four.
BACKEND_CLASSES = {
    "sdk": agent_module.SdkAgent,
    "double": DoubleAgent,
    agent_module.PYDANTIC_BACKEND: PydanticMeetingAgent,
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
    a fourth backend added to `agent.BACKENDS` without a line here would make that claim false while
    every test below stayed green. Derived from the production tuple, never retyped."""
    assert set(BACKEND_CLASSES) == set(agent_module.BACKENDS)


@pytest.mark.parametrize("name", sorted(BACKEND_CLASSES))
def test_build_agent_returns_the_backend_this_file_conforms(name):
    """The other end of the same wire: the classes below are the ones `build_agent` actually hands
    `processing.Deps`. Conformance proven about a class nothing constructs would be conformance
    proven about nothing.

    The settings carry a PRICED, provider-prefixed model for every branch, not only the one that
    needs it: `PydanticMeetingAgent.__init__` prices its configured id at construction (the backstop
    below `worker.startup_checks`), so a `Settings` carrying the SDK backend's bare default would
    refuse here for a reason that has nothing to do with the port. The other two ignore the field.
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
        PydanticMeetingAgent(dataclasses.replace(_settings(), model="openai:gpt-9"))


@pytest.mark.parametrize("name", ["sdk", "double"])
def test_the_backends_that_price_themselves_construct_whatever_the_model_says(name):
    """The specificity half: the price backstop belongs to the backend that computes cost from
    tokens, and only to it. The SDK is priced by its own SDK and the double spends nothing, so
    requiring a priced id of either would refuse a configuration nothing was going to use — the
    same argument the credential check already makes for itself."""
    built = agent_module.build_agent(
        dataclasses.replace(_settings(), backend=name, model="a-model-nothing-prices"))
    assert isinstance(built, BACKEND_CLASSES[name])


def test_an_unknown_backend_fails_fast_rather_than_falling_through_to_one_of_the_three():
    """The dispatch's own refusal, and it names the three so a typo is one line from fixed. A
    fall-through would silently pick the real path or the double — the two outcomes
    `build_agent`'s docstring exists to rule out."""
    with pytest.raises(LibrarianConfigError) as exc_info:
        agent_module.build_agent(config.Settings(repo="/nonexistent", backend="pydanitc"))
    message = str(exc_info.value)
    assert "pydanitc" in message
    for name in agent_module.BACKENDS:
        assert name in message


# ── isinstance: the two methods are present ────────────────────────────────────────────────────
def test_every_backend_satisfies_the_runtime_checkable_port(backend):
    assert isinstance(backend, FilingAgent)


def test_a_class_missing_run_meeting_is_not_a_filing_agent():
    """The benign twin's sharp half: `isinstance` is worth running because it can fail. A backend
    that only implements the ordinary flow is exactly the half-backend the meeting dispatch would
    meet with an `AttributeError` mid-item."""
    class OrdinaryOnly:
        def run(self, *, worktree, material, hints, submitted_by,
                corrective="", reply="", flow_note=""):
            return AgentRun()

    assert not isinstance(OrdinaryOnly(), FilingAgent)


def test_a_class_answering_both_calls_is_a_filing_agent_however_it_was_written():
    """...and its benign half: conformance is STRUCTURAL. Nothing inherits, nothing registers, and
    a class written by somebody who never read `filing_port.py` conforms the moment it answers the
    two calls — which is what lets the test doubles this suite is built on exercise the same
    contract a live backend does."""
    class HandWritten:
        def run(self, *, worktree, material, hints, submitted_by,
                corrective="", reply="", flow_note=""):
            return AgentRun()

        def run_meeting(self, *, worktree, material, meeting_meta, registry,
                        source_page_path, corrective="", reply=""):
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
    """The defaults are the contract too: `processing.py` omits `corrective`/`reply`/`flow_note` on
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
    answers both calls, so `isinstance` says yes — and its `worktree` is positional, which is
    precisely the drift a Protocol cannot see. If the comparison above ever stops being able to
    fail, this test goes red first."""
    class PositionalWorktree:
        def run(self, worktree, *, material, hints, submitted_by,
                corrective="", reply="", flow_note=""):
            return AgentRun()

        def run_meeting(self, worktree, *, material, meeting_meta, registry,
                        source_page_path, corrective="", reply=""):
            return AgentRun()

    assert isinstance(PositionalWorktree(), FilingAgent), (
        "isinstance must still accept it — that is the gap this test exists to cover")
    for method in PORT_METHODS:
        assert (inspect.signature(getattr(PositionalWorktree, method))
                != inspect.signature(getattr(FilingAgent, method)))


def test_the_signature_check_catches_a_renamed_argument_too():
    """The second shape the same gap takes, and the likelier one: a backend written from memory
    spells `meeting_meta` `meta`. Keyword-only throughout means the call site raises `TypeError` on
    a real item — this catches it with no key, no queue and no model."""
    class Renamed:
        def run(self, *, worktree, material, hints, submitted_by,
                corrective="", reply="", flow_note=""):
            return AgentRun()

        def run_meeting(self, *, worktree, material, meta, registry,
                        source_page_path, corrective="", reply=""):
            return AgentRun()

    assert isinstance(Renamed(), FilingAgent)
    assert (inspect.signature(Renamed.run_meeting)
            != inspect.signature(FilingAgent.run_meeting))


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


def _brief_worktree(tmp_path) -> str:
    """A directory carrying just the frozen meeting brief — what `read_meeting_brief` needs and
    nothing else. No git: this file is keyless AND repo-free, and the brief read is an ordinary
    `open()` at a known relative path."""
    brief = (pathlib.Path(__file__).parent / "fixtures" / "repo" / ".claude" / "skills"
             / "meeting-distiller" / "SKILL.md")
    target = tmp_path / ".claude" / "skills" / "meeting-distiller"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(brief.read_text(encoding="utf-8"), encoding="utf-8")
    return str(tmp_path)


def _raises():
    raise RuntimeError("no model here")


# The ORDINARY flow, per backend, over every offline outcome each one actually supports.
# What each supports is a fact about the backend, not a choice, and it is worth writing down:
#
#   * `pydantic` — refuses the flow outright. A fault, and the only one of the three that is a
#     refusal rather than an accident.
#   * `double` — both roads. A well-formed note FILES, which exercises the returning half (an
#     envelope whose `cost_usd` is a real `0.0`: an offline pass spends nothing, and that is an
#     answer rather than a gap); `DOUBLE:bad-shape` drives its account through the same
#     `parse_outcome` the SDK path uses and is refused by it, which exercises the faulting half.
#   * `sdk` — **scoped out, and not faked.** Past the skill read its `run` goes straight into
#     `claude_agent_sdk`, so the only fault it can raise with no key is a `LibrarianConfigError`
#     for a missing skill — the WORKER's config road, which the port deliberately does not price
#     (see the boundary test below). Faking a fault by stubbing the SDK would be asserting the stub.
#     Its envelope semantics are covered where they are real: the golden filing eval.
def _ordinary_flow_cases(tmp_path):
    settings = _settings()
    env = support.build_repo(str(tmp_path / "git"))
    double = DoubleAgent(dataclasses.replace(settings, repo=env.repo))
    return {
        "pydantic": (PydanticMeetingAgent(settings), env.repo, "A note about Acme Corp."),
        "double-files": (double, env.repo, "A note about Acme Corp."),
        "double-bad-shape": (double, env.repo, "DOUBLE:bad-shape\nA note about Acme Corp."),
    }


@pytest.mark.parametrize("name", ["pydantic", "double-files", "double-bad-shape"])
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


def test_the_meeting_only_backends_refusal_is_priced_like_every_other_fault(tmp_path):
    """The F4 case on its own, because it is a REFUSAL rather than an accident and it is the one a
    reader would assume needs no figure: nothing was spent, no model was built, no request was made.
    `0.0` is how the port says exactly that — and saying it is what keeps `processing` able to tell
    it apart from a fault nobody annotated."""
    backend = PydanticMeetingAgent(_settings())

    with pytest.raises(AgentError) as exc_info:
        backend.run(worktree=str(tmp_path), material="an ordinary note", hints={},
                    submitted_by="a@b.test")

    assert hasattr(exc_info.value, "run_cost_usd"), (
        "the ordinary-flow refusal carries no run_cost_usd at all")
    assert exc_info.value.run_cost_usd == 0.0
    # ...and it is still the same refusal, not a thinner one wearing a price
    assert "meeting flow only" in str(exc_info.value)


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
    backend = PydanticMeetingAgent(_settings(), model_factory=factory)

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
    backend = PydanticMeetingAgent(_settings(), model_factory=_raises)

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
