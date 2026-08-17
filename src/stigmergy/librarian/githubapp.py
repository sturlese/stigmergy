"""The `stigmergy-librarian` GitHub App identity: app JWT -> installation token -> push URL.

An App and not the operator's own credentials, because the substrate's point is that *who
changed what* is in the history — a librarian committing with an operator's disk permissions
makes `git blame` lie. So the librarian has its own identity, with `contents: write` on the
knowledge repo and nothing else; the human travels in a `Submitted-by:` trailer and the page's
`submitted_by`.

Credentials come from the environment, the token is minted per push, and neither is ever
logged: the key signs a 9-minute JWT and is dropped; the token is used once, never written to
disk, never into git config, and never into a command line (argv is readable by every process
on the box — that is what `push_config` is for).

`repo_slug` lives here too, and only here: it feeds `push_url`/`push_config` and nothing else, and
it is the one place that reads a checkout's origin — the only reason this module touches `gitcmd`.

Absent configuration is not an error here: `configured()` answers whether the App is set up,
and a run without it pushes to `origin` as whoever the process is — what the tests and the
docker e2e do against a bare local remote. What is deliberately NOT built is a
"commit without pushing" fallback: it would become the silent default and the push path would
never get exercised.
"""
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request

from stigmergy.librarian import gitcmd
from stigmergy.librarian.errors import (
    CloneCredentialHalfSet,
    CloneCredentialRefused,
    CloneCredentialUnavailable,
    LibrarianConfigError,
)

log = logging.getLogger(__name__)

APP_ID_ENV = "STIGMERGY_LIBRARIAN_APP_ID"
INSTALLATION_ID_ENV = "STIGMERGY_LIBRARIAN_INSTALLATION_ID"
PRIVATE_KEY_ENV = "STIGMERGY_LIBRARIAN_PRIVATE_KEY"           # PEM contents
PRIVATE_KEY_FILE_ENV = "STIGMERGY_LIBRARIAN_PRIVATE_KEY_FILE"  # or a path to the PEM
APP_LOGIN_ENV = "STIGMERGY_LIBRARIAN_APP_LOGIN"                # the App's own slug — see below

# GitHub rejects a JWT with `exp` more than 10 minutes out, and clock skew between this machine
# and GitHub is real, so we ask for 9 and backdate `iat` by 60s — the shape GitHub's own docs
# recommend.
_JWT_TTL_S = 540
_JWT_BACKDATE_S = 60

# The App's slug, from which the commit identity derives (`app_identity` below). It is
# deployment-specific: this default is only the name of the App the operator runbook creates.
# Getting it wrong is silent and expensive — commits authored as a slug GitHub does not know
# still push, they simply stop rendering as the App, and the knowledge repo's authorship check
# rejects every one. Renaming an App that already has commits splits the history across two
# identities, which is the reason to leave a working App's name alone.
APP_LOGIN_DEFAULT = "stigmergy-librarian"


def app_login(env: dict | None = None) -> str:
    """The App slug this deployment's commits are authored by."""
    env = os.environ if env is None else env
    return env.get(APP_LOGIN_ENV) or APP_LOGIN_DEFAULT


def configured(env: dict | None = None) -> bool:
    """Is the App configured at all? Partial configuration is a config ERROR, not a silent
    fallback to pushing as the operator — half a credential means somebody meant to set this up
    and a filed page would be attributed to the wrong identity."""
    env = os.environ if env is None else env
    present = [bool(env.get(APP_ID_ENV)), bool(env.get(INSTALLATION_ID_ENV)),
               bool(env.get(PRIVATE_KEY_ENV) or env.get(PRIVATE_KEY_FILE_ENV))]
    if not any(present):
        return False
    if not all(present):
        raise LibrarianConfigError(
            f"the librarian GitHub App is half-configured: set all of {APP_ID_ENV}, "
            f"{INSTALLATION_ID_ENV} and {PRIVATE_KEY_ENV} (or {PRIVATE_KEY_FILE_ENV}), or none "
            f"of them to push as the current user")
    return True


def _private_key(env: dict) -> str:
    inline = env.get(PRIVATE_KEY_ENV)
    if inline:
        return inline
    path = env.get(PRIVATE_KEY_FILE_ENV, "")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as ex:
        # The path is where the App's PRIVATE KEY lives and stays out of the MESSAGE:
        # `entities.remote` catches this exception on the server-side mint path and `server.review`
        # echoes it to a steward verbatim, so the path goes to the operator's log at ERROR and the
        # message names ${PRIVATE_KEY_FILE_ENV} instead.
        log.error("cannot read the librarian App private key file at %r", path, exc_info=True)
        raise LibrarianConfigError(
            f"cannot read the librarian App private key file named by ${PRIVATE_KEY_FILE_ENV} "
            f"({ex.__class__.__name__}) — the path is in the server log") from ex


def _app_jwt(app_id: str, private_key: str) -> str:
    import jwt  # imported here, not at module scope: a run with no App never needs it
    now = int(time.time())
    return jwt.encode({"iat": now - _JWT_BACKDATE_S, "exp": now + _JWT_TTL_S, "iss": str(app_id)},
                      private_key, algorithm="RS256")


