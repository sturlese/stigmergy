#!/usr/bin/env python3
"""Subprocess harness for `test_worker_signals.py`.

The parent test spawns THIS as a real, separate OS process so it can deliver a genuine
SIGINT/SIGTERM to it: anything that blocks gets interrupted by a real signal, never by calling its
handler and assuming — the worker's shutdown handlers cannot be proven by calling `_on_sigint`
directly, only by a real interrupted process. Not a test module itself: no `test_` prefix, not
collected by
pytest, and it takes its wiring from argv rather than fixtures because a subprocess cannot reach
into the parent's fixture graph.

Prints `PROCESSING-STARTED` the moment an item is claimed and the (deliberately slowed) agent is
about to run, so the parent can wait for that exact line with a real timeout (mirrors
`tests/capture/test_cli.py`'s `_read_until("holding the claim")`) instead of guessing with a fixed
sleep before sending a signal.
"""
import argparse
import dataclasses

from stigmergy.capture import evidence as evidence_plane
from stigmergy.index import store
from stigmergy.librarian import worker
from tests.librarian import support


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--bare", required=True)
    ap.add_argument("--worktree-root", required=True)
    ap.add_argument("--sleep-seconds", type=float, default=3.0)
    args = ap.parse_args()

    conn = store.connect(args.dsn)
    env = support.RepoEnv(bare=args.bare, repo=args.repo)
    settings = support.build_settings(env, worktree_root=args.worktree_root, poll_interval_s=0.2)
    # A REAL evidence store, not the in-process `MemoryEvidenceStore` — this process did not
    # write the material the parent test submitted, so only a store both processes actually share
    # (MinIO, exactly like production's own separate server/worker processes) can read it back.
    base_deps = support.build_deps(env, settings, evidence=evidence_plane.store_from_env())

    def announce() -> None:
        print("PROCESSING-STARTED", flush=True)

    deps = dataclasses.replace(
        base_deps, agent=support.DelayedAgent(base_deps.agent, args.sleep_seconds, announce))

    w = worker.Worker(conn, deps, on_output=lambda msg: print(msg, flush=True))
    w.install_signal_handlers()
    processed = w.run()
    print(f"WORKER-DONE processed={processed} stopping={w.stopping} releasing={w.releasing}",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
