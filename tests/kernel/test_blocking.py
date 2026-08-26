"""Contracts for bounded synchronous work in asynchronous transports."""

import asyncio
import threading

import pytest

from stigmergy.kernel import blocking


def test_blocking_capacity_applies_backpressure_without_stalling_the_loop(monkeypatch):
    monkeypatch.setattr(blocking, "BLOCKING_WORKERS", 2)
    release_one = threading.Semaphore(0)
    two_workers_started = threading.Event()
    third_worker_started = threading.Event()
    lock = threading.Lock()
    active = 0
    started = 0

    def work():
        nonlocal active, started
        with lock:
            active += 1
            started += 1
            if active == 2:
                two_workers_started.set()
            if started == 3:
                third_worker_started.set()
        release_one.acquire(timeout=1)
        with lock:
            active -= 1

    async def run():
        tasks = [asyncio.create_task(blocking.run_blocking(work)) for _ in range(3)]
        try:
            assert await asyncio.to_thread(two_workers_started.wait, 0.5)
            assert not third_worker_started.is_set()
            loop_advanced = asyncio.Event()

            async def independent_task():
                await asyncio.sleep(0)
                loop_advanced.set()

            await independent_task()
            assert loop_advanced.is_set()
            release_one.release()
            assert await asyncio.to_thread(third_worker_started.wait, 0.5)
        finally:
            for _ in range(3):
                release_one.release()
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert third_worker_started.is_set()


def test_cancellation_waits_for_blocking_unit_cleanup():
    started = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()

    def work():
        started.set()
        try:
            release.wait(timeout=1)
        finally:
            cleaned.set()

    async def run():
        task = asyncio.create_task(blocking.run_blocking(work))
        assert await asyncio.to_thread(started.wait, 0.5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert cleaned.is_set()
