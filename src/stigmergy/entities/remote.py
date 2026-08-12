"""A server-driven mint: clone the knowledge repo with the librarian App's own credential, mint
through the shared `entities.mint.mint`, push, remove the clone.

A throwaway clone per request, never a standing checkout — a standing one would couple the read
path's startup to GitHub availability for a rare operation, and the `TemporaryDirectory` leaves
nothing behind on success or failure. The credential rides `librarian.githubapp`'s existing
machinery (app JWT -> installation token -> tokenized clone URL); the token lives only in memory
for the one `git clone`, and every error path is scrubbed by `gitcmd._scrub`. An absent
capability REFUSES by naming it (`CapabilityUnavailableError`) rather than degrading. This
module's public seam raises only `entities.errors` types: every `librarian.*` exception is caught
and re-raised, so a caller only ever knows this package's vocabulary.
"""
import logging
import os
import tempfile

from stigmergy.entities import mint as mint_lib
from stigmergy.entities.errors import CapabilityUnavailableError, EntityError
from stigmergy.librarian import gitcmd, githubapp
from stigmergy.librarian.errors import GitError, LibrarianConfigError, LibrarianError

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


def mint_via_clone(repo_url: str, branch: str, credential, *, entity_id: str, name: str,
                   entity_type: str, aliases=(), role: str = "", today: str,
                   submission_id: int | None = None, approved_by: str, on_output=None) -> dict:
    """Clone `repo_url` into a throwaway directory, mint through `entities.mint.mint` as the
    librarian App with an `Approved-by: {approved_by}` trailer, push, clean up in a `finally`.

    `credential` is the env-shaped mapping `librarian.githubapp` reads — required ONLY when
    `repo_url` is `https://`. A local path or `git://` URL authenticates nothing, so `credential`
    may be `None`: the honest statement of when a credential is needed, and what lets the pg
    suite drive this against a real bare remote with no key and no network.
    """
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
            # (see `MINT_FAULT_MESSAGE`).
            log.error("server-driven mint: could not clone the knowledge repo", exc_info=True)
            raise EntityError(
                "the knowledge repo could not be cloned to mint this entity — the server could "
                "not reach it or is not credentialed for it. Nothing was pushed. The details are "
                "in the server log; ask whoever runs this deployment to look") from ex
        try:
            # Configured on the clone too, not only handed to `mint()` as `author`: the rebase in
            # `clone.commit_and_push`'s retry needs SOME committer identity, and a fresh temp
            # clone cannot assume a global `~/.gitconfig` supplies one.
            gitcmd.run("config", "user.name", author[0], cwd=repo)
            gitcmd.run("config", "user.email", author[1], cwd=repo)
            return mint_lib.mint(
                repo, entity_id=entity_id, name=name, entity_type=entity_type, aliases=aliases,
                role=role, branch=branch, today=today, author=author, submission_id=submission_id,
                trailer=f"Approved-by: {_trailer_actor(approved_by)}", on_output=on_output)
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
    """`repo_url`, unchanged for anything that is not `https://`; tokenized otherwise
    (`x-access-token:<token>@host`, a fresh installation token, used for one `git clone` and
    discarded in this same process).

    The residual, stated rather than denied: the token DOES reach the clone's argv and IS
    persisted as `remote.origin.url` in the throwaway clone's `.git/config` — both bounded by the
    `TemporaryDirectory` and the token's ~1h expiry. Accepted here and NOT in `librarian.gitcmd`
    (a continuously-running path with a long-lived worktree); every error path is scrubbed by
    `gitcmd._scrub` and no log line carries it. The stronger shape, if this residual ever stops
    being acceptable: an `http.<url>.extraheader` config triple passed through `env=`.
    """
    if not repo_url:
        raise CapabilityUnavailableError(
            "no knowledge-repo URL is configured for a server-driven mint — set "
            "$STIGMERGY_LIBRARIAN_REPO_URL to the same repo the librarian worker writes to")
    if not repo_url.startswith("https://"):
        return repo_url
    try:
        # `configured()` RAISES on a HALF-set App (some but not all of the three env vars):
        # folding that into the plain "absent" refusal below would tell an operator to configure
        # something that already half exists. Caught so no raw librarian type escapes (module
        # docstring).
        app_configured = bool(credential) and githubapp.configured(credential)
    except LibrarianConfigError as ex:
        raise EntityError(f"the librarian GitHub App is misconfigured: {ex}") from ex
    if not app_configured:
        raise CapabilityUnavailableError(
            f"minting against an https:// knowledge repo needs the librarian GitHub App credential "
            f"(${githubapp.APP_ID_ENV}, ${githubapp.INSTALLATION_ID_ENV} and "
            f"${githubapp.PRIVATE_KEY_ENV} or ${githubapp.PRIVATE_KEY_FILE_ENV}) — this server has "
            f"none of them configured")
    try:
        token = githubapp.installation_token(credential)
    except LibrarianConfigError as ex:
        # The App IS configured but GitHub would not hand back a token (revoked installation,
        # rotated key) — an operational fault, not an absent capability: the fix is "check the
        # App", not "configure one".
        raise EntityError(f"could not mint a GitHub credential to push this entity: {ex}") from ex
    scheme, rest = repo_url.split("://", 1)
    return f"{scheme}://x-access-token:{token}@{rest}"
