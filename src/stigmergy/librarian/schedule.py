"""Durable UTC scheduling for the writer's daily garden and index rebuild of each tenant."""
import datetime
import logging

from stigmergy.capture import ops

log = logging.getLogger(__name__)

UTC = datetime.UTC

DEFAULT_GARDEN_AT = "05:07"
DEFAULT_REBUILD_AT = "04:17"

def parse_daily(value: str, *, default: str) -> tuple[int, int]:
    """Parse ``HH:MM`` and log a fallback without taking the writer offline."""
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
              at: tuple[int, int]) -> bool:
    """Whether the latest scheduled time has passed without a run since. The writer sleeps when
    idle and nothing wakes it at the scheduled hour, so a missed run is caught up when it wakes."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    scheduled = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    if now < scheduled:
        scheduled -= datetime.timedelta(days=1)
    if last_run is None:
        return True
    last = last_run.astimezone(UTC) if last_run.tzinfo else last_run.replace(tzinfo=UTC)
    return last < scheduled


def last_run_at(conn, job: str) -> datetime.datetime | None:
    """Return the last started or finished run, regardless of outcome."""
    row = ops.latest_run(conn, job)
    if row is None:
        return None
    value = row.get("finished_at") or row.get("started_at")
    return datetime.datetime.fromisoformat(value) if isinstance(value, str) else value
