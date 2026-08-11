"""BOTH flows on pydantic-ai: an ITERATING ordinary run with five tools, one structured meeting call.

The third `FilingAgent` implementation, and the first one that is not Claude's. It started with the
meeting flow because that flow was ALREADY portable and nothing had noticed: the agent holds no
page-writing tool (code is the sole author of every page in the set), it explores nothing (the
transcript, the registry and the metadata are all handed to it), and its whole answer is one
structured account. A flow shaped like that does not need an agent harness at all — it needs a
model that can return a typed object. ADR 032 records that half; ADR 020 is why the meeting flow
had the shape in the first place.

**ADR 033 gave the ORDINARY flow the same shape; ADR 034 gave it back its ability to look.** The
gatherer stays and the one-shot call went: `processing` still reads the checkout deterministically
and hands the result over as rendered prompt text, but that block is now the SEED of a run that
holds `search_pages`, `read_page`, `list_page_names`, `resolve_entities` and `write_page` over the
same checkout, writes its own page inside `agent.confined_write`'s allow-list, and returns its
account as `.librarian-outcome.json`. The reason is portability rather than nostalgia: the goal of
moving off the Claude harness was that a provider swap should be a configuration change, never that
the model should stop being able to search — deterministic code may SEED context and IMPLEMENT
tools, and must not replace the judgment that decides when the context is not enough.

**The gatherer is deliberately NOT in here.** `processing` gathers and renders; this backend
receives a string. Two backends must share one context builder and one fence discipline, and a
gatherer living inside a backend is a gatherer the second one reimplements. What IS in here is the
TOOLBOX (`FilingToolbox`) — the tools' bodies, which are `gather.py`'s own pure functions with the
confinement rules asked inside each call rather than in a permission hook.

**What is NOT reused, deliberately.** `kernel.llm.build_processor` is this repo's fake/real
dispatch for every OTHER agent, and it is the wrong seam here: the librarian's offline path is
`double.DoubleAgent` — a whole adversarial backend the suite is built on — and routing this module
through `resolve_backend` would create a SECOND offline path with different semantics answering to
a different variable (`$CLEAN_LLM` rather than `$STIGMERGY_LIBRARIAN_BACKEND`). What IS reused is
everything each flow already owns: `agent.read_skill`/`read_meeting_brief` (the base-commit reads),
`agent.build_prompt`/`build_meeting_prompt` (the per-item message),
`agent.build_system_prompt`/`build_meeting_system_prompt` (the brief's body as instructions) and
`agent.parse_outcome`/`parse_meeting_outcome` (the SAME trust boundary the file channel goes
through — a structured provider is not a trusted one).

**`pydantic_ai` is imported inside the methods**, never at module scope:
a keyless run must not load an agent framework, and the import graph must not claim this package
depends on one unconditionally. `pydantic` itself is module-scope — the output schemas below are
plain data, and a test that builds one by hand must not have to reach through a backend to do it.
"""
import json
import logging
import os
import threading
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from stigmergy import text as textutil
from stigmergy.kernel import registry as registry_module
from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import config, edits, gates, gather, gitcmd, pricing
from stigmergy.librarian import page as page_policy
from stigmergy.librarian.errors import AgentError, LibrarianConfigError, OutcomeShapeError
from stigmergy.librarian.filing_port import AgentRun, priced

log = logging.getLogger(__name__)

BACKEND_NAME = "pydantic"

# RETIRED with the refusals that quoted them: `ADR` (ADR 032, cited by M1's meeting-only refusal)
# and now `ORDINARY_ADR` (ADR 033, cited by `worker._check_brief_matches_backend`, retired in ADR
# 034 — see that function's tombstone in `worker.py` for why the check went).
#
# Both are the same pruning rule applied twice: **a module constant naming a document nothing
# quotes is a reference with no reader**, and this repo prunes those on sight. ADR 032, 033 and 034
# are all still this module's design records and are cited in the prose above, which is where a
# document reference with no runtime reader belongs.

# How many times the FRAMEWORK may re-ask the model when its answer does not satisfy what it was
# asked for. On the MEETING flow that is the output schema, and this constant is read twice on
# purpose there: it is both the `Agent`'s retry budget and the request ceiling handed to
# `UsageLimits`, and those two numbers must agree or the ceiling either strangles a legitimate
# re-validation or stops bounding anything. One request, plus one re-ask: past that the answer is
# not a shape problem the framework can fix by asking again, and it belongs on the WORKER's own
# corrective retry, where the brief says what was wrong.
#
# On the ORDINARY flow it bounds TOOL-call validation instead (a call with arguments the tool's
# signature refuses) and nothing else — that flow's request ceiling is `settings.max_turns`, since
# a loop's budget cannot be a re-ask budget.
#
# **These re-asks are invisible to `AgentPasses.count`**, which counts the worker's passes. See
# ADR 032's envelope semantics: `attempts` means our passes, so a framework re-validation costs
# money that IS banked (the usage accumulator sees it) under an attempt count that does not move.
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
#    make. `parse_outcome` still ACCEPTS one (the offline double declares it), which is the whole
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


