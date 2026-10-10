"""Per-identity token buckets for overall, answer, capture, and deletion traffic, and a per-address
throttle on failed authentication."""
import collections
import ipaddress
import os
import threading
import time

from stigmergy.server.errors import RateLimitError

DEFAULT_OVERALL_PER_MIN = 120
DEFAULT_ASK_PER_MIN = 10
DEFAULT_DELETE_PER_MIN = 3
# Captures spend model tokens, so they keep their own budget under the read allowance.
DEFAULT_CAPTURE_PER_MIN = 30
AUTH_FAILURES = 10
AUTH_FAILURE_WINDOW_SECONDS = 15 * 60
_MAX_TRACKED_ADDRESSES = 10_000


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
                 delete_per_min: int = DEFAULT_DELETE_PER_MIN,
                 capture_per_min: int = DEFAULT_CAPTURE_PER_MIN, clock=time.monotonic):
        self.overall_per_min = overall_per_min
        self.ask_per_min = ask_per_min
        self.delete_per_min = delete_per_min
        self.capture_per_min = capture_per_min
        self._clock = clock
        self._lock = threading.Lock()
        self._overall: dict[str, _Bucket] = {}
        captures = (self.capture_per_min, {})
        self._extra: dict[str, tuple[int, dict[str, _Bucket]]] = {
            "ask": (self.ask_per_min, {}),
            "brain_delete": (self.delete_per_min, {}),
            "brain_submit": captures,
            "brain_upload_finalize": captures,
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


def client_address(scope) -> str:
    """The caller's address for throttling: Fly's edge header on Fly, where the edge sets it, and
    an IPv6 caller's /64, since one host controls the whole prefix."""
    address = ""
    if os.environ.get("FLY_APP_NAME"):
        forwarded = [v.decode("latin-1") for k, v in scope.get("headers") or []
                     if k.lower() == b"fly-client-ip"]
        if len(forwarded) == 1:
            address = forwarded[0].strip()
    if not address:
        client = scope.get("client")
        address = str(client[0]) if client else "unknown"
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    if parsed.version == 6:
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


class AuthFailureThrottle:
    """Refuse authentication from an address after repeated failures inside a sliding window."""

    def __init__(self, failures: int = AUTH_FAILURES, window: float = AUTH_FAILURE_WINDOW_SECONDS,
                 clock=time.monotonic):
        self._failures = failures
        self._window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: collections.OrderedDict[str, collections.deque] = collections.OrderedDict()

    def _recent(self, address: str, now: float) -> collections.deque:
        stamps = self._seen.get(address)
        if stamps is None:
            return collections.deque()
        while stamps and now - stamps[0] > self._window:
            stamps.popleft()
        return stamps

    def blocked(self, address: str) -> bool:
        with self._lock:
            return len(self._recent(address, self._clock())) >= self._failures

    def failed(self, address: str) -> None:
        with self._lock:
            now = self._clock()
            stamps = self._recent(address, now)
            stamps.append(now)
            self._seen[address] = stamps
            self._seen.move_to_end(address)
            while len(self._seen) > _MAX_TRACKED_ADDRESSES:
                self._seen.popitem(last=False)
