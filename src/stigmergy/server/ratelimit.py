"""Per-identity token buckets for overall, answer, and deletion traffic."""
import threading
import time

from stigmergy.server.errors import RateLimitError

DEFAULT_OVERALL_PER_MIN = 30
DEFAULT_ASK_PER_MIN = 10
DEFAULT_DELETE_PER_MIN = 3


class _Bucket:
    """A continuous token bucket that starts full and refills per minute."""

    __slots__ = ("capacity", "tokens", "last")

    def __init__(self, capacity: int, now: float):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last = now

    def available(self, now: float) -> bool:
        elapsed = max(0.0, now - self.last)
        self.tokens = min(self.capacity, self.tokens + elapsed * (self.capacity / 60.0))
        self.last = now
        return self.tokens >= 1.0

    def consume(self) -> None:
        self.tokens -= 1.0


class RateLimiter:
    """Process-wide rate limits partitioned by identity."""

    def __init__(self, overall_per_min: int = DEFAULT_OVERALL_PER_MIN,
                 ask_per_min: int = DEFAULT_ASK_PER_MIN,
                 delete_per_min: int = DEFAULT_DELETE_PER_MIN, clock=time.monotonic):
        self.overall_per_min = overall_per_min
        self.ask_per_min = ask_per_min
        self.delete_per_min = delete_per_min
        self._clock = clock
        self._lock = threading.Lock()
        self._overall: dict[str, _Bucket] = {}
        self._extra: dict[str, tuple[int, dict[str, _Bucket]]] = {
            "ask": (self.ask_per_min, {}),
            "brain_delete": (self.delete_per_min, {}),
        }

    def check(self, identity: str, tool: str) -> None:
        """Spend all applicable buckets or raise without consuming either one."""
        with self._lock:
            self._check(identity, tool)

    def _check(self, identity: str, tool: str) -> None:
        now = self._clock()
        overall = self._bucket(self._overall, identity, self.overall_per_min, now)
        extra_bucket, extra_capacity = None, 0
        if tool in self._extra:
            extra_capacity, buckets = self._extra[tool]
            extra_bucket = self._bucket(buckets, identity, extra_capacity, now)

        if not overall.available(now):
            raise RateLimitError(
                f"rate limited: {self.overall_per_min} requests/min exceeded — wait a moment "
                "and retry")
        if extra_bucket is not None and not extra_bucket.available(now):
            raise RateLimitError(
                f"rate limited: {extra_capacity} {tool} requests/min exceeded — wait a moment "
                "and retry")

        overall.consume()
        if extra_bucket is not None:
            extra_bucket.consume()

    def _bucket(self, buckets: dict[str, _Bucket], identity: str, capacity: int,
               now: float) -> _Bucket:
        bucket = buckets.get(identity)
        if bucket is None:
            bucket = buckets[identity] = _Bucket(capacity, now)
        return bucket
