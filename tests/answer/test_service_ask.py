"""The full answering loop over the real `BrainService`: the agent gathers evidence, the
deterministic verifier judges figures and citations, exactly one corrective retry is allowed, and
the verdict travels with the answer — all under the STRICT GATE, where an unverified figure
suppresses the answer instead of shipping it labeled `failed`. Needs postgres (skips without
it)."""
import asyncio
import types

from pydantic_ai.exceptions import UsageLimitExceeded

import stigmergy.answer.service as service_mod
from stigmergy.answer.synthesize import AnswerOutput, Citation
from tests.answer.conftest import brain_service


def _ask(service, q):
    return asyncio.run(service.ask(q))




def test_prose_question_cites_top_page(ask_service):
    res = _ask(ask_service, "what are the roadmap themes?")
    assert res["refused"] is False
    assert res["citations"]
    assert res["verdict"]["verdict"] == "verified"


def test_unanswerable_question_refuses(ask_service):
    res = _ask(ask_service, "zebra unicorn parking policy in antarctica?")
    assert res["refused"] is True
    assert res["answer_markdown"] == ""
    assert res["verdict"]["verdict"] == "verified"        # refusing cleanly is verified behavior
    assert res["suppressed"] is False                     # a real refusal, not a suppression



def test_refusal_wording_has_no_research_or_ingest_offer(ask_service):
    """The refusal is honest and offer-free — never 'want me to research/ingest it?': a refusal
    states a fact about the run and stops, rather than inviting an action."""
    res = _ask(ask_service, "zebra unicorn parking policy in antarctica?")
    reason = res["reason"].lower()
    for forbidden in ("research", "ingest", "want me to", "shall i", "should i"):
        assert forbidden not in reason


def test_answer_output_schema_no_longer_carries_a_reason_field():
    """Dropped entirely, not merely unread — a field the prompt still asked for and the model
    still filled, that no code path reads, is exactly the half-wired shape a future edit could
    "helpfully" reconnect without re-litigating why it was cut."""
    assert "reason" not in AnswerOutput.model_fields


def test_refusal_carries_the_additive_structured_facts(ask_service):
    """`refusal_case`/`searched`/`surfaced` are ADDITIVE — a client that only ever read
    `.reason` keeps working unchanged, and one that wants the facts structurally can now read them
    beside it (the same posture `reason_code` already takes beside a rejection sentence)."""
    res = _ask(ask_service, "zebra unicorn parking policy in antarctica?")
    assert res["refused"] is True
    assert res["refusal_case"] in ("no_surface", "no_match")
    assert isinstance(res["searched"], list) and isinstance(res["surfaced"], list)


def test_no_surface_when_genuinely_no_tool_call_ran_at_all(ask_service, monkeypatch):
    """The degenerate case: "should not happen given the agent's own instructions, but must not
    crash if it does". A synthesizer that refuses without ever calling a tool leaves
    both `ctx.searched` and `ctx.read_paths_order` empty."""
    class Scripted:
        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            out = AnswerOutput(refused=True, confidence="low")
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])

    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: Scripted())
    res = _ask(ask_service, "anything at all")
    assert res["refused"] is True
    assert res["refusal_case"] == "no_surface"
    assert res["searched"] == [] and res["surfaced"] == []
    assert res["reason"] == "nothing came back this run — no tool call found anything to work with."


def test_a_refused_first_draft_never_spends_the_retry_even_if_the_verifier_fails_it(
        ask_service, monkeypatch):
    """The refusal guard on the retry trigger is STRUCTURAL (`not out.refused`), not a side
    effect of the verifier's data. Today a refusal is vacuously `verified`, so a data-only
    trigger would happen to skip it — but that is a property of `verify()`, not a guarantee of
    the policy, and this module's history includes refusal-reason scanning that could change it.
    Force the verifier to call a refusal `failed`: the retry must still not fire — a refusal is
    an ANSWER, not a defect to repair, and a second paid run could otherwise replace an honest
    refusal with a drafted answer."""
    class Scripted:
        def __init__(self):
            self.calls = 0

        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            self.calls += 1
            deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
            out = AnswerOutput(refused=True, confidence="low")
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])

    monkeypatch.setattr(service_mod, "verify",
                        lambda *a, **kw: {"verdict": "failed", "unverified_figures": [],
                                          "citation_problems": ["forced-a", "forced-b"]})
    scripted = Scripted()
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: scripted)
    res = _ask(ask_service, "anything about globex")
    assert scripted.calls == 1 and res["retried"] is False
    assert res["refused"] is True


