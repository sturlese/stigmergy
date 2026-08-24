"""GitHub App authentication and commit identity."""
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
PRIVATE_KEY_ENV = "STIGMERGY_LIBRARIAN_PRIVATE_KEY"
PRIVATE_KEY_FILE_ENV = "STIGMERGY_LIBRARIAN_PRIVATE_KEY_FILE"
APP_LOGIN_ENV = "STIGMERGY_LIBRARIAN_APP_LOGIN"

# GitHub accepts App JWTs for at most ten minutes; backdating tolerates clock skew.
_JWT_TTL_S = 540
_JWT_BACKDATE_S = 60

APP_LOGIN_DEFAULT = "stigmergy-librarian"


def app_login(env: dict | None = None) -> str:
    """The App slug this deployment's commits are authored by."""
    env = os.environ if env is None else env
    return env.get(APP_LOGIN_ENV) or APP_LOGIN_DEFAULT


def configured(env: dict | None = None) -> bool:
    """Return whether a complete App credential is configured; reject partial credentials."""
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
        log.error(
            "cannot read the librarian App private key file (%s)",
            ex.__class__.__name__,
        )
        raise LibrarianConfigError(
            f"cannot read the librarian App private key file named by ${PRIVATE_KEY_FILE_ENV} "
            f"({ex.__class__.__name__})") from ex


def _app_jwt(app_id: str, private_key: str) -> str:
    import jwt  # imported here, not at module scope: a run with no App never needs it
    now = int(time.time())
    return jwt.encode({"iat": now - _JWT_BACKDATE_S, "exp": now + _JWT_TTL_S, "iss": str(app_id)},
                      private_key, algorithm="RS256")


def installation_token(env: dict | None = None, *, opener=None) -> str:
    """Mint a fresh installation token without logging or persisting it."""
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


def identity(env: dict | None = None) -> tuple[str, str]:
    """Return the GitHub App's commit author name and noreply address."""
    env = os.environ if env is None else env
    login = app_login(env)
    app_id = env.get(APP_ID_ENV, "")
    if not app_id:
        return (login, f"{login}@users.noreply.github.com")
    return (f"{login}[bot]",
            f"{app_id}+{login}[bot]@users.noreply.github.com")
