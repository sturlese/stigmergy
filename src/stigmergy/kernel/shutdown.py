"""A stop request that interrupts the writer only while it waits on a model.

A deploy stops the writer's machine within a few minutes, while one model call can take ten. The
wait is the one moment nothing is half written in Git or Postgres, so a stop request raises there,
and the claim goes back to the queue instead of dying with its lease. Anywhere else the request is
remembered and raises at the next wait.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator


class WriterShutdown(BaseException):
    """Not an `Exception`: no recovery path may swallow it."""


_requested = False
_waiting = False


def clear() -> None:
    global _requested
    _requested = False


def request() -> None:
    """Called from the signal handler."""
    global _requested
    _requested = True
    if _waiting:
        raise WriterShutdown()


@contextlib.contextmanager
def interruptible() -> Iterator[None]:
    global _waiting
    if _requested:
        raise WriterShutdown()
    _waiting = True
    try:
        yield
    finally:
        _waiting = False