def test_ask_retry_fires_and_improves(ask_service, monkeypatch):
    """A first answer with an invented figure and a bogus citation fails the verifier; the
    corrective retry (with findings in the prompt) wins only because it improves."""
    class Scripted:
        def __init__(self):
            self.calls = 0

        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            self.calls += 1
            deps.record(deps.service.search_text("globex quarterly report", deps))
            deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
            if self.calls == 1:
                out = AnswerOutput(answer_markdown="Revenue was 9.9M with 77% margin.",
                                   citations=[Citation(path="entities/nowhere.md", quote="ghost")])
            else:
                assert "DETERMINISTIC VERIFIER" in prompt   # findings reached the retry
                out = AnswerOutput(
                    answer_markdown="Revenue impact was $1.3M ARR.",
                    citations=[Citation(path="wiki/notes/globex-q1-report-final.md",
                                        quote="Revenue impact was $1.3M ARR")])
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])

    scripted = Scripted()
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: scripted)
    res = _ask(ask_service, "globex revenue?")
    assert res["retried"] is True
    assert scripted.calls == 2                             # exactly one retry, never a third
    assert res["verdict"]["verdict"] == "verified"
    assert "1.3M" in res["answer_markdown"] and "9.9M" not in res["answer_markdown"]


# ── the corrective retry carries the FIRST run's message history ──────────────────────────────
# THE point of the change under test: without this, the retry's prompt held only the question, the
# previous draft and the verifier's findings, so the model re-searched and re-read the very pages
# it had just read — a corrective pass cost about as much as a first one. A regression here is
# SILENT (the retry still fires, still wins on an improved draft, every existing retry test above
# still goes green) and costs a second full agent run in production. This is the one test that
# would catch `service.py` dropping the `message_history=` kwarg, or passing the wrong object.
def test_ask_retry_carries_the_first_runs_message_history(ask_service, monkeypatch):
    """`agent.run()`'s SECOND call (the corrective retry) must receive
    `message_history=` set to EXACTLY the object `result.all_messages()` returned from the FIRST
    call — not a copy, not `None`, not an empty stand-in — and the FIRST call must receive none.
    The double is typed against the real shape `agent.run()` returns
    (`output`/`usage`/`all_messages()`, the same `types.SimpleNamespace` idiom every other double in
    this file uses to stand in for pydantic_ai's own `AgentRunResult` — an external SDK boundary,
    not an internal interface of this codebase)."""
    FIRST_RUN_MESSAGES = [{"role": "assistant", "marker": "FIRST-RUN-MESSAGE-HISTORY-7f2a"}]

    class Scripted:
        def __init__(self):
            self.calls = 0
            self.received_history = []

        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            self.calls += 1
            self.received_history.append(message_history)
            deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
            if self.calls == 1:
                out = AnswerOutput(answer_markdown="Revenue was 9.9M with 77% margin.",
                                   citations=[Citation(path="entities/nowhere.md", quote="ghost")])
                all_msgs = FIRST_RUN_MESSAGES
            else:
                assert "DETERMINISTIC VERIFIER" in prompt   # findings reached the retry too
                out = AnswerOutput(
                    answer_markdown="Revenue impact was $1.3M ARR.",
                    citations=[Citation(path="wiki/notes/globex-q1-report-final.md",
                                        quote="Revenue impact was $1.3M ARR")])
                all_msgs = []   # the retry's own run produced no further history worth carrying
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: all_msgs)

    scripted = Scripted()
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: scripted)
    res = _ask(ask_service, "globex revenue?")

    assert scripted.calls == 2 and res["retried"] is True
    assert scripted.received_history[0] is None                     # the FIRST call carries no history
    # the SECOND call carries exactly (same object) what the first run's own all_messages() returned
    assert scripted.received_history[1] is FIRST_RUN_MESSAGES
    assert res["verdict"]["verdict"] == "verified"                   # sanity: this is still a real retry


