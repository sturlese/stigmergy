"""The operator dialect: queue depth, a measured duration, an age, and untrusted captured text
on its way to a terminal.

Below every CLI, because two of its readers are not CLIs at all: `capture.latency` renders a
summary with `format_ms` and is imported by `server.pilot_report`, so a serving process would
otherwise pull `stigmergy-queue` — connection seam, environment reads and all — in to format a
number. `stigmergy-librarian` and `stigmergy-queue` import these so two tools in one
operator's terminal print one dialect.

Imports `stigmergy.text` and nothing else: `tests/test_architecture.py` reserves the reach into
`stigmergy.index` for the three operator CLIs, and this module is not one of them.
"""
from stigmergy import text as textutil

# How an operator gets a stranded claim back RIGHT NOW, written once because its argument is
# subtle: `--visibility-timeout <lease>` releases nothing at second zero. `stigmergy-librarian`
# imports this constant rather than retyping the command.
RECLAIM_NOW = "stigmergy-queue reclaim --visibility-timeout 0"


def depth_line(counts: dict[str, int]) -> str:
    """`queue: queued=3 · claimed=1` — non-zero statuses only, or `queue: empty`. Zeroes are
    dropped on purpose: printing all eight statuses would bury the one or two that matter."""
    depth = " · ".join(f"{status}={n}" for status, n in counts.items() if n)
    return f"queue: {depth or 'empty'}"


def format_ms(value) -> str:
    """A MEASURED duration as one number a person reads: `4.2s`, or `—`. Not
    `worker.human_duration`, which renders a CONFIGURED value and must keep the raw seconds."""
    return "—" if value is None else f"{value / 1000:.1f}s"


def format_age(ms) -> str:
    """How long something has been waiting, as a person says it: `12 min`, `3h`, `1d 2h`.
    An AGE, where sub-second precision is noise — `format_ms` renders a measured latency,
    `worker.human_duration` a configured value.
    """
    if ms is None:
        return "—"
    minutes = int(max(0.0, float(ms)) // 60000)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if not minutes else f"{hours}h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days}d" if not hours else f"{days}d {hours}h"


def clean_for_terminal(text: str, width: int = 0) -> str:
    """Untrusted captured text on its way to a terminal: control characters stripped (a capture
    can contain ANSI escapes), newlines flattened, clipped word-safe — a hard slice through a
    command printed under "run this" is an invalid call, and a message containing a command is an
    executable promise."""
    return textutil.clamp(textutil.sanitize(text or "").replace("\n", " ⏎ "), width)
