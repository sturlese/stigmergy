"""BOTH flows on pydantic-ai: an ITERATING ordinary run with five tools, one structured meeting
call.

Deterministic code may SEED context and IMPLEMENT tools; it must not replace the judgment that
decides when the context is not enough. The gatherer is therefore NOT here — `processing` gathers
and renders, so two backends share one context builder and one fence discipline. What IS here is
`FilingToolbox`, whose confinement rules are asked inside each call rather than in a hook.

`kernel.llm.build_processor` is deliberately NOT reused: routing through `resolve_backend` would
create a SECOND offline path beside `double.DoubleAgent`, with different semantics answering to a
different variable. `agent.*`'s reads, prompt builders and outcome parses ARE reused — the SAME
trust boundary the file channel goes through, because a structured provider is not a trusted one.

`pydantic_ai` is imported inside the methods, never at module scope: a keyless run must not load
an agent framework. `pydantic` itself is module-scope, so the schemas stay buildable by a test.
"""
import json
import logging
import os
import threading
from typing import Literal, get_origin

from pydantic import BaseModel, Field, model_validator

from stigmergy import text as textutil
from stigmergy.kernel import registry as registry_module
from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import config, gates, gather, gitcmd, pricing
from stigmergy.librarian import page as page_policy
from stigmergy.librarian.errors import AgentError, LibrarianConfigError, OutcomeShapeError
from stigmergy.librarian.filing_port import AgentRun, priced

log = logging.getLogger(__name__)

BACKEND_NAME = "pydantic"

# How many times the FRAMEWORK may re-ask the model. On the MEETING flow this is read twice on
# purpose — retry budget AND request ceiling — and the two must agree, or the ceiling either
# strangles a legitimate re-validation or bounds nothing.
OUTPUT_RETRIES = 1

# How much of an `UnexpectedModelBehavior` message reaches the corrective retry's Finding, as ONE
# line: the framework's `__str__` embeds a response body, and this text reaches a PROMPT.
MAX_FAULT_MESSAGE_LEN = 200

# The WORKER LOG's budget for the same text — wider, still bounded: those messages can carry
# captured material or PII verbatim. No fence-neutralize; a log is not a prompt.
MAX_FAULT_LOG_LEN = 500


def _log_fault(flow: str, ex: BaseException) -> None:
    """What a fault actually said, in the operator's log. Only the exception's CLASS travels on the
    wire — a provider error's message can carry the whole prompt back — so this line is the only
    place the rest of it survives, and both halves are clamped for the same reason."""
    log.warning("%s agent: %s: %s (cause=%s)", flow, ex.__class__.__name__,
                textutil.one_line(str(ex), MAX_FAULT_LOG_LEN),
                textutil.one_line(repr(ex.__cause__), MAX_FAULT_LOG_LEN))


def _fault_line(ex: BaseException) -> str:
    """The same message quoted back to the model in the corrective Finding: shorter, and
    fence-neutralized because unlike the log line this one lands inside the next PROMPT."""
    return textutil.one_line(textutil.neutralize_fence(str(ex)), MAX_FAULT_MESSAGE_LEN)

# Read by the preflight that refuses a missing key BEFORE the first claim; an unknown prefix
# simply gets no preflight. RE-EXPORTED from the kernel (audit T1): the vision gate asks the
# same question of the same table, and two tables would drift — `kernel.settings` hosts it so
# this import drags no agent framework into a keyless run.
from stigmergy.kernel.settings import PROVIDER_KEY_ENV, provider_of  # noqa: E402,F401


def prompt_cache_settings(model: str, prompt_cache: str) -> dict | None:
    """The `model_settings` dict for the ORDINARY run's `Agent(...)`, or `None`. A PURE function
    of two resolved strings, needing no `pydantic_ai` import. The ordinary flow ITERATES and every
    request resends a byte-identical growing prefix, so a cache READ at ~0.1x the input rate is
    where the bill lives. Asked through `provider_of`, so there is no second answer to "is this
    model Anthropic's"."""
    if provider_of(model) != "anthropic" or prompt_cache not in config.PROMPT_CACHE_TTLS:
        return None
    return {
        "anthropic_cache_instructions": prompt_cache,
        "anthropic_cache_tool_definitions": prompt_cache,
        "anthropic_cache_messages": prompt_cache,
    }


# ── the accounts, as schemas instead of a file ────────────────────────────────────────────────
# These mirror what `agent.parse_*_outcome` REQUIRES, not everything it ACCEPTS — a deliberate
# asymmetry, and the one place to look when the two look out of step: a schema is what the model is
# ASKED for, and asking for one field in two spellings is how an account ends up declaring both.
# Everything the model must answer is here in the same shape the file channel carries, so the
# boundary parse stays shared.
#
# BOUNDS are deliberately NOT restated — a second set would drift from the one the file channel is
# judged by. REQUIREDNESS is: a defaulted field makes an omission INVISIBLE, so the framework
# accepts a half-empty account, its `OUTPUT_RETRIES` never fire, and the boundary refuses downstream
# having spent the WORKER's one corrective retry. The BOUNDARY still keeps every check, because it
# also judges the FILE channel.
#
# `FilingAccount` is NOT wired as an `output_type` today: the ordinary run's account comes home as a
# file (see the `Agent(...)` construction in `run`, which passes none on purpose — a structured one
# would ask for the account twice, in two shapes). Its validator therefore guards the structured
# ordinary road only if one is ever enabled; `MeetingAccount` is the one that runs.


