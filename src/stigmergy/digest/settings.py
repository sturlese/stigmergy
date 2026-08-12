"""Runtime configuration for `stigmergy-digest`. `DigestSettings.from_args` is the one place the
environment is consulted; modules never read it at import time.

The channel and bot-token env NAMES are defined in `gardener.settings` and re-exported here —
one spelling per literal, and every other digest module imports them from THIS module, the one
funnel into gardener. `dsn`/`repo`/`channels` are deliberately not settings — connection/location
arguments `cli.py` reads off `args`.
"""
import os
from dataclasses import dataclass

from stigmergy.gardener.settings import DIGEST_CHANNEL_ID_ENV, SLACK_BOT_TOKEN_ENV
from stigmergy.server.errors import StartupError

__all__ = ["DIGEST_CHANNEL_ID_ENV", "SLACK_BOT_TOKEN_ENV", "WINDOW_DAYS_ENV",
          "DEFAULT_WINDOW_DAYS", "DigestSettings"]

# ── the window's own default ───────────────────────────────────────────────────────────────────
WINDOW_DAYS_ENV = "STIGMERGY_DIGEST_WINDOW_DAYS"
# Used only on a genuine first-ever run (no watermark, no `--since`). Grep-asserted: this literal
# may not appear outside this module.
DEFAULT_WINDOW_DAYS = 7


def _int_setting(env_name: str, default: int) -> int:
    """Hand-mirrors `gardener.settings._int_setting` (module-private there; one small validator
    does not earn a cross-package edge)."""
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise StartupError(
            f"${env_name}={raw!r} is not a valid integer — unset it to use the default "
            f"({default}) or set it to a positive whole number.") from None
    if value <= 0:
        raise StartupError(
            f"${env_name}={value} must be a positive integer — a zero or negative window would "
            f"make every digest cover no time at all, or run backwards. Unset it to use the "
            f"default ({default}) or set it to a positive integer.")
    return value


@dataclass(frozen=True)
class DigestSettings:
    # Named `digest_channel_id` — the bare identifier is banned below `stigmergy.slack`
    # (`test_no_slack_identifiers_below_the_slack_package`). Empty is honest: a `--dry-run` never
    # needs it; required only when a real post happens (`run._require_channel`).
    digest_channel_id: str = ""
    window_days: int = DEFAULT_WINDOW_DAYS

    @classmethod
    def from_args(cls, args=None) -> "DigestSettings":
        """`args` is accepted, not consulted — env-tunable only; kept for the convention's
        shape."""
        return cls(
            digest_channel_id=os.environ.get(DIGEST_CHANNEL_ID_ENV, ""),
            window_days=_int_setting(WINDOW_DAYS_ENV, DEFAULT_WINDOW_DAYS),
        )
