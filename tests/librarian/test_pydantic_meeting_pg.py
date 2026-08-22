"""The meeting flow on the `pydantic` backend, over a real Postgres queue, a real git repo + bare
remote and a REAL pydantic-ai `Agent` — driven by an offline model, never by a key.

`test_meeting_processing_pg.py` proves the flow against the offline double. This file proves the
same flow against the backend that will actually run it, because the two things a double cannot
stand in for are the two ADR 032 added: **a real framework call** (its usage accounting, its output
schema, its exceptions) and **the money that call costs**. A backend whose port conformance is
checked keylessly and whose behaviour is only ever exercised through a hand-written stand-in has
been tested about everything except being a backend.

**The offline seam is `model_factory`, and it is the backend's own, not a monkeypatch.**
`PydanticFilingAgent(settings, model_factory=…)` takes a zero-arg callable returning anything
pydantic-ai accepts as a model, so the whole distillation path — instructions built from the brief
in the worktree, the per-item prompt, the framework's run, the usage accounting, the pricing, and
`agent.parse_meeting_outcome` at the trust boundary — runs for real against a `TestModel`. Nothing
here stubs a librarian function. Constructing a real `Agent` against an offline model is this
repo's own precedent (`.github/workflows/ci.yml`'s `CLEAN_LLM` note).

**The price is always the CONFIGURED model's**, never the injected one's — asserted below, because
it is the property that decides whether an offline measurement means anything: a seam that let a
test double make a run look free would make every cost number in this milestone unfalsifiable.
"""
import dataclasses
import json
import os

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from stigmergy.capture import schema
from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import gates, pricing, pydantic_backend, worker
from stigmergy.librarian.errors import AgentError, LibrarianConfigError, OutcomeShapeError
from stigmergy.librarian.pydantic_backend import (
    MeetingAccount,
    MeetingAnchoring,
    MeetingDecision,
    NewEntity,
    PydanticFilingAgent,
)
from tests.librarian import support

PRICED_MODEL = "openai:gpt-5.6-terra"

# Digit-free padding, for the reason the double's own filler is: any numeral in a drafted body
# would read as a figure the transcript asserted, and this text is not part of what was archived.
_FILLER = [
    "This page records what the meeting settled, in the brain's own vocabulary.",
    "It is structured for retrieval rather than for reading end to end.",
    "Nothing here asserts anything the transcript did not carry.",
]

_TRANSCRIPT = "Alice and Bob went through the Acme renewal window and settled the pilot scope."

# The registry the fixture knowledge repo ships with holds exactly this one entity, under this id
# — the id is what a filed page's `entity:` frontmatter carries, and it is not derivable from the
# name (`ops/entity-registry.json` maps `acme-corp` -> `Acme Corp`), so it is named rather than computed.
_REGISTERED = "Acme Corp"
_REGISTERED_ID = "acme-corp"
# ...and this one it does not, which is what makes a complete, correct distillation park.
_UNREGISTERED = "Ledgerly"


def _body(link_entity: str) -> str:
    """One decision page's drafted body, padded past the contract linter's thirty-line minimum.

    `len(lines)`, not the non-blank count: the linter trims only leading and trailing blanks, so
    the blank line between sections counts toward the minimum — the same arithmetic
    `double._decision_body` documents, and a body that under-padded would be refused as thin for a
    reason that has nothing to do with what is being tested here.
    """
    lines = ["## Context", "", f"Decided about [[{link_entity}]] in this meeting.", "",
             "## Decision", ""]
    lines += _FILLER
    while len(lines) < 32:
        lines.append("Additional context recorded from the meeting for future readers.")
    return "\n".join(lines)


def _notes() -> str:
    lines = list(_FILLER)
    while len(lines) < 20:
        lines.append("Additional minutes recorded from the meeting for future readers.")
    return "\n".join(lines)


def _account(*, anchor_to: str = _REGISTERED, link_entity: str | None = None,
             decisions: int = 1, decision: str = "file",
             meeting_title: str = "Q3 sync", new_entities=()) -> MeetingAccount:
    """A complete meeting account, in the schema the backend declares as its output type.

    `anchor_to` is what each decision DECLARES its aboutness to be, and `link_entity` what its body
    wikilinks. They are separable on purpose and the separation is load-bearing for the park case:
    `gate_anchoring` resolves the declared names against the registry while the contract linter's
    `dead_links` rule judges the body's wikilinks, so declaring an unregistered entity while
    linking a registered one produces the ONE veto (`anchoring/unresolved`) that
    `processing._refuse_meeting` routes to a park. A body linking the unregistered name too would
    earn a second, unrelated veto and the capture would be refused instead of parked — which is a
    different test.
    """
    linked = link_entity or anchor_to
    return MeetingAccount(
        decision=decision,
        meeting_title=meeting_title,
        attendees=["Alice", "Bob"],
        meeting_notes=_notes(),
        decisions=[
            MeetingDecision(title=f"Q3 sync — decision {index + 1}", body=_body(linked),
                            anchoring=MeetingAnchoring(kind="entity", entities=[anchor_to]))
            for index in range(decisions)],
        summary=f"distilled {decisions} decision(s) from the meeting",
        new_entities=list(new_entities),
    )


