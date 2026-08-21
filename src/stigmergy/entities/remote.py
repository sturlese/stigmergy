"""The server's door into the knowledge repo: clone it with the librarian App's own credential,
act — carry out a steward's decision on
a proposal through `entities.decide.apply` — push, remove the clone.

A throwaway clone per request, never a standing checkout — a standing one would couple the read
path's startup to GitHub availability for a rare operation, and the `TemporaryDirectory` leaves
nothing behind on success or failure. The credential rides `librarian.githubapp`'s existing
machinery (app JWT -> installation token -> tokenized clone URL); the token lives only in memory
for the one `git clone`, and every error path is scrubbed by `gitcmd._scrub`. An absent
capability REFUSES by naming it (`CapabilityUnavailableError`) rather than degrading. This
module's public seam raises only `entities.errors` types: every `librarian.*` exception is caught
and re-raised, so a caller only ever knows this package's vocabulary.

It is also THE boundary where a refusal changes audience. Everything a caller gets from here is
echoed verbatim to a steward over MCP by `server.review`, and the rest of `stigmergy.entities`
writes for an operator standing in a clone. So the four refusal TYPES whose sentences interpolate
that clone are re-worded here and the door-neutral ones pass through — the constants below carry
the full argument, and ADR 030's "two-door refusal wording" amendment carries the rule.
"""
import logging
import os
import tempfile

from stigmergy.entities import decide, generator
from stigmergy.entities.errors import (
    CapabilityUnavailableError,
    CloneStateError,
    CollisionRaceError,
    EntityError,
    PushRaceError,
    TemplateMissingError,
)
from stigmergy.librarian import gitcmd, githubapp
from stigmergy.librarian.errors import (
    CloneCredentialHalfSet,
    CloneCredentialRefused,
    CloneCredentialUnavailable,
    GitError,
    LibrarianError,
)

log = logging.getLogger(__name__)

# One network leg. Generous enough for a cold clone, short enough that a stalled remote cannot
# pin an HTTP worker indefinitely.
MINT_GIT_TIMEOUT_S = 60

# What a caller is told about a git/config fault, and ALL they are told. `server.review` echoes
# every `EntityError` from here verbatim over MCP, so never splice `str(exception)` in: git names
# the throwaway clone's absolute path and `gates.ensure_scanner` interpolates an operator-supplied
# path. The detail is MOVED, not lost — logged at ERROR with the traceback, where an operator can
# read it and a steward cannot.
MINT_FAULT_MESSAGE = (
    "the mint could not be completed — the server hit a git or configuration fault partway "
    "through, not a problem with the identity you approved. Nothing was pushed. The details are "
    "in the server log; ask whoever runs this deployment to look, then approve again")

# ── the refusals this door RE-WORDS, and the ones it deliberately does not (ADR 030's amendment) ─
# `stigmergy.entities` composes its refusals for an operator standing IN the clone: `clone.py` and
# `guard.py` name that clone's path and hand out `git -C <path> ...` to run in it. Here the clone is
# the `TemporaryDirectory` above, deleted before `server.review` echoes the sentence to a steward
# over MCP — so the path is the SERVER HOST's, the command is unrunnable, and an instruction like
# "commit or stash first" is addressed to nobody. Each arm below logs the library's diagnosis with
# `exc_info=True` and raises one of these instead: MOVED, not lost, the same trade
# `MINT_FAULT_MESSAGE` already makes.
#
# Every one leads with what state was left behind, because that is the fact a steward needs before
# any other, and ends with the one action THEY can take. Four sentences and not one, because
# "approve again", "commit the template first" and "ask whoever runs this deployment" are three
# different instructions.
#
# **What passes through untouched, and why — the pass-through set is the design, not an omission:**
#   · birth-field validation (`birth.prepare`) — the steward's own input is what is wrong and the
#     sentence names the character, the field and the consequence. Nothing about the host.
#   · the collision VERDICT (`birth._refuse_collisions`, a plain `CollisionError`) — it names the
#     registered entry and says to point the capture at it. Only `CollisionRaceError`, the
#     post-rebase re-ask, is mapped; catching its base class here would turn a governance verdict
#     into "something moved, approve again" and send a steward round a loop that cannot succeed.
#   · the secrets refusal (`guard.refuse_secrets`) — `_relocate` has already rewritten gitleaks'
#     scratch path to the repo-relative page, and the rule id is what a steward would allowlist.
#   · the drift refusal (`guard.refuse_drift`) — names `generator.FIX_COMMAND`, which is portable
#     and run against the knowledge repo, not against any clone this process made.
# All four are `EntityError`s, which is why there is no bare `except EntityError` arm below and
# must not become one: it would swallow every refusal a steward can actually act on.
CLONE_STATE_FAULT_MESSAGE = (
    "the server's fresh clone was not in a mintable state — a server-side fault, not a problem "
    "with the identity you approved. Nothing was pushed. The details are in the server log; "
    "approve again once whoever runs this deployment has looked")

