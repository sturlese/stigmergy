import time

import pytest

from stigmergy.kernel.deadline import hard_deadline


class OuterExpired(RuntimeError):
    pass


class InnerExpired(RuntimeError):
    pass


def test_hard_deadline_interrupts_blocking_work():
    started = time.monotonic()

    with pytest.raises(InnerExpired), hard_deadline(0.02, InnerExpired):
        time.sleep(1)

    assert time.monotonic() - started < 0.5


def test_nested_deadline_preserves_the_earlier_outer_limit():
    with (
        pytest.raises(OuterExpired),
        hard_deadline(0.02, OuterExpired),
        hard_deadline(1, InnerExpired),
    ):
        time.sleep(1)


def test_inner_expiry_rearms_outer_deadline_during_cleanup():
    started = time.monotonic()

    with (
        pytest.raises(OuterExpired),
        hard_deadline(0.08, OuterExpired),
        hard_deadline(0.02, InnerExpired),
    ):
        try:
            time.sleep(1)
        finally:
            time.sleep(1)

    assert time.monotonic() - started < 0.5
