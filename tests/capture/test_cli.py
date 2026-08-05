"""`stigmergy-queue` — the steward's view, driven in-process through `cli.main(argv)` against real
Postgres. Errors are LOCAL and specific here — generic over HTTP, specific in the local CLI — the
opposite posture from the MCP tools, and these tests check that posture directly (a raw DSN/host
DOES appear in a CLI error, unlike the redacted HTTP path).
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from stigmergy.capture import cli, queue, retention, schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.index import store
from tests.capture.conftest import unique_material

ALICE = "alice@example.com"


def _queue_cli_command() -> list[str]:
    """Prefer the installed console script (the real entry point), same preference order as
    `tests/server/conftest.py::server_command()`; fall back to `python -m` from a source
    checkout."""
    beside = os.path.join(os.path.dirname(sys.executable), "stigmergy-queue")
    if os.path.exists(beside):
        return [beside]
    found = shutil.which("stigmergy-queue")
    if found:
        return [found]
    return [sys.executable, "-m", "stigmergy.capture.cli"]


def _read_until(stream, needle: str, timeout: float = 10.0) -> bool:
    """Read lines from a subprocess stream until one contains `needle`, or time out. Used to know
    a subprocess has reached the exact moment it is sleeping in `--hold` — real timing, not a
    fixed sleep-and-hope delay — before a signal is sent."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = stream.readline()
        if not line:
            return False
        if needle in line:
            return True
    return False


def _submit(conn, **kw):
    kw.setdefault("kind", "raw")
    kw.setdefault("material", unique_material())
    kw.setdefault("hints", None)
    kw.setdefault("submitted_by", ALICE)
    return queue.submit(conn, MemoryEvidenceStore(), **kw)