# ── RETIRED with the one-shot ordinary run (ADR 034) ──────────────────────────────────────────
# `ORDINARY_ENVIRONMENT` ("You have NO tools…"), `ORDINARY_SYSTEM_PROMPT_HEADER` and
# `ORDINARY_OUTCOME_CHANNEL` ("You write no file and you have no tool that could") lived here.
# Nothing composes them: this backend's ordinary run holds five tools and writes its own page and
# outcome file, so every one of those three sentences is now false OF THE ONLY RUN THAT WOULD READ
# THEM — the exact defect `agent.build_filing_header`'s split exists to prevent, which is why they
# are removed rather than left as plausible-looking defaults for the next backend to inherit.
#
# **The SHAPE they described is not retired**, and that is the distinction worth keeping: a backend
# declaring `structured_ordinary = True` still takes `processing._one_pass`'s content-carrying
# branch, `FilingAccount` above is still its account's schema, and the meeting flow below still
# runs exactly that way. What went is one backend's PREAMBLE, not the road.

# This backend's own ordinary environment — the ONE part of the preamble that differs per backend.
# TWO numbered points, because the shared point after it is numbered `3.`; the opening, that shared
# point and the separator come from `agent.build_filing_header`, where they are written once.
#
# **Every capability sentence here is a promise the tool list has to keep.** The five names below
# are the five tools `_register_tools` registers and nothing else — a preamble that named a sixth
# would have the model spend a request discovering it does not exist, and one that omitted a real
# tool would leave a capability the run paid for unused. Written as what each one is FOR rather than
# as a signature list: the signatures are in the tool docstrings, which the framework sends as the
# schema, and repeating them here is how the two come to disagree.
ORDINARY_AGENTIC_ENVIRONMENT = (
    "1. You hold five tools over this repo checkout, and nothing else — no shell, no network, no "
    "subagents:\n"
    "   - `search_pages(query)` — the worker's own ranking of which existing pages a text overlaps "
    "with, over the whole checkout. This is how you look further than the context below.\n"
    "   - `read_page(path)` — one page in full (its frontmatter and its body), by repo-relative "
    "path. It also reads the per-type page templates at `ops/templates/<type>.md`. Nothing else in "
    "this checkout is readable.\n"
    "   - `list_page_names()` — every page name in the repo, which is the whole wikilink "
    "vocabulary. A `[[name]]` resolves only if it is in that list.\n"
    "   - `resolve_entities(names)` — the entity registry's own answer for a list of names: the "
    "canonical id, the aliases and the entity's page when one exists. A name it does not resolve "
    "is not registered, whatever the material calls it.\n"
    "   - `write_page(path, content)` — the ONLY way you write anything, and the only writes it "
    "permits are ONE new `.md` page in this repo's fast-lane knowledge folders and your own "
    "outcome file. A page that already exists is not writable, however its name is spelled.\n"
    "2. The context in the worker's message below is a STARTING POINT, not a boundary: it is what "
    "the worker gathered before this call, and the tools reach the same checkout it read. Use them "
    "when it is not enough — search for the vocabulary the material actually uses, read a candidate "
    "before judging it a duplicate, confirm a page exists before you link it. You write the page "
    "yourself, frontmatter included, and then your account: both go through `write_page`. **Read "
    "`ops/templates/<type>.md` before you write a page of that type.** It is the structural source "
    "of truth for that type's frontmatter and sections; your frontmatter is exactly what the "
    "template declares, with `created`/`updated` set to today, minus the fields the skill below "
    "says the server owns — the worker stamps those from its own facts. An existing page of the "
    "same type is a useful second look at the house style, never the substitute for the template. "
    "Your budgets are finite (a request ceiling and a wall clock), so look with purpose rather "
    "than exhaustively.\n")

ORDINARY_AGENTIC_SYSTEM_PROMPT_HEADER = agent_module.build_filing_header(
    ORDINARY_AGENTIC_ENVIRONMENT)

# The one line of the per-item prompt that says how the account travels home. `agent`'s own default
# (`OUTCOME_CHANNEL_FILE`) says the file but not the TOOL, which is the one thing a run holding
# exactly one write tool needs told: "write your account to X" with no route named is how a model
# reaches for a `Write` it does not have and reports having filed nothing.
ORDINARY_AGENTIC_OUTCOME_CHANNEL = (
    f"\nWhen you are done, write your account to `{agent_module.OUTCOME_FILENAME}` at the repo "
    f"root — with `write_page`, the same tool you wrote the page with — in the shape the skill "
    f"documents, naming the path you wrote in `page_path`. Your final message is not read: the "
    f"outcome file is the whole of what the worker receives from you.")


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


# ══════════════════════════════════════════════════════════════════════════════════════════════
# THE TOOLS (ADR 034) — bodies here, registration in `_register_tools`, confinement inside each one
# ══════════════════════════════════════════════════════════════════════════════════════════════
# What one tool call may carry in and out. The bounds that matter are REUSED rather than invented,
# and that is the point: a tool that bounded page text differently from the boundary, or offered
# more page names than the seeded block does, would be a second answer to a question this package
# has already answered once.
#
#   * a read is bounded by `agent.MAX_PAGE_BODY_LEN` — the repo's own "how long may a whole page
#     body be" constant, the same one `parse_outcome` refuses a drafted body over. A page too long
#     to hand back is a page too long to have been filed;
#   * one LINE is clamped by `gather.MAX_EXCERPT_LINE`, because a page is line-bounded by the
#     contract linter and not character-bounded, so one pathological line can carry a whole body;
#   * the name list is bounded by `gather.MAX_LINK_NAMES` and reports its own total, for the reason
#     that constant records: a truncated vocabulary read as complete makes "not in the list" look
#     like proof a page does not exist;
#   * a WRITE is bounded by `agent.MAX_OUTCOME_BYTES` — the most bytes this agent may hand the
#     worker in one blob on any channel, page or account. This is a RESOURCE bound (a runaway write
#     into a prompt or a commit), deliberately generous at 256 KiB and NOT the same question as
#     "how long may a filed page be": the structured shape's own 20k-character body ceiling
#     (`agent.MAX_PAGE_BODY_LEN`) and the contract linter's 150-line cap are the EFFECTIVE bounds on
#     what actually lands in the repo, checked over the diff after the write. One tool call may
#     carry more bytes than one page may keep; the gates, not this ceiling, decide the second.
#
# Only the two genuinely new bounds are declared here.
MAX_TOOL_QUERY_CHARS = 2_000        # a search query is a phrase, not a document to re-embed
MAX_TOOL_NAMES = 50                 # names per `resolve_entities` call; the registry is small