def _test_model(account: MeetingAccount) -> TestModel:
    """A pydantic-ai `TestModel` that answers with `account` — the framework's own offline model,
    so the run under test is a real `Agent.run` with real usage accounting."""
    return TestModel(custom_output_args=account.model_dump())


def _rig(tmp_path, model_factory, *, model: str = PRICED_MODEL, **setting_overrides):
    """A `RepoEnv` + `Deps` whose agent is a REAL `PydanticFilingAgent` over an offline model.

    Built through `support.build_settings`/`build_deps` — the same wiring every other librarian
    test uses — with the agent injected, which is exactly where `agent.build_agent` would have put
    it. `backend="pydantic"` is set on the settings too, so nothing here is testing a
    configuration a real run could not hold.
    """
    env = support.build_repo(str(tmp_path / "git"))
    settings = support.build_settings(env, worktree_root=str(tmp_path / "worktrees"),
                                      backend="pydantic", model=model, **setting_overrides)
    agent = PydanticFilingAgent(settings, model_factory=model_factory)
    return env, support.build_deps(env, settings, agent=agent), agent


# ── AC2: the golden path, priced ───────────────────────────────────────────────────────────────
def test_a_meeting_files_a_page_set_through_a_real_pydantic_ai_run_and_the_run_costs_money(
        tmp_path, clean_queue, require_gitleaks):
    """The whole flow on the new backend, asserted where it is irreversible: the pages read back
    out of the object database at the sha this filing actually attributed to itself.

    `result_ref`'s sha, never the branch tip — the post-meeting view hook pushes a second commit on
    top, so the tip is "whatever committed last" and reading the set from it would pass for the
    wrong reason.

    **`cost_usd > 0` is the half a double cannot prove.** The offline double reports `0.0` on every
    run, honestly, so every existing meeting test is compatible with a backend that never priced
    anything. Here a real framework run reports real token counts, `pricing.compute_cost_usd`
    multiplies them by the CONFIGURED model's rates, `AgentPasses` banks the pass and `_stamp_cost`
    puts the figure on the row a person reads — and every one of those five steps is production
    code.
    """
    env, deps, _ = _rig(tmp_path, lambda: _test_model(_account(decisions=2)))

    support.submit_meeting(clean_queue, deps, _TRANSCRIPT)
    item, result = worker.process_next(clean_queue, deps)

    assert item["kind"] == schema.MEETING
    assert result.status == schema.FILED, result.report.get("summary")

    _, sha = result.result_ref.rsplit("@", 1)
    changed = support.changed_paths(env.repo, sha)
    assert len([p for p in changed if p.startswith("sources/meetings/")]) == 1
    assert len([p for p in changed if p.startswith("wiki/meetings/")]) == 1
    assert len([p for p in changed if p.startswith("wiki/decisions/")]) == 2
    # the pages are really there, at that sha, anchored to the id the fixture registry resolves
    # `Acme Corp` to — read back out of the object database, never off a removed worktree
    for row in result.report["filed_meeting"]["decisions"]:
        page = support.read_filed_page(env.repo, sha, row["path"])
        assert f'entity: ["{_REGISTERED_ID}"]' in page, page[:400]

    assert result.report["cost_usd"] > 0, (
        "a real model call was priced at nothing — a silent zero reads as free, which is the one "
        "direction this instrument must never lie in")
    assert result.report["cost_usd"] == round(result.report["cost_usd"], 6)