# The repo-relative path is the actionable half and is kept; `generator.TEMPLATE_RELPATH` rather
# than a second literal, so the two doors can never name different files.
TEMPLATE_MISSING_MESSAGE = (
    f"the knowledge repo has no {generator.TEMPLATE_RELPATH}, and a new entity page is that "
    f"template with its identity filled in. Nothing was pushed. Commit the template to the "
    f"knowledge repo (any checkout), then approve again — the next attempt clones fresh and will "
    f"see it")

COLLISION_RACE_MESSAGE = (
    "something else changed the registry while this mint was in flight, and the identity being "
    "approved now resolves to an existing entry. Nothing was pushed and nothing was force-pushed. "
    "Approve again: the next attempt re-checks against what actually landed — and if it refuses "
    "again, the identity already exists (list_entities / describe_entity will show it)")

PUSH_RACE_MESSAGE = (
    "the knowledge repo's main kept moving while this mint retried its push. Nothing was pushed "
    "and nothing was force-pushed; no state was left behind on the server. Approve again — a "
    "quieter moment will land it")

# The clone leg's own refusal — not a ladder arm (nothing has been minted yet) but published the
# same way, so it lives with them rather than inline: the sweep over this module's constants is
# what proves no sentence here names a path, and a literal buried in a handler is outside it.
CLONE_FAILED_MESSAGE = (
    "the knowledge repo could not be cloned to mint this entity — the server could not reach it "
    "or is not credentialed for it. Nothing was pushed. The details are in the server log; ask "
    "whoever runs this deployment to look")

# The two CREDENTIAL faults, told the same way and for the same reason. Neither may splice the
# caught exception's text: `githubapp` raises `LibrarianConfigError` naming the private-key FILE
# PATH, which `server.review` would echo to a steward over MCP.
#
# Both are deliberately unactionable BY THE STEWARD, because neither has a steward-side fix: a
# half-set App and a revoked installation are both operator work. What the steward needs is to
# know it is not their approval that was wrong and that nothing was pushed — the same two facts
# `MINT_FAULT_MESSAGE` leads with.
APP_MISCONFIGURED_MESSAGE = (
    "the librarian GitHub App credential on this server is incomplete, so the identity you "
    "approved could not be pushed. Nothing was written. This is a deployment fault, not a problem "
    "with the identity: the details are in the server log — ask whoever runs this deployment to "
    "look, then approve again")

CREDENTIAL_FAULT_MESSAGE = (
    "the librarian GitHub App would not issue a credential for this push, so the identity you "
    "approved could not be minted. Nothing was written. Its installation may have been revoked or "
    "its key rotated — the details are in the server log; ask whoever runs this deployment to "
    "look, then approve again")


def decide_via_clone(repo_url: str, branch: str, credential, *, action, decided_by: str,
                     on_output=None) -> dict:
    """The same throwaway clone for a steward's DECISION on a proposal: `action` is
    `lambda repo: decide.approve_entity(repo, ...)` or any of its siblings, run through
    `decide.apply` — preflight, drift refusal, the decision, the secrets scan, one pushed commit
    carrying a `Decided-by:` trailer naming the steward. The same error ladder as a mint: every
    sentence a steward can read over MCP is one of the constants above."""
    trailer = f"Decided-by: {_trailer_actor(decided_by)}"
    return _in_fresh_clone(
        repo_url, branch, credential,
        lambda repo, author: decide.apply(repo, action=action, branch=branch, author=author,
                                          trailer=trailer, on_output=on_output))


