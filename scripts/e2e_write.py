#!/usr/bin/env python
"""Write-path e2e driver — the capture queue, end to end, against the real composition.

Runs against real Postgres, real MinIO and a real `stigmergy-server` subprocess over the real MCP
stdio protocol, and proves:

    submit works end to end (a real MCP session -> an ack -> a `queued` row)
    attribution is the server's (`submitted_by` = the resolved `--identity`)
    attribution cannot be forged (an explicit error, NO row and NO blob)
    forged frontmatter is recorded as flagged hints, never trusted
    every capture is archived; identical material -> two rows, exactly one object
    claiming is exactly-once under N parallel claimers
    a dead worker loses nothing (killed mid-claim -> requeued, `attempts` incremented)
    an exhausted item lands in `failed`, recorded in ingest_errors and job_runs
    retention deletes physically (payload/hints gone, the trace survives)
    the queue survives an index rebuild

`scripts/e2e_write.sh` owns the compose lifecycle (empty volumes in, empty volumes out); this
file assumes the stack is already up. Offline by construction: the fake embedder builds the index
and nothing here calls a model, so the whole run is keyless.
"""
import asyncio
import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
# ...and the repo root, so `tests.adversarial_payloads` resolves — the same import
# `scripts/e2e_librarian.py` makes, for the reason that module's docstring gives: an adversarial
# payload lives in ONE place, shared by the pytest suites and by the e2e drivers, so the two halves
# of one guarantee cannot drift apart.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from stigmergy.capture import evidence, queue, retention, schema  # noqa: E402
from stigmergy.index import build, store  # noqa: E402
from stigmergy.index.backends.embedder import build_embedder  # noqa: E402
from tests.adversarial_payloads import FORGED_PAGE  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_REPO = ROOT / "tests" / "index" / "fixtures" / "repo"
STEWARD = "steward@example.com"

MATERIAL = "Decision: the capture queue is durable while the index stays disposable.\n"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(label)


def s3():
    st = evidence.store_from_env()
    return st.client(), st.bucket


def object_count() -> int:
    client, bucket = s3()
    return len(client.list_objects_v2(Bucket=bucket).get("Contents", []))


def row_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        return cur.fetchone()[0]


def table_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (name,))
        return cur.fetchone()[0] is not None


# ── locating this repo's console scripts, the one way ─────────────────────────────────────────
def console_command(script: str, module: str) -> list[str]:
    """The argv prefix that runs one of this repo's console scripts, wherever it is installed.

    Three ways this driver gets run, and it must work under all of them:
      1. `make e2e-write` locally  -> `.venv/bin/<script>` (the venv `make venv` bootstraps)
      2. CI                        -> `pip install -e ".[dev]"` into the runner's own Python, no
                                      `.venv` in the tree at all; the console script lands on PATH
      3. a bare source checkout    -> nothing installed; fall back to `python -m <module>`

    ONE resolver, every caller. The first cut hardcoded `.venv/bin/stigmergy-queue` for the killed
    worker while `stigmergy-server` had this logic — an asymmetry no local run could catch, because
    locally `.venv` always exists and the hardcoded branch always won. CI, which has no `.venv`
    (`e2e_write.sh` already accounts for that with `PY=`; this file did not), died on it at
    phase 5. That is the whole reason this is a function and not three lines at each call site.
    """
    beside = ROOT / ".venv" / "bin" / script
    if beside.exists():
        return [str(beside)]
    found = shutil.which(script)
    return [found] if found else [sys.executable, "-m", module]