def test_fake_synthesizer_completes_a_retry_with_empty_history(ask_service, monkeypatch):
    """The benign twin of the test above: an EMPTY history (`all_messages()` returning `[]`, the
    offline synthesizer's own reality — it has no model turns to carry) must not break the retry.
    Run through the real, unmocked `synthesize.FakeSynthesizer` (the `ask_service` fixture's own
    backend, `llm=\"fake\"`) rather than a scripted double, so this proves the actual offline code
    path — not a stand-in for it — survives being handed `message_history=[]` on its second call.

    `verify` is monkeypatched to force one suppression-shaped verdict (`failed`, two citation
    problems — the shape that spends the retry under the suppression-gated retry's suppression-only policy), because
    the fake synthesizer's own draft (built verbatim from a page it just read) verifies cleanly on
    the first try against every page in this fixture's corpus — there is no fixture page whose
    fake-drafted answer naturally fails verification, so forcing the branch is the only way to
    exercise a REAL second `FakeSynthesizer.run()` call without reshaping the shared corpus (which
    every other test in this module-scoped fixture also depends on). `verify`'s own behavior is
    exhaustively covered elsewhere (`tests/answer/test_verify.py`); this test asserts nothing
    about it beyond forcing the branch once."""
    real_verify = service_mod.verify
    calls = {"n": 0}

    def flaky_verify(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"verdict": "failed", "unverified_figures": [],
                    "citation_problems": ["forced by the test to exercise the retry branch",
                                          "a second forced problem so the gate would suppress"]}
        return real_verify(*a, **kw)

    monkeypatch.setattr(service_mod, "verify", flaky_verify)
    res = _ask(ask_service, "what are the roadmap themes?")

    assert calls["n"] == 2                    # the retry fired exactly once, over the real fake path
    assert res["retried"] is True
    assert "verdict" in res and res["verdict"]["verdict"] in ("verified", "partial", "failed")


def test_partial_first_attempt_ships_without_spending_the_retry(ask_service, monkeypatch):
    """OLD BEHAVIOUR: ANY non-`verified` first draft spent the corrective retry — a second full
    agent run — including this one, a single-citation-problem draft that ships labelled `partial`
    with or without it, so the second run bought a label (measured on staging 2026-08 as the
    dominant case: ~41 % of asks retrying, not one answer ever suppressed). the suppression-gated retry confines the
    retry to drafts the strict gate would SUPPRESS; this is the confinement's benign twin — the
    shippable `partial` draft goes out on ONE run, unretried, still labelled honestly.

    (It replaces the old strictly-worse-retry test at this spot: with the trigger reading the
    gate's own rank, a retry only ever fires FROM the suppression tier, so there is no worse tier
    left for a regressed retry to reach — the guard that test pinned became structural. The tie —
    a retry that also suppresses — is still pinned by
    `test_strict_gate_suppresses_when_retry_also_leaves_an_unverified_figure`.)"""
    class Scripted:
        def __init__(self):
            self.calls = 0

        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            self.calls += 1
            deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
            # no figures (nothing to mistrace) but zero citations -> exactly one problem -> partial
            out = AnswerOutput(answer_markdown="Globex had a strong quarter.", citations=[])
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])

    scripted = Scripted()
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: scripted)
    res = _ask(ask_service, "how did globex do?")

    assert scripted.calls == 1 and res["retried"] is False   # the retry was NOT spent
    assert res["refused"] is False and res["suppressed"] is False
    assert res["verdict"]["verdict"] == "partial"
    assert res["answer_markdown"] == "Globex had a strong quarter."