def test_the_agent_writes_no_file_at_all_and_the_diff_says_so(tmp_path, clean_queue,
                                                              require_gitleaks):
    """The meeting flow's side-effect rule, on the backend that makes it literal: code is the sole
    author of every page in the set, and this backend carries its account home in the envelope
    rather than through a file — so its legal write count is zero, not one.

    Asserted on the COMMITTED diff, which is the only place a stray write would survive to."""
    env, deps, _ = _rig(tmp_path, lambda: _test_model(_account()))

    support.submit_meeting(clean_queue, deps, _TRANSCRIPT)
    _, result = worker.process_next(clean_queue, deps)

    assert result.status == schema.FILED
    _, sha = result.result_ref.rsplit("@", 1)
    changed = support.changed_paths(env.repo, sha)
    assert changed, "the filing committed nothing at all"
    assert not [p for p in changed if p.endswith(".librarian-outcome.json")]
    assert all(p.startswith(("sources/meetings/", "wiki/meetings/", "wiki/decisions/"))
               for p in changed), changed


def test_the_price_is_the_configured_models_and_never_the_injected_ones(tmp_path,
                                                                        require_gitleaks,
                                                                        monkeypatch):
    """**The property that makes every offline cost figure in this milestone mean something.**

    The model this run actually talks to is a `TestModel` whose name is `test`. If the price were
    looked up by the model the seam injected, an offline run would price at nothing and a test
    asserting `cost_usd > 0` would be asserting an accident. So the same run is priced twice
    through the CONFIGURED id with the rates doubled between them, and the dollars have to double
    with them — which no lookup keyed on the injected double could produce.
    """
    env, deps, agent = _rig(tmp_path, lambda: _test_model(_account()))
    call = dict(worktree=env.repo, material=_TRANSCRIPT,
                meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                registry=deps.registry, source_page_path="sources/meetings/q3-sync.md")

    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({PRICED_MODEL: [2.0, 2.0, 12.0]}))
    cheap = agent.run_meeting(**call).cost_usd

    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({PRICED_MODEL: [4.0, 4.0, 24.0]}))
    dear = agent.run_meeting(**call).cost_usd

    assert cheap > 0
    assert dear == pytest.approx(cheap * 2, rel=1e-6), (
        f"doubling the configured model's rates moved the figure from {cheap} to {dear} — the "
        f"price is not being looked up by $STIGMERGY_LIBRARIAN_MODEL")


def test_an_unpriced_configured_model_refuses_even_though_the_injected_model_is_free(
        tmp_path, require_gitleaks):
    """The same property from its other side, and the reason `startup_checks` refuses an unpriced
    id before the first claim: a backend that cannot say what a run cost must not be able to spend
    one.

    **RE-PINNED at the construction site, which is where the refusal moved and is a strictly
    earlier one.** It used to fire from `_cost`, after a real model call — priced work already paid
    for, refused only on the way to reporting it. Same exception, same message, now raised by
    `PydanticFilingAgent.__init__`, so the object that would spend the money cannot be built. The
    injected `TestModel` is free and irrelevant: the price is looked up by the CONFIGURED id, and
    that is the whole point.
    """
    env = support.build_repo(str(tmp_path / "git"))
    settings = support.build_settings(env, worktree_root=str(tmp_path / "worktrees"),
                                      backend="pydantic", model="openai:gpt-9")

    with pytest.raises(LibrarianConfigError, match="openai:gpt-9") as exc_info:
        PydanticFilingAgent(settings, model_factory=lambda: _test_model(_account()))

    # the same refusal `require_priced` gives everywhere else, not a second, thinner one
    assert pricing.PRICING_ENV in str(exc_info.value)
    assert pricing.AS_OF in str(exc_info.value)


# ── AC4: a name the registry does not know is proposed, never parked ─────────────────────────
def test_a_complete_distillation_that_cannot_anchor_and_proposes_nothing_is_the_librarians_fault(
        tmp_path, clean_queue, require_gitleaks):
    """OLD BEHAVIOUR: parked on a steward, with the distillation stored on the row for a re-file
    after a mint. Nothing parks: the brief offers the distiller a proposal road that files, and a
    complete account that declares an unresolvable anchor without taking it is refused on both
    passes. The park used to be a paid call that had to report its cost; the failure still does."""
    env, deps, _ = _rig(tmp_path,
                        lambda: _test_model(_account(anchor_to=_UNREGISTERED,
                                                     link_entity=_REGISTERED, decisions=2)))

    support.submit_meeting(clean_queue, deps, _TRANSCRIPT)
    item, result = worker.process_next(clean_queue, deps)

    assert result.status == schema.FAILED, result.report.get("summary")
    assert result.report["cost_usd"] > 0


