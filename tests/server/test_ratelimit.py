"""`RateLimiter` — the per-identity token bucket, unit-tested with an injectable fake clock so the
boundary itself (30th ok, 31st refused) is exact rather than wall-clock-dependent. Pure unit
tests: no Postgres, no HTTP — the seam is `clock=` on the constructor. No database needed, so this
file runs unconditionally (no `indexed`/`fixture` dependency)."""
import pytest

from stigmergy.server.errors import RateLimitError
from stigmergy.server.ratelimit import RateLimiter


class FakeClock:
    """A controllable `time.monotonic`-shaped clock: starts at 0, advances only when told."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ── the overall bucket: 30/min, exactly on the boundary ────────────────────────────────────────
def test_overall_bucket_30th_ok_31st_refused():
    clock = FakeClock()
    limiter = RateLimiter(overall_per_min=30, ask_per_min=10, clock=clock)
    for _ in range(30):
        limiter.check("steward@example.com", "search_brain")   # 1..30 all succeed, no time passing
    with pytest.raises(RateLimitError, match="30 requests/min"):
        limiter.check("steward@example.com", "search_brain")   # the 31st, same minute, is refused


def test_rate_limit_error_message_is_generic_static_and_names_no_identity():
    """The message an HTTP/stdio caller sees must carry no identity, path or internal detail — a
    static template with the configured integer and nothing else."""
    clock = FakeClock()
    limiter = RateLimiter(overall_per_min=1, clock=clock)
    limiter.check("ana@example.com", "search_brain")
    with pytest.raises(RateLimitError) as exc_info:
        limiter.check("ana@example.com", "search_brain")
    message = str(exc_info.value)
    assert message == "rate limited: 1 requests/min exceeded — wait a moment and retry"
    assert "ana@example.com" not in message


# ── the ask bucket: an ADDITIONAL, stricter 10/min budget, spent only by `ask` ───────────────────
def test_ask_bucket_is_additional_and_stricter_than_overall():
    clock = FakeClock()
    limiter = RateLimiter(overall_per_min=30, ask_per_min=10, clock=clock)
    for _ in range(10):
        limiter.check("steward@example.com", "ask")   # spends BOTH the ask bucket and the overall one
    with pytest.raises(RateLimitError, match="10 ask requests/min"):
        limiter.check("steward@example.com", "ask")   # the ask bucket is exhausted first (10 < 30)


# ── a refusal must never over-charge ───────────────────────────────────────────────────────────
def test_ask_refused_by_its_own_bucket_does_not_consume_the_overall_bucket():
    """`check()` used to consume the overall bucket FIRST, then separately check/spend the ask
    bucket — so a call refused by the (stricter) ask bucket had already spent an overall token it
    never got to use, silently shrinking the budget every OTHER tool could draw on. The property
    that closes it: the ONE ask call that actually ran spends exactly one overall
    token — a refused one spends none — so exactly 30 total calls (1 ask + 29 more) fit before the
    31st is refused, not 30 total ATTEMPTS (1 successful ask + 1 refused ask + 28 more)."""
    clock = FakeClock()
    limiter = RateLimiter(overall_per_min=30, ask_per_min=1, clock=clock)
    limiter.check("steward@example.com", "ask")            # the ONE ask call that runs: spends 1 overall + 1 ask
    with pytest.raises(RateLimitError, match="1 ask requests/min"):
        limiter.check("steward@example.com", "ask")        # ask bucket empty -> refused; must spend NEITHER bucket
    for _ in range(29):
        limiter.check("steward@example.com", "search_brain")   # 29 more fit -> 30 total spent, as expected
    with pytest.raises(RateLimitError, match="30 requests/min"):
        limiter.check("steward@example.com", "search_brain")   # the 31st overall call is refused, right on budget





def test_a_refusal_from_the_overall_bucket_also_never_touches_the_ask_bucket():
    """The symmetric case, characterizing `check()`'s order rather than re-proving the
    over-charging bug above: `check()` evaluates the overall bucket before ever consulting the ask
    one (that was always true — the bug was specifically about consuming the overall bucket on a
    call the ASK check then refused, not this direction), so when overall itself refuses, the ask
    bucket is never even looked at. Proof needs care: after a FULL minute both
    buckets would look identical (fully refilled) regardless, since refill is capped at capacity —
    so this advances the clock by exactly enough to regain ONE overall token (fast refill:
    overall_per_min=60) while the SLOW ask bucket (ask_per_min=1, refills roughly 0.017 tokens in
    that same second) would stay visibly short of a full token had it been touched."""
    clock = FakeClock()
    limiter = RateLimiter(overall_per_min=60, ask_per_min=1, clock=clock)
    for _ in range(60):
        limiter.check("steward@example.com", "search_brain")   # drain the overall bucket to 0
    with pytest.raises(RateLimitError, match="60 requests/min"):
        limiter.check("steward@example.com", "ask")   # refused by OVERALL; the still-full ask bucket
        # must NOT be spent here — this call site never even reaches the ask check (overall raises
        # first), but a regression that reordered or merged the two checks could change that.
    clock.advance(1.0)   # overall regains exactly 1 token; ask regains ~0.017 — negligible either way
    limiter.check("steward@example.com", "ask")   # succeeds ONLY if the ask bucket is still at its full 1


def test_ask_calls_also_spend_the_shared_overall_bucket():
    """ADR 013 §6: `ask`'s OWN internal search/read calls run through
    the same `BrainService` methods, so the overall bucket also throttles ask's fan-out cost — this
    unit proves the primitive half of that claim: an `ask` call consumes the SAME overall bucket a
    `search_brain` call would, not a separate one."""
    clock = FakeClock()
    limiter = RateLimiter(overall_per_min=2, ask_per_min=10, clock=clock)
    limiter.check("steward@example.com", "ask")           # spends 1/2 overall + 1/10 ask
    limiter.check("steward@example.com", "search_brain")  # spends 2/2 overall
    with pytest.raises(RateLimitError, match="2 requests/min"):
        limiter.check("steward@example.com", "read_page")  # overall exhausted, even though ask bucket isn't



# ── buckets are per-identity: one leaked/exhausted token never throttles anyone else ────────────
def test_buckets_are_independent_per_identity():
    clock = FakeClock()
    limiter = RateLimiter(overall_per_min=1, clock=clock)
    limiter.check("ana@example.com", "search_brain")
    with pytest.raises(RateLimitError):
        limiter.check("ana@example.com", "search_brain")
    limiter.check("eng@example.com", "search_brain")   # a different identity, unaffected


# ── refill: the bucket is continuous, not a hard per-minute reset ──────────────────────────────
def test_refill_over_time_restores_tokens():
    clock = FakeClock()
    limiter = RateLimiter(overall_per_min=60, clock=clock)   # 1 token/second, easy to reason about
    for _ in range(60):
        limiter.check("steward@example.com", "search_brain")
    with pytest.raises(RateLimitError):
        limiter.check("steward@example.com", "search_brain")
    clock.advance(1.0)                                       # 1 second later: exactly 1 token back
    limiter.check("steward@example.com", "search_brain")        # succeeds
    with pytest.raises(RateLimitError):
        limiter.check("steward@example.com", "search_brain")    # and only that one


def test_refill_never_exceeds_capacity():
    clock = FakeClock()
    limiter = RateLimiter(overall_per_min=30, clock=clock)
    clock.advance(3600)   # an hour of "idle" before the first call ever happens
    for _ in range(30):
        limiter.check("steward@example.com", "search_brain")   # the bucket never over-fills past 30
    with pytest.raises(RateLimitError):
        limiter.check("steward@example.com", "search_brain")
