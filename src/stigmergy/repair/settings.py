"""Runtime configuration for a page removal — env-tunable, read in ONE place.

`RepairSettings.from_env` is the only function in this package that consults the environment, and
modules never read it at import time (`tests/test_architecture.py`). `dsn` is deliberately not
here: a connection argument, not tunable behaviour.

The bound below is the removal's blast radius, and it belongs to code rather than to the skill: a
brief can be argued with and a constant cannot. It carries weight because nobody reads the prose
the sweep writes before it lands.
"""
import os
from dataclasses import dataclass

from stigmergy.librarian import config as librarian_config
from stigmergy.server.errors import StartupError

MODEL_ENV = "STIGMERGY_REPAIR_MODEL"

# The librarian's own default, deliberately: the sweep writer reads pages and rewrites the ones
# that pointed at a page that is going, in the same prose the filing agent writes, so a deployment
# that has settled on a model for one has settled on it for the other. `$STIGMERGY_REPAIR_MODEL`
# is how an operator disagrees.
DEFAULT_REPAIR_MODEL = librarian_config.DEFAULT_MODEL

# How much ONE removal may be, measured in the bytes its plan carries — a sweep holds every page it
# would rewrite in full, so the apply can recompute the plan and byte-compare it, which makes a
# size the natural bound rather than a count of ops. Around thirty average pages at the default,
# which is already more of a corpus change than one Remove button should stand for.
MAX_PLAN_BYTES_ENV = "STIGMERGY_REPAIR_MAX_PLAN_BYTES"
DEFAULT_MAX_PLAN_BYTES = 100_000

# What a zero or negative value would do to THIS package's arithmetic — the sentence
# `gardener.settings.int_setting` interpolates, written for the bound it guards here.
_POSITIVE_COUNT_WHY = "a zero or negative bound would refuse every removal."


def _int_setting(env_name: str, default: int, *, maximum: int | None = None) -> int:
    """`gardener.settings.int_setting`'s rules, spelled here rather than imported: importing it
    would put a `stigmergy.gardener.settings` edge on this package for one validator, and this
    package reaches the gardener for nothing at all. Two callers, one shape — the parity
    test in `tests/repair/test_settings_parity.py` is what keeps a rule from landing on one copy
    only, which is exactly how this one's `maximum` arrived late."""
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
            f"${env_name}={value} must be a positive integer — {_POSITIVE_COUNT_WHY} Unset it to "
            f"use the default ({default}) or set it to a positive integer.")
    if maximum is not None and value > maximum:
        raise StartupError(
            f"${env_name}={value} is above the maximum of {maximum} — unset it to use the default "
            f"({default}) or set it to a whole number between 1 and {maximum}.")
    return value


@dataclass(frozen=True)
class RepairSettings:
    """The two knobs a removal has: which model writes the pages that stay, and how large a plan
    one removal may be."""

    model: str = DEFAULT_REPAIR_MODEL
    max_plan_bytes: int = DEFAULT_MAX_PLAN_BYTES

    @classmethod
    def from_env(cls) -> "RepairSettings":
        return cls(
            model=os.environ.get(MODEL_ENV) or DEFAULT_REPAIR_MODEL,
            max_plan_bytes=_int_setting(MAX_PLAN_BYTES_ENV, DEFAULT_MAX_PLAN_BYTES),
        )