def test_a_distillation_that_introduces_the_new_entity_files_the_set_with_the_entity_beside_it(
        tmp_path, clean_queue, require_gitleaks):
    """The birth, through a real `Agent.run` on the meeting flow: the account names the new entity
    in `new_entities`, every decision anchors to it, and the commit carries the source page, the
    meeting page, the decisions AND the newborn entity page — with the registry regenerated so
    `entity: ["ledgerly"]` resolves on every decision, and the identity confirmed by whoever
    submitted the meeting (ADR 044)."""
    declared = NewEntity(name=_UNREGISTERED, entity_type="organization",
                         role="a prospect discussed at the sync", aliases=[],
                         summary="Ledgerly is a prospect the Q3 sync discussed.",
                         facts=["Discussed at the Q3 sync"], connections=[])
    env, deps, _ = _rig(tmp_path,
                        lambda: _test_model(_account(anchor_to=_UNREGISTERED,
                                                     link_entity=_UNREGISTERED, decisions=2,
                                                     new_entities=[declared])))

    support.submit_meeting(clean_queue, deps, f"{_TRANSCRIPT} Ledgerly came up as a prospect.")
    _, result = worker.process_next(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")
    _, sha = result.result_ref.rsplit("@", 1)
    changed = support.changed_paths(env.bare, sha)
    assert "wiki/entities/Ledgerly.md" in changed and "ops/entity-registry.json" in changed
    for row in result.report["filed_meeting"]["decisions"]:
        page = support.read_filed_page(env.bare, sha, row["path"])
        assert 'entity: ["ledgerly"]' in page
    assert result.report["entities_born"] == [
        {"id": "ledgerly", "name": "Ledgerly", "type": "organization",
         "confirmed_by": support.DEFAULT_SUBMITTER}]
    assert result.report["cost_usd"] > 0


# over-long answer arrives by, and the one these two tests are named for.
_OVER_LONG_TITLE = "Q" * (agent_module.MAX_IDENTIFIER_LEN + 1)


class _ShapeThenGood:
    """A stateful model factory: a refused shape on the first pass, a good account on the second.

    This is the corrective retry's whole premise — the agent is TOLD what was wrong and gets
    exactly one more try — and it cannot be exercised by a stateless double, because the second
    pass has to differ from the first for a reason the first pass caused.

    **The refused shape is a BOUNDS violation, not a requiredness one** (see `_OVER_LONG_TITLE`):
    the schema now catches the second class itself, and a factory that tried to stage one would be
    testing pydantic's constructor rather than the worker's retry.
    """

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls == 1:
            return _test_model(_account(meeting_title=_OVER_LONG_TITLE))
        return _test_model(_account())


class _RecordingAgent:
    """A transparent wrapper that records what each pass cost — from the envelope when the pass
    returns, and from the FAULT when it raises.

    Not a stand-in for the backend: every call delegates, and the real `PydanticFilingAgent` does
    all the work. It exists because the two roads a pass's spend can travel are invisible from
    outside — `processing` banks one off `AgentRun.cost_usd` and the other off the exception's
    `run_cost_usd`, and a report showing only the total cannot say whether both were taken.
    Mirrors `test_meeting_processing_pg._CountingMeetingAgent`'s shape.
    """

    def __init__(self, inner):
        self.inner = inner
        self.costs = []

    def run_meeting(self, **kwargs):
        try:
            run = self.inner.run_meeting(**kwargs)
        except AgentError as ex:
            self.costs.append(getattr(ex, "run_cost_usd", None))
            raise
        self.costs.append(run.cost_usd)
        return run

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_a_refused_shape_is_corrected_on_the_second_pass_and_the_row_pays_for_both(
        tmp_path, clean_queue, require_gitleaks):
    """Two passes, one filing, and a cost that is the SUM — not the last pass's.

    The first pass is charged through the exception road (`filing_port.priced` attaches
    `run_cost_usd`, `_one_meeting_pass` banks it off the fault) and the second through the
    returning one. A backend that only banked returning passes would under-report every corrected
    filing, which is precisely the shape an operator would never audit: it filed, so it looks fine.

    Asserted as an EQUALITY against the two passes' own figures rather than as "more than one
    pass": a comparison would pass just as well if the first pass were banked at a cent.
    """
    factory = _ShapeThenGood()
    env, deps, _ = _rig(tmp_path, factory)
    recorder = _RecordingAgent(deps.agent)

    support.submit_meeting(clean_queue, deps, _TRANSCRIPT)
    _, result = worker.process_next(clean_queue, dataclasses.replace(deps, agent=recorder))

    assert factory.calls == 2, "the corrective retry did not run on the new backend"
    assert result.status == schema.FILED, result.report.get("summary")
    assert len(recorder.costs) == 2
    assert all(cost and cost > 0 for cost in recorder.costs), (
        f"a pass was banked at nothing: {recorder.costs} — the first one raises, and its spend "
        f"rides on the exception")
    assert result.report["cost_usd"] == round(sum(recorder.costs), 6), (
        f"the row says {result.report['cost_usd']} where the passes cost {recorder.costs}")


def test_a_refused_shape_reaches_the_retry_as_an_outcome_shape_error_carrying_its_findings(
        tmp_path, require_gitleaks):
    """**Not flattened into a bare `AgentError`.** The parse is deliberately outside the backend's
    blanket `except Exception`, because that handler wraps a fault as a class name — and a class
    name carries no findings, so the single most correctable class of problem there is would be the
    one class the corrective retry could not see. That defect was fixed once, in `errors.py`; this
    is the assertion that it was not reintroduced one backend over.

    And it is still PRICED: the run was paid for whether or not its account parses.

    **This test is about the BOUNDARY road**, and after the schema round that is a narrower road
    with a sharper edge. Requiredness is now caught by the schema — the framework re-asks the model
    itself, which is `test_the_framework_repairs_an_incomplete_account_inside_one_worker_pass`'s
    subject one file over. What still reaches `parse_meeting_outcome` is everything the schema
    deliberately declines to restate, which is the BOUNDS: an over-long identifier is a complete,
    schema-valid account the trust boundary refuses. That division is the design, so the two tests
    together are what keeps either half from quietly absorbing the other.
    """
    env, deps, agent = _rig(tmp_path,
                            lambda: _test_model(_account(meeting_title=_OVER_LONG_TITLE)))

    with pytest.raises(OutcomeShapeError) as exc_info:
        agent.run_meeting(worktree=env.repo, material=_TRANSCRIPT,
                          meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                          registry=deps.registry,
                          source_page_path="sources/meetings/q3-sync.md")

    assert exc_info.value.findings, "the shape fault carried no findings to correct with"
    assert exc_info.value.run_cost_usd > 0, (
        "a paid run whose account did not parse reported no spend — the fault road is not priced")


def test_a_framework_that_exhausts_its_output_retries_also_takes_the_retry_road(
        tmp_path, require_gitleaks):
    """**The second road to the same place, and the one that would have been lost.**

    pydantic-ai re-asks the model when its answer does not satisfy the output schema, and gives up
    with `UnexpectedModelBehavior` — which is a SHAPE problem, the exact class the worker's
    corrective retry exists for. Caught by the blanket handler it would arrive as a bare
    `AgentError` carrying a class name and no findings, and the item would finish `failed` without
    the agent ever being told what was wrong: the defect `errors.OutcomeShapeError` was split out to
    fix, reintroduced through a different door.

    Driven by a model that answers with arguments the schema cannot coerce, so the framework really
    does exhaust its own budget — not by raising `UnexpectedModelBehavior` directly, which would
    test the handler against a hand-thrown exception rather than against the framework's behaviour.
    """
    calls = {"n": 0}

    def _unschemable():
        def _wrong_shape(messages, info):
            calls["n"] += 1
            tool = info.output_tools[0]
            # a nested object where the schema declares a string: refused by validation, re-asked,
            # refused again, and then the framework gives up
            return ModelResponse(parts=[ToolCallPart(tool.name, {"decision": {"no": ["pe"]}})])
        return FunctionModel(_wrong_shape)

    env, deps, agent = _rig(tmp_path, _unschemable)

    with pytest.raises(OutcomeShapeError) as exc_info:
        agent.run_meeting(worktree=env.repo, material=_TRANSCRIPT,
                          meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                          registry=deps.registry,
                          source_page_path="sources/meetings/q3-sync.md")

    assert calls["n"] == 1 + pydantic_backend.OUTPUT_RETRIES, (
        "the framework's own re-ask budget is not the constant the backend hands it")
    findings = exc_info.value.findings
    assert findings, "the framework rejection carried no findings to correct with"
    # the SAME gate the file channel's shape findings carry, so `corrective_brief` and
    # `_refuse_meeting` cannot tell the two channels apart — one vocabulary, one class of problem
    assert {f.gate for f in findings} == {agent_module._OUTCOME_GATE}
    assert not gates.unrepairable(findings), (
        "a framework rejection was marked unrepairable, which skips the very retry it should reach")
    assert exc_info.value.run_cost_usd > 0, (
        "two real model calls were made and the fault reported no spend")


def test_a_model_id_the_framework_cannot_resolve_is_a_configuration_fault_not_a_traceback(
        tmp_path, require_gitleaks, monkeypatch):
    """The construction site has its OWN narrow handler, and it exists because the blanket one below
    it would have reported an unresolvable model as "the meeting agent run failed" — sending an
    operator to look at the transcript for a fault in their environment.

    Class name only, like every other wrap here: pydantic-ai's own message names the provider, and
    the rule is that a framework's message never reaches an operator's log verbatim. The configured
    id IS named, because it is ours and it is what they have to change.

    Priced through the override so `__init__`'s own backstop is satisfied — an unpriced id is a
    different refusal, one check earlier.
    """
    unknown = "nosuchprovider:whatever"
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({unknown: [1.0, 1.0, 3.0]}))
    env, deps, agent = _rig(tmp_path, None, model=unknown)

    with pytest.raises(AgentError) as exc_info:
        agent.run_meeting(worktree=env.repo, material=_TRANSCRIPT,
                          meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                          registry=deps.registry,
                          source_page_path="sources/meetings/q3-sync.md")

    message = str(exc_info.value)
    assert "could not resolve the configured model" in message
    assert "ValueError" in message                       # the class...
    assert "Unknown provider" not in message             # ...and never the framework's own sentence
    assert unknown in message                            # ...but our own id, which they can change
    assert exc_info.value.run_cost_usd == 0.0, (
        "a run that never reached a model must price at 0.0, not at a guess")
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_a_mid_run_provider_fault_is_wrapped_by_class_name_and_never_by_its_message(
        tmp_path, require_gitleaks):
    """A provider exception can carry prompt text, which is to say the captured material, and this
    text reaches an operator's log. So the wrap is the class NAME and nothing else.

    The planted message is a credential-shaped string on purpose: if the wrap ever starts splicing
    a provider message in, this is the shape of thing it would splice.
    """
    planted = "sk-live-PLANTED-SECRET-FROM-THE-TRANSCRIPT"

    def _raising():
        def _explode(messages, info):
            raise RuntimeError(planted)
        return FunctionModel(_explode)

    env, deps, agent = _rig(tmp_path, _raising)

    with pytest.raises(AgentError) as exc_info:
        agent.run_meeting(worktree=env.repo, material=_TRANSCRIPT,
                          meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                          registry=deps.registry,
                          source_page_path="sources/meetings/q3-sync.md")

    message = str(exc_info.value)
    assert planted not in message
    assert "RuntimeError" in message
    assert hasattr(exc_info.value, "run_cost_usd"), (
        "a fault with no `run_cost_usd` makes `processing` unable to tell 'nothing was spent' from "
        "'nobody attached it'")
    # the original is kept as the cause, so a stack trace in the worker's own log still has it
    assert isinstance(exc_info.value.__cause__, RuntimeError)


