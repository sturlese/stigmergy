"""`capture.latency` (see its own module docstring for why it lives in `capture` rather than in
`librarian`) — the p50/p95 arithmetic and, above all, its refusal to answer too early.

Pure: a list of floats in, a summary and a sentence out. No database, no clock, no fixtures. That
is the whole reason `capture.queue.filed_latencies_ms` returns SAMPLES rather than percentiles —
the interesting behavior here is the boundary at `MIN_SAMPLES`, and a test that needed ten real
filed captures to reach it would not have been written.

This is the instrument the "capture->page p50 < 5 min" target is settled with, so the tests are
weighted towards the ways a measurement can lie rather than towards the happy path.
"""
import pytest

from stigmergy.capture import cli as queue_cli
from stigmergy.capture import latency


def _samples(n: int, start: float = 1000.0) -> list[float]:
    """`n` distinct ascending samples, so a percentile has something to interpolate between."""
    return [start * i for i in range(1, n + 1)]


# ── percentile: the arithmetic ────────────────────────────────────────────────────────────────────
def test_percentile_of_nothing_is_none_not_zero():
    """`0.0` would render as `0.0s` — a measurement of "instant" where there was no measurement."""
    assert latency.percentile([], 50) is None


def test_percentile_of_one_sample_is_that_sample():
    assert latency.percentile([42.0], 50) == 42.0
    assert latency.percentile([42.0], 95) == 42.0


def test_p50_of_an_odd_count_is_the_middle_value():
    assert latency.percentile([1.0, 2.0, 3.0], 50) == 2.0


def test_p50_of_an_even_count_interpolates():
    assert latency.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5


def test_p100_is_the_maximum_and_p0_the_minimum():
    values = [5.0, 1.0, 3.0]
    assert latency.percentile(values, 0) == 1.0
    assert latency.percentile(values, 100) == 5.0


def test_percentile_does_not_care_about_input_order():
    """It sorts its own input, so a caller cannot produce a wrong answer by handing it the newest
    rows first — which is exactly the order `filed_latencies_ms` returns."""
    assert latency.percentile([3.0, 1.0, 2.0], 50) == latency.percentile([1.0, 2.0, 3.0], 50)


def test_p95_over_a_known_set_matches_linear_interpolation_between_closest_ranks():
    """The definition pinned explicitly (`numpy.percentile`'s default), because two runs over the
    same rows agreeing is the only thing that makes this number comparable across walks."""
    values = [float(v) for v in range(1, 11)]        # 1..10
    # rank = (10-1) * 0.95 = 8.55  ->  values[8] + (values[9]-values[8]) * 0.55 = 9 + 0.55
    assert latency.percentile(values, 95) == pytest.approx(9.55)


# ── summarize: the threshold is the point ─────────────────────────────────────────────────────────
def test_below_the_minimum_no_percentile_is_computed_at_all(capsys):
    """Not "computed and labelled unreliable" — absent. A caller that forgot to check `enough` then
    renders nothing instead of rendering a p95 off three samples."""
    summary = latency.summarize(_samples(3))
    assert summary.samples == 3
    assert summary.enough is False
    assert summary.percentiles_ms == {}


def test_exactly_the_minimum_is_enough():
    """The boundary itself, inclusive: the summary is computed over >= `MIN_SAMPLES` captures."""
    summary = latency.summarize(_samples(latency.MIN_SAMPLES))
    assert summary.enough is True
    assert set(summary.percentiles_ms) == set(latency.PERCENTILES)


def test_one_below_the_minimum_is_not_enough():
    assert latency.summarize(_samples(latency.MIN_SAMPLES - 1)).enough is False


def test_no_samples_at_all_is_the_not_enough_case_and_never_a_crash():
    summary = latency.summarize([])
    assert summary.samples == 0 and summary.enough is False


def test_summarize_tolerates_none_for_no_samples():
    """`filed_latencies_ms` filters `None`s out, but this function is also the seam a `--json`
    consumer and a future dashboard call, and a report is the last place that may crash."""
    assert latency.summarize(None).samples == 0


def test_the_minimum_is_injectable_so_a_test_need_not_fabricate_ten_captures():
    summary = latency.summarize(_samples(2), min_samples=2)
    assert summary.enough is True and summary.min_samples == 2


# ── the rendered sentence ─────────────────────────────────────────────────────────────────────────
def test_the_not_enough_line_says_how_many_there_are_and_how_many_are_needed():
    line = latency.render(latency.summarize(_samples(3)))
    assert "not enough data yet" in line
    assert "3 filed captures" in line
    assert f"{latency.MIN_SAMPLES} needed" in line
    # and NO number that could be mistaken for a measurement
    assert "p50" not in line.replace("p50/p95", "")


def test_the_not_enough_line_is_grammatical_at_one_sample():
    """"1 filed captures" tells a reader nobody read the message — the same rule `report._plural`
    exists for."""
    assert "1 filed capture so far" in latency.render(latency.summarize(_samples(1)))


def test_the_measured_line_names_both_percentiles_and_the_sample_count():
    line = latency.render(latency.summarize(_samples(latency.MIN_SAMPLES)))
    assert "p50=" in line and "p95=" in line
    assert f"over {latency.MIN_SAMPLES} filed captures" in line
    assert "not enough" not in line


def test_the_measured_line_uses_stigmergy_queues_own_duration_format():
    """One dialect across the two tools: `stigmergy-queue show` already prints a capture's own total
    latency, so the aggregate of that number must not appear in a different unit or precision.
    Asserted by formatting the same value through `capture.cli.format_ms` and finding it verbatim."""
    summary = latency.summarize([60_000.0] * latency.MIN_SAMPLES)
    assert f"p50={queue_cli.format_ms(60_000.0)}" in latency.render(summary)
    assert "p50=60.0s" in latency.render(summary)      # ...and that this is what that looks like


# ── the machine-readable shape ────────────────────────────────────────────────────────────────────
def test_the_json_shape_does_not_change_with_the_data():
    """Both keys are present either way, `None` below the threshold rather than absent: a consumer
    must not have to distinguish "missing key" from "not enough data"."""
    thin = latency.summarize(_samples(2)).as_json()
    thick = latency.summarize(_samples(latency.MIN_SAMPLES)).as_json()
    assert set(thin) == set(thick)
    assert thin["p50_ms"] is None and thin["enough_data"] is False
    assert thick["p50_ms"] is not None and thick["enough_data"] is True


def test_the_json_shape_carries_the_threshold_it_judged_against():
    """So a consumer reads the verdict instead of re-deriving it — and so a future change to
    `MIN_SAMPLES` is visible in the data rather than silently reinterpreting old records."""
    assert latency.summarize(_samples(2)).as_json()["min_samples"] == latency.MIN_SAMPLES