def test_strict_gate_suppresses_when_retry_also_leaves_an_unverified_figure(ask_service, monkeypatch):
    """Two failing attempts (never a third) — and because an unverified figure remains, the
    STRICT GATE suppresses the answer and returns a refusal carrying the findings. An untraced
    number never leaves in a human-readable channel; it surfaces only inside `verdict`."""
    class Scripted:
        def __init__(self):
            self.calls = 0

        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            self.calls += 1
            deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
            bad = AnswerOutput(answer_markdown=f"Invented {self.calls * 111}% and {self.calls * 222}%.",
                               citations=[])
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=bad, usage=usage, all_messages=lambda: [])

    scripted = Scripted()
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: scripted)
    res = _ask(ask_service, "globex revenue?")
    assert res["retried"] is True and scripted.calls == 2
    assert res["refused"] is True and res["suppressed"] is True
    assert res["answer_markdown"] == ""                   # no unverified figure leaves
    assert res["verdict"]["verdict"] == "failed"
    assert "111%" in res["verdict"]["unverified_figures"]  # the findings travel in the verdict …
    assert "111" not in res["reason"]                       # … and NEVER in the shipped prose


def test_quote_figure_suppression_earns_the_retry_and_the_gates_findings_reach_it(ask_service, monkeypatch):
    """The suppression-gated trigger reads the GATE's scan, not the raw verifier verdict — and so
    does the corrective brief. A first draft whose body is figure-free but whose citation QUOTE
    carries an invented figure is raw-`partial` (one citation problem; the raw verdict cannot see
    quote figures) yet gate-suppressed — so the retry fires, and the fabricated figure reaches
    the retry prompt BY NAME, which the old brief (built from the raw findings) never carried.
    When the retry supplies a real quote, it wins on the gate's own rank and ships `verified`."""
    class Scripted:
        def __init__(self):
            self.calls = 0

        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            self.calls += 1
            deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
            if self.calls == 1:
                out = AnswerOutput(
                    answer_markdown="Globex performed well this quarter.",
                    citations=[Citation(path="wiki/notes/globex-q1-report-final.md",
                                        quote="secret revenue was 7777777")])   # not in the page
            else:
                assert "DETERMINISTIC VERIFIER" in prompt
                assert "7777777" in prompt   # the GATE's finding — a quote figure — reached the retry
                out = AnswerOutput(
                    answer_markdown="Globex performed well this quarter.",
                    citations=[Citation(path="wiki/notes/globex-q1-report-final.md",
                                        quote="Quarterly business review for Globex")])
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])

    scripted = Scripted()
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: scripted)
    res = _ask(ask_service, "how was globex?")
    assert res["retried"] is True and scripted.calls == 2      # exactly one retry, never a third
    assert res["refused"] is False and res["suppressed"] is False
    assert res["verdict"]["verdict"] == "verified"
    assert res["citations"] and res["citations"][0]["path"] == "wiki/notes/globex-q1-report-final.md"


def test_ask_result_and_audit_carry_token_usage_counts(ask_service, monkeypatch):
    """`ask` returns `usage` — token COUNTS, both runs summed when the retry fires — and
    `audit_summary` copies them into the audit row's closed key set. Counts, not the SDK's whole
    usage object: this feeds the one column whose contract is that it carries no transcript.
    The scripted first draft carries an untraced figure so the retry (and the summing) runs."""
    class Scripted:
        def __init__(self):
            self.calls = 0

        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            self.calls += 1
            deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
            if self.calls == 1:
                out = AnswerOutput(answer_markdown="Invented 999% growth.", citations=[])
            else:
                out = AnswerOutput(
                    answer_markdown="Globex performed well this quarter.",
                    citations=[Citation(path="wiki/notes/globex-q1-report-final.md",
                                        quote="Quarterly business review for Globex")])
            usage = types.SimpleNamespace(requests=2, input_tokens=100, output_tokens=10,
                                          cache_read_tokens=40, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])

    scripted = Scripted()
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: scripted)
    res = _ask(ask_service, "how was globex?")
    assert scripted.calls == 2 and res["retried"] is True
    assert res["usage"] == {"requests": 4, "input_tokens": 200,
                            "cache_read_tokens": 80, "output_tokens": 20}   # both runs, summed
    assert service_mod.audit_summary(res)["usage"] == res["usage"]