def _in_fresh_clone(repo_url: str, branch: str, credential, work) -> dict:
    """Clone, configure the App's identity on the clone, run `work(repo, author)`, clean up —
    with the refusal ladder that turns this package's clone-bound sentences into steward-facing
    ones (the constants above, and the pass-through set they document)."""
    clone_url = _authenticated_url(repo_url, credential)
    author = githubapp.identity(credential or {})
    with tempfile.TemporaryDirectory(prefix="stigmergy-entity-mint-") as tmp:
        repo = os.path.join(tmp, "repo")
        try:
            gitcmd.run("clone", "--quiet", "--branch", branch, clone_url, repo,
                       timeout=MINT_GIT_TIMEOUT_S)
        except GitError as ex:
            # `_scrub` keeps the CREDENTIAL out of `str(ex)`, but scrubbed is not safe to publish:
            # what remains is git's stderr naming this host's temp directory. Logged, not echoed
            # (see `CLONE_FAILED_MESSAGE`).
            log.error("server-driven mint: could not clone the knowledge repo", exc_info=True)
            raise EntityError(CLONE_FAILED_MESSAGE) from ex
        try:
            # Configured on the clone too, not only handed in as `author`: the rebase in
            # `clone.commit_and_push`'s retry needs SOME committer identity, and a fresh temp
            # clone cannot assume a global `~/.gitconfig` supplies one.
            gitcmd.run("config", "user.name", author[0], cwd=repo)
            gitcmd.run("config", "user.email", author[1], cwd=repo)
            return work(repo, author)
        # The ladder, ordered but order-INDEPENDENT: none of these four is a subclass of another
        # (pinned by `tests/entities/test_errors.py`), so no arm shadows the one below it. What the
        # order DOES require is that all four precede any `except EntityError` — there is none, and
        # adding one would silently swallow the pass-through set named above.
        except CloneStateError as ex:
            log.error("server-driven mint: the throwaway clone was not in a mintable state",
                      exc_info=True)
            raise EntityError(CLONE_STATE_FAULT_MESSAGE) from ex
        except TemplateMissingError as ex:
            log.error("server-driven mint: the knowledge repo carries no entity template",
                      exc_info=True)
            raise EntityError(TEMPLATE_MISSING_MESSAGE) from ex
        except CollisionRaceError as ex:
            log.error("server-driven mint: the registry moved under an already-passed gate",
                      exc_info=True)
            raise EntityError(COLLISION_RACE_MESSAGE) from ex
        except PushRaceError as ex:
            log.error("server-driven mint: the push lost its race with the knowledge repo",
                      exc_info=True)
            raise EntityError(PUSH_RACE_MESSAGE) from ex
        except LibrarianError as ex:
            # The seam the module docstring promises, for everything after the clone: e.g.
            # `gates.ensure_scanner` raises `LibrarianConfigError` when gitleaks is absent, and a
            # server host without the scanner is an ordinary deployment. Caught as the BASE class —
            # a seam that holds only for the faults already observed breaks on the next.
            log.error("server-driven mint: a librarian fault after the clone", exc_info=True)
            raise EntityError(MINT_FAULT_MESSAGE) from ex


def _trailer_actor(approved_by: str) -> str:
    """The `Approved-by:` value, collapsed to one line.

    The trailer is half of how `git log` answers "who approved this identity", and the console
    supplies a free-text `actor` — a newline in it would inject arbitrary commit-message lines, a
    second, forged `Approved-by:` among them.
    """
    collapsed = " ".join(str(approved_by or "").split())
    if not collapsed:
        raise EntityError(
            "a server-driven mint needs a non-empty approver — the `Approved-by:` trailer is "
            "half of how `git log` answers who approved an identity")
    return collapsed


def _authenticated_url(repo_url: str, credential) -> str:
    """This door's wording over `githubapp.authenticated_clone_url`, which owns the mechanism and
    the residual-risk argument for every server-driven clone.

    Only the SENTENCES are here, because only they are this package's: `server.review` echoes an
    `EntityError` from this module to a steward over MCP verbatim, so none of the three refusals
    below may carry the librarian exception's own text (it names the App private-key FILE PATH).
    Each is logged with `exc_info=True` — moved, not lost — and answered with a sentence written
    for the reader who will see it.

    The empty-URL check is asked HERE rather than read off the shared refusal, so this door keeps
    naming its own environment variable: the resolver refuses the same state one sentence later,
    for its own caller, exactly as `config.is_repo_checkout` shares a judgement and not a message.
    """
    if not repo_url:
        raise CapabilityUnavailableError(
            "no knowledge-repo URL is configured for a server-driven mint — set "
            "$STIGMERGY_LIBRARIAN_REPO_URL to the same repo the librarian worker writes to")
    try:
        return githubapp.authenticated_clone_url(repo_url, credential)
    except CloneCredentialUnavailable as ex:
        raise CapabilityUnavailableError(
            f"minting against an https:// knowledge repo needs the librarian GitHub App credential "
            f"(${githubapp.APP_ID_ENV}, ${githubapp.INSTALLATION_ID_ENV} and "
            f"${githubapp.PRIVATE_KEY_ENV} or ${githubapp.PRIVATE_KEY_FILE_ENV}) — this server has "
            f"none of them configured") from ex
    except CloneCredentialHalfSet as ex:
        log.error("the librarian GitHub App credential is half-configured", exc_info=True)
        raise EntityError(APP_MISCONFIGURED_MESSAGE) from ex
    except CloneCredentialRefused as ex:
        log.error("the librarian GitHub App would not issue an installation token", exc_info=True)
        raise EntityError(CREDENTIAL_FAULT_MESSAGE) from ex
