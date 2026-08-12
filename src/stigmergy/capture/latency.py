"""capture -> filed and capture -> searchable latency, measured rather than claimed.

Lives in `stigmergy.capture`, not `stigmergy.librarian`: `server.pilot_report` needs it and
`server` may not import `librarian`; pure, so both callers reach it here. The samples come off
the queue rows' own timestamps — nothing to remember to write, nothing that can disagree with
the trace `stigmergy-queue show` prints.

Below `MIN_SAMPLES` the answer is a sentence, not a number: a "p95" of three samples printed to
one decimal place reads as a measurement nobody should believe.
"""
from dataclasses import dataclass, field

from stigmergy.capture import cli as queue_cli

# The floor below which no percentile is reported at all.
MIN_SAMPLES = 10

# Reported percentiles, in print order: p50 is the typical experience, p95 the one people
# complain about; a p99 over a few hundred samples is one sample.
PERCENTILES = (50, 95)


def percentile(values, q: float) -> float | None:
    """The `q`th percentile by linear interpolation between closest ranks — the
    `numpy.percentile` definition, written out rather than imported for six lines. `None` for an
    empty input: returning `0.0` would be a lie that renders as `0.0s`."""
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (float(q) / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


@dataclass(frozen=True)
class LatencySummary:
    """What was measured, and whether it may be believed. `enough` is a field, not a property,
    so the JSON carries the same judgment the prose does."""
    samples: int
    enough: bool
    min_samples: int = MIN_SAMPLES
    percentiles_ms: dict[int, float] = field(default_factory=dict)

    def as_json(self) -> dict:
        """The `--json` shape. Values are `None` below the threshold rather than absent, so the
        shape does not change with the data."""
        out = {"samples": self.samples, "enough_data": self.enough,
               "min_samples": self.min_samples}
        for q in PERCENTILES:
            out[f"p{q}_ms"] = self.percentiles_ms.get(q)
        return out


def summarize(latencies_ms, *, min_samples: int = MIN_SAMPLES) -> LatencySummary:
    """Percentiles over the samples — or an explicit refusal. Below `min_samples` the
    percentiles are left EMPTY, not computed and labelled: a caller that forgot to check
    `enough` renders nothing instead of a number off three samples."""
    samples = [float(v) for v in latencies_ms or ()]
    if len(samples) < max(1, int(min_samples)):
        return LatencySummary(samples=len(samples), enough=False, min_samples=int(min_samples))
    computed = {q: percentile(samples, q) for q in PERCENTILES}
    return LatencySummary(samples=len(samples), enough=True, min_samples=int(min_samples),
                          percentiles_ms={q: v for q, v in computed.items() if v is not None})


def render(summary: LatencySummary) -> str:
    """One line for a terminal. `queue_cli.format_ms` is imported, not reimplemented: the
    aggregate must not appear in a different unit or precision than `stigmergy-queue show`."""
    captures = f"{summary.samples} filed capture" + ("" if summary.samples == 1 else "s")
    if not summary.enough:
        return (f"capture->filed latency: not enough data yet — {captures} so far, "
                f"{summary.min_samples} needed before p50/p95 mean anything")
    parts = " · ".join(f"p{q}={queue_cli.format_ms(summary.percentiles_ms.get(q))}"
                       for q in PERCENTILES)
    return f"capture->filed latency: {parts} over {captures}"