# A control byte (BEL) planted just past a recognizable head, and 280 characters of padding after
# it — long enough that `pydantic_backend.MAX_FAULT_MESSAGE_LEN` (200) must cut it, not merely
# leave it alone.
_CONTROL_BYTE = "\x07"
_FAULT_HEAD = "the account referenced a decision the schema does not carry"
_KNOWN_FAULT = _FAULT_HEAD + _CONTROL_BYTE + " " + ("filler " * 40)


def test_a_raised_unexpected_model_behavior_reaches_the_finding_sanitized_and_clamped(
        tmp_path, require_gitleaks):
    """OLD behaviour, before this change: `_run_meeting`'s UMB arm named only
    `ex.__class__.__name__` in the Finding's message — `str(ex)` reached the `log.warning` line and
    nothing else, so a real UMB fault (the framework exhausting its own output re-validations,
    which `test_a_framework_that_exhausts_its_output_retries_also_takes_the_retry_road` above
    drives for real) was indistinguishable from every other one at the one place a corrective
    retry or a failed report could read what actually went wrong.

    Driven by a model that raises `UnexpectedModelBehavior` DIRECTLY, deliberately unlike that
    test: the property under test here is the WRAP, not whether the framework's own retry budget
    genuinely exhausts. This is `test_a_mid_run_provider_fault_is_wrapped_by_class_name_and_never_
    by_its_message`'s own twin, one exception type over — same direct-raise pattern, opposite
    named-vs-class-only outcome.
    """
    def _raising():
        def _explode(messages, info):
            raise UnexpectedModelBehavior(_KNOWN_FAULT)
        return FunctionModel(_explode)

    env, deps, agent = _rig(tmp_path, _raising)

    with pytest.raises(OutcomeShapeError) as exc_info:
        agent.run_meeting(worktree=env.repo, material=_TRANSCRIPT,
                          meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                          registry=deps.registry,
                          source_page_path="sources/meetings/q3-sync.md")

    findings = exc_info.value.findings
    assert findings and findings[0].gate == agent_module._OUTCOME_GATE
    message = findings[0].message
    assert _CONTROL_BYTE not in message, "the control byte survived sanitize"
    assert _FAULT_HEAD in message, "the recognizable head of the fault text did not survive clamp"
    assert _KNOWN_FAULT.replace(_CONTROL_BYTE, "") not in message, (
        "the whole padded fault text reached the message unclamped")
    assert "…" in message, "no ellipsis — the fault does not read as truncated"
    # `Finding.brief` is unset on this arm too, so the corrective retry still sees the fault text —
    # through the fallback `corrective_brief` documents, not a second copy.
    assert findings[0].brief == ""
    assert _FAULT_HEAD in gates.corrective_brief(findings)


