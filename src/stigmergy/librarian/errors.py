"""Domain errors of the librarian subsystem.

Same posture as `stigmergy.capture.errors`: library code raises these, the console entry point
(`librarian.cli`) maps them to a stderr line plus an exit code, and nothing here is ever put on
the wire — the librarian has no HTTP surface at all. What DOES cross to a human is the
submission report, and that is built in `report.py` from a vetted fact set, never from an
exception message.

**The split that matters** is `LibrarianConfigError` vs everything else. A config error means
the WORKER cannot run — a malformed `ops/acl.json`, a missing gitleaks binary, a repo that is
not a git checkout. It is raised at startup, once, before any item is claimed, precisely so it
cannot become N identical `failed` rows with the real cause buried under attempts-exhausted
noise. Every other error here is per-item and finishes that item, not the worker.

`worker.process_next` softens that for a config fault that surfaces MID-run (the inputs can now
change between two polls — `base_inputs` reads all three at each item's own base): it becomes a
`failed` row with a `config` stage rather than killing the loop. `StaleBaseError` is the one
subclass that opts back out of the softening, and its docstring says why.
"""


class LibrarianError(RuntimeError):
    """Base class for librarian errors.

    Carries `agent_attempts`: how many agent passes this item had spent when the error was raised.
    Zero means the fault happened before the agent ran at all (dedup, the evidence read, resolving
    the base ref, creating the worktree), which is the honest answer for those paths and the reason
    the default is not 1.

    It lives on the exception because the count is known only inside `processing._run_in_worktree`
    while the failure REPORT is composed one layer up, in `worker.process_next`. Without it the
    report named only the queue delivery — "queue delivery 1" while the agent had had two tries —
    so nobody reading a `failed` row could tell whether the corrective retry had run, which is the
    first thing you want to know about a librarian that gave up. `report.failed_system` already
    accepted the number and had no way to be told it.

    `agent_cost_usd` rides along for the same reason: the SDK reports what each pass actually
    cost, the number is known only inside the loop, and a failed item is still a paid item —
    a failure report that omits the spend hides exactly the runs an operator most wants priced.
    """

    agent_attempts: int = 0
    agent_cost_usd: float = 0.0

    def at_agent_attempt(self, attempt: int, cost_usd: float = 0.0) -> "LibrarianError":
        """Record which agent pass this fault happened on (and what the passes had cost) and
        return self, so a raise site reads `raise AgentError(...).at_agent_attempt(n)` in one
        expression."""
        self.agent_attempts = int(attempt)
        self.agent_cost_usd = float(cost_usd)
        return self


class LibrarianConfigError(LibrarianError):
    """The worker cannot run at all: fail closed, loudly, at startup.

    Raised for a malformed or unreadable `ops/acl.json`, an absent secret scanner, a repo path
    that is not a git worktree, or a GitHub App configuration that is half-present. Never raised
    per item — an operator staring at one loud line at startup is the whole point.
    """


class StaleBaseError(LibrarianConfigError):
    """The DEPLOYED worker resolved a base that did not come from the remote.

    `bootstrap.verify_checkout_at_base` refuses exactly this state before the first claim, because a
    container whose credential has been revoked would otherwise file against its own stale clone.
    But `gitcmd.base_ref` runs again per item and answers a failed fetch with a warning and the
    local branch, so a token that expires AFTER boot walks the worker back into the state the
    startup check exists to refuse. `processing.process_item` asks the same question per item; this
    is the answer when it is false.

    **A subclass, so it keeps `LibrarianConfigError`'s original consequence and not the mid-run
    one.** `worker.process_next` turns an ordinary mid-run config fault into a `failed` row — right
    for "this operator's `ops/acl.json` is malformed", wrong here: nothing about the capture caused
    it, the fault applies identically to every row behind it, and finishing them one at a time would
    drain the queue into `failed` for the duration of a broken credential — the N-identical-failed-
    rows outcome this class exists to prevent. So it propagates: the loop stops, the CLI exits 2,
    the item's lease expires and `release_expired` returns it to `queued` untouched, and on a
    platform that restarts the process `stigmergy-librarian-boot` refuses at startup with the same
    reason. One loud failure, no destroyed captures.
    """


class WorktreeError(LibrarianError):
    """An ephemeral worktree could not be created, diffed or removed."""


class GitError(LibrarianError):
    """A git operation failed. Carries the command's own stderr, TRUNCATED and never the token:
    the push URL is built with a minted installation token and must not appear in any message."""


class LeaseLostError(LibrarianError):
    """The queue row this worker was holding was redelivered while the item was being processed.

    Raised immediately BEFORE the push, which is the only irreversible step: another worker owns
    the capture now, and pushing anyway is how one capture is filed twice with the second page
    referenced by no queue row. `queue.finish`'s `attempts` fence catches the same condition
    afterwards, which is the right guarantee for the row and no guarantee at all for a commit
    that has already reached `main`.
    """


class AgentError(LibrarianError):
    """The librarian agent could not produce a usable outcome — it errored, timed out, blew its
    turn or tool-call bound, or wrote an outcome file that does not parse. Always a system fault
    (`failed`), never a judgment about the submitter's material."""


class OutcomeShapeError(AgentError):
    """The outcome file parsed as JSON and does not describe something the worker can act on.

    Split out of `AgentError` for what the split BUYS, not for taxonomy. `AgentError` is an
    exception and not a `Finding`, so every shape problem finished the item without the agent ever
    being told what was wrong — and on the librarian's first real walk that spent BOTH agent
    attempts on one over-long `summary`, doing the same thing twice. The single most correctable
    class of problem there is was the one class the corrective retry could not see.

    So `findings` travels with the exception, and `processing._run_in_worktree` hands it to the one
    corrective pass exactly as it hands back a gate veto. An unknown `decision`, an unknown edit
    kind, a field of the wrong type, a filing with no title: all of them are one sentence away from
    being fixed.

    Still an `AgentError`, deliberately: if the corrective pass does not fix it, every existing
    handler (`processing.PROCESSING_ERRORS`, the worker's `failed` branch) treats it exactly as
    before — a system fault, never a judgment about the submitter's material.

    `findings` are `gates.Finding`s, carried duck-typed: this module is the leaf of the package's
    import graph and must stay importable from anywhere in it, so it does not import `gates`. The
    two consumers are the ones every other finding has — `gates.corrective_brief` and
    `processing._refuse`.
    """

    def __init__(self, findings=()):
        self.findings = tuple(findings)
        super().__init__("; ".join(f.message for f in self.findings)
                         or "the agent's account of what it did is not a shape the worker can "
                            "act on")