def test_strict_gate_ships_citation_only_problem_as_partial(ask_service, monkeypatch):
    """A citation-only problem (no figures) is NOT suppressed — it ships labeled `partial`."""
    class Scripted:
        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
            out = AnswerOutput(answer_markdown="Globex had a strong quarter.", citations=[])
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])

    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: Scripted())
    res = _ask(ask_service, "how was globex?")
    assert res["refused"] is False and res["suppressed"] is False
    assert res["verdict"]["verdict"] == "partial"
    assert res["verdict"]["unverified_figures"] == []
    assert res["answer_markdown"] == "Globex had a strong quarter."


def test_model_has_no_channel_left_to_smuggle_a_refusal_figure_through(ask_service, monkeypatch):
    """Closed ARCHITECTURALLY rather than by scanning harder: a steered model used to be able to
    REFUSE while smuggling an untraced figure into a free-text `reason` field,
    which the gate then had to scan and scrub. `AnswerOutput` no longer HAS a `reason` field at
    all — a synthesizer that tries to set one (a steered real model producing an extra key in its
    structured output, or — as here — a test double passing it positionally) is silently ignored
    by pydantic's own `extra='ignore'` default, so the figure never reaches `ctx` at all. The
    shipped `reason` is composed entirely from `ctx.searched`/`ctx.read_paths_order` (server
    facts), so there is nothing left to scrub in the first place."""
    class Scripted:
        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
            out = AnswerOutput(refused=True, confidence="low",
                               reason="I could not find it, but the classified revenue was 5550000.")
            assert not hasattr(out, "reason")   # dropped from the schema, not merely unread
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])

    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: Scripted())
    res = _ask(ask_service, "what is the classified revenue?")
    assert res["refused"] is True
    assert "5550000" not in res["reason"]                       # the figure never ships in prose …
    assert "5550000" not in res["verdict"]["unverified_figures"]  # … and never enters the verdict
    # a genuine refusal that surfaced a page (globex, via page_text) IS `no_match` structurally —
    # `refusal_case` is computed straight from `ctx.read_paths_order`, unaffected by the defensive
    # backstop below.
    assert res["refusal_case"] == "no_match"
    # A side effect of that backstop, not a defect: the surfaced page's OWN title
    # ("Quarterly Report Q1 2026 FINAL") carries a figure (2026) absent from the question, and
    # `_compose_reason`'s defensive backstop now scans the composed reason against the QUESTION
    # (never the full evidence text — the tool's own `no results for: {query}` echo must not count
    # as "tracing" a model-chosen figure). That backstop fires here, honestly, and falls back to
    # the generic sentence rather than naming a title carrying an unrelated number — still 100%
    # true, just less specific than `no_match`'s named ending.
    assert res["reason"] == "nothing came back this run — no tool call found anything to work with."


def test_strict_gate_suppresses_a_fabricated_citation_quote_figure(ask_service, monkeypatch):
    """The answer body carries no figure, but a fabricated citation QUOTE does — and
    that quote ships. The gate scans citation quotes too, so the untraced figure suppresses the
    whole response (verify() alone would only flag the bad citation and ship it as `partial`)."""
    class Scripted:
        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
            out = AnswerOutput(
                answer_markdown="Globex performed well this quarter.",
                citations=[Citation(path="wiki/notes/globex-q1-report-final.md",
                                    quote="secret revenue was 8888888")])   # not in the page
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])

    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: Scripted())
    res = _ask(ask_service, "how did globex do?")
    assert res["refused"] is True and res["suppressed"] is True
    assert res["answer_markdown"] == "" and res["citations"] == []
    assert "8888888" in res["verdict"]["unverified_figures"]
    assert "8888888" not in res["reason"]


