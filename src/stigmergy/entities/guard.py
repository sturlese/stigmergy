"""The two refusals every governed write to the knowledge repo shares — a steward's decision
(`entities.decide`) and, before ADR 042, the hand mint. Kept together so no door can acquire a
different contract: drift between the registry and the entity pages is refused before anything is
written, and the secret scanner runs over exactly the paths the commit will carry.

There is no deterministic mint any more (ADR 042): an entity is born through the librarian, from
material, with its page written — a steward registering one submits what they know as a capture.
"""
import os

from stigmergy.entities import generator
from stigmergy.entities.errors import EntityError
from stigmergy.librarian import config as librarian_config
from stigmergy.librarian import gates

# The same env var `librarian.config.Settings.gitleaks_bin` reads. Spelled twice because
# `stigmergy.entities` must not import a worker's `Settings` to ask one question; the duplication
# is declared here rather than left to be discovered.
GITLEAKS_BIN_ENV = "STIGMERGY_GITLEAKS_BIN"


def refuse_drift(repo: str, *, action: str) -> None:
    """Refuse to write into a clone whose registry and pages already disagree.

    `regenerate` would otherwise silently resolve somebody else's drift and publish the resolution
    inside a commit whose message says it created ONE entity — `ensure_clean`'s argument, applied
    to the derived file. Drift also invalidates the collision gate's premise: the pages and the
    file describing the same registry.
    """
    outcome = generator.check(repo)
    if not outcome.divergences:
        return
    listed = "\n  ".join(d.message.split(" — run ")[0] for d in outcome.divergences)
    raise EntityError(
        f"refusing to {action} — {generator.REGISTRY_RELPATH} and {generator.ENTITIES_RELDIR}/ "
        f"already disagree in this clone, so the collision check would be asked about a registry "
        f"this commit is not going to publish:\n  {listed}\n"
        f"Run `{generator.FIX_COMMAND}`, review what it writes and commit that first (it is a "
        f"change to who resolves to what, which is exactly the decision this tool exists to put in "
        f"front of a human), then re-run this command")


def refuse_secrets(repo: str, relpaths: list[str], *, action: str) -> None:
    """gitleaks over the exact files this commit will carry, before it is made.

    What a decision commits includes free text a steward typed with untrusted material on screen —
    and the commit is `--no-verify`, so this scan is the one that runs: git cannot
    forget, and `main` is the place a secret must never reach. `gates.scan_worktree_files`, never
    a second gitleaks invocation. A missing scanner REFUSES (`ensure_scanner`) rather than skips —
    a secrets gate that silently passes is worse than no gate.
    """
    gitleaks_bin = os.environ.get(GITLEAKS_BIN_ENV) or librarian_config.Settings.gitleaks_bin
    gates.ensure_scanner(gitleaks_bin)
    findings = gates.scan_worktree_files(repo, relpaths, gitleaks_bin=gitleaks_bin)
    if not findings:
        return
    # gitleaks names the scratch copy it actually read — true and useless, gone before anyone
    # reads the message. Rewritten to the repo-relative names the reader knows.
    listed = "\n  ".join(_relocate(f.message, relpaths) for f in findings)
    raise EntityError(
        f"refusing to {action} — the secret scanner matched something in what this commit would "
        f"carry:\n  {listed}\n"
        f"Nothing was committed and the clone is as it was. If this is a real credential, rotate it "
        f"and take it out of what the decision would write; if it is a false positive, the "
        f"rule id above is what to allowlist in the knowledge repo's gitleaks configuration")


def _relocate(message: str, relpaths: list[str]) -> str:
    """Rewrite a scanner's absolute scratch path as the repo-relative file its reader knows."""
    for relpath in relpaths:
        marker = message.find(relpath)
        if marker == -1:
            continue
        start = message.rfind(" ", 0, marker) + 1
        return message[:start] + message[marker:]
    return message
