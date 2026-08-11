"""The meeting flow on pydantic-ai — one structured model call, no tools, no outcome file.

The third `FilingAgent` implementation, and the first one that is not Claude's. It exists because
the meeting flow was ALREADY portable and nothing had noticed: the agent holds no page-writing tool
(code is the sole author of every page in the set), it explores nothing (the transcript, the
registry and the metadata are all handed to it), and its whole answer is one structured account. A
flow shaped like that does not need an agent harness at all — it needs a model that can return a
typed object. ADR 032 records the decision and the expand–contract plan; ADR 020 is why the flow
has this shape in the first place.

**MEETING ONLY, in this milestone.** `run` exists because the port requires it and raises, which is
a state no worker can reach: `worker.startup_checks` refuses `backend="pydantic"` outright unless
the caller is the meeting-only eval rig (`meeting_only=True`), because a worker's queue carries
ordinary captures too and a backend that half-serves a queue is the configuration this repo refuses
on principle. Lifting that is M2's job, not a flag.

**What is NOT reused, deliberately.** `kernel.llm.build_processor` is this repo's fake/real
dispatch for every OTHER agent, and it is the wrong seam here: the librarian's offline path is
`double.DoubleAgent` — a whole adversarial backend the suite is built on — and routing this module
through `resolve_backend` would create a SECOND offline path with different semantics answering to
a different variable (`$CLEAN_LLM` rather than `$STIGMERGY_LIBRARIAN_BACKEND`). What IS reused is
everything the flow already owns: `agent.read_meeting_brief` (the base-commit read),
`agent.build_meeting_prompt` (the per-item message), `agent.build_meeting_system_prompt` (the
brief's body as instructions) and `agent.parse_meeting_outcome` (the SAME trust boundary the file
channel goes through — a structured provider is not a trusted one).

**`pydantic_ai` is imported inside the method**, exactly as `claude_agent_sdk` is in `agent.py`:
a keyless run must not load an agent framework, and the import graph must not claim this package
depends on one unconditionally. `pydantic` itself is module-scope — the output schema below is
plain data, and a test that builds one by hand must not have to reach through a backend to do it.
"""
import json
import logging
import os

from pydantic import BaseModel, Field

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import gates, pricing
from stigmergy.librarian.errors import AgentError, LibrarianConfigError, OutcomeShapeError
from stigmergy.librarian.filing_port import AgentRun, priced

log = logging.getLogger(__name__)

BACKEND_NAME = "pydantic"

ADR = "docs/decisions/032-filing-port-and-pricing-seam.md"

# How many times the FRAMEWORK may re-ask the model when its answer does not satisfy the output
# schema. One constant, read twice on purpose: it is both the retry budget handed to the `Agent`
# and the request ceiling handed to `UsageLimits`, and those two numbers must agree or the ceiling
# either strangles a legitimate re-validation or stops bounding anything. One request, plus one
# re-ask: past that the answer is not a shape problem the framework can fix by asking again, and it
# belongs on the WORKER's own corrective retry, where the brief says what was wrong.
#
# **These re-asks are invisible to `AgentPasses.count`**, which counts the worker's passes. See
# ADR 032's envelope semantics: `turns`/`tool_calls` are `0` and `attempts` means our passes, so a
# framework re-validation costs money that IS banked (the usage accumulator sees it) under an
# attempt count that does not move.
OUTPUT_RETRIES = 1

# The provider prefixes this milestone names, and the environment variable each family
# authenticates with. Used by `worker.startup_checks`' preflight, which refuses a missing key
# BEFORE the first claim; an unknown prefix is not an error (a provider pydantic-ai supports and
# this table has not heard of is a legitimate configuration), it simply gets no preflight.
PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google-gla": "GEMINI_API_KEY",
}


def provider_of(model: str) -> str:
    """The provider prefix of a pydantic-ai model string, or `""` for a bare name."""
    name = (model or "").strip()
    return name.split(":", 1)[0] if ":" in name else ""


