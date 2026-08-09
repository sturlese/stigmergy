"""A server-driven mint: clone the knowledge repo with the librarian App's own credential, mint
through the shared `entities.mint.mint`, push, remove the clone — ADR 030 D3.

**Why a throwaway clone per request, in the calling process, rather than a standing checkout.**
Rare operation, small repo, seconds of bounded git work. A standing checkout would couple the
public read path's startup to GitHub availability for an operation that happens rarely (D3's own
"rejected: clone at boot"); a `tempfile.TemporaryDirectory` costs nothing between requests and
leaves nothing behind, on success or on failure, because the `finally` in `mint_via_clone` removes
it either way.

**Credential.** `librarian.githubapp`'s existing machinery — app JWT -> installation token -> a
tokenized clone URL — is reused rather than reimplemented (`bootstrap.py`/`gitcredential.py` are
the other two callers of that same machinery, one via a git credential HELPER, one via a `git push`
config header; this is the third shape, a tokenized URL, because the clone here is a single
one-shot subprocess call this process fully controls — see `_authenticated_url`). The token is
embedded in the URL only in memory, for the one `git clone` invocation, and is never logged: a
clone failure raises through `librarian.gitcmd.run`, which already scrubs any credential-bearing
URL out of both the command line and stderr before it reaches an exception message (the same
`_scrub` the push path relies on) — the same "reduce to what is safe, never the raw value" posture
`capture.evidence.S3EvidenceStore._fail` takes for a boto error.

**Refusal, not degradation, when the capability is absent.** A server with no App credential (a
local stdio server, say) or no knowledge-repo URL configured refuses a mint by naming the missing
capability — `entities.errors.CapabilityUnavailableError`, entities' own name for the posture
`server.errors.CapabilityUnavailableError` carries one layer up (this package may not import
`stigmergy.server` at all). `stigmergy.server.review` maps it.

**This module's public seam raises only `entities.errors.EntityError` (or a subclass).** A
`librarian.errors.GitError`/`LibrarianConfigError` reaching all the way out would be a foreign
exception type crossing this package's own boundary — every one `librarian.gitcmd`/`githubapp` can
raise is caught here and re-raised as one of this package's own, so a caller (`stigmergy.server.
review`) only ever has to know this package's vocabulary.
"""
import os
import tempfile

from stigmergy.entities import mint as mint_lib
from stigmergy.entities.errors import CapabilityUnavailableError, EntityError
from stigmergy.librarian import gitcmd, githubapp
from stigmergy.librarian.errors import GitError, LibrarianConfigError, LibrarianError

# One network leg of a server-driven mint. Generous enough for a cold clone of a knowledge repo
# and short enough that a stalled remote cannot pin an HTTP worker indefinitely (audit M2).
MINT_GIT_TIMEOUT_S = 60


def mint_via_clone(repo_url: str, branch: str, credential, *, entity_id: str, name: str,
                   entity_type: str, aliases=(), role: str = "", today: str,
                   submission_id: int | None = None, approved_by: str, on_output=None) -> dict:
    """Clone `repo_url` into a throwaway directory, mint through `entities.mint.mint` as the
    librarian App with an `Approved-by: {approved_by}` trailer, push, clean up in a `finally`.

    `credential` is what `librarian.githubapp.configured`/`installation_token` read (an env-shaped
    mapping — typically `os.environ`) — required ONLY when `repo_url` is `https://`. A caller
    pointed at a local path or a `git://` URL (every test here, and the composition's own bare
    remote) needs no App at all: `credential` may be `None`. That is not a relaxation for tests —
    it is the honest statement of when a credential is actually needed, which is exactly what lets
    the pg suite exercise this function for real, against a real bare remote, with no key and no
    network.
    """
    clone_url = _authenticated_url(repo_url, credential)
    author = githubapp.identity(credential or {})
    with tempfile.TemporaryDirectory(prefix="stigmergy-entity-mint-") as tmp:
        repo = os.path.join(tmp, "repo")
        try:
            gitcmd.run("clone", "--quiet", "--branch", branch, clone_url, repo,
                       timeout=MINT_GIT_TIMEOUT_S)
        except GitError as ex:
            # `str(ex)` is already scrubbed by `gitcmd.run` itself (args and stderr both pass
            # through `_scrub` before the exception is built) — safe to re-embed, never the URL.
            raise EntityError(f"could not clone the knowledge repo to mint this entity: {ex}") from ex
        try:
            # Configured on the clone too (not only handed to `mint()` as `author`): the bounded
            # rebase-and-retry inside `clone.commit_and_push` replays this commit with `git
            # rebase`, which needs SOME committer identity reachable from wherever git looks, and a
            # throwaway clone in a fresh temp directory cannot assume a global `~/.gitconfig`
            # supplies one.
            gitcmd.run("config", "user.name", author[0], cwd=repo)
            gitcmd.run("config", "user.email", author[1], cwd=repo)
            return mint_lib.mint(
                repo, entity_id=entity_id, name=name, entity_type=entity_type, aliases=aliases,
                role=role, branch=branch, today=today, author=author, submission_id=submission_id,
                trailer=f"Approved-by: {_trailer_actor(approved_by)}", on_output=on_output)
        except LibrarianError as ex:
            # The seam the module docstring promises, on the half of the call that had none. The
            # clone above was wrapped and `_authenticated_url` renames both of `githubapp`'s config
            # faults, but everything AFTER the clone reached the caller as a librarian type:
            # `gates.ensure_scanner` — which `mint._refuse_secrets` runs on this very path —
            # raises `LibrarianConfigError` when gitleaks is absent, and the MCP server is a
            # different process from the librarian worker, so a server host without the scanner is
            # the ordinary deployment, not an exotic one. `server.review._mint_entity_proposal`
            # catches this package's vocabulary and nothing else, so that config fault came out of
            # a steward's Approve as an unmapped exception instead of a refusal they could act on.
            # `LibrarianError` (the base) rather than the two subclasses seen so far: a seam that
            # holds only for the faults already observed is the one that breaks on the next.
            raise EntityError(f"the mint could not be completed: {ex}") from ex


