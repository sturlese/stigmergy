"""Domain errors of the librarian subsystem.

Library code raises these; the console entry point (`librarian.cli`) maps them to a stderr line
plus an exit code; nothing here is ever put on the wire — what crosses to a human is the
submission report, built in `report.py` from a vetted fact set, never from an exception message.

The split that matters is `LibrarianConfigError` vs everything else: a config error means the
WORKER cannot run, raised once at startup precisely so it cannot become N identical `failed`
rows with the real cause buried. Every other error is per-item and finishes that item, not the
worker. `worker.process_next` softens a config fault that surfaces MID-run into a `failed` row
with a `config` stage; `StaleBaseError` is the one subclass that opts back out.
"""


class LibrarianError(RuntimeError):
    """Base class for librarian errors.

    Carries `agent_attempts` — zero means the fault happened before the agent ran at all, the
    honest answer for those paths and the reason the default is not 1 — and `agent_cost_usd`,
    because a failed item is still a paid item. Both live on the exception because the counts
    are known only inside `processing` while the failure REPORT is composed one layer up, in
    `worker.process_next`.
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
    """The worker cannot run at all: fail closed, loudly, at startup. Never raised per item —
    an operator staring at one loud line at startup is the whole point."""


class StaleBaseError(LibrarianConfigError):
    """The DEPLOYED worker resolved a base that did not come from the remote.

    `bootstrap.verify_checkout_at_base` refuses this state at startup, but `gitcmd.base_ref`
    runs again per item and answers a failed fetch with a warning and the local branch — so a
    token that expires AFTER boot walks the worker back into the refused state.
    `processing.process_item` asks the same question per item; this is the answer when false.

    A subclass, so it keeps `LibrarianConfigError`'s original consequence and not the mid-run
    softening: the fault applies identically to every row behind this one, and finishing them
    one at a time would drain the queue into `failed` for the duration of a broken credential.
    So it propagates — the loop stops, the CLI exits 2, the item's lease expires and the row
    returns to `queued` untouched. One loud failure, no destroyed captures.
    """


# ── the three credential states `githubapp.authenticated_clone_url` distinguishes ─────────────
# Siblings, never a chain: a caller re-words all three for its own audience, and a subclass here
# would make one arm shadow another depending on the order they happen to be written in. They are
# separate states because a caller cannot tell "nothing is configured" from "half of it is" from
# "GitHub said no" when every one of them arrives as a bare `LibrarianConfigError` — and the
# server-side write door (`repair.apply`, through `server.review`) publishes the difference to
# whoever asked.
class CloneCredentialUnavailable(LibrarianConfigError):
    """Nothing to authenticate a clone WITH: no repo URL configured, or no App configured at all.
    An absent capability, not a fault — the fix is "configure one"."""


class CloneCredentialHalfSet(LibrarianConfigError):
    """Some of the App's three environment variables are set and not all of them. Somebody meant
    to configure this and got it wrong, which is a different sentence from "there is none"."""


class CloneCredentialRefused(LibrarianConfigError):
    """The App IS configured and GitHub would not issue a token for it — a revoked installation,
    a rotated key. An operational fault: the fix is "check the App", not "configure one"."""


class WorktreeError(LibrarianError):
    """An ephemeral worktree could not be created, diffed or removed."""


class GitError(LibrarianError):
    """A git operation failed. Carries the command's own stderr, TRUNCATED and never the token:
    the push URL is built with a minted installation token and must not appear in any message."""


class LeaseLostError(LibrarianError):
    """The queue row this worker was holding was redelivered while the item was being processed.

    Raised immediately BEFORE the push, which is the only irreversible step: another worker owns
    the capture now, and pushing anyway is how one capture is filed twice. `queue.finish`'s
    `attempts` fence catches the same condition afterwards — the right guarantee for the row and
    no guarantee at all for a commit that has already reached `main`.
    """


class AgentError(LibrarianError):
    """The librarian agent could not produce a usable outcome — it errored, timed out, blew its
    turn or tool-call bound, or wrote an outcome file that does not parse. Always a system fault
    (`failed`), never a judgment about the submitter's material."""


class OutcomeShapeError(AgentError):
    """The outcome file parsed as JSON and does not describe something the worker can act on.

    Split out of `AgentError` for what the split BUYS: an exception is not a `Finding`, and a
    shape problem is the single most correctable class there is — so `findings` travels with
    the exception and `processing._run_in_worktree` hands it to the one corrective pass exactly
    as it hands a gate veto. Still an `AgentError`, deliberately: unfixed, every existing
    handler treats it as a system fault, never a judgment about the material.

    `findings` are `gates.Finding`s, carried duck-typed: this module is the leaf of the
    package's import graph and must stay importable from anywhere in it, so it does not import
    `gates`.
    """

    def __init__(self, findings=()):
        self.findings = tuple(findings)
        super().__init__("; ".join(f.message for f in self.findings)
                         or "the agent's account of what it did is not a shape the worker can "
                            "act on")
