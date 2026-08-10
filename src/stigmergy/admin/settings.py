"""Runtime configuration for the admin console — env-only, resolved once at startup.

Two ground rules, both house ones:

- **No new flags.** The server's command line is pinned byte-identical between `fly.toml` and the
  Dockerfile `CMD` (`tests/test_deployment_config.py`), so the console configures itself from the
  environment alone and that pin never moves.
- **Modules never read the environment at import time** (`server/settings.py`'s rule) —
  `AdminSettings.from_env` is the one place these names are consulted.

The console is INERT until `$STIGMERGY_ADMIN_TOKEN_HASH` is set: `configured()` is what
`routes.compose` checks before it builds a single route or runs a line of DDL.
"""
import re
from dataclasses import dataclass

from stigmergy.server.errors import StartupError

TOKEN_HASH_ENV = "STIGMERGY_ADMIN_TOKEN_HASH"
ACTOR_ENV = "STIGMERGY_ADMIN_ACTOR"
GITHUB_TOKEN_ENV = "STIGMERGY_ADMIN_GITHUB_TOKEN"
GITHUB_REPO_ENV = "STIGMERGY_ADMIN_GITHUB_REPO"
CHANNELS_PATH_ENV = "STIGMERGY_ADMIN_CHANNELS_PATH"

# Attribution, not authorization — the default `--by` every mutation form is prefilled with when
# the operator has not set a name of their own (`stigmergy-queue`'s own doctrine for `--by`).
DEFAULT_ACTOR = "admin-console"

# The workflows the crons tab drives live wherever the operator installed them, and that is the
# KNOWLEDGE repo: `deploy/workflows/` here holds templates you copy there, and `.github/workflows/`
# here holds this repository's own CI and nothing else (pinned by `tests/test_workflows_config.py`).
# Still its own variable rather than a reuse of `$STIGMERGY_GITHUB_REPO` — that one names the repo
# the index webhook listens to; same repository in practice, different fact, and collapsing them
# would silently couple a read-side subscription to a write-side credential's scope.
#
# No default: a deployment's own `<owner>/<repo>` is not something this code can guess, and
# guessing wrong would point an operator's cron buttons at somebody else's repository. Unset means
# the crons tab is simply not configured — the same posture the token takes.
DEFAULT_WORKFLOWS_REPO = ""

# `hash_token` produces lowercase sha256 hex, 64 chars. A non-empty value that is NOT that shape is
# an operator error worth refusing at startup rather than serving a console no token can ever open
# — same fail-closed-loudly posture as a malformed `$STIGMERGY_TOKEN_STORE`.
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
        """`env` is injectable for tests (`webhook.webhook_settings_from_env`'s own pattern);
        production passes nothing and reads `os.environ`."""
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
