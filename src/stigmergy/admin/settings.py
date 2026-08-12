"""Runtime configuration for the admin console — env-only, resolved once at startup.

No CLI flags, ever: the server's command line is pinned byte-identical between `fly.toml` and the
Dockerfile `CMD`. `AdminSettings.from_env` is the one place the environment is consulted. The
console is INERT until `$STIGMERGY_ADMIN_TOKEN_HASH` is set — `configured()` is what
`routes.compose` checks before building a single route or running any DDL.
"""
import re
from dataclasses import dataclass

from stigmergy.server.errors import StartupError

TOKEN_HASH_ENV = "STIGMERGY_ADMIN_TOKEN_HASH"
ACTOR_ENV = "STIGMERGY_ADMIN_ACTOR"
GITHUB_TOKEN_ENV = "STIGMERGY_ADMIN_GITHUB_TOKEN"
GITHUB_REPO_ENV = "STIGMERGY_ADMIN_GITHUB_REPO"
CHANNELS_PATH_ENV = "STIGMERGY_ADMIN_CHANNELS_PATH"

# Attribution, not authorization — the default `--by` every mutation form is prefilled with.
DEFAULT_ACTOR = "admin-console"

# Deliberately its own variable, not a reuse of `$STIGMERGY_GITHUB_REPO` (the index webhook's
# repo — same repository in practice, different fact). No default: guessing an `<owner>/<repo>`
# wrong would point the cron buttons at somebody else's repository; unset means the crons tab is
# simply not configured.
DEFAULT_WORKFLOWS_REPO = ""

# `hash_token` produces lowercase sha256 hex, 64 chars. Any other non-empty value is refused at
# startup rather than serving a console no token can ever open.
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AdminSettings:
    token_hash: str = ""
    actor: str = DEFAULT_ACTOR
    github_token: str = ""
    github_repo: str = DEFAULT_WORKFLOWS_REPO
    channels_path: str = ""

    def configured(self) -> bool:
        return bool(self.token_hash)

    def github_configured(self) -> bool:
        """Both halves or neither: a token with no repository builds `/repos//actions/...` and
        fails at the API instead of failing here, where the operator can read why."""
        return bool(self.github_token and self.github_repo)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "AdminSettings":
        """`env` is injectable for tests; production passes nothing and reads `os.environ`."""
        import os

        source = os.environ if env is None else env
        token_hash = (source.get(TOKEN_HASH_ENV) or "").strip().lower()
        if token_hash and not _SHA256_HEX.match(token_hash):
            raise StartupError(
                f"${TOKEN_HASH_ENV} is set but is not a sha256 hex digest (64 hex chars) — "
                f"generate the pair with `stigmergy-admin-token` and set the printed hash, or unset "
                f"it to leave the console disabled.")
        return cls(
            token_hash=token_hash,
            actor=(source.get(ACTOR_ENV) or "").strip() or DEFAULT_ACTOR,
            github_token=(source.get(GITHUB_TOKEN_ENV) or "").strip(),
            github_repo=(source.get(GITHUB_REPO_ENV) or "").strip() or DEFAULT_WORKFLOWS_REPO,
            channels_path=(source.get(CHANNELS_PATH_ENV) or "").strip(),
        )
