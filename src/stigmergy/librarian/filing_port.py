"""The filing agent PORT: what `processing.py` may assume of a backend, written down once.

The seam was a CONVENTION. The first two backends and `processing.py` agreed about two method
signatures, one result envelope and one fault contract, and none of the three stated it anywhere a
THIRD implementation could read — so the only way to learn what a backend owed the worker was to
read the two that existed and hope they agreed. This module states it, `build_agent` returns it,
and a keyless conformance test asserts every backend satisfies it.

**The port has already outlived one of its implementations**, which is the argument for it made in
one sentence: the Claude-Code backend was retired without `processing.py` changing a line, because
what the worker is written against is this file and not that class.

Deliberately the bottom of this package's own graph: it imports `errors` and nothing else, so any
backend module can depend on it without inheriting another one's imports. `AgentRun` lives here
rather than in `agent.py` for the same reason — the envelope belongs to the contract, not to the
first implementation of it — and `agent.AgentRun` stays a re-export so every existing importer is
unaffected.

## The envelope

`AgentRun` is what ONE attempt hands back:

- `outcome` — the agent's account, already parsed and bounded at the trust boundary
  (`agent.parse_outcome` for the ordinary flow, `agent.parse_meeting_outcome` for the meeting
  one). Never a raw `dict`: the worker's own cross-checks and the submitter's report are built
  from it, so the coercion happens once, at the boundary, whatever channel carried it.
- `turns` / `tool_calls` — telemetry, and **zero is a legitimate answer while a real number is
  the other one**. They count a conversational, tool-using loop: the ordinary pydantic-ai run has
  one again (ADR 034) and reports the framework's own accumulated counts, while a run that makes
  ONE structured call and reads its typed output — every `run_meeting`, and any backend declaring
  `structured_ordinary = True` — has neither and reports `0` rather than inventing a `1`. So a
  zero here means "this shape has no loop", never "nobody counted". Nothing downstream branches on
  either — `report.filed` carries no turn counter at all, and the eval runner counts passes at its
  own seam (`CountingAgent`) precisely because no report does.
- `cost_usd` — what THIS attempt cost, in dollars. A backend that is priced by its own provider
  (a harness reporting `total_cost_usd` per run) passes that figure through; one that
  reports only TOKENS computes it through `pricing.compute_cost_usd`. `processing.AgentPasses`
  sums the passes of one item and `_stamp_cost` puts the sum on the report. `0.0` is a real
  answer: an offline double spends nothing, and so does a park re-file.
- `stop_reason` — the provider's own word for why the run ended, for a log and a fault message.
  Free-form, and no code may branch on its spelling.

## The fault contract

A backend that cannot produce a usable outcome raises `AgentError` (or its `OutcomeShapeError`
subclass, which carries `gates.Finding`s into the one corrective retry) — never a provider
exception, and never with a provider message spliced into it: a framework error can carry prompt
text, which is to say the captured material, and this text reaches an operator's log.

**A fault must still say what the attempt cost.** Most agent faults fire AFTER the run was priced,
so `priced()` attaches the figure as `run_cost_usd` on the exception and `processing` banks it off
there (`_one_pass` / `_one_meeting_pass`). `0.0` is the honest figure when nothing priced the run —
a timeout, a provider that raised before answering — and the field must be present either way, or a
`failed` item reports `cost_usd: 0.0` after paying for a full run.

## The side-effect rules, which differ per flow

They are NOT the same rule, and a backend must not average them:

- **`run` (the ordinary flow), `structured_ordinary = False`** — the agent may write inside the
  worktree, bounded by `agent.confined_write`: ONE new `.md` page in one of the creatable fast-lane
  folders, plus its own outcome file. It may never touch a page that already exists; an edit to one
  is DECLARED in the outcome and PERFORMED by `edits.py`. Whether that write arrives from a model
  holding a confined `write_page` tool (the pydantic-ai backend) or from code with no model behind
  it at all (the double) is the BACKEND's business; the allow-list is the same one either way.
- **`run` (the ordinary flow), `structured_ordinary = True`** — the agent writes NO page, exactly
  like `run_meeting` below: code is the sole author (`processing._write_ordinary_page`) and the
  account carries the page's own text in `Outcome.page`. Its only legal write is its own outcome
  file, and a backend that carries the outcome home in the envelope writes nothing whatsoever.
- **`run_meeting`** — the agent writes NO page at all. Code is the sole author of every page in the
  set (`processing._write_meeting_pages`), so the agent's only legal write is its own outcome file,
  and a backend that carries the outcome home in the envelope instead writes nothing whatsoever.
  Both are conforming: `processing` calls `agent.discard_outcome_file` before it takes the diff
  either way, which is harmless when there is no file.

`isinstance(x, FilingAgent)` checks that the two methods are PRESENT — that is all a
`runtime_checkable` Protocol can check. The signatures below are the contract; the conformance test
is what pins them.
"""
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from stigmergy.librarian.errors import AgentError