def _trailer_actor(approved_by: str) -> str:
    """The `Approved-by:` value, collapsed to one line.

    This is the governance record: ADR 030 D1 makes the trailer half of the answer to "who
    approved this identity", and the knowledge repo's authorship check reads it. MCP and Slack
    supply a resolved identity, but the CONSOLE supplies a free-text `actor` (D2 — attribution,
    by design), and a newline in it would inject arbitrary lines into the commit message — a
    second, forged `Approved-by:` among them. `birth._clean_name` already collapses whitespace
    for exactly this reason one field over; the trailer had no equivalent until it became a
    record something reads.
    """
    collapsed = " ".join(str(approved_by or "").split())
    if not collapsed:
        raise EntityError(
            "a server-driven mint needs a non-empty approver — the `Approved-by:` trailer is "
            "half of how `git log` answers who approved an identity")
    return collapsed


def _authenticated_url(repo_url: str, credential) -> str:
    """`repo_url`, unchanged for anything that is not `https://`; tokenized otherwise.

    A `git://` or plain-path remote (the test harness's own shape, and the composition's bare
    remote) authenticates nothing and wants no credential — returned as-is. An `https://` remote
    needs the librarian App, minted fresh here and embedded as `x-access-token:<token>@host` the
    same way a hand-configured `credential.helper` would present it (`gitcredential.USERNAME`'s own
    convention) — acceptable here, unlike `githubapp.push_url`'s deliberately credential-free URL,
    because this URL is built, used for one `git clone` subprocess and discarded inside this same
    process.

    **The residual, stated rather than denied.** Unlike `githubapp.push_url`'s deliberately
    credential-free shape, this token DOES reach the clone's argv (readable via `ps` for the
    clone's duration) and IS persisted by git as `remote.origin.url` in the throwaway clone's
    `.git/config`, where the subsequent push depends on it. Both are bounded: the clone lives
    inside a `TemporaryDirectory` removed in a `finally`, and an installation token expires in
    about an hour. It is accepted here and NOT in `librarian.gitcmd` (whose own comment forbids
    exactly this) because that path runs continuously on every item with a long-lived worktree,
    while this one is a seconds-long, per-request clone that is then deleted. Every error path is
    still scrubbed by `gitcmd._scrub`, and no log line ever carries it. The stronger shape — an
    `http.<url>.extraheader` config triple passed through `env=`, keeping the token out of both
    argv and disk — is the documented upgrade if this residual ever stops being acceptable.
    """
    if not repo_url:
        raise CapabilityUnavailableError(
            "no knowledge-repo URL is configured for a server-driven mint — set "
            "$STIGMERGY_LIBRARIAN_REPO_URL to the same repo the librarian worker writes to")
    if not repo_url.startswith("https://"):
        return repo_url
    try:
        # `configured()` does not only return `False` — a HALF-set App (one or two of the three
        # env vars present, never all three) RAISES instead, deliberately: that shape means
        # somebody meant to set this up, and folding it into the plain "absent" refusal below
        # would tell an operator to configure something that already half exists. Caught here
        # rather than left to escape as a raw `librarian.errors.LibrarianConfigError` — this
        # package's public seam raises only its OWN error types (module docstring).
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
        # The App IS configured but GitHub would not hand back a token (a revoked installation, a
        # rotated key) — an operational fault, not an absent capability, so it is not renamed to
        # CapabilityUnavailableError: the fix is "check the App", not "configure one".
        raise EntityError(f"could not mint a GitHub credential to push this entity: {ex}") from ex
    scheme, rest = repo_url.split("://", 1)
    return f"{scheme}://x-access-token:{token}@{rest}"