def _needed(field: str, instead: str) -> str:
    """One completeness refusal addressed to the MODEL: pydantic-ai hands a validator's
    `ValueError` back as the retry prompt, so this is the only text that can repair the
    account."""
    return f"`{field}` is required and came back empty. {instead}"


def _wants_structure(annotation) -> bool:
    """Is this field DECLARED as a structure — a list or a nested model? The shield that keeps
    `StructuredInbound` away from content: a `str` field's prose is never decoded, however
    JSON-shaped it looks, because its annotation says prose."""
    if get_origin(annotation) in (list, dict):
        return True
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


class StructuredInbound(BaseModel):
    """Every schema below extends THIS, not `BaseModel`: the producer is a MODEL behind a
    provider's tool-calling, and more than one provider serializes a nested list or object as
    its JSON STRING. Measured directly (2026-08-19): GLM-5.2 through OpenRouter returned
    `triage='{}'` — the string — so validation failed, the framework's single output retry
    burned on a SERIALIZATION quirk, and the model's second attempt flattened the account into
    empty lists that validated: the filing golden's meeting captures filed with decisions 0/2.

    INBOUND ONLY, structured fields only, and never at correctness's expense: a bracketed
    string on a field whose annotation is a list or nested model is `json.loads`ed; anything
    that does not parse stays as it arrived for the ordinary validation to refuse. Parent
    validation precedes child validation, so a nested model's own repairs compose with this."""

    @model_validator(mode="before")
    @classmethod
    def _decode_stringified_structures(cls, data):
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for name, field in cls.model_fields.items():
            value = out.get(name)
            if not isinstance(value, str) or not _wants_structure(field.annotation):
                continue
            stripped = value.strip()
            if stripped.startswith(("[", "{")):
                try:
                    out[name] = json.loads(stripped)
                except ValueError:
                    pass
        return out


# ── the one shape BOTH accounts declare ───────────────────────────────────────────────────────
# An edit means the same thing on either flow — `agent._parse_edits` bounds it once,
# `edits.apply_declared` performs it once and `gate_body_rewrite` judges it once — so the two
# accounts name ONE model rather than each carrying its own idea of what an edit is. The name is
# the ordinary account's, from the flow that had the field first.
class OrdinaryEdit(StructuredInbound):
    """One DECLARED edit to a page that already exists — performed by the worker, never by this
    agent (`edits.py`)."""
    path: str = ""
    kind: str = ""
    link: str = ""
    note: str = ""


class MeetingAnchoring(StructuredInbound):
    """One decision's own anchor. `kind` is `entity` (with `entities`) or `company` (with a written
    `reason`); the registry, not this schema, decides whether a name resolves."""
    kind: str = ""
    reason: str = ""
    entities: list[str] = Field(default_factory=list)


class MeetingDecision(StructuredInbound):
    """One decision page's content — a title, a drafted body, and its OWN anchor."""
    title: str = ""
    body: str = ""
    anchoring: MeetingAnchoring = Field(default_factory=MeetingAnchoring)


class MeetingActionItem(StructuredInbound):
    owner: str = ""
    action: str = ""
    done: bool = False


class MeetingFinding(StructuredInbound):
    """A steering attempt the agent noticed. Only `category` travels — never the payload."""
    category: str = ""


# These docstrings are the JSON-schema DESCRIPTIONS the model reads, not notes to a reader here.
class NewEntity(StructuredInbound):
    """An entity the material is about that the registry does not know — PROPOSE it here and
    anchor to its `name`. Fill every field: `name` spelled exactly as the material spells it,
    `entity_type` (person, organization, product, tool, repository, place or project), `role` (one
    line on what it is), `aliases` (the other spellings the material uses for it), `summary` (the
    What / Who paragraph of its page), `facts` (what the material AND the pages already in this
    brain establish about it, one line each — search for the name before you write) and
    `connections` (`[[Page]] — why`, naming pages that exist or the page you are filing). The
    worker writes the entity page from this, confirmed by whoever captured."""
    name: str = ""
    entity_type: str = ""
    role: str = ""
    aliases: list[str] = Field(default_factory=list)
    summary: str = ""
    facts: list[str] = Field(default_factory=list)
    connections: list[str] = Field(default_factory=list)


class NewAlias(StructuredInbound):
    """A spelling the material uses for a REGISTERED entity that the registry does not list yet:
    `entity` is that entity's id (or registered name) and `alias` the spelling, exactly as the
    material writes it. The worker adds it to that entity's registered spellings."""
    entity: str = ""
    alias: str = ""


class EntityUpdate(StructuredInbound):
    """Something the material ESTABLISHES about an entity the registry already knows, to be added
    to that entity's own page: `entity` is its id (or registered name), `facts` the new one-line
    facts, `connections` new `[[Page]] — why` lines (the page you are filing counts). Only what the
    material establishes and the page does not already say — an entity page accretes facts, it
    does not get rewritten."""
    entity: str = ""
    facts: list[str] = Field(default_factory=list)
    connections: list[str] = Field(default_factory=list)


