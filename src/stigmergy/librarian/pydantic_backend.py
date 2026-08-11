"""BOTH flows on pydantic-ai — one structured model call each, no tools, no outcome file.

The third `FilingAgent` implementation, and the first one that is not Claude's. It started with the
meeting flow because that flow was ALREADY portable and nothing had noticed: the agent holds no
page-writing tool (code is the sole author of every page in the set), it explores nothing (the
transcript, the registry and the metadata are all handed to it), and its whole answer is one
structured account. A flow shaped like that does not need an agent harness at all — it needs a
model that can return a typed object. ADR 032 records that half; ADR 020 is why the meeting flow
had the shape in the first place.

**ADR 033 gave the ORDINARY flow the same shape, and this backend now serves both.** M1's `run`
was a refusal, and `worker.startup_checks` refused this backend for any worker outright — because
a worker's queue carries ordinary captures too and a backend that half-serves a queue is the
configuration this repo refuses on principle. What lifted it is not a flag: the ordinary flow got
the meeting flow's division of labour. A deterministic gatherer (`librarian/gather.py`) reads the
checkout at the base commit and `processing` hands the result over as rendered prompt text, the
agent returns a structured account CARRYING the page's own body, and `processing._write_ordinary_page`
writes it. There is nothing left for this backend to explore and nothing left for it to write.

**The gatherer is deliberately NOT in here.** `processing` gathers and renders; this backend
receives a string. Two structured backends must share one context builder and one fence
discipline, and a gatherer living inside a backend is a gatherer the second one reimplements.

**What is NOT reused, deliberately.** `kernel.llm.build_processor` is this repo's fake/real
dispatch for every OTHER agent, and it is the wrong seam here: the librarian's offline path is
`double.DoubleAgent` — a whole adversarial backend the suite is built on — and routing this module
through `resolve_backend` would create a SECOND offline path with different semantics answering to
a different variable (`$CLEAN_LLM` rather than `$STIGMERGY_LIBRARIAN_BACKEND`). What IS reused is
everything each flow already owns: `agent.read_skill`/`read_meeting_brief` (the base-commit reads),
`agent.build_structured_prompt`/`build_meeting_prompt` (the per-item message),
`agent.build_system_prompt`/`build_meeting_system_prompt` (the brief's body as instructions) and
`agent.parse_outcome`/`parse_meeting_outcome` (the SAME trust boundary the file channel goes
through — a structured provider is not a trusted one).

**`pydantic_ai` is imported inside the methods**, exactly as `claude_agent_sdk` is in `agent.py`:
a keyless run must not load an agent framework, and the import graph must not claim this package
depends on one unconditionally. `pydantic` itself is module-scope — the output schemas below are
plain data, and a test that builds one by hand must not have to reach through a backend to do it.
"""
import json
import logging
import os
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import gates, pricing
from stigmergy.librarian.errors import AgentError, LibrarianConfigError, OutcomeShapeError
from stigmergy.librarian.filing_port import AgentRun, priced

log = logging.getLogger(__name__)

BACKEND_NAME = "pydantic"

# The decision record a REFUSAL points an operator at. Read by
# `worker._check_brief_matches_backend`, which is the one message that has to tell somebody where
# the landing-order rule it enforces is written down.
#
# `ADR = "…/032-…"` used to sit beside this and is gone: it was cited by exactly one message — the
# meeting-only refusal ADR 033 removed — and a module constant naming a document nothing quotes is
# the shape this repo prunes on sight. ADR 032 is still this module's other design record and is
# cited in the prose above, which is where a reference with no runtime reader belongs.
ORDINARY_ADR = "docs/decisions/033-structured-filing-flow.md"

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


