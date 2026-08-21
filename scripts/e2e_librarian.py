#!/usr/bin/env python
"""Librarian e2e driver — the filing path, end to end, against the real composition.

Runs against real Postgres, real MinIO, and a real **bare git remote** in a container, and proves:

    N captures from empty volumes, including
      * two duplicates            -> one page, the second row closed as a retry at the same page
      * one carrying figures      -> filed, and carrying NO verification verdict
      * one with a seeded secret  -> rejected whole, no commit, the value never in the report
      * one naming an unregistered entity -> filed, with the entity PROPOSED in the same commit,
                                    then CONFIRMED through `stigmergy-entities approve`
    -> the expected pages COMMITTED TO THE BARE REMOTE
    -> parallel submits produce serialized commits with zero conflicts
    -> `stigmergy-librarian status` reports a real capture->filed p50/p95

`scripts/e2e_librarian.sh` owns the compose lifecycle (empty volumes in, empty volumes out) and the
environment hygiene; this file assumes the stack is up. Offline and keyless: the agent is the
offline double, and nothing here calls a model.

The bare remote is a container rather than a `tmp_path` so the commit is proved to leave the host's
filesystem conventions and land on a remote reached by URL over a network protocol.

"Serialized commits with zero conflicts" is produced by ONE librarian worker — the supported
topology — raced by a second pusher standing in for an operator editing the repo by hand, which is
the race rebase-and-retry exists for. Do not run two workers instead: N>1 against one checkout is
unsafe, `startup_checks` reaps worktrees the other worker is mid-item in.
"""
import concurrent.futures
import contextlib
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))       # so `tests.adversarial_payloads` resolves — see its docstring

from stigmergy.capture import (  # noqa: E402
    decisions,
    evidence,
    latency,  # noqa: E402
    queue,
    schema,
)
from stigmergy.entities import generator  # noqa: E402
from stigmergy.index import store  # noqa: E402
from stigmergy.kernel import registry as registry_module  # noqa: E402
from stigmergy.librarian import gitcmd  # noqa: E402
from stigmergy.review_kinds import KIND_IDENTITY_PROPOSAL  # noqa: E402
from tests import adversarial_payloads as payloads  # noqa: E402

# The bare remote the composition publishes: loopback only, anonymous, push-enabled.
REMOTE_URL = os.environ.get("STIGMERGY_E2E_GIT_REMOTE", "git://127.0.0.1:9418/stigmergy.git")

# The knowledge-repo skeleton every checkout is seeded from — the librarian suites' own frozen
# fixture, reused so the e2e and the suite file against the same graph shape.
FIXTURE_REPO = ROOT / "tests" / "librarian" / "fixtures" / "repo"

SUBMITTER = "e2e.tester@stigmergy.test"
COMMIT_ENV = {"GIT_AUTHOR_NAME": "e2e-seed", "GIT_AUTHOR_EMAIL": "e2e@stigmergy.test",
              "GIT_COMMITTER_NAME": "e2e-seed", "GIT_COMMITTER_EMAIL": "e2e@stigmergy.test"}

# `latency.MIN_SAMPLES` is the floor below which `status` refuses to print a percentile, so
# submitting fewer would assert the refusal rather than a real measurement.
ORDINARY_CAPTURES = latency.MIN_SAMPLES

# Submitted twice, unchanged, inside the dedup window: level 1, retry collapse.
DUPLICATE = "The Acme Corp renewal call was moved to Thursday, same agenda.\n"

# A body full of real numerals — where a resurrected verification verdict would surface. The double
# copies the material into the body verbatim, which is how the numerals reach the page.
WITH_FIGURES = ("Acme Corp confirmed the renewal at 512000 usd for the year, up from 480000 usd.\n"
                "Both numbers come from the signed order form.\n")

SECRET = f"Acme Corp handed us their CI token to debug the webhook: {payloads.GITHUB_PAT}\n"

