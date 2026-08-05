"""Runtime configuration for `stigmergy-digest`: the digest window's own default, env-tunable per
the `Settings.from_args` convention (`server/settings.py` states it; `gardener/settings.py` applies
it one package over) — modules never read the environment at import time;
`DigestSettings.from_args` is the ONE place env fallbacks are consulted.

**The channel and bot-token env NAMES are defined in `gardener.settings`, not here, and
re-exported.** `STIGMERGY_DIGEST_CHANNEL_ID` is the ONE name both `gardener` (the SLA notice) and
`digest` read: one channel, one place to look, and each SIDE imports the name rather than spelling
a second independent copy of the literal. `SLACK_BOT_TOKEN_ENV` is re-exported for the identical
reason: `gardener.settings` already hand-mirrors it once from `slack.settings.BOT_TOKEN_ENV` (with
its own stated rationale); a second, independent hand-mirror here would be a THIRD copy of the same
literal. Every other `digest` module that needs either name imports it from HERE, never straight
from `gardener.settings` — this module is the one funnel, so "which digest module reaches into
gardener" stays answerable by reading one file.

Deliberately does NOT carry `dsn`/`repo`/`channels` — those are connection/location arguments
`cli.py` reads directly off `args` (mirroring `gardener/settings.py`'s identical posture, which
itself mirrors `views/cli.py`), not tunable behaviour.
"""
import os
from dataclasses import dataclass

from stigmergy.gardener.settings import DIGEST_CHANNEL_ID_ENV, SLACK_BOT_TOKEN_ENV
from stigmergy.server.errors import StartupError

__all__ = ["DIGEST_CHANNEL_ID_ENV", "SLACK_BOT_TOKEN_ENV", "WINDOW_DAYS_ENV",
          "DEFAULT_WINDOW_DAYS", "DigestSettings"]

# ── the window's own default ───────────────────────────────────────────────────────────────────
WINDOW_DAYS_ENV = "STIGMERGY_DIGEST_WINDOW_DAYS"
# Used only when NEITHER a watermark (the latest completed `job='digest'` run) NOR an explicit
# `--since` is available — a genuine first-ever run (`run.py::_resolve_since`). Grep-asserted
# (`tests/test_architecture.py`, mirroring the fence-literal ban and `gardener`'s own threshold
# scan): this literal may not appear outside this module's own constants/defaults.
DEFAULT_WINDOW_DAYS = 7


def _int_setting(env_name: str, default: int) -> int:
    """Mirrors `gardener.settings._int_setting` exactly (hand-mirrored, not imported — that
    function is module-private, and a single small validator does not earn a new shared-utility
    edge between two sibling packages that otherwise declare none)."""
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
    # Named `digest_channel_id`, never the bare word this comment is carefully NOT using twice:
    # that identifier is banned anywhere below `stigmergy.slack` (`tests/test_architecture.py::
    # test_no_slack_identifiers_below_the_slack_package`), and `GardenerSettings` already set the
    # precedent this field matches on purpose. Empty is a real, honest state: a `--dry-run` never
    # needs it. Required only at the moment a REAL post is about to happen — see `run.py::
    # _require_channel`, checked lazily, never at startup.
    digest_channel_id: str = ""
    window_days: int = DEFAULT_WINDOW_DAYS

    @classmethod
    def from_args(cls, args=None) -> "DigestSettings":
        """`args` is accepted, not consulted — no flag overrides either of these (env-tunable
        only, the same posture `GardenerSettings.from_args` takes for its own thresholds).
        Kept for the convention's own sake, and so a future flag can be added here without a
        rename."""
        return cls(
            digest_channel_id=os.environ.get(DIGEST_CHANNEL_ID_ENV, ""),
            window_days=_int_setting(WINDOW_DAYS_ENV, DEFAULT_WINDOW_DAYS),
        )
