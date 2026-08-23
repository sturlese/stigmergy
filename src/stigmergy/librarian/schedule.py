"""The night shift: the worker's own maintenance schedule, in one place.

Every unattended pass this deployment runs is declared HERE — what it is called, when it is due,
and what it costs — and `worker.Worker` asks this module rather than growing a fourth
`maybe_<something>` method with its own copy of the interval arithmetic. That copy is what this
module exists to prevent: the view sweep and the repair pass each grew one, and the second was
written by reading the first.

**Everything runs on the IDLE branch, and only there**. A queued capture is somebody
waiting; a maintenance pass is not, so no pass may start while the queue has work and every pass
that runs is handed a `should_stop` it consults between units. What that buys is the property the
crons could never have: maintenance cannot delay a filing, because it does not run while there is
one to do.

**Two shapes of due-ness, and the difference is not cosmetic.** An INTERVAL pass ("every 15
minutes") converges something and its cost is proportional to what changed — the view sweep, the
repair pass. A DAILY pass ("at 05:07 UTC") is a whole-corpus sweep whose cost is proportional to
the corpus, and running one every fifteen minutes would be a bill nobody asked for. A daily pass
also has to survive a restart: the worker is not a cron, so "due" is answered from the last
`job_runs` row rather than from an in-process timer, and a container that restarts at 05:06 does
not run yesterday's pass twice.

**What is NOT here, and why.** The index REBUILD is not a pass, and cannot be: the deployed
worker's environment has no embedding key at all — `bootstrap.READ_PATH_ONLY_ENV` strips it before
exec, deliberately, so that the write path cannot reach the read path's credential. Rebuilding
stays an operator command run with the key exported, and the admin console's Jobs page names that
command rather than offering a button that could only ever fail. Drift between the corpus and the
served index is read on the console's Index page, which lints the LIVE index on demand.
"""
import datetime
import logging

from stigmergy.capture import ops

log = logging.getLogger(__name__)

UTC = datetime.UTC

# The daily passes' own clock, in UTC minutes past midnight. Spelled as a time rather than an
# interval because that is what an operator reasons about ("the garden runs at five past five"),
# and because a corpus-sized pass wants to land when nobody is capturing.
#
# Staggered, and the order is the dependency: the garden writes findings, and the repair pass
# answers them — so the garden goes first and the repair pass, which runs on its own interval,
# picks them up on its next tick. Retention purges last, when the day's work is recorded.
DEFAULT_GARDEN_AT = "05:07"
DEFAULT_RETENTION_AT = "04:42"

# How wide a window a daily pass may still fire in, once due. A worker that was down at 05:07 and
# came back at 05:20 should still garden today; one that comes back at 23:00 should not, because a
# pass that lands twelve hours late is a pass whose findings the day's own repair passes never saw.
# Deliberately generous enough for a deploy and a restart, and far short of a day.
DUE_WINDOW_MINUTES = 180


def parse_daily(value: str, *, default: str) -> tuple[int, int]:
    """`"HH:MM"` → `(hour, minute)`, falling back to `default` for anything unreadable.

    Falls back rather than raising, and that is a deliberate asymmetry with every other setting in
    this package: a malformed interval is a startup refusal because a worker that polls wrong is
    worse than one that does not start, while a malformed daily time only decides WHEN a
    maintenance pass runs. Refusing to start a worker over it would trade a filing outage for a
    scheduling typo. The fallback is logged, so it is not silent.
    """
    raw = str(value or "").strip() or default
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    log.warning("unreadable daily schedule %r — using %s instead", value, default)
    return parse_daily(default, default="00:00")


def daily_due(now: datetime.datetime, last_run: datetime.datetime | None,
              at: tuple[int, int], *, window_minutes: int = DUE_WINDOW_MINUTES) -> bool:
    """Is a daily pass due at `now`, given when it last ran?

    Three conditions, and each is one way a naive answer goes wrong:

    - **past today's time**, so it does not fire at 04:00 for a 05:07 pass;
    - **inside the window**, so a worker that starts at 23:00 does not run a pass whose whole
      point was to be followed by a day of repair passes;
    - **not already run today**, answered from the LAST RUN rather than a flag, so a restart at
      05:08 does not garden a second time. This is the condition a cron gets for free and a
      long-lived process has to earn.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    scheduled = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    if now < scheduled:
        return False
    if (now - scheduled) > datetime.timedelta(minutes=window_minutes):
        return False
    if last_run is None:
        return True
    last = last_run.astimezone(UTC) if last_run.tzinfo else last_run.replace(tzinfo=UTC)
    return last < scheduled


def last_run_at(conn, job: str) -> datetime.datetime | None:
    """When `job` last ran, whatever its outcome — the durable half of `daily_due`.

    WHATEVER its outcome, deliberately: a pass that ERRORED did run, and re-running it on the next
    idle tick would turn one bad night into a loop. The `job_runs` row is what an operator reads
    either way.
    """
    row = ops.latest_run(conn, job)
    if row is None:
        return None
    return row.get("finished_at") or row.get("started_at")