# A name the seeded registry does not carry. Nothing parks on it: the librarian creates the entity
# page with `approved_by` EMPTY, regenerates the registry and files the note anchored to the
# newborn, all in ONE commit — and phase 5c has a steward confirm it afterwards.
PROPOSES_AN_ENTITY = "DOUBLE:propose=Globex Corp\nA note about the Globex Corp pilot.\n"
PROPOSED_ID = "globex-corp"
PROPOSED_NAME = "Globex Corp"
PROPOSED_PAGE = f"{generator.ENTITIES_RELDIR}/{PROPOSED_NAME}.md"
# Who confirms it in phase 5c. A second identity from the submitter on purpose: `approved_by` on
# the page must name the person who DECIDED, never the person who captured the material.
STEWARD = "e2e.steward@stigmergy.test"

# The meeting flow, folded into the SAME drain — `worker.process_next` dispatches by `kind`, so
# this needs no second worker and no second phase.
MEETING_TITLE = "Acme Q3 renewal sync"
MEETING_DATE = "2026-07-29"
MEETING_MATERIAL = (
    "Dana and Alice discussed the Acme Corp renewal. Acme confirmed the deal at 512000 usd for "
    "the year, matching the signed order form. Alice will send the updated contract by Friday.\n")


def submit_meeting(conn, evidence_store, material: str) -> dict:
    """`stigmergy-meeting drop`'s own enqueue seam, called directly rather than via subprocess."""
    hints = {"title": MEETING_TITLE, "meeting_date": MEETING_DATE,
             "source_label": "granola-manual"}
    return queue.submit(conn, evidence_store, kind=schema.MEETING, material=material, hints=hints,
                        submitted_by=SUBMITTER)


def _submit_meeting_on_own_connection(evidence_store, material: str) -> dict:
    with store.connect() as conn:
        return submit_meeting(conn, evidence_store, material)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(label)


def assert_parts_carry_no_verdict(source_pages, *, meeting_sha: str, cwd: str, check) -> None:
    """Every part of a split meeting transcript, on the bare remote, carries NO `verification:` —
    nothing computes one, so a verdict here would be a page asserting a check that no longer runs.

    SHARED with `e2e_librarian_container.py`, which imports it: one function called twice, so the
    next change to this assertion cannot land on only one driver. `check` is passed in rather than
    imported, so a failure lands in the calling driver's own summary.
    """
    for part in source_pages:
        part_body = gitcmd.run("show", f"{meeting_sha}:{part}", cwd=cwd, check=False).stdout
        check(f"{part} is committed on the remote carrying NO verification verdict",
              "\nverification:" not in part_body,
              f"{len(part_body)} bytes")


# ── the console script, resolved one way (mirrors scripts/e2e_write.py) ───────────────────────────
def console_command(script: str, module: str) -> list[str]:
    """`.venv/bin/<script>` locally, whatever is on PATH in CI, `python -m <module>` from a bare
    checkout. Never hardcode `.venv/bin/...`: CI has no venv."""
    beside = ROOT / ".venv" / "bin" / script
    if beside.exists():
        return [str(beside)]
    found = shutil.which(script)
    return [found] if found else [sys.executable, "-m", module]


def child_env() -> dict:
    env = dict(os.environ)
    src = str(ROOT / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
    env["STIGMERGY_INDEX_DSN"] = store.dsn()
    return env


# ── phase 1: seed the remote and clone the workers' checkouts ─────────────────────────────────────
def seed_remote(workdir: pathlib.Path) -> None:
    """Push the fixture skeleton to the (empty) bare remote, so there is a `main` to branch from."""
    seed = workdir / "seed"
    gitcmd.run("init", "--quiet", "-b", "main", str(seed))
    shutil.copytree(FIXTURE_REPO, seed, dirs_exist_ok=True)
    gitcmd.run("add", "-A", cwd=str(seed))
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", "chore: seed the e2e knowledge repo",
               cwd=str(seed), env=COMMIT_ENV)
    gitcmd.run("remote", "add", "origin", REMOTE_URL, cwd=str(seed))
    gitcmd.run("push", "--quiet", "-u", "origin", "main", cwd=str(seed))


def clone_checkout(workdir: pathlib.Path, name: str) -> pathlib.Path:
    path = workdir / name
    gitcmd.run("clone", "--quiet", REMOTE_URL, str(path))
    return path