# ── the account, as a schema instead of a file ────────────────────────────────────────────────
# A field-for-field mirror of the JSON `agent.parse_meeting_outcome` already reads, so the two
# channels carry the SAME shape and the boundary parse is shared rather than forked. Every field
# has a default: a provider that omits an optional list must produce an outcome the boundary can
# judge (and refuse on its own terms), not a validation error inside the framework that never
# reaches the corrective retry.
#
# Bounds are deliberately NOT restated here. `parse_meeting_outcome` owns them — identifiers are
# refused over `MAX_IDENTIFIER_LEN`, prose and page bodies are truncated, lists are capped — and a
# second set of limits in a schema would be a second answer to the same question, drifting from the
# one the file channel is judged by.
class MeetingAnchoring(BaseModel):
    """One decision's own anchor. `kind` is `entity` (with `entities`) or `company` (with a written
    `reason`); the registry, not this schema, decides whether a name resolves."""
    kind: str = ""
    reason: str = ""
    entities: list[str] = Field(default_factory=list)


class MeetingDecision(BaseModel):
    """One decision page's content — a title, a drafted body, and its OWN anchor."""
    title: str = ""
    body: str = ""
    anchoring: MeetingAnchoring = Field(default_factory=MeetingAnchoring)


class MeetingActionItem(BaseModel):
    owner: str = ""
    action: str = ""
    done: bool = False


class MeetingFinding(BaseModel):
    """A steering attempt the agent noticed. Only `category` travels — never the payload."""
    category: str = ""


class MeetingTriage(BaseModel):
    """Why the capture was parked, when `decision` is `triage`."""
    kind: str = ""
    names: list[str] = Field(default_factory=list)
    judged_type: str = ""


class MeetingAccount(BaseModel):
    """The whole account of one meeting: `decision` is `file` or `triage`, and the rest is the page
    set's CONTENT — never a page path, because code decides every path in this flow."""
    decision: str = ""
    meeting_title: str = ""
    attendees: list[str] = Field(default_factory=list)
    meeting_notes: str = ""
    action_items: list[MeetingActionItem] = Field(default_factory=list)
    decisions: list[MeetingDecision] = Field(default_factory=list)
    summary: str = ""
    findings: list[MeetingFinding] = Field(default_factory=list)
    triage: MeetingTriage = Field(default_factory=MeetingTriage)


# This backend's own environment paragraph — the ONE part of the preamble that differs per backend.
# The opening, the shared points and the separator come from `agent.build_meeting_header`, which is
# where they are written once.
MEETING_ENVIRONMENT = (
    "Your environment:\n"
    "\n"
    "1. You have NO tools. You cannot read, search or write anything, and you do not write your "
    "account to a file: you RETURN it, as the structured object this run's output schema "
    "declares. That schema mirrors the shape the skill documents, field for field. Everything you "
    "need is in the worker's own message below: the transcript, the entity registry (every entity "
    "this brain already knows), the meeting metadata, and the source page's own path.\n")

# **The one place this run contradicts the brief, said out loud and immediately before it.**
#
# The brief is the knowledge repo's text and this milestone changes not one word of it — which
# means it still tells its reader, in its own voice, that it holds a `Write` tool and returns its
# account by writing `.librarian-outcome.json`. Injecting that under a preamble saying "you have NO
# tools" hands the model a flat contradiction and leaves it to guess which half is operative: a
# model that resolves it the other way describes writing a file it cannot write, and the run comes
# back with an account that is about the wrong thing. That is not a cosmetic prompt defect — it is
# noise on the exact measurement M3's retire-or-keep decision reads.
#
# So the override is NAMED, positioned last (a reader meets the correction before the text being
# corrected), and scoped as narrowly as it can honestly be: the tool and the file describe the SHAPE
# of the account, and every other word of the procedure applies unchanged.
OVERRIDE_NOTE = (
    f"One override, and it is the only place this run departs from the skill below. The skill was "
    f"written for a run that holds a `Write` tool and returns its account by writing "
    f"`{agent_module.OUTCOME_FILENAME}` at the repo root. This run has NEITHER: no tool, no file. "
    f"Where the skill describes that tool or that file, read it as describing the SHAPE of your "
    f"account only — you return that same object as this run's structured output instead. Every "
    f"other word of the skill applies to you unchanged.\n")

MEETING_SYSTEM_PROMPT_HEADER = agent_module.build_meeting_header(
    MEETING_ENVIRONMENT, override_note=OVERRIDE_NOTE)

