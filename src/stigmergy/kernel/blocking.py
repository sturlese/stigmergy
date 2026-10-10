"""Bounded execution for synchronous work owned by asynchronous transports."""
from __future__ import annotations

import asyncio
import functools
import weakref
from collections.abc import Callable

import anyio
import anyio.to_thread

BLOCKING_WORKERS = 8

_limiters: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _limiter() -> anyio.CapacityLimiter:
    """Return this event loop's shared, bounded worker capacity."""
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = anyio.CapacityLimiter(BLOCKING_WORKERS)
        _limiters[loop] = limiter
    return limiter


async def run_blocking[T](function: Callable[..., T], /, *args, **kwargs) -> T:
    """Run one complete blocking unit without stalling the event loop.

    Cancellation waits for the unit to finish so callers that own a database connection can
    commit or roll back and close it before the cancellation is observed.
    """
    if kwargs:
        function = functools.partial(function, **kwargs)
    worker = asyncio.create_task(
        anyio.to_thread.run_sync(function, *args, limiter=_limiter())
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # `asyncio.Task.cancel()` bypasses AnyIO's cancellation shielding. Keep the worker task
        # alive until its complete unit has released its limiter token and closed its resources.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            worker.result()
        except BaseException:  # cancellation wins once the worker has completed cleanup
            pass
        raise