def child_env() -> dict:
    """The environment a spawned child gets: this process's, plus `src` on `PYTHONPATH` and the
    resolved DSN. The `PYTHONPATH` entry matters only for resolution case 3 above — a child asked
    to run `python -m stigmergy.…` from an uninstalled checkout has to be able to import it, the
    same way this driver does for itself at the top of the file. Harmless in cases 1 and 2, where
    the package is installed and the entry is simply redundant."""
    env = dict(os.environ)
    src = str(ROOT / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
    env["STIGMERGY_INDEX_DSN"] = store.dsn()
    return env


# ── phase 1: the real MCP protocol ────────────────────────────────────────────────────────────


async def call(session, name: str, **args) -> dict:
    result = await session.call_tool(name, args)
    return json.loads(result.content[0].text)


async def submit_phase(workdir: pathlib.Path) -> list[dict]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    identities = workdir / "identities.json"
    identities.write_text(json.dumps({STEWARD: "*"}), encoding="utf-8")
    cmd, *base = console_command("stigmergy-server", "stigmergy.server.mcp_server")
    params = StdioServerParameters(
        command=cmd,
        args=[*base, "--identity", STEWARD, "--identities", str(identities),
              "--embedder", "fake"],
        env=child_env())

    acks = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        check("brain_submit and brain_submissions are registered MCP tools",
              {"brain_submit", "brain_submissions"} <= tools, ", ".join(sorted(tools)))

        acks.append(await call(session, "brain_submit", kind="raw", material=MATERIAL,
                               hints={"entity": "stigmergy", "title": "durable queue"}))
        acks.append(await call(session, "brain_submit", kind="raw", material=MATERIAL))
        acks.append(await call(session, "brain_submit", kind="page", material=FORGED_PAGE))

        check("the ack carries a submission id and says queued",
              all(a.get("status") == "queued" and isinstance(a.get("id"), int) for a in acks),
              json.dumps(acks[0].get("message", ""))[:140])
        check("attribution is the server's resolved identity",
              all(a.get("submitted_by") == STEWARD for a in acks))
        check("forged frontmatter is recorded as flagged hints, never trusted",
              sorted(acks[2].get("flagged_hints", [])) ==
              ["acl", "content_hash", "submitted_by", "verification"],
              str(acks[2].get("flagged_hints")))

        before_rows, before_objects = row_count(CONN), object_count()
        spoof = await call(session, "brain_submit", kind="raw", material="forged capture\n",
                           submitted_by="ceo@example.com")
        check("a forged submitted_by is an explicit error",
              "error" in spoof and "submitted_by" in spoof["error"], str(spoof)[:160])
        check("the refusal created no row and no blob",
              row_count(CONN) == before_rows and object_count() == before_objects,
              f"rows {before_rows}->{row_count(CONN)}, objects {before_objects}->{object_count()}")

        listed = await call(session, "brain_submissions")
        own = listed.get("submissions", [])
        check("brain_submissions returns the caller's submissions, all marked mine",
              len(own) == 3 and all(row["mine"] for row in own), f"{len(own)} rows")
        # A fence check USED to live here as "every excerpt starts with <<<UNTRUSTED-DATA", and
        # the withholding rule made it fail in CI while every unit test stayed green — correctly,
        # because these rows are `queued` and a queued row's material is withheld until the
        # librarian has scanned it. There is no echoed text here left to fence.
        #
        # It is REPLACED rather than rewritten as `... for row in own if row["excerpt"]`, which was
        # the first instinct and is worse than nothing: with every excerpt withheld at this point,
        # that predicate is `all([])` — a check that cannot fail, wearing the name of a security
        # assertion. The fence is genuinely asserted where an excerpt IS shown, by two tests that
        # deliberately move a row past the gate first:
        #   tests/server/test_service_capture.py::test_submissions_echoed_excerpt_is_fenced_as_untrusted_data
        #   tests/server/test_mcp_harness.py (see its own comment at the T10 line)
        # What this e2e can still prove, and nothing checked before, is the other half of R1:
        check("R1 — a queued row withholds its material and explains the absence",
              all(not row["excerpt"] and row["withheld_reason"] for row in own),
              f"{sum(1 for r in own if r['withheld_reason'])}/{len(own)} rows carry a reason")
    return acks


# ── phase 4: exactly-once claiming ────────────────────────────────────────────────────────────
def claim_once(_i: int):
    with store.connect() as conn:
        item = queue.claim_next(conn)
        return item["id"] if item else None


def main() -> int:
    global CONN
    dsn = store.dsn()
    print(f"== write-path e2e against {dsn} and {evidence.store_from_env().endpoint_url} ==",
          flush=True)

    print("\n-- building the index (fake embedder) so the server has something to serve --",
          flush=True)
    CONN = store.connect(dsn)
    build.rebuild(CONN, str(FIXTURE_REPO), build_embedder("fake"))
    schema.ensure_capture_schema(CONN)

    print("\n-- phase 1: submit over the real MCP stdio protocol --", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        acks = asyncio.run(submit_phase(pathlib.Path(tmp)))

    print("\n-- phase 2: the evidence plane --", flush=True)
    check("every capture is archived under sha256/<ab>/<cd>/<hash>",
          all(a["blob_refs"][0] == f"sha256/{a['content_sha256'][:2]}/"
              f"{a['content_sha256'][2:4]}/{a['content_sha256']}" for a in acks),
          acks[0]["blob_refs"][0])
    check("identical material: two rows, exactly one object",
          acks[0]["blob_refs"] == acks[1]["blob_refs"] and acks[0]["id"] != acks[1]["id"]
          and object_count() == 2, f"{object_count()} objects for {row_count(CONN)} rows")

    print("\n-- phase 3: an index rebuild must not take the durable half with it --", flush=True)
    rows_before = row_count(CONN)
    build.rebuild(CONN, str(FIXTURE_REPO), build_embedder("fake"))
    check("capture_queue survives `stigmergy-index --rebuild`",
          row_count(CONN) == rows_before, f"{rows_before} rows before, {row_count(CONN)} after")
    check("audit_log, job_runs and ingest_errors survive it too",
          all(table_exists(CONN, t) for t in schema.DURABLE_TABLES))

    print("\n-- phase 4: N parallel claimers, M queued rows --", flush=True)
    queued = queue.counts_by_status(CONN)["queued"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = [cid for cid in pool.map(claim_once, range(8)) if cid is not None]
    check("exactly M claims for M queued rows",
          len(claimed) == queued, f"{len(claimed)} claims for {queued} queued")
    check("no row was claimed twice",
          len(set(claimed)) == len(claimed), str(sorted(claimed)))

    print("\n-- phase 5: a worker dies mid-claim --", flush=True)
    queue.release_expired(CONN, visibility_timeout_s=0)
    victim = queue.claim_next(CONN, visibility_timeout_s=300)   # a row for the doomed worker
    queue.release_expired(CONN, visibility_timeout_s=0)         # ...back to the queue for the CLI
    worker = subprocess.Popen(
        [*console_command("stigmergy-queue", "stigmergy.capture.cli"), "--json", "claim",
         "--hold", "60", "--visibility-timeout", "2"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=child_env())
    time.sleep(4)                                              # let it claim, then kill it
    worker.kill()
    worker.wait(timeout=10)
    time.sleep(2.5)                                            # outlive the 2s visibility timeout
    before = queue.get_submission_trace(CONN, victim["id"])
    reclaimed = None
    for _ in range(8):                                         # the sweep runs on the claim path
        candidate = queue.claim_next(CONN, visibility_timeout_s=2)
        if candidate is None:
            break
        reclaimed = candidate
        if candidate["id"] == before["id"]:
            break
    check("an abandoned claim becomes claimable again",
          reclaimed is not None, f"reclaimed #{reclaimed['id'] if reclaimed else '-'}")
    check("attempts is incremented across the redelivery",
          reclaimed is not None and reclaimed["attempts"] >= 2,
          f"attempts={reclaimed['attempts'] if reclaimed else '-'}")

    print("\n-- phase 6: a poison item fails, then retention deletes its material --", flush=True)
    for _ in range(queue.DEFAULT_MAX_ATTEMPTS + 2):            # burn the attempts
        queue.release_expired(CONN, visibility_timeout_s=0)
        if queue.claim_next(CONN, visibility_timeout_s=0) is None:
            break
    queue.release_expired(CONN, visibility_timeout_s=0)
    counts = queue.counts_by_status(CONN)
    check("an exhausted item lands in `failed`", counts["failed"] > 0, str(counts))
    with CONN.cursor() as cur:
        cur.execute("SELECT count(*) FROM ingest_errors WHERE source = 'capture_queue'")
        errors = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM job_runs WHERE job LIKE 'capture-%'")
        runs = cur.fetchone()[0]
    check("ingest_errors and job_runs recorded it",
          errors > 0 and runs > 0, f"{errors} ingest_errors, {runs} job_runs")

    failed_id = next(r["id"] for r in queue.list_all_submissions(CONN, limit=200)
                     if r["status"] == schema.FAILED)
    result = retention.purge(CONN, older_than_days=0)
    check("purge reports what it deleted", result["purged"] > 0, str(result))
    with CONN.cursor() as cur:
        cur.execute("SELECT payload IS NULL, hints IS NULL, submitted_by, status, created_at,"
                    " finished_at, result_ref FROM capture_queue WHERE id = %s", (failed_id,))
        payload_gone, hints_gone, submitter, status, created, finished, _ref = cur.fetchone()
    check("payload and hints are physically gone",
          payload_gone and hints_gone)
    check("id, submitter, timestamps and status survive",
          bool(submitter) and status == schema.FAILED and created is not None
          and finished is not None)
    check("the evidence blob is untouched by retention", object_count() == 2,
          f"{object_count()} objects")

    print()
    if failures:
        print(f"WRITE-PATH E2E FAILED: {len(failures)} check(s): " + "; ".join(failures))
        return 1
    print("WRITE-PATH E2E OK — exercised against real postgres + real minio")
    return 0


CONN = None

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        if CONN is not None:
            with contextlib.suppress(Exception):
                CONN.close()
