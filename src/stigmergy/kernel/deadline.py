"""Nestable process deadlines for serialized writer work."""

from __future__ import annotations

import contextlib
import signal
import threading
import time
from collections.abc import Callable, Iterator


@contextlib.contextmanager
def hard_deadline(
    seconds: float | None,
    error_factory: Callable[[], BaseException],
) -> Iterator[None]:
    if seconds is None:
        yield
        return
    if seconds <= 0:
        raise error_factory()
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        raise RuntimeError("hard deadlines require the main thread on a POSIX runtime")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_delay, previous_interval = signal.setitimer(signal.ITIMER_REAL, 0)
    started = time.monotonic()
    previous_is_earlier = previous_delay > 0 and previous_delay <= seconds
    effective_seconds = previous_delay if previous_is_earlier else seconds

    def restore_previous() -> None:
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_delay <= 0:
            return
        remaining = previous_delay - (time.monotonic() - started)
        if remaining > 0:
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_interval)

    def expired(signum, frame):
        if previous_is_earlier and callable(previous_handler):
            previous_handler(signum, frame)
        restore_previous()
        raise error_factory()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, effective_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        restore_previous()