def remote_head() -> str:
    """`main` on the BARE REMOTE via `ls-remote` — never a local clone's stale tracking ref."""
    out = gitcmd.run("ls-remote", REMOTE_URL, "refs/heads/main").stdout.strip()
    return out.split()[0] if out else ""


def remote_commits(checkout: pathlib.Path) -> list[str]:
    """Every commit on the remote's `main`, newest first, fetched fresh."""
    gitcmd.run("fetch", "--quiet", "origin", "main", cwd=str(checkout))
    return gitcmd.run("log", "--format=%H", "FETCH_HEAD", cwd=str(checkout)).stdout.split()


# ── phase 2: submit ───────────────────────────────────────────────────────────────────────────────
def submit(conn, evidence_store, material: str) -> dict:
    return queue.submit(conn, evidence_store, kind="raw", material=material, hints=None,
                        submitted_by=SUBMITTER)


def submit_all(conn, evidence_store) -> dict:
    """Every capture this run files, submitted in PARALLEL — the property starts at the front door,
    not only at the drain."""
    materials = {f"ordinary-{n}": f"Note {n}: the Acme Corp renewal is progressing steadily.\n"
                 for n in range(ORDINARY_CAPTURES)}
    materials.update({
        "duplicate-a": DUPLICATE,
        "duplicate-b": DUPLICATE,          # byte-identical, same submitter, inside the window
        "figures": WITH_FIGURES,
        "secret": SECRET,
        "proposes": PROPOSES_AN_ENTITY,
    })
    acks: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for label, material in materials.items():
            # One connection per submitting thread: psycopg connections are not thread-safe, and a
            # shared one would serialize exactly the concurrency this phase is about.
            futures[pool.submit(_submit_on_own_connection, evidence_store, material)] = label
        # One meeting drop in the same batch: `kind="meeting"` needs its own hints, so it goes
        # through `submit_meeting`.
        futures[pool.submit(_submit_meeting_on_own_connection, evidence_store,
                            MEETING_MATERIAL)] = "meeting"
        for future in concurrent.futures.as_completed(futures):
            acks[futures[future]] = future.result()
    return acks


def _submit_on_own_connection(evidence_store, material: str) -> dict:
    with store.connect() as conn:
        return submit(conn, evidence_store, material)


# ── phase 3: drain with one worker, while a human races it on the same branch ─────────────────────
# A HUMAN's cadence, not a load generator's: what is measured is that rebase-and-retry survives a
# realistic race. Pushing every fraction of a second measures the retry BUDGET instead, which
# belongs in `gitcmd`, and exhausts it against captures nothing conflicted with.
HUMAN_EDITS = 4
HUMAN_EDIT_GAP_S = 1.5


def human_editor(checkout: pathlib.Path, stop: list[bool]) -> int:
    """An operator editing the knowledge repo by hand while the librarian pushes to the same branch.

    Writes a `.txt` under `ops/` deliberately: not a page, so it cannot touch the contract linter or
    the anchoring gate and contributes only a commit the librarian's next push must rebase past.
    Returns how many edits landed.
    """
    landed = 0
    for n in range(HUMAN_EDITS):
        if stop[0]:
            break
        path = checkout / "ops" / "human-scratch.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"a human's own note, number {n}\n")
        gitcmd.run("add", "-A", cwd=str(checkout))
        gitcmd.run("commit", "--quiet", "--no-verify", "-m", f"chore: a human edit {n}",
                   cwd=str(checkout), env=COMMIT_ENV)
        # Rebase onto whatever the librarian pushed since: this pusher is not privileged, and a
        # `--force` here would destroy the property the phase measures.
        gitcmd.run("fetch", "--quiet", "origin", "main", cwd=str(checkout))
        rebased = gitcmd.run("rebase", "--quiet", "FETCH_HEAD", cwd=str(checkout), check=False)
        if rebased.returncode != 0:
            gitcmd.run("rebase", "--abort", cwd=str(checkout), check=False)
            continue
        if gitcmd.run("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=str(checkout),
                      check=False).returncode == 0:
            landed += 1
        time.sleep(HUMAN_EDIT_GAP_S)
    return landed