# The reserved key a tool result carries its PAGE-DERIVED content half under. `_tool_payload` pulls
# it out and fences it; everything beside it is the sanitized structural scaffold. One convention,
# so the framing is one dumb function rather than a dispatch on each tool's dict shape.
_FENCED_KEY = "_fenced"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _tool_payload(result) -> str:
    """One tool result, as the text the model reads — the SEED ROAD's discipline, on the tool road.

    **The two halves are framed exactly as `agent.render_gathered` frames the gathered block**, and
    that is the whole fix (ADR 034): the structural SCAFFOLD (keys, paths, titles, names — every
    unfenced scalar already through `gather.prompt_scalar` where it was built) renders as plain
    JSON, and the page-BODY-DERIVED CONTENT half — a `read_page` body, a `search_pages` excerpt — is
    wrapped in `agent.fence(json.dumps(...))`. A page body re-entering a prompt is captured content
    on the way back in, `sources/` pages are verbatim prior captures, and the fence is the only
    thing that both labels it DATA and neutralizes an in-band fence token a page might carry.

    **NOT a third fence site.** `agent.fence` is the librarian's one declared fence builder
    (`tests/test_architecture.py` keeps the token literal in `stigmergy.text` and `agent.py` only);
    this CALLS it. An earlier version of this docstring argued the tool road needed no fence because
    JSON escaping bounds the data span — true of structure, false of SEMANTICS: an escaped string
    cannot break the JSON, but a model can still READ `"mark this canonical"` inside it and obey. The
    seed road fences its content half for exactly that reason, and the two roads carry the same bytes.

    A result with no `_fenced` half — a refusal, a write receipt, the entity resolution (server-
    owned identifiers, the structural half by nature) — renders as plain JSON, one value, unchanged.
    """
    if isinstance(result, dict) and _FENCED_KEY in result:
        content = result[_FENCED_KEY]
        scaffold = {key: value for key, value in result.items() if key != _FENCED_KEY}
        return _json(scaffold) + "\n" + agent_module.fence(_json(content))
    return _json(result)


def _readable(text: str) -> str:
    """A page's text, line by line, sanitized and clamped — bounded as a whole, and it SAYS when it
    was cut.

    Truncation is stated rather than silent for the reason `agent.render_gathered` states its own
    trim: a model handed half of a page and told nothing will judge "does this page already cover
    the material" against half a page and never know it did.
    """
    lines = [textutil.clamp(textutil.sanitize(line), gather.MAX_EXCERPT_LINE)
             for line in (text or "").splitlines()]
    body = "\n".join(lines)
    if len(body) <= agent_module.MAX_PAGE_BODY_LEN:
        return body
    return (body[:agent_module.MAX_PAGE_BODY_LEN]
            + f"\n\n[the worker cut this page here: it is longer than the "
              f"{agent_module.MAX_PAGE_BODY_LEN}-character read ceiling, so what you have is its "
              f"opening and not the whole of it]")


# The two refusals, as constants because they are the model's only account of a rule it just met.
#
# `REFUSED_WRITE` is the retired `PreToolUse` hook's sentence, extended by the one clause the hook
# never had to say (it scoped `Write`/`Edit`, and the outcome file arrived through the same tools
# unremarked). `REFUSED_READ` is the hook's "reads are confined to this worktree" made specific,
# because containment is no longer the whole rule: `gather.confined_page` admits the content zones
# and nothing else, so a message saying only "this worktree" would send a model round the same
# refusal for `.claude/`, `ops/` and every dotfile in turn.
#
# Neither one echoes the path that was refused. A refusal is prompt text, and a path the material
# chose is attacker-reachable text — this is the same rule `report.py` follows about a rejected
# capture's payload, applied to the one surface a model reads mid-run.
REFUSED_WRITE = (
    "writes are confined to a NEW .md page in one of this repo's fast-lane knowledge folders; an "
    "edit to a page that already exists is declared in the outcome's `edits` and performed by the "
    "worker. Your own outcome file at the repo root is the one other write this tool allows.")

REFUSED_READ = (
    "reads are confined to the knowledge pages of this checkout: a repo-relative path to an "
    "existing .md page under one of the content zones, or a per-type page template at "
    f"`{gather.TEMPLATE_DIR}/<type>.md`. Use `search_pages` or `list_page_names` to find a page; "
    "nothing else in the checkout is readable.")