def installation_token(env: dict | None = None, *, opener=None) -> str:
    """Mint a fresh installation token. Never logged, never persisted, used once.

    `opener` is injectable so a test can drive the whole path — JWT signing included — against a
    stub instead of GitHub.
    """
    env = os.environ if env is None else env
    if not configured(env):
        raise LibrarianConfigError("the librarian GitHub App is not configured")

    token_jwt = _app_jwt(env[APP_ID_ENV], _private_key(env))
    request = urllib.request.Request(
        f"https://api.github.com/app/installations/{env[INSTALLATION_ID_ENV]}/access_tokens",
        method="POST",
        headers={"Authorization": f"Bearer {token_jwt}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": app_login(env)})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        # The body can echo request detail; the status is what an operator acts on.
        raise LibrarianConfigError(
            f"GitHub refused an installation token (HTTP {ex.code}) — check the App id, the "
            f"installation id and that the key has not been rotated") from ex
    except Exception as ex:  # noqa: BLE001 — network/JSON: class name only, never the response
        raise LibrarianConfigError(
            f"could not mint a GitHub installation token ({ex.__class__.__name__})") from ex

    token = payload.get("token")
    if not token:
        raise LibrarianConfigError("GitHub returned no token in the installation response")
    return token


def repo_slug(clone: str) -> str:
    """`owner/name` from a checkout's `origin`, for the two functions below. `""` when it has no
    remote — every caller asks only after `configured()` has said an App push is happening.

    Both GitHub dialects, because both are real: a deployed clone's `https://` URL and the `git@`
    form an operator's own checkout usually carries. It lives HERE, beside its only consumers, and
    it is the ONE implementation: the librarian's filing push, the views writer and the repair
    applier each carried a copy, and three copies of a URL parser is three places for one dialect
    to be forgotten.
    """
    url = gitcmd.origin_url(clone)
    slug = url.rsplit(":", 1)[-1] if url.startswith("git@") else url.split("github.com/")[-1]
    return slug.removesuffix(".git")


def push_url(repo_slug: str) -> str:
    """The push URL — **credential-free on purpose**: a token in the URL is a token in argv,
    world-readable through `ps` and `/proc/<pid>/cmdline`. The token travels in the environment
    instead; see `push_config`."""
    return f"https://github.com/{repo_slug}.git"


def push_config(token: str, repo_slug: str) -> dict[str, str]:
    """The `GIT_CONFIG_*` triple that authenticates one push, for `gitcmd.run`'s `env=`.

    `GIT_CONFIG_COUNT`/`KEY_0`/`VALUE_0` is git's own way to pass configuration for a single
    invocation without touching any config FILE, so the token lands neither on disk nor in argv.
    The setting is an `http.<url>.extraheader` carrying a Basic credential — the same mechanism
    GitHub's own Actions checkout uses. Scoped to the one URL rather than set globally: a header
    attached to every https request would be sent to whatever host a redirect pointed at.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{push_url(repo_slug)}.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def authenticated_clone_url(repo_url: str, credential) -> str:
    """`repo_url`, unchanged for anything that is not `https://`; tokenized otherwise
    (`x-access-token:<token>@host`, a fresh installation token, used for one `git clone` and
    discarded in this same process).

    The ONE resolver for every server-driven clone — `entities.remote` mints through it and
    `repair.remote` applies an approved proposal through it, so the two doors cannot come to
    disagree about when a credential is needed. Each caller re-words the three refusals below for
    its own audience; only the JUDGEMENT is shared, the same bargain `config.is_repo_checkout`
    already strikes.

    The residual, stated rather than denied: the token DOES reach the clone's argv and IS
    persisted as `remote.origin.url` in the throwaway clone's `.git/config` — both bounded by the
    caller's `TemporaryDirectory` and the token's ~1h expiry. Accepted here and NOT in
    `librarian.gitcmd` (a continuously-running path with a long-lived worktree); every error path
    is scrubbed by `gitcmd._scrub` and no log line carries it. The stronger shape, if this
    residual ever stops being acceptable: an `http.<url>.extraheader` config triple passed
    through `env=`.
    """
    if not repo_url:
        raise CloneCredentialUnavailable(
            "no knowledge-repo URL was given for a server-driven clone")
    if not repo_url.startswith("https://"):
        return repo_url
    try:
        # `configured()` RAISES on a HALF-set App (some but not all of the three env vars):
        # folding that into the plain "absent" refusal below would tell an operator to configure
        # something that already half exists.
        app_configured = bool(credential) and configured(credential)
    except LibrarianConfigError as ex:
        raise CloneCredentialHalfSet(str(ex)) from ex
    if not app_configured:
        raise CloneCredentialUnavailable(
            f"cloning an https:// knowledge repo needs the GitHub App credential (${APP_ID_ENV}, "
            f"${INSTALLATION_ID_ENV} and ${PRIVATE_KEY_ENV} or ${PRIVATE_KEY_FILE_ENV})")
    try:
        token = installation_token(credential)
    except LibrarianConfigError as ex:
        raise CloneCredentialRefused(str(ex)) from ex
    scheme, rest = repo_url.split("://", 1)
    return f"{scheme}://x-access-token:{token}@{rest}"


def identity(env: dict | None = None) -> tuple[str, str]:
    """`(name, email)` for the commit author. The `<app-slug>[bot]` form with the numeric
    `<id>+<slug>[bot]@users.noreply.github.com` address is GitHub's own convention for App
    commits — it is what makes the commit render as the App rather than as an unknown user."""
    env = os.environ if env is None else env
    login = app_login(env)
    app_id = env.get(APP_ID_ENV, "")
    if not app_id:
        return (login, f"{login}@users.noreply.github.com")
    return (f"{login}[bot]",
            f"{app_id}+{login}[bot]@users.noreply.github.com")
