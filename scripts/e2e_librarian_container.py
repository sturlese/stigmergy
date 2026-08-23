#!/usr/bin/env python
"""Containerized-librarian e2e driver — the DEPLOYED artifact, exercised locally.

The host e2e (`e2e_librarian.py`) drains the queue with a worker on the HOST, which proves the
filing path but not the deployed ARTIFACT — one image, the `stigmergy-librarian-boot` entry point,
a clone the worker makes itself. This driver runs the SAME image `fly.toml`'s `worker` process
group runs, through the SAME entry point, and asserts three properties:

    * from EMPTY VOLUMES, the container clones the bare remote, files N captures, and the
      commits land ON THE REMOTE (read back through a fresh clone that never saw the container);
    * SIGTERM (`docker compose stop`) exits it CLEANLY — code 0, its own shutdown line, no
      traceback — within the grace period: the same shutdown rule every worker obeys, applied to a
      container instead of a subprocess;
    * a mid-item SIGKILL is REDELIVERED by the lease and completed by the next worker, leaving one
      page and one commit rather than none or two.

Keyless and offline by construction: the composition's `librarian` service runs the offline
double and carries no OPENAI_API_KEY and no Anthropic key. `scripts/e2e_librarian_container.sh`
owns the compose lifecycle (empty volumes in, empty volumes out) and the environment hygiene.

The bare remote, the fixture repo skeleton, the seed/clone/read-back helpers and the console-script
resolver are IMPORTED from `e2e_librarian.py`, never copied, so a change to one is visible in the
other.
"""
import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import e2e_librarian as base  # noqa: E402 — the host driver, reused for everything git-shaped

from stigmergy.capture import evidence, queue, schema  # noqa: E402
from stigmergy.index import store  # noqa: E402
from stigmergy.librarian import gitcmd  # noqa: E402

SERVICE = "librarian"
SUBMITTER = "e2e.container@stigmergy.test"

# Enough work that the drain lasts several seconds, which is what makes "kill it mid-item"
# reachable without a synthetic delay.
FIRST_BATCH = 6
SECOND_BATCH = 4

DRAIN_TIMEOUT_S = 300.0
STOP_TIMEOUT_S = 120.0

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(label)


# ── the composition, driven by name ───────────────────────────────────────────────────────────
def compose(*args: str, check_rc: bool = True) -> subprocess.CompletedProcess:
    """`docker compose --profile librarian ...`. The profile is what keeps this service out of
    every other target's `up` (see docker-compose.yml)."""
    return subprocess.run(["docker", "compose", "--profile", SERVICE, *args],
                          cwd=str(ROOT), capture_output=True, text=True, check=check_rc)


def container_id() -> str:
    """The service's container, including a stopped or killed one (`-a`) — which is the whole
    reason this exists: the exit code is only readable after it is gone."""
    listed = compose("ps", "-aq", SERVICE, check_rc=False).stdout.split()
    return listed[-1] if listed else ""


def container_state(field: str) -> str:
    """One field of the container's state, from `docker inspect` — the exit code a supervisor sees."""
    cid = container_id()
    if not cid:
        return ""
    return subprocess.run(["docker", "inspect", "--format", f"{{{{{field}}}}}", cid],
                          capture_output=True, text=True, check=False).stdout.strip()


def logs() -> str:
    return compose("logs", "--no-color", SERVICE, check_rc=False).stdout


def start_worker() -> None:
    compose("up", "-d", SERVICE)


def counts(conn) -> dict:
    return queue.counts_by_status(conn)