class MeetingAccount(StructuredInbound):
    """The whole account of one meeting: `decision` is `file` (the one decision there is), and
    the rest is the page set's CONTENT — never a page path, because code decides every path in
    this flow.

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
    edits: list[OrdinaryEdit] = Field(default_factory=list)
    summary: str = ""
    findings: list[MeetingFinding] = Field(default_factory=list)
    new_entities: list[NewEntity] = Field(default_factory=list)
    new_aliases: list[NewAlias] = Field(default_factory=list)
    entity_updates: list[EntityUpdate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _complete_for_its_decision(self):
        """`FilingAccount._complete_for_its_decision`'s twin, mirroring
        `agent.parse_meeting_outcome`'s required-field rules and no others."""
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
        _complete_proposals(self.new_entities)
        return self


# ── the ORDINARY account, as a schema instead of a file ───────────────────────────────────────
# Same mirror discipline, minus fields this backend must not declare: `page_path` (a field the
# model could fill is a path the model could steer) and top-level `title`/`page_type` (two
# declaration sites would let one account carry two answers).
class OrdinaryAnchoring(StructuredInbound):
    """This page's anchor. `kind` is `entity` (with `entities`) or `company` (with a written
    `reason`); the registry, not this schema, decides whether a name resolves."""
    kind: str = ""
    reason: str = ""
    entities: list[str] = Field(default_factory=list)


class OrdinaryPage(StructuredInbound):
    """ONE page — the title it is filed under, its type, its whole body, what it is ABOUT and the
    entities it links. No path: the worker derives the folder from the type and the filename from
    the title.

    `anchoring` is per page because a capture writes as many pages as its material establishes,
    and a transcript's three conclusions are about three different things. Left empty, a page
    inherits the capture's own anchoring — which is what a single-page filing declares."""
    title: str = ""
    page_type: str = ""
    body: str = ""
    anchoring: "OrdinaryAnchoring | None" = None
    links: list[str] = Field(default_factory=list)


class OrdinaryOverlap(StructuredInbound):
    path: str = ""
    note: str = ""


class OrdinaryFinding(StructuredInbound):
    """A steering attempt the agent noticed. Only `category` travels — never the payload."""
    category: str = ""