# ── connection failure: LOCAL and specific (unlike the redacted HTTP posture) ──────────────────
def test_cli_unreachable_database_prints_a_clean_message_and_exits_2(capsys):
    rc = cli.main(["--dsn", "postgresql://stigmergy:stigmergy@127.0.0.1:1/nope", "list"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "cannot reach the queue database" in captured.err
    assert "make db-up" in captured.err


# ── list ─────────────────────────────────────────────────────────────────────────────────────────
def test_cli_list_json_reports_counts_and_submissions(clean_queue, capsys):
    ack = _submit(clean_queue, submitted_by=ALICE)

    rc = cli.main(["--dsn", store.dsn(), "--json", "list"])

    captured = capsys.readouterr()
    assert rc == 0
    out = json.loads(captured.out)
    assert out["counts"]["queued"] == 1
    assert [row["id"] for row in out["submissions"]] == [ack["id"]]


def test_cli_list_with_no_submissions_says_so(clean_queue, capsys):
    rc = cli.main(["--dsn", store.dsn(), "list"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "no submissions" in captured.out


def test_cli_list_human_readable_shows_flagged_hints(clean_queue, capsys):
    material = "---\nsubmitted_by: ceo@example.com\n---\n\nforged\n"
    _submit(clean_queue, kind="page", material=material)

    rc = cli.main(["--dsn", store.dsn(), "list"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "flagged=submitted_by" in captured.out


def test_cli_list_respects_status_filter(clean_queue, capsys):
    _submit(clean_queue)
    queue.claim_next(clean_queue)   # one row is now 'claimed'
    _submit(clean_queue)            # a second stays 'queued'

    cli.main(["--dsn", store.dsn(), "--json", "list", "--status", "claimed"])

    out = json.loads(capsys.readouterr().out)
    assert len(out["submissions"]) == 1
    assert out["submissions"][0]["status"] == "claimed"


def test_cli_list_human_readable_shows_the_error_note_on_a_failed_row(clean_queue, capsys):
    ack = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FAILED,
                expected_attempts=claimed["attempts"], error="claim expired after 3 attempts")

    rc = cli.main(["--dsn", store.dsn(), "list"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "! claim expired after 3 attempts" in captured.out


def test_cli_list_withholds_the_material_of_a_row_a_secrets_refusal_bounced(clean_queue, capsys):
    """The steward's terminal is the other read-back of the same rows, through the same helper.

    A steward runs `stigmergy-queue list` over EVERYBODY's captures, so a refused secret printed
    here is somebody else's credential on an operator's screen — and in their scrollback. The
    excerpt is replaced by the queue's own sentence, not clipped or redacted around the match.
    """
    planted = "ghp_" + "a1B2c3D4e5" * 3 + "f6g7hi"          # a PAT SHAPE; grants nothing
    ack = _submit(clean_queue, material=f"the CI token is {planted}",
                  hints={"title": f"rotate {planted}"})
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.REJECTED,
                 expected_attempts=claimed["attempts"],
                 error="rejected — gitleaks matched a likely secret near line 1",
                 report={"status": schema.REJECTED,
                         schema.REASON_CODE_KEY: schema.REASON_SECRET,
                         "summary": "rejected — gitleaks matched a likely secret near line 1"})

    assert cli.main(["--dsn", store.dsn(), "list"]) == 0
    human = capsys.readouterr().out
    assert planted not in human
    assert schema.WITHHELD_MATERIAL_NOTE in human

    assert cli.main(["--dsn", store.dsn(), "--json", "list"]) == 0
    assert planted not in capsys.readouterr().out        # the machine surface too


# ── list/show render a PARKED row: who is waited on, for how long, and the exact reply command,
# with the needs_input/triage age surfaced on both ───────────────────────────────────────────────
def test_cli_list_human_readable_shows_who_is_waited_on_and_the_reply_command(clean_queue, capsys):
    ack = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.NEEDS_INPUT,
                expected_attempts=claimed["attempts"],
                error="needs_input — which entity is this about?")

    rc = cli.main(["--dsn", store.dsn(), "list"])

    captured = capsys.readouterr()
    assert rc == 0
    assert f"waiting on: {ALICE}" in captured.out
    assert "parked" in captured.out
    assert f"answer with:  {schema.reply_invocation(ack['id'])}" in captured.out
    assert f"stigmergy-queue show {ack['id']}" in captured.out
    # the full multi-line question is NOT printed on `list` — only the invocation
    assert "needs_input — which entity" not in captured.out


def test_cli_list_human_readable_shows_a_triage_row_waiting_on_a_steward(clean_queue, capsys):
    ack = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.TRIAGE,
                expected_attempts=claimed["attempts"], error="parked for the steward")

    rc = cli.main(["--dsn", store.dsn(), "list"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "waiting on: a steward" in captured.out


def test_cli_list_human_readable_notes_a_purged_payload(clean_queue, capsys):
    ack = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FAILED,
                expected_attempts=claimed["attempts"], error="poison item")
    from stigmergy.capture import retention
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET finished_at = now() - make_interval(days => 45)"
                    " WHERE id = %s", (ack["id"],))
    retention.purge(clean_queue, older_than_days=30)

    rc = cli.main(["--dsn", store.dsn(), "list"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "(payload purged)" in captured.out


# ── show ─────────────────────────────────────────────────────────────────────────────────────────
def test_cli_show_unknown_id_exits_1(clean_queue, capsys):
    rc = cli.main(["--dsn", store.dsn(), "show", "999999999"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "no submission" in captured.err


def test_cli_show_json_matches_the_library_trace(clean_queue, capsys):
    ack = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FILED,
                expected_attempts=claimed["attempts"], result_ref="wiki/x.md")

    rc = cli.main(["--dsn", store.dsn(), "--json", "show", str(ack["id"])])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["id"] == ack["id"]
    assert out["status"] == "filed"
    assert out["result_ref"] == "wiki/x.md"


def test_cli_show_human_readable_reports_the_trace_and_latencies(clean_queue, capsys):
    ack = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FILED,
                expected_attempts=claimed["attempts"], result_ref="wiki/x.md")

    rc = cli.main(["--dsn", store.dsn(), "show", str(ack["id"])])

    captured = capsys.readouterr()
    assert rc == 0
    assert f"#{ack['id']} filed (raw) by {ALICE}" in captured.out
    assert "queue wait:" in captured.out and "total:" in captured.out
    assert "result_ref  wiki/x.md" in captured.out


def test_cli_show_human_readable_notes_a_purged_payload(clean_queue, capsys):
    ack = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FAILED,
                expected_attempts=claimed["attempts"], error="poison item")
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET payload = NULL, hints = NULL WHERE id = %s",
                    (ack["id"],))

    rc = cli.main(["--dsn", store.dsn(), "show", str(ack["id"])])

    captured = capsys.readouterr()
    assert rc == 0
    assert "note        poison item" in captured.out
    assert "purged by retention" in captured.out


def test_cli_show_human_readable_prints_the_whole_question_and_the_reply_invocation(clean_queue,
                                                                                    capsys):
    """`show` prints the question WHOLE (`_print_note`'s own docstring), never clipped — it is
    what a steward reads before deciding whether to reply on somebody's behalf."""
    ack = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    question = (f"needs_input — capture #{ack['id']} is parked on one question before it can be "
               "filed: your material seems to be about \"Nebula Systems\".")
    queue.finish(clean_queue, ack["id"], status=schema.NEEDS_INPUT,
                expected_attempts=claimed["attempts"], error=question)

    rc = cli.main(["--dsn", store.dsn(), "show", str(ack["id"])])

    captured = capsys.readouterr()
    assert rc == 0
    assert "question" in captured.out
    assert "Nebula Systems" in captured.out
    assert f"answer with {schema.reply_invocation(ack['id'])}" in captured.out
    assert f"waiting on: {ALICE}" in captured.out
    # the `asked` trace event is suppressed while still needs_input (it IS the question above) —
    # printed once, not twice, on the one screen where both would otherwise appear
    assert captured.out.count("Nebula Systems") == 1
    assert "(the question, printed above)" in captured.out


def test_cli_show_human_readable_prints_the_reply_once_the_row_has_moved_on(clean_queue, capsys):
    """`reply` survives past `needs_input` (it is not cleared by the transition into `triage`),
    and `show` prints it once the question block is gone — the only surviving copy of what the
    submitter said, on the row that used to ask it."""
    from stigmergy.capture import queue as queue_module

    ack = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.NEEDS_INPUT,
                expected_attempts=claimed["attempts"], error="which entity?")
    queue_module.record_reply(clean_queue, ack["id"], answer="Ghost Company Inc", actor=ALICE)
    reclaimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.TRIAGE,
                expected_attempts=reclaimed["attempts"], error="still unresolved")

    rc = cli.main(["--dsn", store.dsn(), "show", str(ack["id"])])

    captured = capsys.readouterr()
    assert rc == 0
    assert "reply       Ghost Company Inc" in captured.out
    assert "waiting on: a steward" in captured.out


def test_cli_show_human_readable_lists_the_disposition_events_with_actor_and_note(clean_queue,
                                                                                  capsys):
    from stigmergy.capture import dispositions

    ack = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.TRIAGE,
                expected_attempts=claimed["attempts"], error="parked")
    dispositions.requeue(clean_queue, ack["id"], actor="steward", note="have another go")

    rc = cli.main(["--dsn", store.dsn(), "show", str(ack["id"])])

    captured = capsys.readouterr()
    assert rc == 0
    assert schema.EVENT_REQUEUED in captured.out
    assert "by steward" in captured.out
    assert "have another go" in captured.out


# ── claim ────────────────────────────────────────────────────────────────────────────────────────
def test_cli_claim_reports_nothing_when_the_queue_is_empty(clean_queue, capsys):
    rc = cli.main(["--dsn", store.dsn(), "claim"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "nothing to claim" in captured.out


def test_cli_claim_claims_one_item_and_never_finishes_it(clean_queue, capsys):
    ack = _submit(clean_queue)

    rc = cli.main(["--dsn", store.dsn(), "--json", "claim"])

    captured = capsys.readouterr()
    assert rc == 0
    # the JSON item (indent=2, multi-line) is followed by a plain "exiting WITHOUT finishing..."
    # line — `raw_decode` parses the leading JSON value and stops, ignoring the trailing text.
    item, _ = json.JSONDecoder().raw_decode(captured.out)
    assert item["id"] == ack["id"]
    assert item["status"] == "claimed"
    # the CLI drains nothing: the row stays claimed, it never files it
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status FROM capture_queue WHERE id = %s", (ack["id"],))
        assert cur.fetchone()[0] == "claimed"


def test_cli_claim_with_hold_prints_the_holding_and_exit_messages(clean_queue, capsys):
    _submit(clean_queue)
    rc = cli.main(["--dsn", store.dsn(), "claim", "--hold", "0.05", "--visibility-timeout", "1"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "holding the claim for 0.05s" in captured.out
    assert "exiting WITHOUT finishing" in captured.out


# ── claim --hold interrupted (Ctrl-C): the invited case, `_report_orphaned_lease` ───────────────
# Pure-unit half: `time.sleep` monkeypatched to raise `KeyboardInterrupt` immediately — fast and
# deterministic for the MESSAGE SHAPE and the DB-state assertions. The "no traceback reaches the
# operator" claim itself can only be proven by a REAL interrupted process (below); a monkeypatch
# never lets an exception escape in the first place, so it cannot be evidence for that specific
# claim — only for what the message says once caught.
def _raise_keyboard_interrupt(_seconds) -> None:
    raise KeyboardInterrupt


def test_cli_claim_hold_interrupted_json_reports_orphaned_lease_and_exits_130(
        clean_queue, capsys, monkeypatch):
    ack = _submit(clean_queue)
    monkeypatch.setattr(cli.time, "sleep", _raise_keyboard_interrupt)

    rc = cli.main(["--dsn", store.dsn(), "--json", "claim", "--hold", "5",
                   "--visibility-timeout", "300"])

    assert rc == 130
    out = capsys.readouterr().out
    # --json prints TWO JSON values here: the claimed item first, then (after the interrupt) the
    # claim_interrupted event — decode the first, then decode again from where it left off.
    decoder = json.JSONDecoder()
    item, _ = decoder.raw_decode(out)
    assert item["id"] == ack["id"]
    # a plain "holding the claim..." line sits between the two JSON values (indent=2, so each
    # top-level object opens with a lone "{" line) — find the SECOND such opening and decode from there.
    lines = out.splitlines(keepends=True)
    second_open = [i for i, line in enumerate(lines) if line.strip() == "{"][1]
    event, _ = decoder.raw_decode("".join(lines[second_open:]))
    assert event["event"] == "claim_interrupted"
    assert event["id"] == ack["id"]
    assert event["status"] == "claimed"
    assert event["orphaned_lease"] is True
    assert event["visibility_timeout_s"] == 300
    # the regression itself: the recovery command names 0, never the configured 300
    assert "stigmergy-queue reclaim --visibility-timeout 0" in event["recovers"]
    assert "reclaim --visibility-timeout 300" not in event["recovers"]


def test_cli_claim_hold_interrupted_human_readable_reports_orphaned_lease_and_exits_130(
        clean_queue, capsys, monkeypatch):
    ack = _submit(clean_queue)
    monkeypatch.setattr(cli.time, "sleep", _raise_keyboard_interrupt)

    rc = cli.main(["--dsn", store.dsn(), "claim", "--hold", "5", "--visibility-timeout", "300"])

    captured = capsys.readouterr()
    assert rc == 130
    assert f"interrupted while holding the claim on #{ack['id']}" in captured.out
    assert f"#{ack['id']} is still 'claimed'" in captured.out
    assert "stigmergy-queue reclaim --visibility-timeout 0" in captured.out
    assert "reclaim --visibility-timeout 300" not in captured.out


def test_cli_claim_hold_interrupted_leaves_the_row_genuinely_claimed_with_its_lease_orphaned(
        clean_queue, monkeypatch):
    """The message's claim about the world must be true, not just well-worded: after the
    interrupt, the row really is still `claimed`, on the SAME delivery, with nothing finishing
    it."""
    ack = _submit(clean_queue)
    monkeypatch.setattr(cli.time, "sleep", _raise_keyboard_interrupt)

    rc = cli.main(["--dsn", store.dsn(), "claim", "--hold", "5"])

    assert rc == 130
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status, attempts, finished_at FROM capture_queue WHERE id = %s",
                    (ack["id"],))
        status, attempts, finished_at = cur.fetchone()
    assert status == "claimed"
    assert attempts == 1
    assert finished_at is None


# ── real SIGINT to a real subprocess — the honest way to prove this. A monkeypatched time.sleep
# (above) is fast and precise for message shape and DB state; only a genuinely interrupted process
# can prove "no traceback reaches the operator" and that the exit code a real shell would observe
# is actually 130. ──────────────────────────────────────────────────────────────────────────────
def test_cli_claim_real_sigint_during_hold_exits_130_no_traceback_row_stays_claimed(clean_queue):
    ack = _submit(clean_queue)
    proc = subprocess.Popen(
        [*_queue_cli_command(), "--dsn", store.dsn(), "claim", "--hold", "10",
         "--visibility-timeout", "300"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert _read_until(proc.stdout, "holding the claim"), (
            "the subprocess never reported holding the claim — nothing to interrupt")
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 130
    assert err == ""                                     # the invited interrupt reports on STDOUT
    assert "Traceback" not in out and "Traceback" not in err
    assert f"interrupted while holding the claim on #{ack['id']}" in out
    assert f"#{ack['id']} is still 'claimed'" in out
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status, attempts FROM capture_queue WHERE id = %s", (ack["id"],))
        status, attempts = cur.fetchone()
    assert status == "claimed"
    assert attempts == 1


def test_cli_claim_real_sigint_json_recovery_string_is_visibility_timeout_zero(clean_queue):
    """The second defect's own regression guard, against a REAL interrupted process: the
    recovery command names `--visibility-timeout 0`, never the `--visibility-timeout 300` this
    run was actually configured with."""
    ack = _submit(clean_queue)
    proc = subprocess.Popen(
        [*_queue_cli_command(), "--dsn", store.dsn(), "--json", "claim", "--hold", "10",
         "--visibility-timeout", "300"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert _read_until(proc.stdout, "holding the claim")
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 130
    assert err == ""
    # `_read_until` already consumed everything through the "holding the claim" line above, so
    # what `communicate()` returns here is exactly the claim_interrupted event's JSON, alone.
    event, _ = json.JSONDecoder().raw_decode(out)
    assert event["event"] == "claim_interrupted"
    assert event["id"] == ack["id"]
    assert "stigmergy-queue reclaim --visibility-timeout 0" in event["recovers"]
    assert "reclaim --visibility-timeout 300" not in event["recovers"]


def test_cli_real_sigint_while_connecting_exits_130_stderr_only_no_traceback():
    """The OTHER `main()` interrupt handler, real: a DSN pointed at a non-routable address (RFC
    5737 TEST-NET-1, reserved for documentation/examples — never expected to answer) hangs in
    `psycopg.connect`, giving a real window to deliver SIGINT before the connection attempt could
    possibly resolve on its own."""
    bad_dsn = "postgresql://stigmergy:stigmergy@192.0.2.1:5432/stigmergy?connect_timeout=20"
    proc = subprocess.Popen([*_queue_cli_command(), "--dsn", bad_dsn, "list"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.5)
    if proc.poll() is not None:
        proc.communicate()
        pytest.skip("the process did not hang connecting to a non-routable address in this "
                    "environment — cannot exercise the real-SIGINT-during-connect path here")
    proc.send_signal(signal.SIGINT)
    try:
        out, err = proc.communicate(timeout=25)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 130
    assert out == ""
    assert "stigmergy-queue: interrupted while connecting to the queue database" in err
    assert "Traceback" not in err


# ── the generic Ctrl-C net: both main() call sites, pure-unit (main()'s dispatch is monkeypatched
# directly — the real-subprocess tests above already prove the SAME shared `_interrupted()`
# helper against a real signal for the connect-time branch; racing a real SIGINT against a
# sub-millisecond local `list` call would be unreliable rather than honest, so that branch's
# message/exit-code/stream shape is proven here instead) ────────────────────────────────────────
def test_cli_connect_interrupted_exits_130_with_the_generic_message(capsys, monkeypatch):
    """The in-process complement to `test_cli_real_sigint_while_connecting_exits_130_stderr_only_
    no_traceback` above: that test proves the real signal really works end to end (and closes no
    coverage, since it runs in a separate process — same reason `test_mcp_adapter.py` exists
    beside the real stdio harness); this one is fast, deterministic, and closes it."""
    def boom(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_connect", boom)

    rc = cli.main(["--dsn", store.dsn(), "list"])

    captured = capsys.readouterr()
    assert rc == 130
    assert captured.out == ""
    assert "stigmergy-queue: interrupted while connecting to the queue database" in captured.err
    assert "Traceback" not in captured.err


def test_cli_generic_interrupt_during_a_subcommand_exits_130_stderr_only_stdout_untouched(
        clean_queue, capsys, monkeypatch):
    def boom(_conn, _args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cmd_list", boom)

    rc = cli.main(["--dsn", store.dsn(), "list"])

    captured = capsys.readouterr()
    assert rc == 130
    assert captured.out == ""                                    # --json stdout stays parseable
    assert "stigmergy-queue: interrupted during `list`" in captured.err
    assert "Traceback" not in captured.err


# ── reclaim ──────────────────────────────────────────────────────────────────────────────────────
def test_cli_reclaim_reports_released_and_failed_counts(clean_queue, capsys):
    _submit(clean_queue)
    queue.claim_next(clean_queue, visibility_timeout_s=0)

    rc = cli.main(["--dsn", store.dsn(), "--json", "reclaim", "--visibility-timeout", "0"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == {"released": 1, "failed": 0}


# ── the recovery advice's own claim about the world, made and broken: `--visibility-timeout 0`
# releases a row claimed MOMENTS ago; the configured, non-zero timeout does not — together, the
# exact property the wrong instruction violated ─────────────────────────────────────────────────
def test_cli_reclaim_visibility_timeout_zero_releases_a_just_claimed_row(clean_queue, capsys):
    ack = _submit(clean_queue)
    queue.claim_next(clean_queue, visibility_timeout_s=300)   # fresh — NOT already-expired

    rc = cli.main(["--dsn", store.dsn(), "--json", "reclaim", "--visibility-timeout", "0"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == {"released": 1, "failed": 0}
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status FROM capture_queue WHERE id = %s", (ack["id"],))
        assert cur.fetchone()[0] == "queued"


def test_cli_reclaim_visibility_timeout_300_does_not_release_a_fresh_claim(clean_queue, capsys):
    """The bug's own scenario, reproduced: the SAME fresh claim, reclaimed with the CONFIGURED
    (300s) timeout instead of 0 — releases nothing, which is exactly why the old message's advice
    ('reclaim --visibility-timeout 300') did nothing at second zero."""
    ack = _submit(clean_queue)
    queue.claim_next(clean_queue, visibility_timeout_s=300)

    rc = cli.main(["--dsn", store.dsn(), "--json", "reclaim", "--visibility-timeout", "300"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == {"released": 0, "failed": 0}
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status FROM capture_queue WHERE id = %s", (ack["id"],))
        assert cur.fetchone()[0] == "claimed"


# ── purge ────────────────────────────────────────────────────────────────────────────────────────
def _backdated_terminal_row(conn):
    ack = _submit(conn)
    claimed = queue.claim_next(conn)
    queue.finish(conn, ack["id"], status=schema.FAILED, expected_attempts=claimed["attempts"])
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET finished_at = now() - make_interval(days => 45)"
                    " WHERE id = %s", (ack["id"],))
    return ack["id"]


def test_cli_purge_dry_run_reports_without_mutating(clean_queue, capsys):
    old_id = _backdated_terminal_row(clean_queue)

    rc = cli.main(["--dsn", store.dsn(), "--json", "purge", "--dry-run", "--older-than-days", "30"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["dry_run"] is True
    assert old_id in out["ids"]
    with clean_queue.cursor() as cur:
        cur.execute("SELECT payload IS NULL FROM capture_queue WHERE id = %s", (old_id,))
        assert cur.fetchone()[0] is False   # untouched


def test_cli_purge_deletes_payload_and_hints(clean_queue, capsys):
    old_id = _backdated_terminal_row(clean_queue)

    rc = cli.main(["--dsn", store.dsn(), "--json", "purge", "--older-than-days", "30"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["purged"] == 1
    with clean_queue.cursor() as cur:
        cur.execute("SELECT payload IS NULL, hints IS NULL FROM capture_queue WHERE id = %s",
                    (old_id,))
        payload_null, hints_null = cur.fetchone()
    assert payload_null and hints_null


def test_cli_purge_human_readable_names_the_survivors(clean_queue, capsys):
    _backdated_terminal_row(clean_queue)
    rc = cli.main(["--dsn", store.dsn(), "purge", "--older-than-days", "30"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "purged payload+hints of 1 terminal submission" in captured.out
    assert "result_ref survive" in captured.out


# ── the steward's drain: requeue / resolve / reject ─────────────────────────────────────────────
def _parked_row(conn, *, status=schema.TRIAGE):
    ack = _submit(conn)
    claimed = queue.claim_next(conn)
    queue.finish(conn, ack["id"], status=status, expected_attempts=claimed["attempts"],
                error="which entity?" if status == schema.TRIAGE else "")
    return ack["id"]


def test_cli_requeue_json_reports_the_row_back_in_the_queue(clean_queue, capsys):
    row_id = _parked_row(clean_queue)

    rc = cli.main(["--dsn", store.dsn(), "--json", "requeue", str(row_id), "--by", "steward",
                  "--note", "try again"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"] == "queued"
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status FROM capture_queue WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "queued"


def test_cli_requeue_human_readable_names_the_row_and_the_unchanged_attempts(clean_queue, capsys):
    row_id = _parked_row(clean_queue)

    rc = cli.main(["--dsn", store.dsn(), "requeue", str(row_id), "--by", "steward"])

    captured = capsys.readouterr()
    assert rc == 0
    assert f"requeued #{row_id}" in captured.out
    assert "attempts unchanged" in captured.out
    assert "claimable now" in captured.out


def test_cli_resolve_json_echoes_page_and_commit(clean_queue, capsys):
    row_id = _parked_row(clean_queue)

    rc = cli.main(["--dsn", store.dsn(), "--json", "resolve", str(row_id), "--by", "steward",
                  "--note", "folded into the page",
                  "--page", "wiki/entities/Jordan Reyes.md", "--commit", "abc123"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"] == "resolved"
    assert out["warning"] == ""
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status, result_ref, report FROM capture_queue WHERE id = %s",
                    (row_id,))
        status, result_ref, report = cur.fetchone()
    assert status == "resolved"
    assert result_ref == "wiki/entities/Jordan Reyes.md@abc123"
    assert "wiki/entities/Jordan Reyes.md@abc123" in report["summary"]


def test_cli_resolve_human_readable_prints_the_page_and_commit(clean_queue, capsys):
    row_id = _parked_row(clean_queue)

    rc = cli.main(["--dsn", store.dsn(), "resolve", str(row_id), "--by", "steward",
                  "--note", "folded in", "--page", "wiki/entities/X.md",
                  "--commit", "abc123"])

    captured = capsys.readouterr()
    assert rc == 0
    assert f"resolved #{row_id}" in captured.out
    assert "wiki/entities/X.md" in captured.out
    assert "abc123" in captured.out
    assert captured.err == ""   # no missing-pointer warning when both are given


def test_cli_resolve_with_no_page_and_no_commit_warns_on_stderr(clean_queue, capsys):
    """The missing-pointer warning is a real finding, not decoration (`_cmd_resolve`'s own
    docstring): `resolve` still does what it was asked, but says out loud what that costs."""
    row_id = _parked_row(clean_queue)

    rc = cli.main(["--dsn", store.dsn(), "resolve", str(row_id), "--by", "steward", "--note", "done"])

    captured = capsys.readouterr()
    assert rc == 0
    assert f"resolved #{row_id}" in captured.out
    assert "no --page and no --commit" in captured.err


def test_cli_resolve_json_warning_field_is_empty_string_when_a_pointer_is_given(clean_queue,
                                                                                capsys):
    row_id = _parked_row(clean_queue)
    cli.main(["--dsn", store.dsn(), "--json", "resolve", str(row_id), "--by", "steward",
             "--note", "done", "--commit", "abc123"])
    out = json.loads(capsys.readouterr().out)
    assert out["warning"] == ""


def test_cli_resolve_json_warning_field_names_the_gap_when_neither_is_given(clean_queue, capsys):
    row_id = _parked_row(clean_queue)
    cli.main(["--dsn", store.dsn(), "--json", "resolve", str(row_id), "--by", "steward",
             "--note", "done"])
    out = json.loads(capsys.readouterr().out)
    assert "no --page and no --commit" in out["warning"]


def test_cli_reject_json_records_the_reason(clean_queue, capsys):
    row_id = _parked_row(clean_queue)

    rc = cli.main(["--dsn", store.dsn(), "--json", "reject", str(row_id), "--by", "steward",
                  "--reason", "wrong venue"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"] == "rejected"
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status, report FROM capture_queue WHERE id = %s", (row_id,))
        status, report = cur.fetchone()
    assert status == "rejected"
    assert "wrong venue" in report["summary"]


def test_cli_reject_human_readable_names_the_actor(clean_queue, capsys):
    row_id = _parked_row(clean_queue)
    rc = cli.main(["--dsn", store.dsn(), "reject", str(row_id), "--by", "steward",
                  "--reason", "no"])
    captured = capsys.readouterr()
    assert rc == 0
    assert f"rejected #{row_id}" in captured.out
    assert "by: steward" in captured.out


# ── the drain's refusals surface as ONE clean line through main()'s `except CaptureError` ───────
def test_cli_requeue_a_claimed_row_is_refused_with_exit_1_and_no_traceback(clean_queue, capsys):
    row_id = _parked_row(clean_queue)
    cli.main(["--dsn", store.dsn(), "requeue", str(row_id), "--by", "steward"])   # -> queued
    queue.claim_next(clean_queue)                                             # -> claimed

    rc = cli.main(["--dsn", store.dsn(), "requeue", str(row_id), "--by", "steward"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "stigmergy-queue:" in captured.err
    assert "currently claimed" in captured.err
    assert "Traceback" not in captured.err


def test_cli_resolve_a_nonexistent_id_is_refused_with_exit_1(clean_queue, capsys):
    rc = cli.main(["--dsn", store.dsn(), "resolve", "999999999", "--by", "steward", "--note", "x"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "does not exist" in captured.err


def test_cli_reject_a_queued_row_is_refused_never_parked(clean_queue, capsys):
    ack = _submit(clean_queue)   # queued — never claimed, never parked
    rc = cli.main(["--dsn", store.dsn(), "reject", str(ack["id"]), "--by", "steward",
                  "--reason", "x"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "acts only on a PARKED row" in captured.err


# ── SIGINT during a disposition exits cleanly, no traceback ─────────────────────────────────────
def test_cli_generic_interrupt_during_resolve_exits_130_no_traceback_row_untouched(
        clean_queue, capsys, monkeypatch):
    """The same generic Ctrl-C net proven above for `list`
    (`test_cli_generic_interrupt_during_a_subcommand_exits_130_stderr_only_stdout_untouched`),
    exercised on `resolve` specifically: a disposition is a single UPDATE statement (module
    docstring: "nothing was left half-written"), so an interrupt here must leave the row exactly
    as it was — still parked, not resolved."""
    row_id = _parked_row(clean_queue)

    def boom(_conn, _args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cmd_resolve", boom)

    rc = cli.main(["--dsn", store.dsn(), "resolve", str(row_id), "--by", "steward", "--note", "x"])

    captured = capsys.readouterr()
    assert rc == 130
    assert captured.out == ""
    assert "stigmergy-queue: interrupted during `resolve`" in captured.err
    assert "Traceback" not in captured.err
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status FROM capture_queue WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == schema.TRIAGE   # left exactly as it was, not half-resolved


# ── build_parser: the argument surface itself (a regression here breaks every subcommand) ──────
def test_build_parser_status_choices_match_the_schema_vocabulary():
    parser = cli.build_parser()
    status_action = next(a for a in parser._subparsers._group_actions[0].choices["list"]._actions
                         if a.dest == "status")
    assert tuple(status_action.choices) == schema.STATUSES


def test_build_parser_requires_a_subcommand():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


# ── retention.purge is reachable from BOTH the CLI and a direct library call, same result ──────
def test_cli_purge_and_direct_retention_purge_agree(clean_queue, capsys):
    old_id = _backdated_terminal_row(clean_queue)
    cli.main(["--dsn", store.dsn(), "--json", "purge", "--dry-run", "--older-than-days", "30"])
    via_cli = json.loads(capsys.readouterr().out)
    via_library = retention.purge(clean_queue, older_than_days=30, dry_run=True)
    assert via_cli["ids"] == via_library["ids"] == [old_id]


# ── reclaim states its own horizon, or refuses ────────────────────────────────────────────────
def test_cli_reclaim_without_a_horizon_refuses_and_says_what_to_run(clean_queue, capsys):
    """OLD BEHAVIOUR: a bare `stigmergy-queue reclaim` swept against 300s — a number this CLI picked
    because it was the queue's own claim default, not because it had anything to do with the
    worker whose work it was seizing. A capture held 400s into a 900s lease was requeued while its
    worker ran. There is no safe default, so the command asks.

    ISSUE #30, a second defect in the SAME refusal: it pointed the operator at
    `$STIGMERGY_LIBRARIAN_VISIBILITY_TIMEOUT` — a variable this repo reads NOWHERE (this message was
    its only occurrence in the whole tree). An operator who set it got silently nothing back: the
    worker's real lease is DERIVED from `$STIGMERGY_LIBRARIAN_TIMEOUT_S` (900s at the class default;
    e.g. staging's 600s budget derives 1500s — `librarian.config.minimum_visibility_timeout_s`), and
    the resolved number is what `stigmergy-librarian status --json` prints as `visibility_timeout_s`
    (`docs/reference/operator-runbook.md`'s "dead worker mid-item" drill already teaches this exact
    route). The refusal must name the real source, never the dead one.
    """
    _submit(clean_queue)
    rc = cli.main(["--dsn", store.dsn(), "reclaim"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "needs --visibility-timeout" in captured.err
    assert "no safe default" in captured.err

    # the two canonical horizons are still both named: 0 right after a kill, and the worker's own
    # lease to sweep only genuinely abandoned claims ...
    assert "stigmergy-queue reclaim --visibility-timeout 0" in captured.err
    assert "worker's own lease" in captured.err

    # ... but the SECOND horizon now points at its real source, not a variable nothing reads
    # (issue #30): the derived lease, read through the command that actually answers it.
    assert "STIGMERGY_LIBRARIAN_VISIBILITY_TIMEOUT" not in captured.err, (
        "the refusal still advertises a variable this repo reads nowhere (issue #30)")
    assert "STIGMERGY_LIBRARIAN_TIMEOUT_S" in captured.err
    assert "stigmergy-librarian status --json" in captured.err
    assert "visibility_timeout_s" in captured.err


def test_cli_reclaim_refusal_names_commands_that_actually_run(clean_queue, capsys):
    """A refusal that tells a human to run something is an executable promise: both invocations
    the message prints are parsed out of it and run here."""
    cli.main(["--dsn", store.dsn(), "reclaim"])
    message = capsys.readouterr().err

    suggested = [line.strip() for line in message.splitlines()
                 if line.strip().startswith("stigmergy-queue reclaim")]
    assert len(suggested) == 2, f"the refusal stopped naming two commands: {suggested}"

    for command in suggested:
        argv = ["--dsn", store.dsn(), *command.split()[1:]]
        assert cli.main(argv) == 0, f"the refusal suggested `{command}`, which does not work"


def test_cli_reclaim_with_an_explicit_horizon_still_releases(clean_queue, capsys):
    """The benign twin: making the flag mandatory must not break the thing operators actually do
    after killing a worker."""
    ack = _submit(clean_queue)
    queue.claim_next(clean_queue, visibility_timeout_s=0)
    rc = cli.main(["--dsn", store.dsn(), "--json", "reclaim", "--visibility-timeout", "0"])
    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["released"] == 1
    assert queue.current_status(clean_queue, ack["id"]) == schema.QUEUED
