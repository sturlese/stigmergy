"""Per-identity rate limiting: a token bucket caps how much of the server — and the OpenAI spend
behind it — a single leaked token can drain. Two buckets on every call the service layer wraps:
`overall` (30/min, every tool call, including the reads an `ask` run makes internally) and `ask`
(an ADDITIONAL 10/min for the expensive synthesizer).

`propose_per_min` is accepted and stored but NOTHING spends it — a deliberate dead knob, kept as
the shape for the next expensive tool: one line in `_extra`, never a third copy of the branch in
`check`. The clock is injectable (`clock=`) so tests pin the boundary without real sleeps.
"""
import time

from stigmergy.server.errors import RateLimitError

DEFAULT_OVERALL_PER_MIN = 30
DEFAULT_ASK_PER_MIN = 10
DEFAULT_PROPOSE_PER_MIN = 5
# `brain_delete` is the most expensive call this server serves — a clone, a model call, gitleaks, a
# whole-repo lint and a push, each pinning a worker thread for its duration — and it is the one
# that WRITES. Stricter than `ask` because a person deleting pages does it a handful of times in a
# sitting, and a caller spending this bucket is a caller holding threads nobody else can use.
DEFAULT_DELETE_PER_MIN = 3


class _Bucket:
    """A continuous token bucket: starts full (30th immediate call ok, 31st refused), refills at
    `capacity` tokens/minute. `available`/`consume` are split so `check()` can peek BOTH buckets
    before committing either — a refusal never over-charges the other bucket."""

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
    """Process-wide, shared across every identity and (for HTTP) every request — construct one
    instance at startup and inject it into every `BrainService` (stdio: one; HTTP: one per
    request, all pointed at the same limiter, so the budget is honestly per-identity across the
    whole process, not per connection)."""

    def __init__(self, overall_per_min: int = DEFAULT_OVERALL_PER_MIN,
                 ask_per_min: int = DEFAULT_ASK_PER_MIN,
                 propose_per_min: int = DEFAULT_PROPOSE_PER_MIN,
                 delete_per_min: int = DEFAULT_DELETE_PER_MIN, clock=time.monotonic):
        self.overall_per_min = overall_per_min
        self.ask_per_min = ask_per_min
        self.propose_per_min = propose_per_min
        self.delete_per_min = delete_per_min
        self._clock = clock
        self._overall: dict[str, _Bucket] = {}
        # One extra, stricter bucket per named expensive tool, on top of the shared overall one —
        # the next such tool is one line here, never a second copy of the branch in `check`.
        self._extra: dict[str, tuple[int, dict[str, _Bucket]]] = {
            "ask": (self.ask_per_min, {}),
            "brain_delete": (self.delete_per_min, {}),
        }

    def check(self, identity: str, tool: str) -> None:
        """Raise `RateLimitError` when `identity` is over budget. Every tool spends the shared
        overall bucket; a tool named in `self._extra` additionally spends its own stricter one.
        Both buckets are checked BEFORE either is consumed — a refusal leaves both untouched."""
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