def test_benign_twin_a_normal_successful_meeting_carries_no_fault_machinery(tmp_path,
                                                                            require_gitleaks):
    """The specificity half, mirrored from the ordinary flow's own twin
    (`test_agentic_processing_pg.test_benign_twin_a_normal_successful_run_carries_no_fault_
    machinery`): `MAX_FAULT_MESSAGE_LEN`'s sanitize/clamp seam belongs to the UMB and
    provider-fault arms above and nowhere else. An ordinary `summary` — longer than
    `MAX_FAULT_MESSAGE_LEN` but well under `agent_module.MAX_PROSE_LEN` — must reach
    `run.outcome.summary` byte for byte; a regression that ran every outcome field through the
    fault seam would silently clip a real summary at 200 characters and this is what would notice.
    """
    long_summary = "distilled the meeting, " + ("padding word " * 20)
    assert len(long_summary) > pydantic_backend.MAX_FAULT_MESSAGE_LEN, (
        "the fixture is not exercising the ceiling")
    account = _account().model_copy(update={"summary": long_summary})
    env, deps, agent = _rig(tmp_path, lambda: _test_model(account))

    run = agent.run_meeting(worktree=env.repo, material=_TRANSCRIPT,
                            meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                            registry=deps.registry,
                            source_page_path="sources/meetings/q3-sync.md")

    assert run.outcome.summary == long_summary
    assert "UnexpectedModelBehavior" not in run.outcome.summary