@dataclass
class AgentRun:
    """One agent attempt's result — the envelope every backend returns. See the module docstring
    for what each field means and which of them may legitimately be zero.

    `outcome` is typed `Any` on purpose and it is not laziness: the ordinary flow puts an
    `agent.Outcome` here and the meeting flow an `agent.MeetingOutcome`, two deliberately separate
    schemas (a page versus a page SET), and naming either one here would import an implementation
    into its own contract.
    """
    outcome: Any = None
    turns: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    stop_reason: str = ""


def priced(run: AgentRun, ex: AgentError) -> AgentError:
    """Attach the attempt's known spend to a fault raised mid-run, and return the exception so a
    raise site reads `raise priced(run, AgentError(...))` in one expression.

    The caller reads `run_cost_usd` off the exception because no `AgentRun` ever returned — and
    most agent faults fire AFTER the run was priced (a non-`success` stop, a blown tool-call
    budget, an unreadable outcome file). A timeout prices at `0.0` honestly: nothing ever arrived
    to price it.
    """
    ex.run_cost_usd = run.cost_usd
    return ex


@runtime_checkable
class FilingAgent(Protocol):
    """The two calls `processing.py` makes, and the only two it may make — plus the two things it
    must be able to ASK a backend before making them.

    Keyword-only throughout, matching what the flows already call with: the argument lists are long
    and half of them are strings, so a positional call site is a defect waiting for somebody to
    swap two of them.
    """

    # ── the capabilities a backend DECLARES rather than ones the worker sniffs ─────────────────
    # Which shape of the ordinary flow this backend answers, and it is a declaration precisely so
    # `processing` never has to ask `isinstance(agent, PydanticFilingAgent)`. A type test would put
    # the flow's own branch inside the worker's knowledge of which classes exist — so a fourth
    # backend, or a test double standing in for one, would take the wrong branch by being the
    # wrong class rather than by declaring the wrong thing.
    #
    # `False` — both shipped backends — means the EXPLORING shape: the agent writes the page inside
    # `agent.confined_write`'s allow-list and declares the path it wrote in `Outcome.page_path`,
    # with its account arriving through the outcome FILE. It is the shape the retired Claude-Code
    # backend answered and the shape the pydantic-ai backend answers again (ADR 034), and the
    # double keeps it exercised offline on every `make test`.
    #
    # `True` — no shipped backend today, and the shape `processing` still implements (ADR 033) —
    # means the STRUCTURED shape: the agent holds no tool and writes nothing at all, and its
    # account CARRIES the page's own text in `Outcome.page` for code to write. Both halves of the
    # outcome envelope are valid; this is what says which one is required of this backend.
    structured_ordinary: bool

    # Whether the deterministic gatherer (`gather.gather`, rendered by `agent.render_gathered`)
    # runs before an ORDINARY call and arrives in `gathered`. A second declaration rather than a
    # consequence of the first, because after ADR 034 the two questions genuinely came apart: the
    # pydantic-ai backend writes its own page (`structured_ordinary = False`) AND wants the
    # gathered context, because that context is the SEED its tools then go further from. Inferring
    # one from the other would have made "it explores" mean "it starts from nothing", which is the
    # design M2 shipped and M4 corrected.
    #
    # `False` — the offline double — means no gather is built at all: it is directive-driven, so
    # paying for a corpus walk per pass would buy a string nothing reads.
    #
    # Read on the branch that USES it (`processing._one_pass`'s legacy branch), and read as a plain
    # attribute so a wrapper that swallowed it fails loudly rather than silently starving a run of
    # its context — see `structured_ordinary` above and the wrapper tests in
    # `tests/librarian/test_filing_port_conformance.py`.
    wants_gathered: bool

    def run(self, *, worktree: str, material: str, hints: dict, submitted_by: str,
            corrective: str = "", reply: str = "", flow_note: str = "",
            gathered: str = "") -> AgentRun:
        """The ordinary flow: file ONE capture as one new page.

        `corrective` is the repair brief of the single retry (`gates.corrective_brief`), `reply` the
        submitter's answer to the one ask-back question, and `flow_note` a server-composed fact
        about the flow this item rides (today: the source attachment's half of the work). All four
        are empty on a first, unattached pass.

        `gathered` is the deterministic gatherer's context, ALREADY RENDERED to prompt text
        (`agent.render_gathered` over `gather.gather`) — a string, not the dataclass, and that is
        the seam rather than a convenience. The gatherer belongs to the FLOW, not to a backend: two
        backends must share one context builder and one fence discipline, and handing a backend the
        object instead would invite each one to render it its own way. Empty for a backend that
        declares `wants_gathered = False`, which is handed nothing.
        """
        ...

    def run_meeting(self, *, worktree: str, material: str, meeting_meta: dict, registry,
                    source_page_path: str, corrective: str = "", reply: str = "") -> AgentRun:
        """The meeting flow: distil ONE transcript into a page SET's worth of content.

        Everything the agent needs is handed over — the transcript, the drop's metadata, the
        resolved entity registry and the source page's path, which code has already decided — so a
        backend needs no filesystem exploration to answer.
        """
        ...
