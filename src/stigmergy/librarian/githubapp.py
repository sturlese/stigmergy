"""The `stigmergy-librarian` GitHub App identity: app JWT -> installation token -> push URL.

**Why an App and not the operator's own credentials.** The point of git as the substrate is that
the audit is already done — *who changed what* is in the history. A librarian committing with an
operator's disk permissions makes `git blame` lie about the one thing the substrate exists to
record. So the librarian has its own identity, with `contents: write` on the knowledge repo and
nothing else, and every filed page carries the human in a `Submitted-by:` trailer and in the
page's `submitted_by`.

**Credentials come from the environment, the token is minted per push, and neither is ever
logged.** The App's private key is a credential that can rewrite the company's knowledge; it is
read, used to sign a 9-minute JWT, and dropped. The installation token it buys lives ~1 hour at
GitHub but is used once, here, and never written to disk, never into the worktree's git config,
and never into a command line either (that is what `push_config` is for): argv is readable by
every process on the box through `ps` and `/proc/<pid>/cmdline`.

**Absent configuration is not an error here.** `configured()` answers whether the App is set up;
a run without it pushes to `origin` as whoever the process is — which is exactly what the
tests and the docker e2e do against a bare local remote that needs no credentials at all. What
is deliberately NOT built is a "commit without pushing" fallback: it would become the silent
default and the push path would never get exercised.
"""
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request

from stigmergy.librarian.errors import LibrarianConfigError

log = logging.getLogger(__name__)

APP_ID_ENV = "STIGMERGY_LIBRARIAN_APP_ID"
INSTALLATION_ID_ENV = "STIGMERGY_LIBRARIAN_INSTALLATION_ID"
PRIVATE_KEY_ENV = "STIGMERGY_LIBRARIAN_PRIVATE_KEY"           # PEM contents
PRIVATE_KEY_FILE_ENV = "STIGMERGY_LIBRARIAN_PRIVATE_KEY_FILE"  # or a path to the PEM

# GitHub rejects a JWT with `exp` more than 10 minutes out, and clock skew between this machine
# and GitHub is real, so we ask for 9 and backdate `iat` by 60s — the shape GitHub's own docs
# recommend.
_JWT_TTL_S = 540
_JWT_BACKDATE_S = 60

APP_LOGIN = "stigmergy-librarian"


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
        # The path IS operator-sensitive — it is where the App's private key lives — and this
        # message names it because it is a local CLI diagnostic (spec Notes for Developer: generic
        # over HTTP, specific in the CLI). That is only true because `worker.process_next` no longer
        # interpolates a mid-run `LibrarianConfigError` into the wire report: it did, briefly, which
        # made this docstring's "never a wire message" false and put this path in front of every
        # authenticated MCP identity. The guarantee lives THERE, not here — so if that handler is
        # ever changed back, this sentence is wrong again.
        raise LibrarianConfigError(
            f"cannot read the librarian App private key at {path!r}: {ex.__class__.__name__}") from ex


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
                 "User-Agent": APP_LOGIN})
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


def push_url(repo_slug: str) -> str:
    """The push URL — **credential-free on purpose**.

    The token used to be interpolated here and handed to `git push` as an argument. argv is world
    readable (`ps`, `/proc/<pid>/cmdline`), so that published the credential to every process on
    the machine for the lifetime of the push. The URL is now plain and the token travels in the
    environment instead; see `push_config`.
    """
    return f"https://github.com/{repo_slug}.git"


def push_config(token: str, repo_slug: str) -> dict[str, str]:
    """The `GIT_CONFIG_*` triple that authenticates one push, for `gitcmd.run`'s `env=`.

    `GIT_CONFIG_COUNT`/`KEY_0`/`VALUE_0` is git's own way to pass configuration for a single
    invocation without touching any config FILE, so the token lands neither on disk nor in argv.
    The setting is an `http.<url>.extraheader` carrying a Basic credential, which is the same
    mechanism GitHub's own Actions checkout uses.

    Scoped to the one URL rather than set globally (`http.extraheader`): a header attached to
    every https request would be sent to whatever other host a redirect or a misconfigured remote
    pointed at.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{push_url(repo_slug)}.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def identity(env: dict | None = None) -> tuple[str, str]:
    """`(name, email)` for the commit author. The `<app-slug>[bot]` form with the numeric
    `<id>+<slug>[bot]@users.noreply.github.com` address is GitHub's own convention for App
    commits — it is what makes the commit render as the App rather than as an unknown user."""
    env = os.environ if env is None else env
    app_id = env.get(APP_ID_ENV, "")
    if not app_id:
        return (APP_LOGIN, f"{APP_LOGIN}@users.noreply.github.com")
    return (f"{APP_LOGIN}[bot]",
            f"{app_id}+{APP_LOGIN}[bot]@users.noreply.github.com")