def test_an_account_over_the_outcome_ceiling_is_refused_on_this_channel_too(tmp_path,
                                                                            require_gitleaks):
    """The SAME ceiling the file channel applies to `.librarian-outcome.json`, on the channel that
    has no file to stat.

    A structured output is bounded by the schema's SHAPE and by nothing else — every string field is
    unbounded — so a model that repeats a whole transcript into `meeting_notes` produces an account
    that would be parsed and truncated field by field only after costing the memory on the way in.
    One constant, two channels: a bound that existed on one of them is a bound an operator would
    reasonably assume applies to both.
    """
    huge = _account()
    huge.meeting_notes = "x" * (agent_module.MAX_OUTCOME_BYTES + 1)
    env, deps, agent = _rig(tmp_path, lambda: _test_model(huge))

    with pytest.raises(AgentError) as exc_info:
        agent.run_meeting(worktree=env.repo, material=_TRANSCRIPT,
                          meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                          registry=deps.registry,
                          source_page_path="sources/meetings/q3-sync.md")

    message = str(exc_info.value)
    assert str(agent_module.MAX_OUTCOME_BYTES) in message
    assert not isinstance(exc_info.value, OutcomeShapeError), (
        "an over-ceiling account is not a shape the corrective retry can repair by re-asking — it "
        "is a fault, and routing it to the retry would spend a second full run on the same answer")
    assert exc_info.value.run_cost_usd > 0, "the oversized account was paid for"