class FilingToolbox:
    """What the five tools DO, with no agent framework anywhere near it.

    A plain object rather than five closures inside `_run`, for exactly the reason
    `agent.confined_write` is a module-level function rather than a hook body: the first version of
    that rule lived inside the run where nothing could reach it, and it was wrong in three ways at
    once — including one that denied every legitimate write on macOS. Every refusal below is
    reachable with a temporary directory and no model, and `tests/librarian/test_filing_toolbox_unit
    .py` is where each one fires against a real checkout — a rule nobody can call directly is a rule
    nobody has tried to break.

    **The tools run in THREADS.** pydantic-ai drives a sync tool through `run_in_executor`, so two
    `search_pages` calls the model batched in one turn can enter `corpus()` at once. `_corpus` and
    `_registry` cache the checkout's parse for the life of ONE run — `search_pages` is the tool a
    model calls most, and re-walking the whole knowledge repo per call would make the model's
    curiosity quadratic in the size of the corpus, on the one per-item cost that already scales with
    it (`config.GATE_BUDGET_S`). `_lock` is what makes "parsed at most once" true under that
    concurrency rather than "once if the calls happen to be serial": without it, two threads that
    both see `None` both walk the corpus, and the whole point of the cache is lost on exactly the
    turn a model searches hardest.
    """

    def __init__(self, worktree: str, *, top_k: int, excerpt_lines: int):
        self.worktree = os.path.realpath(worktree)
        self.top_k = max(int(top_k), 1)
        self.excerpt_lines = max(int(excerpt_lines), 0)
        # Read ONCE, before the model runs: the paths that already exist at the base commit. The
        # retired write hook read them at the same moment and said why — recomputing per call would
        # let a page the agent itself just wrote start counting as "existing", so its second write
        # of its own draft would be denied as an edit to somebody else's page.
        self.existing = gitcmd.tracked_paths(self.worktree)
        self._corpus = None
        self._registry = None
        self._lock = threading.Lock()

    # ── the parses, once per run — guarded because the tools run in threads ───────────────────
    # Double-checked: the fast path reads the cached value with no lock, and only a miss takes it,
    # re-checking inside so the loser of a race returns the winner's parse rather than a second one.
    def corpus(self) -> gather.Corpus:
        if self._corpus is None:
            with self._lock:
                if self._corpus is None:
                    self._corpus = gather.load_corpus(self.worktree)
        return self._corpus

    def registry(self):
        """The entity registry AT THIS ITEM'S BASE COMMIT, and it needs no new port parameter to be
        that: the worktree IS the checkout at that commit, so the file inside it is the base-commit
        file — the same reasoning `agent.read_skill` makes about the brief. Read through
        `config.REGISTRY_RELPATH`, this package's one spelling of where the registry lives."""
        if self._registry is None:
            with self._lock:
                if self._registry is None:
                    self._registry = registry_module.load_registry(
                        os.path.join(self.worktree, *config.REGISTRY_RELPATH.split("/")))
        return self._registry

    # ── the five bodies ───────────────────────────────────────────────────────────────────────
    # Every UNFENCED scalar that re-enters the prompt goes through `gather.prompt_scalar` — the SAME
    # sanitizer the seed road's structural half uses (`gather.structural_payload`), never a second
    # one. The page-BODY-derived free text (a read body, a search excerpt) is the CONTENT half and
    # is FENCED instead, by `_tool_payload`, exactly as `render_gathered` fences the gathered block.
    def search_pages(self, query: str) -> dict:
        """Rank the checkout's pages against `query`, through the gatherer's own scorer."""
        text = (query or "").strip()[:MAX_TOOL_QUERY_CHARS]
        if not text:
            return {"query": "", "matches": [],
                    "note": "an empty query matches nothing; search for the words the material "
                            "actually uses"}
        ps = gather.prompt_scalar
        found = gather.candidates_payload(gather.search_candidates(
            self.corpus(), text, top_k=self.top_k, excerpt_lines=self.excerpt_lines))
        # The identifiers (path/title/type/links_to) sanitized into the scaffold; the page-derived
        # EXCERPT fenced, keyed by the same sanitized path so the model can correlate the two.
        matches = [{"path": ps(c["path"]), "title": ps(c["title"]), "type": ps(c["type"]),
                    "links_to": [ps(name) for name in c["links_to"]]} for c in found]
        excerpts = [{"path": ps(c["path"]), "excerpt": c["excerpt"]} for c in found]
        return {"query": text, "matches": matches, "corpus_pages": len(self.corpus().rows),
                _FENCED_KEY: {"excerpts": excerpts}}

    def read_page(self, path: str) -> dict:
        """One page in full — refused unless `gather.confined_page` allows it.

        **That rule admits the content zones AND `ops/templates/*.md`, on evidence rather than
        symmetry.** This run writes the page's own container, and the template is what says what a
        container of that type owes: the knowledge repo's contract linter names those files as the
        per-type schema reference, the retired tool-holding harness read them before drafting (its
        brief said so in as many words), and the alternative — copy the shape from an existing page
        of the same type — has no source in a young brain, nor in the golden fixture, which carries
        no `wiki/concepts` page at all. Everything else outside the zones stays refused, `ops/`'s
        own `acl.json` and `entity-registry.json` first among them.

        The refusal names what IS readable rather than what went wrong with this path: a model that
        asked for `../../ops/acl.json` needs to know the shape of the permission, and a message
        echoing the path it asked for would put an attacker-chosen string back in the prompt for
        nothing.
        """
        resolved_rel = gather.confined_page(self.worktree, path or "")
        if not resolved_rel:
            return {"refused": REFUSED_READ}
        # `confined_page` returns the CANONICAL resolved relpath it judged; open and echo THAT, not
        # the asked string, so the file read is the file the rule approved (no symlink re-follow, no
        # NFD spelling that names another page).
        full = os.path.join(self.worktree, *resolved_rel.split("/"))
        try:
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as ex:
            # The class name only, never the message: an OS error carries a filesystem path.
            return {"refused": f"that page could not be read ({ex.__class__.__name__})"}
        # The path is a sanitized scaffold scalar; the BODY is the content half and is fenced.
        return {"path": gather.prompt_scalar(resolved_rel),
                _FENCED_KEY: {"content": _readable(text)}}

    def list_page_names(self) -> dict:
        """The wikilink vocabulary, through `edits.page_names` — the SAME reading `edits.validate`
        answers "does this link resolve" with, so a name offered here cannot be one the edit
        validator then refuses."""
        ps = gather.prompt_scalar
        names = sorted(edits.page_names(self.worktree, confined=True))
        return {"names": [ps(name) for name in names[:gather.MAX_LINK_NAMES]], "total": len(names)}

    def resolve_entities(self, names) -> dict:
        """The registry's own answer for each name: resolved or not, and its page when it has one.

        `resolved: false` is a REAL answer and the brief's third anchoring outcome depends on it —
        a name the registry does not know is a park, never an invention — so an unresolved name is
        returned as itself rather than dropped from the list.
        """
        ps = gather.prompt_scalar
        registry = self.registry()
        asked = [str(n).strip() for n in (names or []) if str(n).strip()][:MAX_TOOL_NAMES]
        rows = self.corpus().rows
        out = []
        for name in asked:
            cid = registry.canonical_id(name)
            if not cid:
                out.append({"asked": ps(name), "resolved": False})
                continue
            entity = registry.entities.get(cid) or {}
            out.append({
                "asked": ps(name),
                "resolved": True,
                "id": ps(cid),
                "name": ps(str(entity.get("name") or "")),
                "aliases": [ps(str(a)) for a in (entity.get("aliases") or [])],
                "page": ps(gather.entity_page(rows, cid, registry.canonical_id)) or None,
            })
        return {"entities": out}

    def write_page(self, path: str, content: str) -> dict:
        """The ONE write, gated by `agent.confined_write_target` — the same allow-list the offline
        double writes through and the retired harness's hook called.

        **It writes through `page.open_for_new` / `open_for_rewrite`, never a bare `open`** — the
        rule `page.py` wrote for this exact call site: `confined_write` allow-lists paths that do
        NOT exist yet, and the hardened opener (`O_EXCL` + `O_NOFOLLOW`) is what makes that
        invariant hold at the moment of writing rather than a moment before it. A bare `open(p, "w")`
        truncates through a symlink and past any race. The one path that legitimately EXISTS when
        written is the draft the run is iterating on (write, then fix a heading) — untracked, so
        `confined_write` still allows it — and that takes `open_for_rewrite`.

        `full` is built from the RESOLVED relpath `confined_write_target` judged, not the asked
        string, so `wiki/notes/sub/../x.md` writes `wiki/notes/x.md` rather than making a stray
        `sub/` directory the rule never approved.

        The refusal is the SDK hook's own sentence, kept deliberately: it is the wording two live
        runs of a tool-holding agent were corrected by, and a rule whose message changes with its
        enforcement mechanism teaches the next reader that the rule changed too.
        """
        target = (path or "").strip()
        rel = agent_module.confined_write_target(self.worktree, target, existing=self.existing)
        if rel is None:
            return {"refused": REFUSED_WRITE}
        blob = content or ""
        size = len(blob.encode("utf-8"))
        if size > agent_module.MAX_OUTCOME_BYTES:
            return {"refused": f"that write is {size} bytes, over the "
                               f"{agent_module.MAX_OUTCOME_BYTES}-byte ceiling for one write"}
        full = os.path.join(self.worktree, *rel.split("/"))
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            opener = page_policy.open_for_rewrite if os.path.exists(full) else page_policy.open_for_new
            with opener(full) as f:
                f.write(blob)
        except OSError as ex:
            # Same posture as the read: the class name, never the path in the message.
            return {"refused": f"that page could not be written ({ex.__class__.__name__})"}
        # The RESOLVED relpath, which is the file actually written — for a clean lane path it equals
        # the asked target, and for `sub/../x.md` it is the page the rule approved.
        log.info("the filing agent wrote %s (%d bytes)", rel, size)
        return {"written": rel, "bytes": size}


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

    # The EXPLORING shape of the ordinary flow, declared rather than inferred (see
    # `filing_port.FilingAgent.structured_ordinary`). `processing` reads THIS, never
    # `isinstance(agent, PydanticFilingAgent)`: a fourth backend, or a test double standing in for
    # one, must be able to take the other branch by declaring it rather than by being the right
    # class.
    #
    # **It flipped `True` -> `False` in ADR 034**, and the flip is the whole milestone at the seam:
    # this backend holds a confined `write_page` tool again, writes its own page, and returns its
    # account through the outcome FILE — so `processing._one_pass` takes the legacy branch, the one
    # the double has kept exercised offline since the Claude-Code harness retired.
    structured_ordinary = False

    # ...and it still wants the gathered context, which is why that is a SECOND declaration rather
    # than the inverse of the first (see `filing_port.FilingAgent.wants_gathered`). The gather is
    # this run's SEED: the tools go further than it, they do not replace it. A run that started
    # from nothing would spend its first requests rediscovering what code can hand it for free.
    wants_gathered = True

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
        """The ordinary flow: file ONE capture, ITERATING over the checkout (ADR 034).

        Deliberately NOT structurally parallel to `run_meeting` below any more, and the asymmetry
        is the decision rather than drift: a meeting transcript is handed everything it could
        possibly need (the whole registry, the metadata, the source path) and has nothing to go
        looking for, while an ordinary capture is one paragraph about a brain of unknown shape —
        "what does this already say about X" is a question no gatherer answers completely, because
        the words the material uses need not be the words the pages use. So this flow gets tools
        and that one keeps its single call.

        `gathered` is the deterministic gatherer's context, already rendered by `processing` — the
        SEED, not the boundary.
        """
        import asyncio
        return asyncio.run(self._run(
            worktree=worktree, material=material, hints=hints, submitted_by=submitted_by,
            corrective=corrective, reply=reply, flow_note=flow_note, gathered=gathered))

    def _register_tools(self, filer, toolbox: "FilingToolbox") -> None:
        """Register the five tools on one `Agent`, binding each to `toolbox`'s own body.

        **These docstrings are prompt text.** pydantic-ai sends a tool's docstring and signature to
        the model as the tool's schema, so they are the model's usage guide and not developer
        notes: each says what the tool ANSWERS, what it refuses, and what to do with the answer.
        The engineering rationale for each rule lives on `FilingToolbox`'s own methods, where the
        next developer will look for it — two audiences, two texts, one behaviour.

        Every wrapper is thin on purpose: the body is `toolbox`'s, so the confinement rules are
        testable with no framework, and this function's only job is the framing.
        """
        @filer.tool_plain
        def search_pages(query: str) -> str:
            """Find existing pages whose text overlaps a query, ranked, with an excerpt of each.

            This is how you look further than the context the worker gathered for you. Search for
            the words this brain would use, not only the words the capture uses: a capture about
            "the renewal window" may be about a page called "Contract terms". Several narrow
            searches beat one broad one — each returns the top matches only.

            The ranking is lexical (shared terms, weighted by where they appear), so a match is a
            suggestion and never a verdict: read a page before you call it a duplicate of the
            material. Returns JSON: `matches` (path, title, type, links_to, excerpt) and
            `corpus_pages`, the size of the whole checkout.
            """
            return _tool_payload(toolbox.search_pages(query))

        @filer.tool_plain
        def read_page(path: str) -> str:
            """Read one existing page in full — its frontmatter and its body.

            Use it before judging overlap versus duplicate, before linking a page you have only
            seen the title of, and to follow the neighbourhood one hop further. `path` is
            repo-relative, exactly as `search_pages` returns it (for example
            `wiki/notes/Some Page.md`).

            **It also reads the page TEMPLATES, at `ops/templates/<type>.md`** — one per page type,
            and each one is the structural source of truth for what a page of that type owes: the
            frontmatter fields it declares and the sections it carries. Read the template for the
            type you are filing before you write the page.

            Everything else is refused — a path outside the repo, a settings file, the entity
            registry, a dotfile — and the refusal says what IS readable. A very long page comes
            back cut, and says so where it was cut.
            """
            return _tool_payload(toolbox.read_page(path))

        @filer.tool_plain
        def list_page_names() -> str:
            """Every page name in this repo: the whole wikilink vocabulary.

            A `[[name]]` you write resolves only if it is in this list, and a link that resolves to
            nothing is refused by the contract linter and costs the whole capture. Use it to check
            a name before you link it, and to check your own title is not already taken — a title
            that collides with an existing page is refused rather than written over it.

            The list is bounded and reports its `total`: when `total` is larger than the list, a
            name's absence proves nothing, so search for it instead of concluding it is missing.
            """
            return _tool_payload(toolbox.list_page_names())

        @filer.tool_plain
        def resolve_entities(names: list[str]) -> str:
            """Ask the entity registry what it knows about a list of names.

            The registry is the ONLY thing that decides whether a name is an entity: for each name
            you get `resolved` true or false, and when true, the canonical id, the registry's own
            spelling, its aliases, and its page when this brain has one (`page` is null when the
            entity is registered but has no page yet — a real state, and a different one from "not
            registered").

            Use it before declaring an anchor. A name it does not resolve is not registered, however
            the material spells it: park the capture as `unresolved-entity` rather than inventing an
            entity or falling back to company-wide scope to get it filed.
            """
            return _tool_payload(toolbox.resolve_entities(names))

        @filer.tool_plain
        def write_page(path: str, content: str) -> str:
            """Write a file. This is the only way you write anything, including your own account.

            Two writes are permitted and no others: ONE new `.md` page in this repo's fast-lane
            knowledge folders — the whole file, its frontmatter block included — and your outcome
            file at the repo root. A page that already exists is not writable however its name is
            spelled (case and accents are folded before the comparison), because an edit to an
            existing page is DECLARED in your account's `edits` and performed by the worker.

            A refused write returns a refusal and changes nothing; it is not an error to recover
            from by trying a different path out of the lane. `content` is written verbatim, so
            write the page you mean to file.
            """
            return _tool_payload(toolbox.write_page(path, content))

    async def _run(self, *, worktree, material, hints, submitted_by, corrective, reply="",
                   flow_note="", gathered="") -> AgentRun:
        import asyncio

        # Imported HERE, never at module scope — see the module docstring.
        from pydantic_ai import Agent
        from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
        from pydantic_ai.usage import RunUsage, UsageLimits

        from stigmergy.kernel.usage_repair import ensure_usage_extraction_repaired

        # The framework's own usage extraction silently reports ZERO tokens for any OpenAI model
        # that carries reasoning details, and this backend exists to turn tokens into dollars. It
        # matters MORE on an iterating run than on a single call: an unrepaired extraction under-
        # prices every request in the loop, not one. Idempotent; deferring to the framework the day
        # it is fixed.
        ensure_usage_extraction_repaired()

        run = AgentRun()
        worktree_root = os.path.realpath(worktree)

        # The skill comes out of the WORKTREE, which is the checkout at this item's base commit —
        # `agent.read_skill`, deliberately not a second reader of the same file. A missing skill
        # raises `LibrarianConfigError` here, before any model call is spent.
        instructions = agent_module.build_system_prompt(
            agent_module.read_skill(worktree_root),
            header=ORDINARY_AGENTIC_SYSTEM_PROMPT_HEADER)
        prompt = agent_module.build_prompt(
            material=material, hints=hints, submitted_by=submitted_by, gathered_block=gathered,
            outcome_channel=ORDINARY_AGENTIC_OUTCOME_CHANNEL, corrective=corrective, reply=reply,
            flow_note=flow_note)

        # Model resolution gets its OWN narrow try, for the reason `_run_meeting` records: the
        # blanket handler below would report a configuration fault as "the run failed".
        #
        # **No `output_type`.** The account does not come home in the envelope on this flow — it
        # comes home as `.librarian-outcome.json`, written through `write_page` and read back
        # below. A structured output_type here would ask the model for the account TWICE, in two
        # shapes, and leave `_cross_check_outcome` two claims to reconcile.
        try:
            model = self.model_factory() if self.model_factory else self.settings.model
            filer = Agent(model, instructions=instructions, retries=OUTPUT_RETRIES)
        except Exception as ex:  # noqa: BLE001 — class name only, like every other wrap here
            raise priced(run, AgentError(
                f"could not resolve the configured model ({ex.__class__.__name__}); "
                f"$STIGMERGY_LIBRARIAN_MODEL is {self.settings.model!r}")) from ex

        # The tools are built and registered OUTSIDE that try, and after it, deliberately.
        #
        # AFTER, because model resolution is the cheap, common configuration fault and it should
        # still be the first thing a misconfigured worker meets. OUTSIDE, because these two lines
        # have failure modes that are not the operator's model: `FilingToolbox` reads the checkout
        # (`git ls-files`), whose fault is a `GitError` that `processing.PROCESSING_ERRORS` already
        # names as its own stage, and a tool whose signature the framework cannot turn into a
        # schema is OUR defect, whose honest destination is the traceback `worker.process_next`
        # prints for an unexpected exception. Wrapping either as "could not resolve the configured
        # model" is the exact mislabelling the narrow try above exists to prevent, one fault over.
        toolbox = FilingToolbox(worktree_root, top_k=self.settings.gather_top_k,
                                excerpt_lines=self.settings.gather_excerpt_lines)
        self._register_tools(filer, toolbox)
        # OUR usage accumulator, handed in rather than read off the result: pydantic-ai fills this
        # object as the run proceeds, so a run that dies mid-flight still leaves its real counts
        # here — which on a LOOP is the difference between pricing eleven requests and pricing none.
        usage = RunUsage()
        # The iteration budget. `settings.max_turns` is the retired backend's conversational bound
        # under a new mechanism and the SAME semantic — how many times this agent may go round —
        # so it is un-deprecated rather than replaced by a second number an operator would have to
        # learn (see `config.DEFAULT_MAX_TURNS`). `max_tool_calls` stays deprecated: the framework
        # already accumulates `RunUsage.tool_calls`, the request ceiling bounds the loop that makes
        # them, and a second hand-counted ceiling needs a defect behind it rather than a symmetry.
        #
        # Passed straight through — NOT `max(..., 1)`. A tool run needs at least two requests (one to
        # call a tool, one to write its account), so a `max_turns` below 2 fails every capture at
        # full cost; silently clamping it to 1 would rewrite an operator's number, which this
        # package refuses on principle. `worker.startup_checks` refuses `< 2` BY NAME before the
        # first claim, so a run that reaches here has a usable ceiling.
        limits = UsageLimits(request_limit=int(self.settings.max_turns))
        try:
            # The wall clock is a bound WE own — pydantic-ai has none — and the worker's visibility
            # lease is derived from it (`config.minimum_visibility_timeout_s`). It bounds ONE agent
            # pass; the lease covers `MAX_AGENT_ATTEMPTS` passes plus the gate/commit/push budget, so
            # a single pass that runs its full `timeout_s` still fits inside the lease. The guarantee
            # is that no ONE pass runs unbounded, not an absolute promise no capture is ever
            # redelivered — a sync tool that itself hangs past the timeout is interrupted between
            # awaits, and the headroom (`VISIBILITY_HEADROOM_S`) is what the estimate leans on.
            async with asyncio.timeout(self.settings.timeout_s):
                result = await filer.run(prompt, usage=usage, usage_limits=limits)
        # **The fault arms do NOT record `turns`/`tool_calls`.** They fire by raising, so the local
        # `run` never returns — `priced()` attaches `run_cost_usd` to the exception (which
        # `report.failed_system` reads) and nothing else off `run` is consumed. Counting the loop
        # onto an envelope that is discarded is a dead assignment; the numbers live on the RETURNING
        # road, where the envelope self-describes (see `_counted` below).
        except TimeoutError as ex:
            run.cost_usd = self._fault_cost(usage, flow="filing")
            raise priced(run, AgentError(
                f"the filing agent exceeded its {self.settings.timeout_s}s budget")) from ex
        except UsageLimitExceeded as ex:
            # CAUGHT BY NAME, above the blanket arm below, because this fault has an operator's
            # answer in it and the blanket one would report it as "the run failed
            # (UsageLimitExceeded)" — a class name, at somebody who can fix this in one variable.
            # A capture whose filing genuinely needs more looking than the ceiling allows is a
            # legitimate reason to raise it; a model looping is a reason not to.
            run.cost_usd = self._fault_cost(usage, flow="filing")
            raise priced(run, AgentError(
                f"the filing agent used all "
                f"{self.settings.max_turns} of its model requests for one capture without "
                f"finishing (the iteration budget, $STIGMERGY_LIBRARIAN_MAX_TURNS)")) from ex
        except UnexpectedModelBehavior as ex:
            # A SHAPE problem — the class the worker's corrective retry exists for. Travels as an
            # `OutcomeShapeError` carrying a finding, exactly as a refused account from the file
            # channel does; see `_run_meeting`'s own arm for the full argument.
            run.cost_usd = self._fault_cost(usage, flow="filing")
            raise priced(run, OutcomeShapeError([gates.Finding(
                agent_module._OUTCOME_GATE, "framework-rejected",
                f"the filing run ended badly ({ex.__class__.__name__}): call the tools this run "
                f"declares, with the arguments they declare, and write your account to "
                f"{agent_module.OUTCOME_FILENAME} with `write_page`")])) from ex
        except Exception as ex:  # noqa: BLE001 — class name only: provider errors carry prompt text
            run.cost_usd = self._fault_cost(usage, flow="filing")
            raise priced(run, AgentError(
                f"the filing agent run failed ({ex.__class__.__name__})")) from ex

        run.cost_usd = self._cost(usage, flow="filing")
        self._counted(run, usage)
        run.stop_reason = str(getattr(result.response, "finish_reason", "") or "")
        # The account is the FILE, not the final message. `result.output` is plain text on this
        # flow and is deliberately ignored: a model that says "I filed it" in prose and wrote no
        # outcome file has filed nothing, and reading the prose would invent an account.
        #
        # Read HERE rather than in `processing`, mirroring the double: the backend that owns the
        # channel is the backend that drains it. `read_outcome` deletes the file as it parses, so
        # `processing`'s own `discard_outcome_file` a moment later is a harmless no-op — and the
        # ceiling, the JSON parse and every bound in `parse_outcome` are the SAME ones the double's
        # account goes through, because a model that has just read untrusted material is not a
        # trusted writer whichever framework carried it.
        try:
            run.outcome = agent_module.read_outcome(worktree_root)
        except AgentError as ex:
            priced(run, ex)
            raise
        return run

    @staticmethod
    def _counted(run: AgentRun, usage) -> None:
        """Put the framework's own loop counters on the envelope — on the RETURNING road only.

        `RunUsage` accumulates `requests` and `tool_calls` as the run proceeds and pydantic-ai
        mutates it in place, so the returned envelope self-describes: a run reports the real number
        of requests it made and tools it called. Counting them a second time in the tool wrappers
        was the alternative and is exactly the second answer to one question this package refuses.

        **Not called on the fault road.** A fault raises rather than returns, so its envelope is
        discarded — the spend travels on the exception (`priced` → `run_cost_usd`, which
        `report.failed_system` reads), and putting loop counters on an object nobody holds is a dead
        assignment. Nothing downstream reads `turns`/`tool_calls` off a fault anyway.

        Read defensively (`getattr`), for the reason `_cost` is: the framework's usage object has
        grown fields before, and an injected offline model may hand back a simpler one.
        """
        run.turns = int(getattr(usage, "requests", 0) or 0)
        run.tool_calls = int(getattr(usage, "tool_calls", 0) or 0)

    def run_meeting(self, *, worktree: str, material: str, meeting_meta: dict, registry,
                    source_page_path: str, corrective: str = "", reply: str = "") -> AgentRun:
        """One structured call: the brief as instructions, the item as the prompt, a typed account
        back — and UNCHANGED by ADR 034, deliberately.

        Giving this flow tools is a separate decision with its own evidence, and there is nothing
        here for a tool to fetch: the transcript, the whole entity registry, the drop's metadata
        and the source page's path are all in the prompt, and code writes every page in the set. A
        `read_page` here would be a capability with no question to answer.
        """
        import asyncio
        return asyncio.run(self._run_meeting(
            worktree=worktree, material=material, meeting_meta=meeting_meta, registry=registry,
            source_page_path=source_page_path, corrective=corrective, reply=reply))

    async def _run_meeting(self, *, worktree, material, meeting_meta, registry, source_page_path,
                           corrective, reply="") -> AgentRun:
        import asyncio

        # Imported HERE, never at module scope — the rule `agent.py`'s own docstring records, and
        # `tests/test_architecture.py` enforces: an offline run must not load an agent
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
        # look like a tool-using loop's. The port documents zero as a legitimate answer, and nothing
        # downstream branches on either counter.
        run = AgentRun()
        worktree_root = os.path.realpath(worktree)

        # The brief comes out of the WORKTREE, which is the checkout at this item's base commit —
        # `agent.read_meeting_brief`, deliberately not a second reader of it. A missing
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
        # retired backend's conversational bound (30 turns of an agent loop), and borrowing it here
        # would license thirty full requests for a flow that must make one.
        limits = UsageLimits(request_limit=1 + OUTPUT_RETRIES)
        try:
            # The wall clock is a bound WE own — pydantic-ai has none, exactly like the harness before it,
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
        # the ordinary flow's own outcome read takes: the run was paid for whether or not its
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