def test_two_citation_only_problems_refuse_with_findings(ask_service, monkeypatch):
    """Two citation-only problems (no figures) = a `failed` verdict, which never ships.
    It refuses with the findings travelling in the verdict — only EXACTLY ONE citation-only problem
    ships, labeled `partial`."""
    class Scripted:
        async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
            deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
            out = AnswerOutput(
                answer_markdown="Globex performed well.",
                citations=[Citation(path="wiki/entities/nowhere-a.md", quote="ghost a"),
                           Citation(path="wiki/entities/nowhere-b.md", quote="ghost b")])
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])

    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: Scripted())
    res = _ask(ask_service, "how did globex do?")
    assert res["refused"] is True and res["suppressed"] is True
    assert res["verdict"]["verdict"] == "failed"
    assert len(res["verdict"]["citation_problems"]) == 2
    # the reason is composed server-side (`run_facts_reason`'s `suppressed_citations` template)
    # and states the ACTUAL cause — citations, not figures — never a figure, and never a PATH
    # (nowhere-a/b do not exist as pages, so their titles fall back to the generic placeholder
    # rather than leaking the path).
    assert res["refusal_case"] == "suppressed_citations"
    assert "couldn't confirm word-for-word" in res["reason"]
    assert "figure" not in res["reason"] and "nowhere" not in res["reason"]
    assert "wiki/entities" not in res["reason"]      # never a path, even a fabricated one
    assert "figure" not in res["reason"] and "nowhere" not in res["reason"]


# `ask` handles a usage-budget refusal at either model call site.
class _BudgetExceededFirstRun:
    """`UsageLimitExceeded` on the very FIRST `agent.run()` call, before any `AnswerOutput` ever
    existed to verify — observed on the real model (ANSWER_LLM=openrouter) over the 8-tool-call
    budget: golden case `aurora-timeline-q1` hit it and the exception propagated uncaught out of
    `AnswerService.ask()`, crashing `evals/run_qa.py` mid-run."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
        self.calls += 1
        # A real tool call first, so the composed refusal has something structured to cite —
        # `page_text` records `ctx.read_paths_order` (note_page) and the query is noted directly.
        # ONE page, deliberately, and a digit-free one (Acme, not Globex/Initech/Roadmap, whose
        # titles all carry "2026"): a `search_text` call would surface all of them and the
        # composed reason would trip `_compose_reason`'s own defensive figure scan, falling
        # back to the generic sentence and making this test assert nothing about the actual
        # budget-exceeded wording.
        deps.note_query("total-compensation acme")
        deps.record(deps.service.page_text("wiki/notes/acme-payroll.md", deps))
        raise UsageLimitExceeded("The next request would exceed the request_limit of 6")


class _BudgetExceededOnRetry:
    """Same road, the OTHER call site: the FIRST run drafts an answer that fails verification (so
    `verdict` already carries real findings by the time the exception hits), and it is the
    CORRECTIVE RETRY that exceeds the budget."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, deps=None, usage_limits=None, message_history=None):
        self.calls += 1
        deps.record(deps.service.page_text("wiki/notes/globex-q1-report-final.md", deps))
        if self.calls == 1:
            out = AnswerOutput(answer_markdown=f"Invented {self.calls * 111}% and {self.calls * 222}%.",
                               citations=[])
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0, details={})
            return types.SimpleNamespace(output=out, usage=usage, all_messages=lambda: [])
        assert "DETERMINISTIC VERIFIER" in prompt   # findings reached the retry before it hit budget
        raise UsageLimitExceeded("The next request would exceed the request_limit of 6")


def test_budget_exceeded_on_first_run_refuses_honestly_without_spending_the_retry(ask_service, monkeypatch):
    """`ask` used to let `UsageLimitExceeded` propagate uncaught out of the FIRST `agent.run()`
    call. This pins the contract that replaced it — a first-class honest refusal, composed the
    same way every other refusal is — so it goes red against the old behaviour with the raw
    `UsageLimitExceeded` rather than reaching any of these assertions."""
    scripted = _BudgetExceededFirstRun()
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: scripted)
    res = _ask(ask_service, "total-compensation acme?")
    assert scripted.calls == 1                            # the corrective retry was never spent
    assert res["refused"] is True and res["suppressed"] is False
    assert res["retried"] is False
    assert res["answer_markdown"] == ""
    assert res["refusal_case"] == "budget_exceeded"
    # vacuously verified — no drafted answer ever existed to distrust (same posture every other
    # genuine refusal takes; `verify_answer.verify` would say the same for an `out.refused` draft)
    assert res["verdict"] == {"verdict": "verified", "unverified_figures": [], "citation_problems": []}
    # the reason names what ran so far — never invents an explanation, never offers to retry
    assert res["searched"] == ["total-compensation acme"]
    assert res["surfaced"] == ["Acme payroll summary"]
    assert res["reason"] == ('searched "total-compensation acme", surfaced Acme payroll summary — '
                             "the answer could not be completed within the tool budget.")
    assert res["usage"] is None    # the run died mid-flight; there is no usage object to read