def test_an_ordinary_sized_account_sails_past_that_ceiling(tmp_path, clean_queue,
                                                           require_gitleaks):
    """The benign twin, and the one with a cost attached: the ceiling stands between every real
    meeting and its page set, so a bound set too low would refuse ordinary work. A full, padded,
    two-decision account is nowhere near it."""
    env, deps, _ = _rig(tmp_path, lambda: _test_model(_account(decisions=2)))

    support.submit_meeting(clean_queue, deps, _TRANSCRIPT)
    _, result = worker.process_next(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")


def test_a_price_that_disappears_mid_flight_cannot_replace_the_fault_it_was_annotating(
        tmp_path, require_gitleaks, monkeypatch):
    """**The defense with the sharpest failure mode, and it had none of its own tests.**

    `_cost` can refuse — an unpriced model raises `LibrarianConfigError` — and it is called from
    INSIDE the fault handlers. Letting that escape would replace the fault being reported with a
    configuration complaint about the annotation, and the operator would never see what actually
    went wrong: the run failed for one reason and the log says another.

    Reproduced the way a deployment reaches it: a model priced only by
    `$STIGMERGY_LIBRARIAN_PRICING` (so construction's own backstop passes), and the variable edited
    away before the run — the override is read at CALL time, which is the whole point of it. The
    fault then has to keep its own message and price at `0.0` honestly.
    """
    only_priced_by_override = "openai:gpt-9"
    monkeypatch.setenv(pricing.PRICING_ENV, json.dumps({only_priced_by_override: [2.0, 2.0, 12.0]}))

    def _explode():
        def _boom(messages, info):
            raise RuntimeError("the provider fell over")
        return FunctionModel(_boom)

    env, deps, agent = _rig(tmp_path, _explode, model=only_priced_by_override)
    monkeypatch.delenv(pricing.PRICING_ENV)          # ...the operator edits their env and restarts

    with pytest.raises(AgentError) as exc_info:
        agent.run_meeting(worktree=env.repo, material=_TRANSCRIPT,
                          meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                          registry=deps.registry,
                          source_page_path="sources/meetings/q3-sync.md")

    message = str(exc_info.value)
    assert "the meeting agent run failed" in message, (
        "the pricing complaint replaced the fault it was supposed to annotate")
    assert pricing.PRICING_ENV not in message
    assert exc_info.value.run_cost_usd == 0.0, (
        "an unpriceable fault must report 0.0, which is the honest figure when nothing could be "
        "computed — not a guess and not a crash")


def test_the_wall_clock_is_ours_and_a_pass_that_outlives_it_is_refused(tmp_path,
                                                                       require_gitleaks):
    """pydantic-ai has no wall clock of its own, exactly like the Agent SDK, and the worker's
    visibility lease is derived from this number — a pass that could outlive it is a capture two
    workers file. The refusal names the budget, so an operator reading a `failed` row knows which
    knob it was."""
    import asyncio

    def _slow():
        async def _crawl(messages, info):
            await asyncio.sleep(30)
            raise AssertionError("the timeout did not fire")
        return FunctionModel(_crawl)

    env, deps, agent = _rig(tmp_path, _slow, timeout_s=1)

    with pytest.raises(AgentError) as exc_info:
        agent.run_meeting(worktree=env.repo, material=_TRANSCRIPT,
                          meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                          registry=deps.registry,
                          source_page_path="sources/meetings/q3-sync.md")

    assert "1s" in str(exc_info.value)
    assert exc_info.value.run_cost_usd == 0.0, (
        "a timeout that never reached a priced response must price at 0.0 honestly, not at a guess")


def test_a_missing_meeting_brief_refuses_before_a_model_call_is_spent(tmp_path,
                                                                      require_gitleaks):
    """The brief is read out of the WORKTREE — the checkout at this item's base commit, the same
    read the SDK backend makes — and a missing one is a configuration fault, refused before
    anything is paid for. The factory doubles as the assertion: it must never be reached."""
    def _never():
        raise AssertionError("a model was built before the operating procedure was found")

    env, deps, agent = _rig(tmp_path, _never)
    os.remove(os.path.join(env.repo, ".claude", "skills", "meeting-distiller", "SKILL.md"))

    with pytest.raises(LibrarianConfigError):
        agent.run_meeting(worktree=env.repo, material=_TRANSCRIPT,
                          meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                          registry=deps.registry,
                          source_page_path="sources/meetings/q3-sync.md")


def test_the_envelope_reports_zero_turns_and_zero_tool_calls_rather_than_inventing_a_one(
        tmp_path, require_gitleaks):
    """A structured backend makes one model call and reads its typed output: it has no
    conversational loop and no tool to call, so `0` is the honest telemetry. The port documents
    zero as a legitimate answer and nothing downstream branches on either counter — a `1` here
    would be a number invented to look like the SDK backend's."""
    env, deps, agent = _rig(tmp_path, lambda: _test_model(_account()))

    run = agent.run_meeting(worktree=env.repo, material=_TRANSCRIPT,
                            meeting_meta={"title": "Q3 sync", "meeting_date": "2026-07-29"},
                            registry=deps.registry,
                            source_page_path="sources/meetings/q3-sync.md")

    assert (run.turns, run.tool_calls) == (0, 0)
    assert run.outcome is not None and run.outcome.decision == "file"
    assert run.cost_usd > 0
