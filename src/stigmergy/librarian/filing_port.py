"""The filing agent PORT: what `processing.py` may assume of a backend, written down once.

`build_agent` returns it and a keyless conformance test asserts every backend satisfies it. This
is the bottom of the package's graph — it imports `errors` and nothing else — and `AgentRun` lives
here because the envelope belongs to the contract (`agent.AgentRun` is a re-export).

## The envelope

`AgentRun` is what ONE attempt hands back. `outcome` is already parsed and bounded at the trust
boundary, never a raw `dict`. `turns`/`tool_calls`/`cost_usd` are telemetry nothing branches on,
and **zero is a legitimate answer** in all three: a one-call run has no loop, and an offline
double or a park re-file spends nothing. `stop_reason` is the provider's free-form word for why
the run ended; no code may branch on its spelling.

## The fault contract

A backend that cannot produce a usable outcome raises `AgentError` (or `OutcomeShapeError`, which
carries `gates.Finding`s into the one corrective retry) — never a provider exception and never
with a provider message spliced in, because a framework error can carry prompt text, which is the
captured material. A fault must still carry `run_cost_usd` (`priced()`), or a `failed` item
reports `0.0` after paying for a full run.

## The side-effect rules, which differ per flow

- **`run`, `structured_ordinary = False`** — the agent may write inside the worktree, bounded by
  `agent.confined_write`: ONE new `.md` page plus its own outcome file, never an existing page.
  An edit to one is DECLARED in the outcome and PERFORMED by `edits.py`.
- **`run`, `structured_ordinary = True`** — the agent writes NO page; code is the sole author and
  the account carries the page's text in `Outcome.page`. Its only legal write is its outcome file.
- **`run_meeting`** — the agent writes NO page at all; code authors every page in the set. An edit
  to a page that already exists is DECLARED in the outcome and PERFORMED by `edits.py`, exactly as
  on the first `run` shape above.

`isinstance(x, FilingAgent)` checks only that the two methods are PRESENT; the signatures below
are the contract, and the conformance test pins them.
"""
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from stigmergy.librarian.errors import AgentError


@dataclass
class AgentRun:
    """One agent attempt's result — the envelope every backend returns; see the module docstring.

    `outcome` is typed `Any` on purpose: the ordinary flow puts an `agent.Outcome` here and the
    meeting flow an `agent.MeetingOutcome`, and naming either would import an implementation into
    its own contract.
    """
    outcome: Any = None
    turns: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    stop_reason: str = ""


def priced(run: AgentRun, ex: AgentError) -> AgentError:
    """Attach the attempt's known spend to a fault raised mid-run and return the exception, so a
    raise site reads `raise priced(run, AgentError(...))` in one expression. The caller reads
    `run_cost_usd` off the exception because no `AgentRun` ever returned."""
    ex.run_cost_usd = run.cost_usd
    return ex


@runtime_checkable
class FilingAgent(Protocol):
    """The two calls `processing.py` makes, and the only two it may make, plus the two
    capabilities it may ASK a backend about first.

    Keyword-only throughout: the argument lists are long and half of them are strings, so a
    positional call site is a defect waiting for somebody to swap two of them.
    """

    # ── the capabilities a backend DECLARES rather than ones the worker sniffs ─────────────────
    # Which shape of the ordinary flow this backend answers, DECLARED so `processing` never asks
    # `isinstance`. `False` is the EXPLORING shape: the agent writes the page under
    # `agent.confined_write` and declares it in `Outcome.page_path`. `True` is the STRUCTURED
    # shape: no tools, no writes, the page's text carried in `Outcome.page` for code to write.
    structured_ordinary: bool

    # Whether the deterministic gatherer runs before an ORDINARY call and arrives in `gathered`.
    # Not the inverse of the first: a backend can write its own page AND want that context as the
    # SEED its tools go further from. A plain attribute, so a wrapper that swallowed it fails
    # loudly rather than starving a run of its context.
    wants_gathered: bool

    def run(self, *, worktree: str, material: str, hints: dict, submitted_by: str,
            corrective: str = "", flow_note: str = "", gathered: str = "",
            acl: list[str] | None = None) -> AgentRun:
        """The ordinary flow: file ONE capture as one new page.

        `corrective` is the single retry's repair brief, `flow_note` a fact about the flow this
        item rides; both empty on a first, unattached pass. `gathered` is the gatherer's context
        ALREADY RENDERED to prompt text, so backends share one context builder and one fence
        discipline.

        `acl` is the audience this capture is filed at, and a backend that holds READ TOOLS must
        scope them to it: a
        model may not read what the page it is writing could not cite. A tool-less backend has
        nothing to scope — the worker already scoped `gathered` — and ignores it. `None` is an
        open page, and the narrow default: a caller that forgets it starves a run rather than
        widening one.
        """
        ...

    def run_meeting(self, *, worktree: str, material: str, meeting_meta: dict, registry,
                    source_page_path: str, corrective: str = "", gathered: str = "") -> AgentRun:
        """The meeting flow: distil ONE transcript into a page SET's worth of content.

        Everything the agent needs is handed over, so a backend needs no filesystem exploration
        to answer — `gathered` included: it is `run`'s own argument, the gatherer's context ALREADY
        RENDERED to prompt text by the worker, so both flows share one context builder and one
        fence discipline. Unconditional here rather than gated on `wants_gathered`: no backend on
        this flow holds a tool, so there is no second shape for the context to take.
        """
        ...
