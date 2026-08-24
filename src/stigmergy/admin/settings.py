"""Environment-backed master backoffice configuration."""
import os
import re
from dataclasses import dataclass

from stigmergy.server.errors import StartupError

TOKEN_HASH_ENV = "STIGMERGY_ADMIN_TOKEN_HASH"
ACTOR_ENV = "STIGMERGY_ADMIN_ACTOR"

DEFAULT_ACTOR = "marc"

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AdminSettings:
    token_hash: str = ""
    actor: str = DEFAULT_ACTOR

    def configured(self) -> bool:
        return bool(self.token_hash)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "AdminSettings":
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
        )
