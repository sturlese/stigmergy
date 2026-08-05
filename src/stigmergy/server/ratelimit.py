"""Per-identity rate limiting: a token bucket caps how much of the server — and the OpenAI spend
behind it — a single leaked token can drain.

TWO buckets are consulted on every call the service layer wraps (`BrainService._call` /
`.call_async` — one seam, both transports):

- `overall`: 30 requests/min, spent by every tool call (`search_brain`, `read_page`,
  `list_entities`, `ask`, `review_decide`, ...) — including the read calls an `ask` run makes
  internally through the same `BrainService` methods, so the budget also throttles `ask`'s
  fan-out cost, not just its own call count.
- `ask`: an ADDITIONAL 10 requests/min, spent only by `ask` itself (the OpenAI-backed
  synthesizer is the expensive resource behind a public URL).

`propose_per_min` is accepted and stored, but NOTHING spends it: `self._extra` registers `ask`
alone, and no tool is keyed to a propose bucket. It survives as the shape for the next expensive
write tool, because the tool it was written for — a worktree, the eight code gates, gitleaks over
the worktree, a linter SUBPROCESS and a real remote push — was the one entry point on this seam
with no cost bucket of its own at all, sharing the same 30/min budget as every read tool. Adding
that next tool should be one line in `_extra`, never a third copy of the branch in `check`.

The clock is injectable (constructor `clock=`) so tests can drive the bucket deterministically
without real sleeps, which is how the boundary (30th ok, 31st refused) is pinned.
"""
import time

from stigmergy.server.errors import RateLimitError

DEFAULT_OVERALL_PER_MIN = 30
DEFAULT_ASK_PER_MIN = 10
DEFAULT_PROPOSE_PER_MIN = 5


class _Bucket:
    """A continuous token bucket: starts full (so the (N+1)th immediate call is the first
    refusal — 30th ok, 31st refused), refills at `capacity` tokens/minute.

    `available`/`consume` are split on purpose: `check()` below peeks BOTH buckets `ask` needs
    before committing either, so a call refused by the ask bucket never over-charges the overall
    one — refilling in `available` is idempotent (safe to call without following it with
    `consume`), so peeking costs nothing."""

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
                 propose_per_min: int = DEFAULT_PROPOSE_PER_MIN, clock=time.monotonic):
        self.overall_per_min = overall_per_min
        self.ask_per_min = ask_per_min
        self.propose_per_min = propose_per_min
        self._clock = clock
        self._overall: dict[str, _Bucket] = {}
        # One extra, stricter bucket per named expensive tool, on top of the shared overall one.
        # A dict of (capacity, per-identity buckets) rather than a hand-written `if tool == ...`
        # per entry: every such tool is the same SHAPE of exception to "every tool spends the
        # shared bucket and nothing more", so the next one is one line here rather than a second
        # copy of the branch below.
        self._extra: dict[str, tuple[int, dict[str, _Bucket]]] = {
            "ask": (self.ask_per_min, {}),
        }

    def check(self, identity: str, tool: str) -> None:
        """Raise `RateLimitError` when `identity` is over budget for this call. Every tool spends
        the shared overall bucket; a tool named in `self._extra` additionally spends its own
        stricter bucket.

        Both buckets `this call` needs are checked for availability BEFORE either is consumed:
        a refusal — from either bucket — leaves BOTH untouched, so a call that never ran never
        spends any part of next minute's budget either."""
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