# The one line of the per-item prompt that differs between the two channels — see
# `agent.build_meeting_prompt`, whose default is the file channel's own sentence.
OUTCOME_CHANNEL = (
    "\nReturn your account as the structured object this run's output schema declares, in the "
    "shape the skill documents. You write no file and you have no tool that could.")


class PydanticMeetingAgent:
    """The meeting flow's pydantic-ai backend. Conforms to `filing_port.FilingAgent` structurally —
    never by inheritance, so a backend is a class that answers the two calls and nothing more.

    `model_factory` is the offline seam, and it is the ONLY one this module has. It is a zero-arg
    callable returning anything pydantic-ai accepts as a model — a `TestModel`, a `FunctionModel`,
    or a model object built by hand — so the whole distillation path can be exercised keylessly by
    constructing this backend directly and injecting it as `processing.Deps.agent`, which is where
    every librarian test already injects an agent. Absent (every production path,
    `agent.build_agent` included), the run resolves `settings.model` through pydantic-ai itself.

    **The price is always looked up by the CONFIGURED model id**, never by whatever the seam
    injected: an offline test therefore prices exactly the arithmetic a live run would, and an
    injected double can never make a run look free.
    """

    def __init__(self, settings, *, model_factory=None):
        self.settings = settings
        self.model_factory = model_factory
        # A BACKSTOP, not the loud road. `worker.startup_checks` is where an unpriced model is
        # meant to be refused — before a single item is claimed, with the whole configuration in
        # front of the operator — and this repeats the question at the one point that cannot be
        # reached around: constructing the thing that will spend the money. The alternative is a
        # backend that runs, pays, and only then discovers it cannot say what the run cost, which
        # is precisely the `$0.00`-reads-as-free failure this milestone exists to close.
        pricing.require_priced(settings.model)

    def run(self, *, worktree: str, material: str, hints: dict, submitted_by: str,
            corrective: str = "", reply: str = "", flow_note: str = "") -> AgentRun:
        """The ordinary flow — refused, and unreachable through a worker.

        The port requires the method, so it exists and it is honest about why it does nothing:
        `worker.startup_checks` refuses `backend="pydantic"` for any worker before a single item is
        claimed, precisely so this branch is never how somebody discovers the limitation. It is
        still a real refusal rather than a `NotImplementedError`, because `AgentError` is the
        family `processing` already turns into a `failed` row with a sentence on it — a bare
        `NotImplementedError` would surface as an unexpected crash with a traceback at an operator.

        **Priced at `0.0` like every other fault this backend raises**, and that is the port's rule
        rather than a formality: `processing` reads `run_cost_usd` off the exception, so a fault
        without the field cannot be told apart from one nobody attached it to. Nothing was spent
        here — no model was built and no request was made — and `0.0` is the honest way to say so.
        """
        raise priced(AgentRun(), AgentError(
            f"the {BACKEND_NAME!r} librarian backend serves the meeting flow only in this "
            f"milestone, and this capture is an ordinary one. A worker must run backend 'sdk' (the "
            f"real agent, every flow) or 'double' (the offline double, every flow); see {ADR}"))

    def run_meeting(self, *, worktree: str, material: str, meeting_meta: dict, registry,
                    source_page_path: str, corrective: str = "", reply: str = "") -> AgentRun:
        """One structured call: the brief as instructions, the item as the prompt, a typed account
        back. Structurally parallel to `agent.SdkAgent.run_meeting` on purpose — a backend swap
        should be a provider change, not a mechanism one."""
        import asyncio
        return asyncio.run(self._run_meeting(
            worktree=worktree, material=material, meeting_meta=meeting_meta, registry=registry,
            source_page_path=source_page_path, corrective=corrective, reply=reply))

    async def _run_meeting(self, *, worktree, material, meeting_meta, registry, source_page_path,
                           corrective, reply="") -> AgentRun:
        import asyncio

        # Imported HERE, never at module scope — the same rule `agent.SdkAgent._run` follows for
        # `claude_agent_sdk`, and for the same reason: an offline run must not load an agent
        # framework, and the import graph must not claim this package depends on one unconditionally.
        from pydantic_ai import Agent
        from pydantic_ai.exceptions import UnexpectedModelBehavior
        from pydantic_ai.usage import RunUsage, UsageLimits

        # `turns` and `tool_calls` stay at the envelope's own zero and are never assigned: there is
        # no conversational loop here and no tool to call, so a `1` would be a number invented to
        # look like the SDK backend's. The port documents zero as a legitimate answer, and nothing
        # downstream branches on either counter.
        run = AgentRun()
        worktree_root = os.path.realpath(worktree)

        # The brief comes out of the WORKTREE, which is the checkout at this item's base commit —
        # the same read `SdkAgent._run_meeting` makes, deliberately not a second reader. A missing
        # brief raises `LibrarianConfigError` here, before any model call is spent.
        instructions = agent_module.build_meeting_system_prompt(
            agent_module.read_meeting_brief(worktree_root),
            header=MEETING_SYSTEM_PROMPT_HEADER)
        prompt = agent_module.build_meeting_prompt(
            material=material, meeting_meta=meeting_meta, registry=registry,
            source_page_path=source_page_path, corrective=corrective, reply=reply,
            outcome_channel=OUTCOME_CHANNEL)

        # Model resolution and construction get their OWN narrow try. They can fail — an id
        # pydantic-ai cannot resolve, a provider package that is not installed, a factory that
        # raises — and the blanket handler below would report those as "the meeting agent run
        # failed", which sends an operator looking at the transcript for a fault in their
        # configuration. `read_meeting_brief`'s `LibrarianConfigError` deliberately stays outside
        # both: it is the worker's own config road, and `process_next` already names it.
        try:
            model = self.model_factory() if self.model_factory else self.settings.model
            distiller = Agent(model, output_type=MeetingAccount, instructions=instructions,
                              retries=OUTPUT_RETRIES)
        except Exception as ex:  # noqa: BLE001 — class name only, like every other wrap here
            raise priced(run, AgentError(
                f"could not resolve the configured model ({ex.__class__.__name__}); "
                f"$STIGMERGY_LIBRARIAN_MODEL is {self.settings.model!r}")) from ex
        # OUR usage accumulator, handed in rather than read off the result: pydantic-ai fills this
        # object as the run proceeds, so a run that dies mid-flight still leaves its real counts
        # here — which is what lets a fault carry `run_cost_usd` instead of the honest-but-useless
        # 0.0 a result-only read would force.
        usage = RunUsage()
        # The request ceiling, from the SAME constant as the retry budget above: this flow makes one
        # model call and only output re-validation can add more, so the ceiling is exactly what the
        # framework is allowed to spend. `settings.max_turns` is deliberately NOT reused — it is the
        # SDK backend's conversational bound (30 turns of an agent loop), and borrowing it here
        # would license thirty full requests for a flow that must make one.
        limits = UsageLimits(request_limit=1 + OUTPUT_RETRIES)
        try:
            # The wall clock is a bound WE own — pydantic-ai has none, exactly like the Agent SDK,
            # and the worker's visibility lease is derived from this number
            # (`config.minimum_visibility_timeout_s`). A pass that could outlive it is a capture two
            # workers file.
            async with asyncio.timeout(self.settings.timeout_s):
                result = await distiller.run(prompt, usage=usage, usage_limits=limits)
        except TimeoutError as ex:
            run.cost_usd = self._fault_cost(usage)
            raise priced(run, AgentError(
                f"the meeting agent exceeded its {self.settings.timeout_s}s budget")) from ex
        except UnexpectedModelBehavior as ex:
            # The framework exhausted its own output re-validations: the model kept answering with
            # something the schema refuses. That is a SHAPE problem — the one class the worker's
            # corrective retry exists for — so it travels as an `OutcomeShapeError` carrying a
            # finding, exactly as a refused account from the file channel does. Wrapped as a bare
            # `AgentError` (the branch below) it would finish the item with a class name and no
            # brief, which is the defect `errors.OutcomeShapeError` was split out to fix.
            run.cost_usd = self._fault_cost(usage)
            raise priced(run, OutcomeShapeError([gates.Finding(
                # The same gate name the file channel's own shape findings carry, so
                # `corrective_brief` and `processing._refuse_meeting` cannot tell the two channels
                # apart — one vocabulary for one class of problem.
                agent_module._OUTCOME_GATE, "framework-rejected",
                f"the account did not satisfy this run's output schema after "
                f"{OUTPUT_RETRIES} re-validation attempt(s) ({ex.__class__.__name__}); return "
                f"every field the schema declares, in the shape the skill documents")])) from ex
        except Exception as ex:  # noqa: BLE001 — class name only: provider errors carry prompt text
            run.cost_usd = self._fault_cost(usage)
            raise priced(run, AgentError(
                f"the meeting agent run failed ({ex.__class__.__name__})")) from ex

        run.cost_usd = self._cost(usage)
        run.stop_reason = str(getattr(result.response, "finish_reason", "") or "")
        # The SAME boundary the file channel goes through. A typed provider response is not a
        # trusted one: it was written by a model that has just read an untrusted transcript, and
        # every bound, every coercion and every correctable shape finding lives in that parser.
        #
        # Deliberately OUTSIDE the try above: `OutcomeShapeError` must reach the corrective retry
        # carrying its findings, and the blanket `except Exception` would have turned it into a
        # bare `AgentError` with a class name — the exact defect `errors.OutcomeShapeError` was
        # split out to fix, reintroduced one backend over. It is still PRICED, on the same road
        # `SdkAgent._run_meeting`'s own outcome read takes: the run was paid for whether or not its
        # account parses.
        raw = result.output.model_dump()
        # The SAME ceiling the file channel applies to `.librarian-outcome.json`, on the channel
        # that has no file to stat. A structured output is bounded by the schema's SHAPE and by
        # nothing else — every string field is unbounded, and a model that repeats a transcript
        # into `meeting_notes` produces an account that is parsed, truncated field by field, and
        # only then found to have cost a lot of memory on the way. One constant, two channels.
        # Dumped ONCE: `parse_meeting_outcome` reads the dict, not these bytes.
        size = len(json.dumps(raw, ensure_ascii=False, default=str).encode("utf-8"))
        if size > agent_module.MAX_OUTCOME_BYTES:
            raise priced(run, AgentError(
                f"the meeting agent's account is {size} bytes, over the "
                f"{agent_module.MAX_OUTCOME_BYTES}-byte ceiling"))
        try:
            run.outcome = agent_module.parse_meeting_outcome(raw)
        except AgentError as ex:
            priced(run, ex)
            raise
        return run

    def _fault_cost(self, usage) -> float:
        """`_cost`, on a road where it must never raise.

        Every caller of this is already handling a fault, and `_cost` can itself refuse — an
        unpriced model raises `LibrarianConfigError`. Letting that escape from an `except` block
        would replace the fault being reported with a configuration complaint about the annotation,
        and the operator would never see what actually went wrong. `0.0` is the honest figure when
        the price cannot be resolved: nothing was computed, and the fault keeps its own message.
        """
        try:
            return self._cost(usage)
        except LibrarianConfigError:
            log.warning("could not price the failed pass: no price is configured for %r — the "
                        "fault below is reported with a spend of $0.00", self.settings.model)
            return 0.0

    def _cost(self, usage) -> float:
        """This attempt's dollars, computed from tokens because no provider here prices itself.

        Read defensively (`getattr`) for the same reason `answer.service._usage_facts` is: the
        framework's usage object has grown fields before, and an injected offline model may hand
        back a simpler one.
        """
        counts = {name: getattr(usage, name, 0) or 0
                  for name in ("input_tokens", "cache_read_tokens", "cache_write_tokens",
                               "output_tokens")}
        cost = pricing.compute_cost_usd(
            self.settings.model,
            input_tokens=counts["input_tokens"],
            cached_input_tokens=counts["cache_read_tokens"],
            cache_write_tokens=counts["cache_write_tokens"],
            output_tokens=counts["output_tokens"])
        log.info("meeting pass on %s: %s prompt tokens (%s cached, %s cache-written) / %s output "
                 "-> $%s (computed from librarian/pricing.py, as of %s)", self.settings.model,
                 counts["input_tokens"], counts["cache_read_tokens"],
                 counts["cache_write_tokens"], counts["output_tokens"], cost, pricing.AS_OF)
        return cost