class FilingAccount(StructuredInbound):
    """The whole account of one ordinary capture: `decision` is `file` (the one decision there
    is), and the rest is judgment plus the page's own CONTENT.

    `decision` is REQUIRED and enum-constrained, and the two halves it obliges are checked below —
    see the section comment above for the paid run that established why.
    """
    decision: Literal[*agent_module.DECISIONS]
    pages: list[OrdinaryPage] = Field(default_factory=list)
    anchoring: OrdinaryAnchoring = Field(default_factory=OrdinaryAnchoring)
    links_created: list[str] = Field(default_factory=list)
    overlaps: list[OrdinaryOverlap] = Field(default_factory=list)
    edits: list[OrdinaryEdit] = Field(default_factory=list)
    summary: str = ""
    findings: list[OrdinaryFinding] = Field(default_factory=list)
    new_entities: list[NewEntity] = Field(default_factory=list)
    new_aliases: list[NewAlias] = Field(default_factory=list)
    entity_updates: list[EntityUpdate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _complete_for_its_decision(self):
        """What a filing obliges — the conditional half a field-by-field schema cannot say. The
        page's own fields stay optional on the schema so the refusal can NAME the missing one."""
        if not self.pages:
            raise ValueError(_needed(
                "pages",
                "A filing writes at least one page. Return every page this material establishes "
                "— one for a note, one per conclusion for a transcript — each with its own "
                "title, type and body."))
        for n, page in enumerate(self.pages, 1):
            where = f"pages[{n}]" if len(self.pages) > 1 else "pages[1]"
            if not (page.title or "").strip():
                raise ValueError(_needed(
                    f"{where}.title",
                    "It is the page's name, its filename and — for the first page — the commit "
                    "subject a human reads in `git log`, and there is nothing else to derive it "
                    "from."))
            if not (page.page_type or "").strip():
                raise ValueError(_needed(
                    f"{where}.page_type",
                    "Name the TYPE (note, decision or concept) — never a folder or a path; the "
                    "worker puts the page where a page of that type goes."))
            if not (page.body or "").strip():
                raise ValueError(_needed(
                    f"{where}.body",
                    "The worker writes the page from this account, so the page's own text has to "
                    "be in it: return the whole page below its H1, with no frontmatter block."))
        _complete_proposals(self.new_entities)
        return self


def _complete_proposals(new_entities) -> None:
    """The three fields without which a proposed entity has no page — mirrored from
    `agent._parse_new_entities`, the file channel's own rule, so both channels refuse the same
    half-proposal one road earlier than the worker would."""
    for n, proposed in enumerate(new_entities):
        for field_name, why in (("name", "an entity is a name before it is anything else"),
                                ("entity_type", "one of person, organization, product, tool, "
                                                "repository, place or project"),
                                ("summary", "the What / Who paragraph the entity page lands "
                                            "with — nobody edits it afterwards")):
            if not (getattr(proposed, field_name, "") or "").strip():
                raise ValueError(_needed(f"new_entities[{n}].{field_name}", f"{why}."))


# The ONE per-backend part of the preamble. TWO numbered points, because the shared point after
# it is numbered `3.`. Every capability sentence is a promise the tool list must keep: these five
# names are exactly the five `_register_tools` registers, written as what each is FOR.
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
    "verbatim comes back with `near` — the registered entities that name partly spells — which are "
    "candidates for you to judge, not answers. A name that is genuinely new is proposed in your "
    "account (`new_entities`), never written to `wiki/entities/` yourself.\n"
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

# Names the TOOL, not just the file: "write your account to X" with no route named is how a model
# reaches for a `Write` it does not have and reports having filed nothing.
ORDINARY_AGENTIC_OUTCOME_CHANNEL = (
    f"\nWhen you are done, write your account to `{agent_module.OUTCOME_FILENAME}` at the repo "
    f"root — with `write_page`, the same tool you wrote the page with — in the shape the skill "
    f"documents, naming the path you wrote in `page_path`. Your final message is not read: the "
    f"outcome file is the whole of what the worker receives from you.")


# This backend's MEETING environment paragraph; the rest comes from `agent.build_meeting_header`.
MEETING_ENVIRONMENT = (
    "Your environment:\n"
    "\n"
    "1. You have NO tools. You cannot read, search or write anything, and you do not write your "
    "account to a file: you RETURN it, as the structured object this run's output schema "
    "declares. That schema mirrors the shape the skill documents, field for field. Everything you "
    "need is in the worker's own message below: the transcript, the entity registry (every entity "
    "this brain already knows), the meeting metadata, and the source page's own path.\n")

# The one place this run contradicts the brief, said out loud immediately before it: the brief
# tells its reader it holds a `Write` tool, and injecting that under "you have NO tools" hands the
# model a contradiction to resolve either way.
OVERRIDE_NOTE = (
    f"One override, and it is the only place this run departs from the skill below. The skill was "
    f"written for a run that holds a `Write` tool and returns its account by writing "
    f"`{agent_module.OUTCOME_FILENAME}` at the repo root. This run has NEITHER: no tool, no file. "
    f"Where the skill describes that tool or that file, read it as describing the SHAPE of your "
    f"account only — you return that same object as this run's structured output instead. Every "
    f"other word of the skill applies to you unchanged.\n")

MEETING_SYSTEM_PROMPT_HEADER = agent_module.build_meeting_header(
    MEETING_ENVIRONMENT, override_note=OVERRIDE_NOTE)

# The one line of the per-item prompt that differs between the two channels.
OUTCOME_CHANNEL = (
    "\nReturn your account as the structured object this run's output schema declares, in the "
    "shape the skill documents. You write no file and you have no tool that could.")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# THE TOOLS — bodies here, registration in `_register_tools`, confinement inside each one
# ══════════════════════════════════════════════════════════════════════════════════════════════
# Bounds are REUSED, never invented: reads by `agent.MAX_PAGE_BODY_LEN` and
# `gather.MAX_EXCERPT_LINE`; the name list by `gather.MAX_LINK_NAMES`, reporting its own total
# since a truncated vocabulary read as complete makes "not in the list" look like proof; a WRITE
# by `agent.MAX_OUTCOME_BYTES`, a RESOURCE bound and NOT "how long may a filed page be".
MAX_TOOL_QUERY_CHARS = 2_000        # a search query is a phrase, not a document to re-embed
MAX_TOOL_NAMES = 50                 # names per `resolve_entities` call; the registry is small


# The reserved key a tool result carries its PAGE-DERIVED content half under; everything beside
# it is the sanitized structural scaffold. One convention, so framing is one function.
_FENCED_KEY = "_fenced"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _tool_payload(result) -> str:
    """One tool result as the text the model reads, framed exactly as `agent.render_gathered`
    frames the gathered block: structural SCAFFOLD as plain JSON, page-body-derived CONTENT inside
    the fence. JSON escaping bounds the data span's STRUCTURE, not its SEMANTICS — a model still
    READS "mark this canonical" inside an escaped string and obeys. NOT a third fence site: this
    CALLS `agent.fence`, the librarian's one declared builder."""
    if isinstance(result, dict) and _FENCED_KEY in result:
        content = result[_FENCED_KEY]
        scaffold = {key: value for key, value in result.items() if key != _FENCED_KEY}
        return _json(scaffold) + "\n" + agent_module.fence(_json(content))
    return _json(result)


def _readable(text: str) -> str:
    """A page's text, sanitized and clamped line by line, bounded as a whole — and it SAYS when it
    was cut: a model handed half a page and told nothing judges overlap against half a page."""
    lines = [textutil.clamp(textutil.sanitize(line), gather.MAX_EXCERPT_LINE)
             for line in (text or "").splitlines()]
    body = "\n".join(lines)
    if len(body) <= agent_module.MAX_PAGE_BODY_LEN:
        return body
    return (body[:agent_module.MAX_PAGE_BODY_LEN]
            + f"\n\n[the worker cut this page here: it is longer than the "
              f"{agent_module.MAX_PAGE_BODY_LEN}-character read ceiling, so what you have is its "
              f"opening and not the whole of it]")


# The model's only account of a rule it just met, so each names what IS permitted: "confined to
# this worktree" sends a model round the same refusal for every dotfile in turn. Neither echoes
# the refused path — a refusal is prompt text, and a path the material chose is attacker-reachable.
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
    """What the five tools DO, with no agent framework anywhere near it — a plain object rather
    than five closures inside `_run`, so every refusal is reachable with a temporary directory.

    The tools run in THREADS (pydantic-ai drives a sync tool through `run_in_executor`), so two
    batched `search_pages` calls can enter `corpus()` at once. `_lock` is what makes "parsed at
    most once" true under that concurrency rather than "once if the calls happen to be serial".
    """

    def __init__(self, worktree: str, *, top_k: int, excerpt_lines: int,
                 acl: list[str] | None = None):
        self.worktree = os.path.realpath(worktree)
        self.top_k = max(int(top_k), 1)
        self.excerpt_lines = max(int(excerpt_lines), 0)
        # The audience of the page this run is writing. Every tool below answers from the corpus
        # scoped to it, so the model cannot read — and therefore cannot cite or link
        # to — a page its own output would not be readable beside. `None` is an open page, which
        # admits open rows only: the fail-closed direction for a caller that forgets to pass it.
        # `is None`, never truthiness: `[]` is a value meaning nobody, and collapsing it here
        # would make the tools and the seeded gather disagree about the corpus.
        self.acl = None if acl is None else list(acl)
        # ONCE, before the model runs: recomputing per call would let a page the agent just wrote
        # count as "existing", denying it a second write of its own draft.
        self.existing = gitcmd.tracked_paths(self.worktree)
        self._corpus = None
        self._registry = None
        self._lock = threading.Lock()

    # Double-checked because the tools run in threads: a race's loser returns the winner's parse.
    def corpus(self) -> gather.Corpus:
        if self._corpus is None:
            with self._lock:
                if self._corpus is None:
                    self._corpus = gather.load_corpus(self.worktree, acl=self.acl)
        return self._corpus

    def registry(self):
        """The entity registry AT THIS ITEM'S BASE COMMIT — the worktree IS that checkout. Read
        through `config.REGISTRY_RELPATH`, this package's one spelling of where it lives."""
        if self._registry is None:
            with self._lock:
                if self._registry is None:
                    self._registry = registry_module.load_registry(
                        os.path.join(self.worktree, *config.REGISTRY_RELPATH.split("/")))
        return self._registry

    # ── the five bodies ───────────────────────────────────────────────────────────────────────
    # Every UNFENCED scalar re-entering the prompt goes through `gather.prompt_scalar`, the SAME
    # sanitizer as the seed road; page-body-derived text is the CONTENT half and is fenced.
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
        # Identifiers into the scaffold; the EXCERPT fenced, keyed by the same sanitized path.
        matches = [{"path": ps(c["path"]), "title": ps(c["title"]), "type": ps(c["type"]),
                    "links_to": [ps(name) for name in c["links_to"]]} for c in found]
        excerpts = [{"path": ps(c["path"]), "excerpt": c["excerpt"]} for c in found]
        return {"query": text, "matches": matches, "corpus_pages": len(self.corpus().rows),
                _FENCED_KEY: {"excerpts": excerpts}}

    def read_page(self, path: str) -> dict:
        """One page in full — refused unless `gather.confined_page` allows it AND the page is one
        this run's own audience may cite. The first rule admits the content zones AND
        `ops/templates/*.md`, since this run writes a page's own container; everything else stays
        refused, `ops/entity-registry.json` and `ops/identities.json` first.

        The second rule is "a model never reads what it may not cite", and it is asked HERE as
        well as in `corpus()` because this
        tool reaches the filesystem rather than the parsed rows: a path the model names directly —
        guessed, or read off a wikilink in material it was given — must meet the same scope its
        searches do. A template is not a corpus page and carries no audience, so it is admitted by
        the first rule alone.

        Both refusals return the SAME sentence. "You may not read that" and "there is no such
        page" must not be distinguishable, or this tool becomes an existence oracle for a model
        that will happily report what it found."""
        resolved_rel = gather.confined_page(self.worktree, path or "")
        if not resolved_rel:
            return {"refused": REFUSED_READ}
        if not gather.may_read(self.worktree, self.corpus(), resolved_rel):
            return {"refused": REFUSED_READ}
        # The CANONICAL relpath the rule judged, never the asked string: no symlink re-follow,
        # no NFD spelling that names another page.
        full = os.path.join(self.worktree, *resolved_rel.split("/"))
        try:
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as ex:
            # The class name only: an OS error's message carries a filesystem path.
            return {"refused": f"that page could not be read ({ex.__class__.__name__})"}
        # The path is a sanitized scaffold scalar; the BODY is the content half, fenced.
        return {"path": gather.prompt_scalar(resolved_rel),
                _FENCED_KEY: {"content": _readable(text)}}

    def list_page_names(self) -> dict:
        """The wikilink vocabulary, derived from the SAME scoped corpus every other tool answers
        from — so a page this run may not cite is not offered as something to link
        to, which is how the upward link stops being writable rather than being caught afterwards.

        Still a subset of what `edits.validate` resolves, so the old promise holds: a name offered
        here cannot be refused later as a dead link."""
        ps = gather.prompt_scalar
        names = self.corpus().link_names
        return {"names": [ps(name) for name in names[:gather.MAX_LINK_NAMES]], "total": len(names)}

    def resolve_entities(self, names) -> dict:
        """The registry's own answer for each name. `resolved: false` is a REAL answer the brief's
        third anchoring outcome depends on — a name the registry does not know is a PROPOSAL,
        never an invention — so an unresolved name is returned as itself rather than dropped.

        An unresolved name carries `near`: the registered entities that name partly spells, through
        `gather.match_registry` — the SAME rule that built the seeded block, so asking again cannot
        get a different set. They are candidates to JUDGE. Resolving one is still declaring its id
        and still meeting `gate_anchoring`.
        """
        ps = gather.prompt_scalar
        registry = self.registry()
        asked = [str(n).strip() for n in (names or []) if str(n).strip()][:MAX_TOOL_NAMES]
        rows = self.corpus().rows
        out = []
        for name in asked:
            cid = registry.canonical_id(name)
            if not cid:
                near, _total = gather.match_registry(registry, name)
                out.append({"asked": ps(name), "resolved": False,
                            "near": [{"id": ps(entity_id), "name": ps(entity_name),
                                      "aliases": [ps(a) for a in entity_aliases],
                                      "match": ps(kind)}
                                     for entity_id, entity_name, entity_aliases, kind in near]})
                continue
            entity = registry.entities.get(cid) or {}
            out.append({
                "asked": ps(name),
                "resolved": True,
                "id": ps(cid),
                "name": ps(str(entity.get("name") or "")),
                "aliases": [ps(str(a)) for a in (entity.get("aliases") or [])],
                "proposed": bool(entity.get("proposed")),
                "page": ps(gather.entity_page(rows, cid, registry.canonical_id)) or None,
            })
        return {"entities": out}

    def write_page(self, path: str, content: str) -> dict:
        """The ONE write, gated by `agent.confined_write_target` — the same allow-list the offline
        double writes through.

        Never a bare `open`: `confined_write` allow-lists paths that do NOT exist yet, and the
        hardened opener (`O_EXCL` + `O_NOFOLLOW`) makes that hold at the moment of writing, where
        `open(p, "w")` truncates through a symlink and past any race. `full` is built from the
        RESOLVED relpath, so `wiki/notes/sub/../x.md` writes `wiki/notes/x.md`.
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
            # Same posture as the read: the class name, never the path.
            return {"refused": f"that page could not be written ({ex.__class__.__name__})"}
        log.info("the filing agent wrote %s (%d bytes)", rel, size)
        return {"written": rel, "bytes": size}


class PydanticFilingAgent:
    """The pydantic-ai backend for BOTH flows, conforming to `filing_port.FilingAgent`
    structurally and never by inheritance. `model_factory` is the ONLY offline seam here, so the
    whole path is exercisable keylessly; the price is always looked up by the CONFIGURED model id,
    so an injected double can never make a run look free."""

    # Declared, not inferred: `processing` reads THIS and never `isinstance`, so a test double
    # standing in for a backend takes a branch by declaring it.
    structured_ordinary = False

    # NOT the inverse of the first: the gather is this run's SEED, which the tools go further
    # than rather than replace.
    wants_gathered = True

    def __init__(self, settings, *, model_factory=None):
        self.settings = settings
        self.model_factory = model_factory
        # A BACKSTOP at the one point that cannot be reached around: constructing the thing that
        # will spend the money.
        pricing.require_priced(settings.model)

    def run(self, *, worktree: str, material: str, hints: dict, submitted_by: str,
            corrective: str = "", flow_note: str = "", gathered: str = "",
            acl: list[str] | None = None) -> AgentRun:
        """The ordinary flow: file ONE capture, ITERATING over the checkout. Deliberately NOT
        parallel to `run_meeting` — a meeting is handed everything it could need, while an
        ordinary capture is one paragraph about a brain of unknown shape whose pages need not use
        the material's words. `gathered` is the SEED, not the boundary."""
        import asyncio
        return asyncio.run(self._run(
            worktree=worktree, material=material, hints=hints, submitted_by=submitted_by,
            corrective=corrective, flow_note=flow_note, gathered=gathered, acl=acl))

    def _register_tools(self, filer, toolbox: "FilingToolbox") -> None:
        """Register the five tools on one `Agent`, binding each to `toolbox`'s own body.

        THE DOCSTRINGS BELOW ARE PROMPT TEXT: pydantic-ai sends a tool's docstring and signature
        to the model as its schema, so they are the model's usage guide, never developer notes.
        The engineering rationale lives on `FilingToolbox`'s methods.
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

            Use it before declaring an anchor. A name it does not resolve VERBATIM comes back with
            `near`: the registered entities that name partly spells. Those are candidates for your
            judgment, not answers — read the corpus and decide whether the material really is about
            one of them, and anchor by declaring THAT entity's id.

            If none of them is it, the thing is NEW: declare it in your account's `new_entities`
            (every field filled) and anchor to its name — the worker writes the entity page beside
            yours, confirmed by whoever captured. Never invent an entity id, and never fall back to
            company-wide scope to get something filed: a wrong anchor corrupts a timeline silently,
            a declared identity costs nobody anything.
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

    async def _run(self, *, worktree, material, hints, submitted_by, corrective,
                   flow_note="", gathered="", acl=None) -> AgentRun:
        import asyncio

        # Imported HERE, never at module scope — see the module docstring.
        from pydantic_ai import Agent
        from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
        from pydantic_ai.usage import RunUsage, UsageLimits

        from stigmergy.kernel.usage_repair import ensure_usage_extraction_repaired

        # The framework's usage extraction reports ZERO tokens for any OpenAI model carrying
        # reasoning details, which on an iterating run under-prices every request. Idempotent.
        ensure_usage_extraction_repaired()

        run = AgentRun()
        worktree_root = os.path.realpath(worktree)

        # Out of the WORKTREE, this item's base commit; a missing skill raises before any spend.
        instructions = agent_module.build_system_prompt(
            agent_module.read_skill(worktree_root),
            header=ORDINARY_AGENTIC_SYSTEM_PROMPT_HEADER)
        prompt = agent_module.build_prompt(
            material=material, hints=hints, submitted_by=submitted_by, gathered_block=gathered,
            outcome_channel=ORDINARY_AGENTIC_OUTCOME_CHANNEL, corrective=corrective,
            flow_note=flow_note)

        # Its OWN narrow try: the blanket handler below would report a configuration fault as
        # "the run failed". No `output_type` — the account comes home as a file, and a structured
        # one would ask for it TWICE, in two shapes.
        try:
            model = self.model_factory() if self.model_factory else self.settings.model
            # Keyed by the CONFIGURED model id, the same rule `_cost` follows.
            filer = Agent(model, instructions=instructions, retries=OUTPUT_RETRIES,
                          model_settings=prompt_cache_settings(self.settings.model,
                                                               self.settings.prompt_cache))
        except Exception as ex:  # noqa: BLE001 — class name only, like every other wrap here
            raise priced(run, AgentError(
                f"could not resolve the configured model ({ex.__class__.__name__}); "
                f"$STIGMERGY_LIBRARIAN_MODEL is {self.settings.model!r}")) from ex

        # AFTER that try, so model resolution stays the first thing a misconfigured worker meets;
        # OUTSIDE it, because a tool signature the framework cannot schema is OUR defect.
        toolbox = FilingToolbox(worktree_root, top_k=self.settings.gather_top_k,
                                excerpt_lines=self.settings.gather_excerpt_lines, acl=acl)
        self._register_tools(filer, toolbox)
        # OURS, handed in rather than read off the result: a run that dies mid-flight still
        # leaves its real counts here.
        usage = RunUsage()
        # Passed straight through, NOT `max(..., 1)`: silently clamping would rewrite an
        # operator's number. `worker.startup_checks` refuses `< 2` BY NAME before the first claim.
        limits = UsageLimits(request_limit=int(self.settings.max_turns))
        try:
            # A bound WE own — pydantic-ai has none — and the lease derives from it. It bounds
            # ONE pass, not redelivery: a sync tool hangs until the next await.
            async with asyncio.timeout(self.settings.timeout_s):
                result = await filer.run(prompt, usage=usage, usage_limits=limits)
        # The fault arms record no `turns`/`tool_calls`: they raise, the envelope is discarded,
        # and the spend travels on the exception instead.
        except TimeoutError as ex:
            run.cost_usd = self._fault_cost(usage, flow="filing")
            raise priced(run, AgentError(
                f"the filing agent exceeded its {self.settings.timeout_s}s budget")) from ex
        except UsageLimitExceeded as ex:
            # BY NAME above the blanket arm: this fault has an operator's answer in one variable.
            run.cost_usd = self._fault_cost(usage, flow="filing")
            raise priced(run, AgentError(
                f"the filing agent used all "
                f"{self.settings.max_turns} of its model requests for one capture without "
                f"finishing (the iteration budget, $STIGMERGY_LIBRARIAN_MAX_TURNS)")) from ex
        except UnexpectedModelBehavior as ex:
            # A SHAPE problem, so it travels as an `OutcomeShapeError` carrying a finding, like a
            # refused account from the file channel. Named in the Finding too, since a bare class
            # name is indistinguishable from every other UMB fault.
            _log_fault("filing", ex)
            run.cost_usd = self._fault_cost(usage, flow="filing")
            fault = _fault_line(ex)
            raise priced(run, OutcomeShapeError([gates.Finding(
                agent_module._OUTCOME_GATE, "framework-rejected",
                f"the filing run ended badly ({ex.__class__.__name__}: {fault}): call the tools "
                f"this run declares, with the arguments they declare, and write your account to "
                f"{agent_module.OUTCOME_FILENAME} with `write_page`")])) from ex
        except Exception as ex:  # noqa: BLE001 — class name only: provider errors carry prompt text
            _log_fault("filing", ex)
            run.cost_usd = self._fault_cost(usage, flow="filing")
            raise priced(run, AgentError(
                f"the filing agent run failed ({ex.__class__.__name__})")) from ex

        run.cost_usd = self._cost(usage, flow="filing")
        self._counted(run, usage)
        run.stop_reason = str(getattr(result.response, "finish_reason", "") or "")
        # The account is the FILE, not the final message: a model that says "I filed it" in prose
        # and wrote no outcome file has filed nothing. Through the SAME bounds the double's
        # account goes through — a model that just read untrusted material is not a trusted writer.
        try:
            run.outcome = agent_module.read_outcome(worktree_root)
        except AgentError as ex:
            priced(run, ex)
            raise
        return run

    @staticmethod
    def _counted(run: AgentRun, usage) -> None:
        """Put the framework's own loop counters on the envelope, on the RETURNING road only:
        `RunUsage` is mutated in place, so counting again in the wrappers would be a second answer
        to one question. Read defensively; an injected offline model may hand back less."""
        run.turns = int(getattr(usage, "requests", 0) or 0)
        run.tool_calls = int(getattr(usage, "tool_calls", 0) or 0)

    def run_meeting(self, *, worktree: str, material: str, meeting_meta: dict, registry,
                    source_page_path: str, corrective: str = "", gathered: str = "") -> AgentRun:
        """One structured call: the brief as instructions, the item as the prompt, a typed account
        back. Deliberately tool-less — everything it could fetch is already in the prompt, `gathered`
        included, and code writes every page in the set."""
        import asyncio
        return asyncio.run(self._run_meeting(
            worktree=worktree, material=material, meeting_meta=meeting_meta, registry=registry,
            source_page_path=source_page_path, corrective=corrective,
            gathered=gathered))

    async def _run_meeting(self, *, worktree, material, meeting_meta, registry, source_page_path,
                           corrective, gathered="") -> AgentRun:
        import asyncio

        # Imported HERE, never at module scope — see the module docstring.
        from pydantic_ai import Agent
        from pydantic_ai.exceptions import UnexpectedModelBehavior
        from pydantic_ai.usage import RunUsage, UsageLimits

        from stigmergy.kernel.usage_repair import ensure_usage_extraction_repaired

        # The framework reports ZERO tokens for any OpenAI model carrying reasoning details.
        # Load-bearing, not defensive; idempotent.
        ensure_usage_extraction_repaired()

        # `turns`/`tool_calls` stay at the envelope's own zero: no loop and no tool here, so a `1`
        # would be invented. The port documents zero as a legitimate answer.
        run = AgentRun()
        worktree_root = os.path.realpath(worktree)

        # Out of the WORKTREE, this item's base commit; a missing brief raises before any spend.
        instructions = agent_module.build_meeting_system_prompt(
            agent_module.read_meeting_brief(worktree_root),
            header=MEETING_SYSTEM_PROMPT_HEADER)
        prompt = agent_module.build_meeting_prompt(
            material=material, meeting_meta=meeting_meta, registry=registry,
            source_page_path=source_page_path, corrective=corrective,
            gathered_block=gathered, outcome_channel=OUTCOME_CHANNEL)

        # Their OWN narrow try: the blanket handler below would report a configuration fault as
        # "the meeting agent run failed". `read_meeting_brief`'s error stays outside both.
        try:
            model = self.model_factory() if self.model_factory else self.settings.model
            distiller = Agent(model, output_type=MeetingAccount, instructions=instructions,
                              retries=OUTPUT_RETRIES)
        except Exception as ex:  # noqa: BLE001 — class name only, like every other wrap here
            raise priced(run, AgentError(
                f"could not resolve the configured model ({ex.__class__.__name__}); "
                f"$STIGMERGY_LIBRARIAN_MODEL is {self.settings.model!r}")) from ex
        # OURS, handed in rather than read off the result, so a fault carries a real
        # `run_cost_usd` instead of a forced 0.0.
        usage = RunUsage()
        # From the SAME constant as the retry budget: one call plus re-validation is all this flow
        # may spend, where `settings.max_turns` would license thirty.
        limits = UsageLimits(request_limit=1 + OUTPUT_RETRIES)
        try:
            # A bound WE own; the lease derives from it, and a pass outliving the lease is a
            # capture two workers file.
            async with asyncio.timeout(self.settings.timeout_s):
                result = await distiller.run(prompt, usage=usage, usage_limits=limits)
        except TimeoutError as ex:
            run.cost_usd = self._fault_cost(usage)
            raise priced(run, AgentError(
                f"the meeting agent exceeded its {self.settings.timeout_s}s budget")) from ex
        except UnexpectedModelBehavior as ex:
            # The framework exhausted its re-validations — a SHAPE problem, so it travels as an
            # `OutcomeShapeError`; a bare `AgentError` would finish the item with no brief.
            _log_fault("meeting", ex)
            run.cost_usd = self._fault_cost(usage)
            fault = _fault_line(ex)
            raise priced(run, OutcomeShapeError([gates.Finding(
                # The file channel's gate name: one vocabulary for one class of problem.
                agent_module._OUTCOME_GATE, "framework-rejected",
                f"the account did not satisfy this run's output schema after "
                f"{OUTPUT_RETRIES} re-validation attempt(s) ({ex.__class__.__name__}: {fault}); "
                f"return every field the schema declares, in the shape the skill documents")])) from ex
        except Exception as ex:  # noqa: BLE001 — class name only: provider errors carry prompt text
            _log_fault("meeting", ex)
            run.cost_usd = self._fault_cost(usage)
            raise priced(run, AgentError(
                f"the meeting agent run failed ({ex.__class__.__name__})")) from ex

        run.cost_usd = self._cost(usage)
        run.stop_reason = str(getattr(result.response, "finish_reason", "") or "")
        # The SAME boundary the file channel goes through: a typed provider response is not a
        # trusted one. OUTSIDE the try above, so `OutcomeShapeError` keeps its findings.
        raw = result.output.model_dump()
        # The SAME ceiling on a channel with no file to stat: a structured output is bounded by
        # the schema's SHAPE alone, so every string field is unbounded.
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
        """`_cost` on a road where it must never raise: a `LibrarianConfigError` escaping an
        `except` block would replace the fault being reported."""
        try:
            return self._cost(usage, flow=flow)
        except LibrarianConfigError:
            log.warning("could not price the failed pass: no price is configured for %r — the "
                        "fault below is reported with a spend of $0.00", self.settings.model)
            return 0.0

    def _cost(self, usage, *, flow: str = "meeting") -> float:
        """This attempt's dollars, computed from tokens because no provider here prices itself.
        ONE arithmetic for both flows; `flow` names the pass in the log line and nothing else."""
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