def wait_until(conn, predicate, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate(counts(conn)):
            return True
        time.sleep(0.2)
    return False


def submit_batch(conn, evidence_store, label: str, how_many: int) -> dict:
    return {f"{label}-{n}": queue.submit(
        conn, evidence_store, kind="raw",
        material=f"{label} {n}: the Acme Corp renewal is progressing steadily.\n",
        hints=None, submitted_by=SUBMITTER) for n in range(how_many)}


def filed_rows(conn, acks: dict) -> dict:
    return {label: queue.get_submission_trace(conn, ack["id"]) for label, ack in acks.items()}


def main() -> int:
    dsn = store.dsn()
    print(f"== containerized librarian e2e against {dsn} and {base.REMOTE_URL} ==", flush=True)

    workdir = pathlib.Path(os.environ.get("STIGMERGY_E2E_WORKDIR")
                           or (ROOT / "out" / "e2e-librarian-container"))
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    conn = store.connect(dsn)
    try:
        schema.ensure_capture_schema(conn)
        # FATAL, not recorded via `check()`: everything below submits and drains fixture captures,
        # so a non-empty queue means this is not the disposable database it was pointed at.
        starting = counts(conn)
        if sum(starting.values()) != 0:
            raise SystemExit(
                f"e2e_librarian_container: the capture queue at {dsn} is not empty "
                f"({json.dumps(starting)}). Run `make e2e-librarian-container` (which pins the DSN "
                f"and wipes the volumes) rather than this script directly, and never against a "
                f"queue holding captures you have not drained.")
        check("the queue starts empty (empty volumes in)", True, json.dumps(starting))

        print("\n-- phase 1: seed the bare remote (the container clones it itself) --", flush=True)
        base.seed_remote(workdir)
        seeded = base.remote_head()
        check("the bare remote has a main branch", len(seeded) == 40, seeded[:12])

        print("\n-- phase 2: submit, then start the WORKER CONTAINER --", flush=True)
        evidence_store = evidence.store_from_env()
        acks = submit_batch(conn, evidence_store, "ordinary", FIRST_BATCH)
        # In the SAME batch: one pipe files every kind, so this rides the same drain and the same
        # worker. Its kind buys it one thing — a `sources/meetings/` archive instead of
        # `sources/notes/` — which phase 3b is what checks.
        acks["meeting"] = base.submit_meeting(conn, evidence_store, base.MEETING_MATERIAL)
        check(f"{FIRST_BATCH + 1} captures queued", len(acks) == FIRST_BATCH + 1, str(len(acks)))
        start_worker()
        drained = wait_until(conn, lambda c: c[schema.QUEUED] == 0 and c[schema.CLAIMED] == 0,
                             DRAIN_TIMEOUT_S)
        check("the container drained the queue", drained, json.dumps(counts(conn)))

        boot_log = logs()
        check("it cloned the repo itself and verified the checkout is at the base ref",
              "stigmergy-librarian-boot" in boot_log and "is at" in boot_log,
              next((line for line in boot_log.splitlines() if "boot:" in line), "(no boot line)"))
        check("...and it is running, not exited", container_state(".State.Running") == "true",
              container_state(".State.Status"))

        print("\n-- phase 3: the commits, read off the BARE REMOTE --", flush=True)
        verify = base.clone_checkout(workdir, "verify")
        head = base.remote_head()
        commits = base.remote_commits(verify)
        rows = filed_rows(conn, acks)
        # "meeting" is still excluded from THIS count and only from this one: the number being
        # asserted is `FIRST_BATCH`, the size of the raw batch. It rides the same pipe and the same
        # commit shape — phase 3b is what checks the one thing its kind decides.
        filed = [r for label, r in rows.items()
                if label != "meeting" and r["status"] == schema.FILED and r["result_ref"]]
        pages = {r["result_ref"] for r in filed}
        # EVERY page this batch filed, transcript included — what the final no-double-file tally is
        # counted against. Kept separate from `pages` on purpose: that one is scoped to the raw
        # batch because the number beside it is `FIRST_BATCH`.
        # OLD BEHAVIOUR: the final check reused `pages`, so the transcript's commit was counted in
        # the numerator and its page was not, and the tally came out one short the moment a
        # transcript started filing through the same pipe as everything else.
        first_refs = {r["result_ref"] for r in rows.values() if r["result_ref"]}

        check("every ordinary capture was filed by the container",
              len(filed) == FIRST_BATCH,
              json.dumps({k: v["status"] for k, v in rows.items()}))
        check("the remote's main advanced", head != seeded and len(head) == 40, head[:12])
        check("one librarian commit per filed page, and no more",
              len(pages) == FIRST_BATCH and all(sha in commits
                                                for sha in (p.rsplit("@", 1)[1] for p in pages)),
              f"{len(pages)} pages, {len(commits)} commits on the remote")
        for ref in sorted(pages):
            page_path, sha = ref.rsplit("@", 1)
            body = gitcmd.run("show", f"{sha}:{page_path}", cwd=str(verify), check=False).stdout
            trailer = gitcmd.run("log", "-1", "--format=%B", sha, cwd=str(verify),
                                 check=False).stdout
            check(f"{page_path} is on the remote at {sha[:12]}, attributed and trailered",
                  body.startswith("---") and f"submitted_by: {SUBMITTER}" in body
                  and f"Submitted-by: {SUBMITTER}" in trailer, f"{len(body)} bytes")

        print("\n-- phase 3b: the transcript, filed by the CONTAINER's worker through the same "
              "pipe --", flush=True)
        # OLD BEHAVIOUR: this read a `filed_meeting` block — `meeting_page` plus a `decisions` list
        # — and expected the page SET a retired flow wrote. A transcript declares its pages in
        # `pages_filed` like every other capture, and `source_pages` carries its verbatim archive,
        # which is deliberately NOT among them.
        meeting_row = rows["meeting"]
        check("the transcript was filed by the container through the ordinary pipe",
              meeting_row["status"] == schema.FILED,
              meeting_row["report"].get("summary", "")[:120])
        # `\0` rather than indexing a ref that may be empty: a transcript that did not file has
        # already been recorded as a FAIL above, and crashing here would lose phases 4 and 5 with it.
        meeting_sha = meeting_row["result_ref"].rsplit("@", 1)[-1] or "\0"
        meeting_pages = meeting_row["report"].get("pages_filed") or []
        # `source_pages` is a LIST: a long transcript splits into cross-linked parts, so the arity
        # is N>=1. The set comparison stays exact — every part in the commit, nothing else.
        source_pages = meeting_row["report"].get("source_pages") or []
        expected_paths = {*source_pages, *meeting_pages}
        # Split on NEWLINES, never on whitespace: a page path is `<Title>.md` and a title has
        # spaces in it, so `.split()` shreds one path into four tokens and the set comparison below
        # can then only ever fail. The host driver has always split this output the right way; this
        # copy did not, and the paths it compared happened to be slugified until a capture's own
        # page joined them.
        committed_paths = {p for p in gitcmd.run("show", "--name-only", "--format=", meeting_sha,
                                                 cwd=str(verify),
                                                 check=False).stdout.split("\n") if p.strip()}
        check("...every page it established and every part of its verbatim archive, all in ONE "
              "commit and nothing else",
              meeting_sha in commits and source_pages and meeting_pages
              and committed_paths == expected_paths,
              f"committed={sorted(committed_paths)} expected={sorted(expected_paths)}")
        check("...archived under sources/meetings/, with the pages it established under wiki/",
              all(p.startswith("sources/meetings/") for p in source_pages)
              and all(p.startswith("wiki/") for p in meeting_pages),
              f"sources={source_pages} pages={meeting_pages}")
        base.assert_parts_carry_no_verdict(source_pages, meeting_sha=meeting_sha,
                                           cwd=str(verify), check=check)

        print("\n-- phase 4: SIGTERM on the container --", flush=True)
        started = time.monotonic()
        compose("stop", SERVICE)
        elapsed = time.monotonic() - started
        exit_code = container_state(".State.ExitCode")
        stopped_log = logs()
        check("it exited 0 on SIGTERM", exit_code == "0", f"exit code {exit_code!r}")
        check("...saying what it was doing, in its own words",
              "received SIGTERM" in stopped_log and "stopped after" in stopped_log,
              next((line for line in stopped_log.splitlines() if "SIGTERM" in line), "(no line)"))
        # Quote the traceback: the stack is torn down before anyone can go looking for it. The
        # window runs FORWARD from the marker — the final line is the one that says what went wrong.
        log_lines = stopped_log.splitlines()
        first_tb = next((i for i, line in enumerate(log_lines) if "Traceback" in line), None)
        tb_context = "\n".join(log_lines[first_tb:first_tb + 40]) if first_tb is not None else ""
        check("...with no traceback", "Traceback" not in stopped_log, tb_context)
        check("...well inside the grace period", elapsed < STOP_TIMEOUT_S, f"{elapsed:.1f}s")

        print("\n-- phase 5: a mid-item SIGKILL is redelivered and finished by the next worker --",
              flush=True)
        second = submit_batch(conn, evidence_store, "killed", SECOND_BATCH)
        start_worker()
        # Kill on the FIRST observation of a claimed row, so the signal lands while an item is
        # genuinely in flight rather than between items.
        deadline, killed_mid_item = time.monotonic() + DRAIN_TIMEOUT_S, False
        while time.monotonic() < deadline:
            if counts(conn)[schema.CLAIMED] >= 1:
                compose("kill", "-s", "SIGKILL", SERVICE)
                killed_mid_item = counts(conn)[schema.CLAIMED] >= 1
                break
            time.sleep(0.02)
        check("a capture was in flight when the container was SIGKILLed", killed_mid_item,
              json.dumps(counts(conn)))
        check("...and the container is gone, killed rather than stopped",
              container_state(".State.Running") == "false"
              and container_state(".State.ExitCode") != "0",
              f"exit code {container_state('.State.ExitCode')!r}")

        stranded = counts(conn)[schema.CLAIMED]
        # The LEASE returns it, through the same `release_expired` the worker's own sweep calls.
        # What is forced here is the CLOCK, not the mechanism: the deployed lease is minutes long
        # and an e2e cannot wait for it. Equivalent to `stigmergy-queue reclaim -v 0`.
        released = queue.release_expired(conn, visibility_timeout_s=0,
                                         max_attempts=queue.DEFAULT_MAX_ATTEMPTS)
        check("the abandoned claim was returned to the queue by the lease sweep",
              released["released"] >= stranded >= 1, json.dumps(released))

        start_worker()
        drained = wait_until(conn, lambda c: c[schema.QUEUED] == 0 and c[schema.CLAIMED] == 0,
                             DRAIN_TIMEOUT_S)
        check("the next worker drained what the killed one left", drained,
              json.dumps(counts(conn)))

        rows = filed_rows(conn, second)
        check("every redelivered capture reached `filed`",
              all(r["status"] == schema.FILED for r in rows.values()),
              json.dumps({k: v["status"] for k, v in rows.items()}))
        # The redelivery must not double-file: one page, one commit, however many deliveries it
        # took. Counted on a fresh clone of the remote, after everything.
        after = base.clone_checkout(workdir, "verify-2")
        commits_after = base.remote_commits(after)
        librarian_commits = [sha for sha in commits_after
                             if "Filed by the librarian from capture #" in gitcmd.run(
                                 "log", "-1", "--format=%B", sha, cwd=str(after)).stdout]
        distinct = {r["result_ref"] for r in rows.values()} | first_refs
        check("one commit per filed page across BOTH workers — the kill filed nothing twice",
              len(librarian_commits) == len(distinct),
              f"{len(librarian_commits)} librarian commits for {len(distinct)} pages")
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    print()
    if failures:
        print(f"CONTAINER LIBRARIAN E2E FAILED: {len(failures)} check(s): " + "; ".join(failures))
        return 1
    print("CONTAINER LIBRARIAN E2E OK — exercised against the deployed image, from "
          "empty volumes, keyless")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