def test_budget_exceeded_on_corrective_retry_keeps_the_first_runs_outcome(ask_service, monkeypatch):
    """The RETRY's own `UsageLimitExceeded` must never crash `ask` either — it falls back to
    whatever the FIRST run already produced, the same "the retry wins only if it improves" logic
    an unimproved retry already takes (there is simply no `out2`/`v2` to compare here). The first
    run's own draft still goes through the strict gate normally: an unverified figure suppresses
    it into a `suppressed_figures` refusal, not a `budget_exceeded` one — the run DID produce a
    judged draft, it just never got a corrective second look."""
    scripted = _BudgetExceededOnRetry()
    monkeypatch.setattr(service_mod, "build_synthesizer", lambda settings: scripted)
    res = _ask(ask_service, "globex revenue?")
    assert scripted.calls == 2                            # the retry WAS attempted (and hit budget)
    assert res["retried"] is True
    assert res["refused"] is True and res["suppressed"] is True
    assert res["answer_markdown"] == ""
    assert res["verdict"]["verdict"] == "failed"
    assert "111%" in res["verdict"]["unverified_figures"]
    assert res["refusal_case"] == "suppressed_figures"     # the FIRST run's outcome, not a budget one
    assert "111" not in res["reason"]                       # findings never leak into shipped prose


def test_renderers_neutralize_a_hostile_title(answer_indexed):
    """A page whose TITLE reproduces the fence close token cannot forge a fence in either
    renderer — page_text neutralizes the title in the head, and search_text wraps the whole listing
    in the fence with in-band tokens neutralized. In both, the ONLY intact close delimiter is the
    renderer's own."""
    conn, fx = answer_indexed
    # `_FENCE_NEUTRALIZED` comes from `stigmergy.text`, the one module that builds the fence —
    # `service.py` keeps no copy of its own.
    from stigmergy.answer.brain import AnswerBrain
    from stigmergy.text import _FENCE_NEUTRALIZED
    brain = AnswerBrain(brain_service(conn, fx, "steward"))

    page = brain.page_text(fx.HOSTILE_TITLE)
    assert page.count("UNTRUSTED-DATA;end>>>") == 1        # only the body fence's close survives
    assert _FENCE_NEUTRALIZED in page                       # the title's token was neutralized

    listing = brain.search_text("hostile title probe")
    assert listing.startswith("<<<UNTRUSTED-DATA\n")        # the whole listing is fenced
    assert listing.count("UNTRUSTED-DATA;end>>>") == 1      # the title's in-band token cannot close it
    assert _FENCE_NEUTRALIZED in listing


def test_search_text_carries_current_page_metadata(answer_indexed):
    conn, fx = answer_indexed
    from stigmergy.answer.brain import AnswerBrain
    brain = AnswerBrain(brain_service(conn, fx, "steward"))
    listing = brain.search_text("globex quarterly report revenue")
    assert "note" in listing
    assert "2026-04-01" in listing



def test_page_text_fences_body_and_reports_current_metadata(answer_indexed):
    conn, fx = answer_indexed
    from stigmergy.answer.brain import AnswerBrain
    brain = AnswerBrain(brain_service(conn, fx, "steward"))
    txt = brain.page_text(fx.GLOBEX_DRAFT)
    assert "<<<UNTRUSTED-DATA" in txt
    assert "updated: 2026-03-31" in txt
    assert "unknown page" in brain.page_text("nope.md")