# ── the accounts, as schemas instead of a file ────────────────────────────────────────────────
# Field-for-field mirrors of the JSON `agent.parse_outcome` / `parse_meeting_outcome` already read,
# so the two channels carry the SAME shape and the boundary parse is shared rather than forked.
#
# **BOUNDS are deliberately not restated here; REQUIREDNESS is, and the distinction was paid for.**
# `parse_*_outcome` owns the bounds — identifiers refused over `MAX_IDENTIFIER_LEN`, prose
# truncated, a page body refused, lists capped — and a second set of limits in a schema would be a
# second answer to one question, drifting from the one the file channel is judged by.
#
# Requiredness is the opposite case, and the first PAID run of the structured ordinary flow is what
# established it. Every field carried a default, including `decision`, on the reasoning that a
# provider omitting something should produce an account the boundary can judge and refuse on its
# own terms rather than a validation error inside the framework. That reasoning had the mechanism
# backwards. A default does not make an omission visible — it makes it INVISIBLE: the framework's
# output validation passed a half-empty account, so its own `OUTPUT_RETRIES` never fired, and
# `parse_outcome` then refused downstream with `unknown-decision` or a missing `title`. Five of the
# golden's ordinary captures died that way, two passes each: the WORKER's one corrective retry was
# spent re-asking a model to repair a shape a brief cannot reliably teach, when the framework
# would have re-asked for free and with the exact field named.
#
# So the schema demands what the boundary demands, and the two enforcement points are declared
# duplication rather than an accident:
#
#   * the SCHEMA (here) is the cheap, early road — the framework re-asks the model with the
#     validator's own message, before a single byte reaches this package;
#   * the BOUNDARY (`agent.parse_*_outcome`) keeps every check regardless, because it also judges
#     the FILE channel and because a typed provider response is not a trusted one.
#
# Nothing is hardcoded twice: `decision` derives its enum from `agent.DECISIONS`, and the parked
# kinds and their required fields from `agent.TRIAGE_KINDS` / `agent.TRIAGE_REQUIRED_FIELD`.


