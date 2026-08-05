"""capture -> filed and capture -> searchable latency, measured rather than claimed.

This is the instrument the "capture->page p50 < 5 min" target is settled with, so the one thing
it must never do is produce a confident-looking number nobody should believe.

**This lives in `stigmergy.capture`, not in `stigmergy.librarian`.** `stigmergy-pilot-report` needs the
same percentile/rendering logic from `stigmergy.server`, which may not import `stigmergy.librarian`
(`tests/test_architecture.py`) — and this module is pure and depends only on `stigmergy.capture`
(`queue.filed_latencies_ms`, `cli.format_ms`), so it belongs at the layer both callers
(`librarian.cli`, `server.pilot_report`) can reach, not duplicated at each.

**Everything here is pure.** The samples come from `capture.queue.filed_latencies_ms`, which reads
`created_at` and `finished_at` off the queue rows and nothing else: no instrumentation, no second
clock, nothing the librarian has to remember to write, and therefore nothing that can be
inconsistent with the trace an operator can read for themselves with `stigmergy-queue show`. The
percentile arithmetic and the wording live here, where a test drives them with a list of floats and
no database at all.

**Below `MIN_SAMPLES` the answer is a sentence, not a number.** Three captures produce a "p95" that
is simply the slowest of the three, printed to one decimal place, and a number printed to one
decimal place is read as a measurement. Below ten captures the honest framing says how many samples
exist and how many are missing — an operator who wants the number can then go and create them,
which is the actual next step.
"""
from dataclasses import dataclass, field

from stigmergy.capture import cli as queue_cli

# The floor: a percentile is computed from the trace alone over >= 10 captures. Below it, no
# percentile is reported at all — see the module docstring.
MIN_SAMPLES = 10

# Which percentiles are reported, in the order they are printed. p50 is the typical experience and
# p95 is the one people actually complain about; a p99 over a few hundred samples is one sample.
PERCENTILES = (50, 95)


def percentile(values, q: float) -> float | None:
    """The `q`th percentile of `values` by linear interpolation between closest ranks.

    The same definition `numpy.percentile`/`statistics.quantiles(method="inclusive")` use, written
    out because this module has no reason to pull in a dependency for six lines, and because being
    explicit about the interpolation is what makes two runs over the same rows agree.

    `None` for an empty input: there is no 50th percentile of nothing, and returning `0.0` would be
    a lie that renders as `0.0s`.
    """
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
    """What was measured, and whether it may be believed.

    `enough` is a field rather than a computed property so the JSON a machine reads carries the same
    judgment the prose does — a consumer must not have to re-derive the threshold to know whether
    `p50_ms` means anything.
    """
    samples: int
    enough: bool
    min_samples: int = MIN_SAMPLES
    percentiles_ms: dict[int, float] = field(default_factory=dict)

    def as_json(self) -> dict:
        """The `--json` shape. Keys are strings (`"p50_ms"`) because that is what a JSON consumer
        expects to select on, and the values are `None` below the threshold rather than absent, so
        the shape does not change with the data."""
        out = {"samples": self.samples, "enough_data": self.enough,
               "min_samples": self.min_samples}
        for q in PERCENTILES:
            out[f"p{q}_ms"] = self.percentiles_ms.get(q)
        return out


def summarize(latencies_ms, *, min_samples: int = MIN_SAMPLES) -> LatencySummary:
    """Percentiles over the samples — or an explicit refusal to compute them.

    Below `min_samples` the percentiles are left EMPTY rather than computed and labelled: a caller
    that forgot to check `enough` then renders nothing, instead of rendering a number off three
    samples. Failing closed is worth more here than convenience, because the number's whole purpose
    is to settle whether the write path is fast enough.
    """
    samples = [float(v) for v in latencies_ms or ()]
    if len(samples) < max(1, int(min_samples)):
        return LatencySummary(samples=len(samples), enough=False, min_samples=int(min_samples))
    computed = {q: percentile(samples, q) for q in PERCENTILES}
    return LatencySummary(samples=len(samples), enough=True, min_samples=int(min_samples),
                          percentiles_ms={q: v for q, v in computed.items() if v is not None})


def render(summary: LatencySummary) -> str:
    """One line for a terminal, in `stigmergy-queue`'s duration format.

    `queue_cli.format_ms` is imported rather than reimplemented: `stigmergy-queue show` already prints
    a capture's own total latency, and the aggregate of that number must not appear in a different
    unit or a different precision one command over.
    """
    captures = f"{summary.samples} filed capture" + ("" if summary.samples == 1 else "s")
    if not summary.enough:
        return (f"capture->filed latency: not enough data yet — {captures} so far, "
                f"{summary.min_samples} needed before p50/p95 mean anything")
    parts = " · ".join(f"p{q}={queue_cli.format_ms(summary.percentiles_ms.get(q))}"
                       for q in PERCENTILES)
    return f"capture->filed latency: {parts} over {captures}"