def drain(checkout: pathlib.Path, human_checkout: pathlib.Path, conn,
          timeout_s: float = 180.0) -> tuple[str, int, int]:
    """One `stigmergy-librarian run`, one concurrent human pusher, until the queue empties — then a
    real SIGTERM (the interface a supervisor uses).

    Returns `(worker stdout, exit code, human edits landed)`. stderr is kept SEPARATE and never
    asserted on: the worker logs a per-item fault with `exc_info=True`, so a genuine refusal
    legitimately writes a traceback there. "It did not crash" is the exit code and the closing line.
    """
    proc = subprocess.Popen(
        [*console_command("stigmergy-librarian", "stigmergy.librarian.cli"),
         "--dsn", store.dsn(), "--repo", str(checkout), "--backend", "double",
         "run", "--poll-interval", "0.2"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        env=child_env())

    stop = [False]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        human = pool.submit(human_editor, human_checkout, stop)
        deadline = time.monotonic() + timeout_s
        drained = False
        while time.monotonic() < deadline:
            counts = queue.counts_by_status(conn)
            if counts[schema.QUEUED] == 0 and counts[schema.CLAIMED] == 0:
                drained = True
                break
            if proc.poll() is not None:
                break                              # the worker died; stop waiting and report it
            time.sleep(0.3)
        stop[0] = True
        landed = human.result()

    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    try:
        output = proc.communicate(timeout=60)[0] or ""
    except subprocess.TimeoutExpired:
        proc.kill()
        output = proc.communicate()[0] or ""
    check("the queue drained before the timeout", drained,
          json.dumps(queue.counts_by_status(conn)))
    return output, proc.returncode, landed


# ── phase 4: what `status` says ───────────────────────────────────────────────────────────────────
def read_status() -> dict:
    out = subprocess.run(
        [*console_command("stigmergy-librarian", "stigmergy.librarian.cli"),
         "--dsn", store.dsn(), "--json", "status"],
        cwd=str(ROOT), capture_output=True, text=True, env=child_env(), check=True).stdout
    return json.JSONDecoder().raw_decode(out)[0]


def main() -> int:
    dsn = store.dsn()
    print(f"== librarian e2e against {dsn}, {evidence.store_from_env().endpoint_url} "
          f"and {REMOTE_URL} ==", flush=True)

    workdir = pathlib.Path(os.environ.get("STIGMERGY_E2E_WORKDIR") or (ROOT / "out" / "e2e-librarian"))
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    conn = store.connect(dsn)
    try:
        schema.ensure_capture_schema(conn)
        # FATAL, not recorded via `check()`: this is a precondition about WHICH DATABASE this is,
        # not an assertion about the run. Everything below submits fixture captures — one carrying a
        # seeded secret — and drains them, so a non-empty queue means it is not the disposable one.
        counts = queue.counts_by_status(conn)
        if sum(counts.values()) != 0:
            raise SystemExit(
                f"e2e_librarian: the capture queue at {dsn} is not empty ({json.dumps(counts)}). "
                f"This e2e submits and drains 15 fixture captures, one carrying a seeded secret, so "
                f"it runs only against the composition's disposable database from empty volumes. "
                f"Run `make e2e-librarian` (which pins the DSN and wipes the volumes) rather than "
                f"this script directly, and never against a queue holding captures you have not "
                f"drained.")
        check("the queue starts empty (empty volumes in)", True, json.dumps(counts))

        print("\n-- phase 1: seed the bare remote, clone the worker's and the human's checkouts --",
              flush=True)
        seed_remote(workdir)
        seeded = remote_head()
        check("the bare remote in the composition has a main branch", len(seeded) == 40, seeded[:12])
        worker_checkout = clone_checkout(workdir, "librarian")
        human_checkout = clone_checkout(workdir, "human")
        check("the librarian's and the human's checkouts both cloned from it",
              worker_checkout.is_dir() and human_checkout.is_dir())

        print("\n-- phase 2: submit every capture in parallel --", flush=True)
        evidence_store = evidence.store_from_env()
        acks = submit_all(conn, evidence_store)
        expected = ORDINARY_CAPTURES + 6   # +5 ordinary-flow fixtures, +1 meeting drop
        check(f"{expected} captures queued", len(acks) == expected, str(len(acks)))
        check("the two duplicates archived to ONE evidence object",
              acks["duplicate-a"]["blob_refs"] == acks["duplicate-b"]["blob_refs"],
              acks["duplicate-a"]["blob_refs"][0])

        print("\n-- phase 3: one worker drains it while a human pushes to the same branch --",
              flush=True)
        output, worker_rc, human_landed = drain(worker_checkout, human_checkout, conn)
        check("the worker stopped cleanly on SIGTERM and exited 0",
              worker_rc == 0 and "received SIGTERM" in output and "stopped after" in output,
              f"rc={worker_rc}; {output.splitlines()[-1] if output else '(no output)'}")
        check("its own output carries no traceback", "Traceback" not in output)
        check("the human really did land commits on the branch mid-drain, so the librarian's "
              "pushes had something to rebase past", human_landed >= 1, f"{human_landed} edits")

        print("\n-- phase 4: the outcomes, per capture --", flush=True)
        rows = {label: queue.get_submission_trace(conn, ack["id"]) for label, ack in acks.items()}
        # Every state a claim can be finished into, and there is no other kind left: nothing waits
        # on a person any more, so "the queue drained" and "every row is done" are the same fact.
        check("every capture reached a terminal state",
              all(r["status"] in schema.FINISHED_STATUSES for r in rows.values()),
              json.dumps({k: v["status"] for k, v in rows.items()}))

        ordinary = [rows[f"ordinary-{n}"] for n in range(ORDINARY_CAPTURES)]
        check(f"all {ORDINARY_CAPTURES} ordinary captures were filed",
              all(r["status"] == schema.FILED for r in ordinary),
              json.dumps([r["status"] for r in ordinary]))
        check("each filed page names wiki/<folder>/<Title>.md@<sha>",
              all(r["result_ref"].startswith("wiki/") and "@" in r["result_ref"]
                  for r in ordinary),
              ordinary[0]["result_ref"])

        # dedup level 1: ONE page, the second row closed AT that page as a retry rather than a
        # rejection — resubmitting identical material is ordinary behavior, not an error.
        dup_a, dup_b = rows["duplicate-a"], rows["duplicate-b"]
        # Which one is the retry is genuinely undetermined: they were submitted in parallel.
        retry, first = ((dup_b, dup_a) if dup_b["report"].get("retry_of") else (dup_a, dup_b))
        check("dedup: both duplicate rows are filed", {dup_a["status"], dup_b["status"]} ==
              {schema.FILED}, f'{dup_a["status"]}/{dup_b["status"]}')
        check("dedup: the second row points at the FIRST row's page",
              retry["result_ref"] == first["result_ref"] != "", retry["result_ref"])
        check("dedup: and its report says it was a retry, not a second capture",
              retry["report"].get("retry_of") == first["id"]
              and "retry of" in retry["report"].get("summary", ""),
              retry["report"].get("summary", "")[:120])

        figures = rows["figures"]
        check("the capture carrying figures was filed", figures["status"] == schema.FILED,
              figures["report"].get("summary", "")[:120])
        # Nothing computes an ingest-time figure verdict, so nothing may stamp one: a verdict
        # reappearing over these numerals would be a page asserting a check that does not run.
        check("...and carrying NO verification verdict — nothing computes one",
              figures["report"].get("verification") is None,
              str(figures["report"].get("verification")))

        secret = rows["secret"]
        check("the seeded secret bounced the whole capture",
              secret["status"] == schema.REJECTED, secret["report"].get("summary", "")[:120])
        check("...naming the rule and never the value",
              payloads.GITHUB_PAT not in json.dumps(secret)
              and "github-pat" in json.dumps(secret["report"]))
        check("...and it created no page", secret["result_ref"] == "")

        meeting = rows["meeting"]
        check("the meeting drop was filed as a page SET", meeting["status"] == schema.FILED,
              meeting["report"].get("summary", "")[:120])
        filed_meeting = meeting["report"].get("filed_meeting") or {}
        # `source_pages` is a LIST: a long transcript splits into cross-linked parts, so the arity
        # is N>=1, never exactly one.
        source_pages = filed_meeting.get("source_pages") or []
        check("...naming at least one source page part, one meeting page and its decisions",
              source_pages
              and all(p.startswith("sources/meetings/") for p in source_pages)
              and filed_meeting.get("meeting_page", "").startswith("wiki/meetings/")
              and isinstance(filed_meeting.get("decisions"), list),
              json.dumps(filed_meeting)[:200])
        check("...and result_ref names the meeting page, per commit",
              meeting["result_ref"].startswith(filed_meeting.get("meeting_page", "\0"))
              and "@" in meeting["result_ref"], meeting["result_ref"])

        # OLD BEHAVIOUR: this capture parked on a question to its submitter and produced no page
        # and no commit. It files now, and the name it is about is proposed beside it.
        proposes = rows["proposes"]
        check("the capture about an unregistered entity was FILED, not parked",
              proposes["status"] == schema.FILED, proposes["report"].get("summary", "")[:120])
        check("...and its report names the identity a steward still has to confirm",
              [e.get("id") for e in proposes["report"].get("entities_proposed") or []]
              == [PROPOSED_ID]
              and "a steward confirms, merges or declines" in proposes["report"].get("summary", ""),
              json.dumps(proposes["report"].get("entities_proposed")))
        check("...anchored to the entity born in the same commit",
              proposes["report"].get("anchored_to") == f"{PROPOSED_NAME} (`{PROPOSED_ID}`)",
              proposes["report"].get("anchored_to", ""))

        print("\n-- phase 5: the commits, read off the BARE REMOTE --", flush=True)
        # A FRESH clone taken after the drain, so every sha and byte below is what the remote holds.
        verify = clone_checkout(workdir, "verify")
        head = remote_head()
        commits = remote_commits(verify)
        librarian_commits = [sha for sha in commits
                             if "Filed by the librarian from capture #" in gitcmd.run(
                                 "log", "-1", "--format=%B", sha, cwd=str(verify)).stdout]
        # "meeting" is EXCLUDED from the ordinary-flow accounting: its commit carries a different
        # subject phrase and adds multiple files, so it matches neither the phrase filter above nor
        # the "one page, one commit, one added file" shape below. Phase 5b covers it.
        filed = [r for label, r in rows.items()
                if label != "meeting" and r["status"] == schema.FILED and r["result_ref"]]
        distinct_pages = {r["result_ref"] for r in filed}
        # The note the proposing capture filed — its commit carries the newborn entity page and the
        # regenerated registry beside it, which the per-page loop below has to expect.
        proposed_note_path = proposes["result_ref"].rsplit("@", 1)[0]

        check("the remote's main advanced", head != seeded and len(head) == 40, head[:12])
        # ONE capture, ONE commit — against the librarian's own commits only, since the human landed
        # others on purpose. DISTINCT `result_ref`s, since the retry row contributes no commit.
        check("one librarian commit per filed page, and no more",
              len(librarian_commits) == len(distinct_pages),
              f"{len(librarian_commits)} librarian commits for {len(distinct_pages)} pages "
              f"({len(commits)} commits total, the human's included)")
        check("zero conflicts: no capture failed on the push path",
              not any("conflict" in json.dumps(r).lower() for r in rows.values()),
              json.dumps({k: v["status"] for k, v in rows.items() if v["status"] == schema.FAILED}))
        check("the history is linear — serialized writes rebased onto the human's, not merged",
              all(len(gitcmd.run("rev-list", "--parents", "-n", "1", sha,
                                 cwd=str(verify)).stdout.split()) <= 2 for sha in commits))

        for ref in sorted(distinct_pages):
            page_path, sha = ref.rsplit("@", 1)
            # Run against a clone that only ever saw the remote: this is what proves the sha in a
            # submitter's report is real.
            present = sha in commits
            body = gitcmd.run("show", f"{sha}:{page_path}", cwd=str(verify), check=False).stdout
            check(f"{page_path} is committed on the remote at {sha[:12]}",
                  present and body.startswith("---"), f"{len(body)} bytes")
            check(f"...and it is filed as developing, attributed to {SUBMITTER}",
                  "status: developing" in body and f"submitted_by: {SUBMITTER}" in body)
            trailer = gitcmd.run("log", "-1", "--format=%B", sha, cwd=str(verify),
                                 check=False).stdout
            # One page, one commit, one added file — EXCEPT the capture that proposed an identity:
            # the entity page and the regenerated registry ride in the same commit as the note, or
            # the note would be anchored to an id the registry does not carry yet.
            expected_files = {page_path}
            if page_path == proposed_note_path:
                expected_files |= {PROPOSED_PAGE, generator.REGISTRY_RELPATH}
            committed = set(gitcmd.run("show", "--name-only", "--format=", sha, cwd=str(verify),
                                       check=False).stdout.split("\n"))
            check("...with the submitter in the commit trailer, and exactly the files it filed",
                  f"Submitted-by: {SUBMITTER}" in trailer
                  and {p for p in committed if p.strip()} == expected_files,
                  f"{sorted(p for p in committed if p.strip())}")

        print("\n-- phase 5b: the meeting page SET, read off the BARE REMOTE --", flush=True)
        meeting_page_path, meeting_sha = meeting["result_ref"].rsplit("@", 1)
        check("the meeting distiller's commit is on the remote", meeting_sha in commits,
              meeting_sha[:12])
        meeting_subject = gitcmd.run("log", "-1", "--format=%B", meeting_sha, cwd=str(verify),
                                     check=False).stdout
        check("...as one App-bot commit, feat(meeting), naming the meeting distiller and the "
              "submitter",
              meeting_subject.startswith("feat(meeting):")
              and "Filed by the librarian's meeting distiller from capture #" in meeting_subject
              and f"Submitted-by: {SUBMITTER}" in meeting_subject,
              meeting_subject.splitlines()[0] if meeting_subject else "(empty)")
        decision_paths = [d.get("path", "") for d in filed_meeting.get("decisions", [])]
        expected_paths = {*source_pages, meeting_page_path, *decision_paths}
        committed_paths = set(gitcmd.run("show", "--name-only", "--format=", meeting_sha,
                                         cwd=str(verify), check=False).stdout.split())
        check("...containing exactly the page SET: every source page part, one meeting page, its "
              "decisions, no more",
              committed_paths == expected_paths,
              f"committed={sorted(committed_paths)} expected={sorted(expected_paths)}")
        # EVERY part, not just the first: each is a page in its own right.
        assert_parts_carry_no_verdict(source_pages, meeting_sha=meeting_sha, cwd=str(verify),
                                      check=check)
        check("...and the meeting page itself is present too",
              gitcmd.run("show", f"{meeting_sha}:{meeting_page_path}", cwd=str(verify),
                        check=False).stdout.startswith("---"))

        print("\n-- phase 5c: the identity the librarian proposed, and a steward confirming it --",
              flush=True)
        proposed_sha = proposes["result_ref"].rsplit("@", 1)[1]
        entity_page = gitcmd.run("show", f"{proposed_sha}:{PROPOSED_PAGE}", cwd=str(verify),
                                 check=False).stdout
        # The empty string IS the proposal mark: a page with a name in `approved_by` is confirmed,
        # and one with nothing there is waiting on a steward.
        check(f"{PROPOSED_PAGE} is on the remote in the note's OWN commit, unconfirmed",
              entity_page.startswith("---")
              and f'{generator.APPROVED_BY_KEY}: ""' in entity_page, f"{len(entity_page)} bytes")
        before_entry = json.loads(gitcmd.run(
            "show", f"{proposed_sha}:{generator.REGISTRY_RELPATH}", cwd=str(verify),
            check=False).stdout)["entities"][PROPOSED_ID]
        check("...and the registry regenerated beside it carries the entry as proposed",
              before_entry[registry_module.PROPOSED_KEY] is True
              and before_entry[registry_module.APPROVED_BY_KEY] == "",
              json.dumps(before_entry))

        # The steward's own door, driven as a steward drives it: their clone, their identity, one
        # pushed commit, one ledger row. `--by` is attribution; `preflight` still wants the clone's
        # git identity, which a real steward has from their global config and a throwaway has not.
        steward_checkout = clone_checkout(workdir, "steward")
        gitcmd.run("config", "user.name", "e2e-steward", cwd=str(steward_checkout))
        gitcmd.run("config", "user.email", STEWARD, cwd=str(steward_checkout))
        approved = subprocess.run(
            [*console_command("stigmergy-entities", "stigmergy.entities.cli"),
             "--dsn", store.dsn(), "--repo", str(steward_checkout), "--json",
             "approve", PROPOSED_ID, "--by", STEWARD],
            cwd=str(ROOT), capture_output=True, text=True, env=child_env())
        stderr_tail = (approved.stderr.strip().splitlines() or ["(no stderr)"])[-1]
        check("`stigmergy-entities approve` confirmed it from the steward's own clone",
              approved.returncode == 0, f"rc={approved.returncode}; {stderr_tail}")
        decision = json.loads(approved.stdout) if approved.returncode == 0 else {}
        check("...landing ONE more commit on the bare remote",
              decision.get("commit", "\0") in remote_commits(verify),
              decision.get("commit", "(none)")[:12])
        check("...recorded in the review ledger as the steward's approval, so the librarian will "
              "not re-propose it",
              (decisions.latest_decision_for(conn, item_kind=KIND_IDENTITY_PROPOSAL,
                                             item_id=PROPOSED_ID) or {}).get("verdict")
              == decisions.APPROVE,
              json.dumps(decisions.latest_decision_for(conn, item_kind=KIND_IDENTITY_PROPOSAL,
                                                       item_id=PROPOSED_ID), default=str)[:160])

        # A clone that has only ever seen the remote: the registry is DERIVED from the pages, so
        # this is the one read that proves the page and the entry moved together.
        confirmed = clone_checkout(workdir, "confirmed")
        after_entry = json.loads(
            (confirmed / generator.REGISTRY_RELPATH).read_text(encoding="utf-8")
        )["entities"][PROPOSED_ID]
        check("...and a fresh clone reads the entry as no longer proposed, approved by the steward",
              after_entry[registry_module.PROPOSED_KEY] is False
              and after_entry[registry_module.APPROVED_BY_KEY] == STEWARD,
              json.dumps(after_entry))
        confirmed_page = (confirmed / PROPOSED_PAGE).read_text(encoding="utf-8")
        check("...derived from the entity page, which now names who confirmed it",
              f'{generator.APPROVED_BY_KEY}: ""' not in confirmed_page
              and STEWARD in confirmed_page,
              f"{len(confirmed_page)} bytes")

        print("\n-- phase 6: the latency measurement --", flush=True)
        status = read_status()
        measured = status["latency"]
        check(f"status reports at least {latency.MIN_SAMPLES} filed captures",
              measured["samples"] >= latency.MIN_SAMPLES, str(measured["samples"]))
        check("...so it computes p50 and p95 rather than refusing", measured["enough_data"] is True)
        check("...and both are real durations computed from the trace alone",
              isinstance(measured["p50_ms"], (int, float)) and measured["p50_ms"] > 0
              and measured["p95_ms"] >= measured["p50_ms"],
              f"p50={measured['p50_ms']:.0f}ms p95={measured['p95_ms']:.0f}ms")
        check("status agrees with the queue about what is in flight",
              status["in_flight"] == [] and status["counts"][schema.CLAIMED] == 0,
              json.dumps(status["counts"]))
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    print()
    if failures:
        print(f"LIBRARIAN E2E FAILED: {len(failures)} check(s): " + "; ".join(failures))
        return 1
    print("LIBRARIAN E2E OK — exercised against real postgres, real minio and a "
          "real bare git remote, from empty volumes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