def _needed(field: str, instead: str) -> str:
    """One completeness refusal, addressed to the MODEL rather than to an operator.

    These strings are not diagnostics: pydantic-ai hands a validator's `ValueError` back to the
    model as its retry prompt, so this is the only text that gets a chance to repair the account.
    Same shape as `gates.Finding.brief` for that reason — name the field, then name the repair.
    """
    return f"`{field}` is required and came back empty. {instead}"
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
    set's CONTENT — never a page path, because code decides every path in this flow.

    **Given the SAME treatment as `FilingAccount`, on a flow where the defect had not fired yet.**
    The mechanism is identical — a defaulted `decision` means the framework's output validation
    accepts a half-empty account, its own retries never run, and `parse_meeting_outcome` refuses
    downstream having spent the worker's one corrective pass. The meeting flow has real passing
    runs (the terra trial, the golden's two meeting captures), so this is not a fix for an observed
    failure; it is closing a known mechanism on two samples' worth of evidence, which this
    repository's own rule about untested rules asks for. It costs a passing run nothing: a complete
    account satisfies both, and an incomplete one is repaired one road earlier.
    """
    decision: Literal[*agent_module.DECISIONS]
    meeting_title: str = ""
    attendees: list[str] = Field(default_factory=list)
    meeting_notes: str = ""
    action_items: list[MeetingActionItem] = Field(default_factory=list)
    decisions: list[MeetingDecision] = Field(default_factory=list)
    summary: str = ""
    findings: list[MeetingFinding] = Field(default_factory=list)
    triage: MeetingTriage = Field(default_factory=MeetingTriage)

    @model_validator(mode="after")
    def _complete_for_its_decision(self):
        """`FilingAccount._complete_for_its_decision`'s twin, over what a MEETING decision obliges
        — mirroring `agent.parse_meeting_outcome`'s own required-field rules and no others."""
        if self.decision == "file":
            if not (self.meeting_title or "").strip():
                raise ValueError(_needed(
                    "meeting_title",
                    "It names the meeting page the worker is about to write, and the drop's own "
                    "title hint is not a substitute for what the transcript turned out to be."))
            for n, decided in enumerate(self.decisions, start=1):
                if not (decided.title or "").strip():
                    raise ValueError(_needed(
                        f"decisions[{n - 1}].title",
                        "Every decision you describe becomes its own page, and the title is that "
                        "page's name — decision number "
                        f"{n} has none."))
            return self

        kind = (self.triage.kind or "").strip()
        if kind not in agent_module.TRIAGE_KINDS:
            raise ValueError(_needed(
                "triage.kind",
                f"Parking says WHY: one of {', '.join(agent_module.TRIAGE_KINDS)}."))
        # The plural field, and the ONE place this flow's rule differs from the ordinary one: a
        # meeting can fail to anchor on several names at once, so `parse_meeting_outcome` asks for
        # `names` where `parse_outcome` asks for `name`.
        if kind == agent_module.TRIAGE_UNRESOLVED_ENTITY and not [
                n for n in self.triage.names if (n or "").strip()]:
            raise ValueError(_needed(
                "triage.names",
                "They are the names a steward has to register, and the whole of what the "
                "submitter is told about this park."))
        return self


# ── the ORDINARY account, as a schema instead of a file (ADR 033) ─────────────────────────────
# The same field-for-field mirror discipline the meeting schema above follows, over the shape
# `agent.parse_outcome` reads — and with THREE deliberate omissions, each one a field this backend
# must not be able to declare:
#
#  * `page_path` — code decides every path here (`processing._write_ordinary_page`), from the
#    title and from `page.FOLDER_BY_TYPE`. A field the model could fill is a path the model could
#    steer, and `_cross_check_outcome` would then be defending against a claim nothing needed to
#    make. `parse_outcome` still ACCEPTS one (the `sdk` backend declares it), which is the whole
#    of the expand–contract: one parser, two shapes, and each backend emitting only its own.
#  * top-level `title`/`page_type` — they live in `page` for this backend, and `parse_outcome`
#    fills the single fields every downstream reader uses from there. Declaring both would let one
#    account carry two answers to one question.
class OrdinaryAnchoring(BaseModel):
    """This page's anchor. `kind` is `entity` (with `entities`) or `company` (with a written
    `reason`); the registry, not this schema, decides whether a name resolves."""
    kind: str = ""
    reason: str = ""
    entities: list[str] = Field(default_factory=list)


class OrdinaryPage(BaseModel):
    """The page itself — the title it is filed under, its type, and its whole body. No path: the
    worker derives the folder from the type and the filename from the title."""
    title: str = ""
    page_type: str = ""
    body: str = ""


class OrdinaryOverlap(BaseModel):
    path: str = ""
    note: str = ""


class OrdinaryEdit(BaseModel):
    """One DECLARED edit to a page that already exists — performed by the worker, never by this
    agent (`edits.py`)."""
    path: str = ""
    kind: str = ""
    link: str = ""
    note: str = ""


class OrdinaryFinding(BaseModel):
    """A steering attempt the agent noticed. Only `category` travels — never the payload."""
    category: str = ""


class OrdinaryTriage(BaseModel):
    """Why the capture was parked, when `decision` is `triage`."""
    kind: str = ""
    name: str = ""
    judged_type: str = ""


class FilingAccount(BaseModel):
    """The whole account of one ordinary capture: `decision` is `file` or `triage`, and the rest is
    judgment plus the page's own CONTENT.

    `decision` is REQUIRED and enum-constrained, and the two halves it obliges are checked below —
    see the section comment above for the paid run that established why.
    """
    decision: Literal[*agent_module.DECISIONS]
    page: OrdinaryPage = Field(default_factory=OrdinaryPage)
    anchoring: OrdinaryAnchoring = Field(default_factory=OrdinaryAnchoring)
    links_created: list[str] = Field(default_factory=list)
    overlaps: list[OrdinaryOverlap] = Field(default_factory=list)
    edits: list[OrdinaryEdit] = Field(default_factory=list)
    summary: str = ""
    findings: list[OrdinaryFinding] = Field(default_factory=list)
    triage: OrdinaryTriage = Field(default_factory=OrdinaryTriage)

    @model_validator(mode="after")
    def _complete_for_its_decision(self):
        """What THIS decision obliges — the conditional half a field-by-field schema cannot say.

        `OrdinaryPage`'s own fields stay optional on purpose: a `triage` account legitimately
        carries no page at all, so requiring them individually would refuse the correct outcome for
        a capture this brain cannot place. The obligation is on the PAIRING, which is exactly what a
        model validator is for.
        """
        if self.decision == "file":
            if not (self.page.title or "").strip():
                raise ValueError(_needed(
                    "page.title",
                    "It is the page's name, its filename and the commit subject a human reads in "
                    "`git log`, and there is nothing else to derive it from."))
            if not (self.page.page_type or "").strip():
                raise ValueError(_needed(
                    "page.page_type",
                    "Name the TYPE (note, decision or concept) — never a folder or a path; the "
                    "worker puts the page where a page of that type goes."))
            if not (self.page.body or "").strip():
                raise ValueError(_needed(
                    "page.body",
                    "The worker writes the page from this account, so the page's own text has to "
                    "be in it: return the whole page below its H1, with no frontmatter block. If "
                    "this capture should not be filed at all, park it with `decision`: \"triage\" "
                    "instead."))
            return self

        kind = (self.triage.kind or "").strip()
        if kind not in agent_module.TRIAGE_KINDS:
            raise ValueError(_needed(
                "triage.kind",
                f"Parking says WHY: one of {', '.join(agent_module.TRIAGE_KINDS)}."))
        required = agent_module.TRIAGE_REQUIRED_FIELD[kind]
        if not (getattr(self.triage, required, "") or "").strip():
            raise ValueError(_needed(
                f"triage.{required}",
                f"It is the one thing the submitter is told about a {kind!r} park."))
        return self


# This backend's own ordinary environment — the ONE part of the preamble that differs per backend.
# TWO numbered points, because the SDK's own environment (`agent.ORDINARY_SDK_ENVIRONMENT`) is two
# and the shared point after it is numbered `3.`; the opening, that shared point and the separator
# come from `agent.build_filing_header`, where they are written once.
ORDINARY_ENVIRONMENT = (
    "1. You have NO tools. You cannot read, search or write anything, and you do not write your "
    "account to a file: you RETURN it, as the structured object this run's output schema "
    "declares. Everything you need is in the worker's own message below: the captured material, "
    "the entities it names resolved through the registry, the candidate pages this brain already "
    "holds (with excerpts), the link neighbourhood around them, and the repo's own page names. "
    "The page contract and this type's template are summarised in the procedure below; you do not "
    "read them from the checkout because you cannot.\n"
    "2. The worker writes the page from what you return — the filename from your title, the "
    "folder from your page type, the server-owned frontmatter, the declared edits, the commit. "
    "Your job is judgment and DRAFTING: where it belongs, what it anchors to, what it overlaps, "
    "and the page's own text.\n")

ORDINARY_SYSTEM_PROMPT_HEADER = agent_module.build_filing_header(ORDINARY_ENVIRONMENT)

# The one line of the per-item prompt that differs between the two channels — see
# `agent.build_prompt`, whose default is the file channel's own sentence.
ORDINARY_OUTCOME_CHANNEL = (
    "\nReturn your account as the structured object this run's output schema declares, in the "
    "shape the skill documents — the page's own text included, in `page.body`. You write no file "
    "and you have no tool that could.")


# This backend's own MEETING environment paragraph.
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


class PydanticFilingAgent:
    """The pydantic-ai backend, for BOTH flows. Conforms to `filing_port.FilingAgent` structurally —
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

    # The STRUCTURED shape of the ordinary flow, declared rather than inferred (see
    # `filing_port.FilingAgent.structured_ordinary`). `processing` reads THIS, never
    # `isinstance(agent, PydanticFilingAgent)`: a fourth backend, or a test double standing in for
    # one, must be able to take the structured branch by declaring it rather than by being the
    # right class.
    structured_ordinary = True

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
            corrective: str = "", reply: str = "", flow_note: str = "",
            gathered: str = "") -> AgentRun:
        """The ordinary flow: file ONE capture, in one structured call, writing nothing.

        Structurally parallel to `run_meeting` below and to `agent.SdkAgent.run` above, on purpose —
        a backend swap should be a provider change, not a mechanism one. `gathered` is the
        deterministic gatherer's context, already rendered to prompt text by `processing`; this
        backend never builds one (see the module docstring).
        """
        import asyncio
        return asyncio.run(self._run(
            worktree=worktree, material=material, hints=hints, submitted_by=submitted_by,
            corrective=corrective, reply=reply, flow_note=flow_note, gathered=gathered))

    async def _run(self, *, worktree, material, hints, submitted_by, corrective, reply="",
                   flow_note="", gathered="") -> AgentRun:
        import asyncio

        # Imported HERE, never at module scope — see the module docstring.
        from pydantic_ai import Agent
        from pydantic_ai.exceptions import UnexpectedModelBehavior
        from pydantic_ai.usage import RunUsage, UsageLimits

        from stigmergy.kernel.usage_repair import ensure_usage_extraction_repaired

        ensure_usage_extraction_repaired()

        # `turns`/`tool_calls` stay at the envelope's own zero — one call, no tools, no loop.
        run = AgentRun()
        worktree_root = os.path.realpath(worktree)

        # The skill comes out of the WORKTREE, which is the checkout at this item's base commit —
        # the same read `SdkAgent._run` makes, deliberately not a second reader. A missing skill
        # raises `LibrarianConfigError` here, before any model call is spent.
        instructions = agent_module.build_system_prompt(
            agent_module.read_skill(worktree_root), header=ORDINARY_SYSTEM_PROMPT_HEADER)
        prompt = agent_module.build_structured_prompt(
            material=material, hints=hints, submitted_by=submitted_by, gathered_block=gathered,
            outcome_channel=ORDINARY_OUTCOME_CHANNEL, corrective=corrective, reply=reply,
            flow_note=flow_note)

        # Model resolution gets its OWN narrow try, for the reason `_run_meeting` records: the
        # blanket handler below would report a configuration fault as "the run failed".
        try:
            model = self.model_factory() if self.model_factory else self.settings.model
            filer = Agent(model, output_type=FilingAccount, instructions=instructions,
                          retries=OUTPUT_RETRIES)
        except Exception as ex:  # noqa: BLE001 — class name only, like every other wrap here
            raise priced(run, AgentError(
                f"could not resolve the configured model ({ex.__class__.__name__}); "
                f"$STIGMERGY_LIBRARIAN_MODEL is {self.settings.model!r}")) from ex
        usage = RunUsage()
        limits = UsageLimits(request_limit=1 + OUTPUT_RETRIES)
        try:
            async with asyncio.timeout(self.settings.timeout_s):
                result = await filer.run(prompt, usage=usage, usage_limits=limits)
        except TimeoutError as ex:
            run.cost_usd = self._fault_cost(usage, flow="filing")
            raise priced(run, AgentError(
                f"the filing agent exceeded its {self.settings.timeout_s}s budget")) from ex
        except UnexpectedModelBehavior as ex:
            # A SHAPE problem — the class the worker's corrective retry exists for. Travels as an
            # `OutcomeShapeError` carrying a finding, exactly as a refused account from the file
            # channel does; see `_run_meeting`'s own arm for the full argument.
            run.cost_usd = self._fault_cost(usage, flow="filing")
            raise priced(run, OutcomeShapeError([gates.Finding(
                agent_module._OUTCOME_GATE, "framework-rejected",
                f"the account did not satisfy this run's output schema after "
                f"{OUTPUT_RETRIES} re-validation attempt(s) ({ex.__class__.__name__}); return "
                f"every field the schema declares, in the shape the skill documents")])) from ex
        except Exception as ex:  # noqa: BLE001 — class name only: provider errors carry prompt text
            run.cost_usd = self._fault_cost(usage, flow="filing")
            raise priced(run, AgentError(
                f"the filing agent run failed ({ex.__class__.__name__})")) from ex

        run.cost_usd = self._cost(usage, flow="filing")
        run.stop_reason = str(getattr(result.response, "finish_reason", "") or "")
        # The SAME boundary the file channel goes through. A typed provider response is not a
        # trusted one: it was written by a model that has just read untrusted material.
        # Deliberately OUTSIDE the try above, so an `OutcomeShapeError` reaches the corrective
        # retry carrying its findings instead of being flattened into a bare `AgentError`.
        raw = result.output.model_dump()
        # The SAME ceiling the file channel applies to `.librarian-outcome.json`, on the channel
        # that has no file to stat — and it matters MORE here than in the meeting flow, because
        # `page.body` is an unbounded string a model can fill with the whole material. One
        # constant, two channels. Dumped ONCE: `parse_outcome` reads the dict, not these bytes.
        size = len(json.dumps(raw, ensure_ascii=False, default=str).encode("utf-8"))
        if size > agent_module.MAX_OUTCOME_BYTES:
            raise priced(run, AgentError(
                f"the filing agent's account is {size} bytes, over the "
                f"{agent_module.MAX_OUTCOME_BYTES}-byte ceiling"))
        try:
            run.outcome = agent_module.parse_outcome(raw)
        except AgentError as ex:
            priced(run, ex)
            raise
        return run

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

        from stigmergy.kernel.usage_repair import ensure_usage_extraction_repaired

        # The framework's own usage extraction silently reports ZERO tokens for any OpenAI model
        # that carries reasoning details — which is every response from a reasoning model, and the
        # first paid run of this backend priced at $0.0000 because of it. This whole module exists
        # to turn tokens into dollars, so a shim that gets the tokens back is load-bearing rather
        # than defensive. Idempotent; deferring to the framework the day it is fixed.
        ensure_usage_extraction_repaired()

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

    def _fault_cost(self, usage, *, flow: str = "meeting") -> float:
        """`_cost`, on a road where it must never raise.

        Every caller of this is already handling a fault, and `_cost` can itself refuse — an
        unpriced model raises `LibrarianConfigError`. Letting that escape from an `except` block
        would replace the fault being reported with a configuration complaint about the annotation,
        and the operator would never see what actually went wrong. `0.0` is the honest figure when
        the price cannot be resolved: nothing was computed, and the fault keeps its own message.
        """
        try:
            return self._cost(usage, flow=flow)
        except LibrarianConfigError:
            log.warning("could not price the failed pass: no price is configured for %r — the "
                        "fault below is reported with a spend of $0.00", self.settings.model)
            return 0.0

    def _cost(self, usage, *, flow: str = "meeting") -> float:
        """This attempt's dollars, computed from tokens because no provider here prices itself.

        ONE arithmetic for both flows — `flow` names the pass in the log line and nothing else. A
        second `_cost` per flow would be a second multiplication at a second call site, which is
        the one thing `pricing.compute_cost_usd`'s own docstring says never to grow.

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
        log.info("%s pass on %s: %s prompt tokens (%s cached, %s cache-written) / %s output "
                 "-> $%s (computed from librarian/pricing.py, as of %s)", flow, self.settings.model,
                 counts["input_tokens"], counts["cache_read_tokens"],
                 counts["cache_write_tokens"], counts["output_tokens"], cost, pricing.AS_OF)
        return cost


# ── the name this class had while it served one flow ──────────────────────────────────────────
# `PydanticMeetingAgent` was accurate for exactly one milestone and is now a lie: this backend
# serves both flows (ADR 033). Renaming it outright would have broken four test modules in the
# same commit that changed the behaviour they cover, which is the one thing the breaking-change
# doctrine here refuses — so the rename EXPANDS first and the old spelling stays importable.
#
# **Removal criterion, and it is an adoption signal rather than a date**: the alias goes when no
# module outside this file imports it. The consumers are fully enumerable — they are all in this
# repository's own `tests/librarian/` — so this is a transition with a known end, not an
# indefinite compatibility surface.
PydanticMeetingAgent = PydanticFilingAgent
