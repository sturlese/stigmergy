"""`stigmergy-librarian once` — driven in-process through `cli.main(argv)` against real Postgres,
a real git repo/bare remote and the offline double (the default backend). At least one test per
surface runs on the DEFAULTS, the configuration an operator actually gets with no flag passed —
every filing test here relies on `--backend`'s own default rather than passing `double`
explicitly.

Real (MinIO) evidence is required here for the same reason `test_worker_signals.py` needs it:
`cli.main` builds its OWN `evidence_plane.store_from_env()` inside `_cmd_once` (mirrors
production — `cli.py`'s own module docstring), so a `MemoryEvidenceStore` this TEST process
built would be invisible to it. `worktree_root` is deliberately left at its default (there is no
CLI flag for it): a real invocation falls back to the system temp dir, and that fallback is
worth exercising rather than always overriding it, per the same rule.
"""
import json

import pytest

from stigmergy.capture import cli as queue_cli
from stigmergy.capture import queue, schema
from stigmergy.index import store
from stigmergy.librarian import cli, config
from tests import testdb
from tests.librarian import support


def _run(capsys, *argv):
    exit_code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return exit_code, out, err


@pytest.fixture()
def cli_rig(tmp_path, require_gitleaks, require_minio, clean_queue):
    """A real repo + bare remote and the real MinIO evidence store `cli.main` will itself
    resolve. Returns `(env, conn, argv)`; `conn` is a SEPARATE connection from the one `cli.main`
    opens and closes per invocation, used only for this test's own setup/assertions."""
    env, deps = support.build_rig(tmp_path, evidence=require_minio)
    argv = ["--dsn", testdb.dsn(), "--repo", env.repo]
    conn = store.connect(testdb.dsn())
    yield env, conn, argv
    conn.close()


def _submit(conn, evidence, material: str) -> dict:
    return queue.submit(conn, evidence, kind="raw", material=material, hints=None,
                        submitted_by="cli.tester@stigmergy.test")


def test_once_with_an_empty_queue_prints_the_shared_nothing_to_claim_sentence(capsys, cli_rig):
    _, _, argv = cli_rig
    exit_code, out, err = _run(capsys, *argv, "once")
    assert exit_code == 0
    # Byte-identical to `stigmergy-queue claim`'s own sentence, on its own line — two tools describing
    # the same state differently is how an operator learns to distrust both. The context line above
    # it is this tool's, not that sentence.
    assert cli.NOTHING_TO_CLAIM in out.splitlines()
    assert err == ""


# ── the base ref: named, not implied ──────────────────────────────────────────────────────────────
def test_once_reports_the_ref_the_worktree_will_branch_from(capsys, cli_rig):
    """A worktree branches from `origin/<branch>` when there is a remote, which is right for a
    service and is not what an operator assumes while looking at their own working copy. A walk
    once lost an item to exactly that gap — a skill commit that existed locally and not on the
    remote — so the ref is printed rather than inferred."""
    env, conn, argv = cli_rig
    exit_code, out, err = _run(capsys, *argv, "once")

    assert exit_code == 0
    line = next(line for line in out.splitlines() if line.startswith("filing into"))
    assert env.repo in line
    assert "origin/main@" in line


def test_the_reported_ref_is_the_commit_the_page_is_actually_filed_against(capsys, cli_rig):
    """The promise is executable: the sha the line names must be the parent of the commit the
    librarian produces, or the line is decoration."""
    env, conn, argv = cli_rig
    from stigmergy.capture import evidence as evidence_plane
    _submit(conn, evidence_plane.store_from_env(), "A capture filed against a named ref.")

    exit_code, out, err = _run(capsys, *argv, "once")
    assert exit_code == 0
    reported = next(line for line in out.splitlines()
                    if line.startswith("filing into")).rsplit("@", 1)[-1].strip()

    filed_sha = next(line for line in out.splitlines() if line.startswith("#")).split("@")[-1]
    filed_sha = filed_sha.split(",")[0].strip()
    parent = support.gitcmd.run("rev-parse", f"{filed_sha}^", cwd=env.repo).stdout.strip()
    assert parent.startswith(reported)


def test_json_output_carries_the_base_ref_instead_of_a_prose_line(capsys, cli_rig):
    """`--json` leads with the machine-readable value (this CLI's own convention), so the context
    goes INTO the object rather than in front of it."""
    env, conn, argv = cli_rig
    from stigmergy.capture import evidence as evidence_plane
    _submit(conn, evidence_plane.store_from_env(), "A capture reported as JSON.")

    exit_code, out, err = _run(capsys, *argv, "--json", "once")

    assert exit_code == 0
    event, _ = json.JSONDecoder().raw_decode(out)
    assert event["base"]["ref"] == "origin/main"
    assert len(event["base"]["commit"]) == 40
    assert "filing into" not in out


# ── the sweep: `once` recovers stranded claims, visibly ───────────────────────────────────────────
def test_once_sweeps_a_stranded_claim_back_to_the_queue_and_says_so(capsys, cli_rig, monkeypatch):
    """An interrupted item once sat `claimed` for fifty minutes past its lease. `queue.claim_next`
    has always swept on its own hot path, so the recovery was not missing — it was INVISIBLE, and
    `once` is the surface where that matters, because a walk drains by hand and an operator staring
    at a stuck row has no way to see whether anything is repairing it.

    The row is claimed for real and its `claimed_at` backdated an hour, which is what an interrupted
    run leaves behind — rather than lowering the lease, which `startup_checks` would refuse anyway.
    """
    env, conn, argv = cli_rig
    from stigmergy.capture import evidence as evidence_plane
    ack = _submit(conn, evidence_plane.store_from_env(), "A capture stranded by an interrupt.")
    queue.claim_next(conn, visibility_timeout_s=config.DEFAULT_VISIBILITY_TIMEOUT_S)
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '1 hour' WHERE id = %s",
                    (ack["id"],))
    assert _status_of(conn, ack["id"]) == schema.CLAIMED

    monkeypatch.setattr(cli.worker, "process_next", lambda *a, **k: None)
    exit_code, out, err = _run(capsys, *argv, "once")

    assert exit_code == 0
    assert "swept 1 stranded claim(s) back to the queue" in out
    # and the number in that line carries its human unit, not only its seconds
    assert f"{config.DEFAULT_VISIBILITY_TIMEOUT_S}s (15 min)" in out
    assert _status_of(conn, ack["id"]) == schema.QUEUED


def test_once_says_nothing_about_a_sweep_that_moved_nothing(capsys, cli_rig):
    """The no-op case stays silent. A line printed on every invocation is a line nobody reads."""
    _, _, argv = cli_rig
    exit_code, out, err = _run(capsys, *argv, "once")
    assert exit_code == 0
    assert "swept" not in out


def test_once_files_an_ordinary_capture_and_prints_the_report_prose(capsys, cli_rig):
    env, conn, argv = cli_rig
    from stigmergy.capture import evidence as evidence_plane
    _submit(conn, evidence_plane.store_from_env(), "A capture about Acme Corp, via the CLI.")

    exit_code, out, err = _run(capsys, *argv, "once")

    assert exit_code == 0
    assert any(line.startswith("#") for line in out.splitlines())
    assert schema.FILED in out
    assert err == ""


def test_once_json_output_leads_with_a_machine_readable_event(capsys, cli_rig):
    env, conn, argv = cli_rig
    from stigmergy.capture import evidence as evidence_plane
    _submit(conn, evidence_plane.store_from_env(), "A capture about Acme Corp, via JSON.")

    exit_code, out, err = _run(capsys, *argv, "--json", "once")

    assert exit_code == 0
    event, _ = json.JSONDecoder().raw_decode(out)
    assert event["status"] == schema.FILED
    assert "result_ref" in event and "report" in event


def test_the_backend_flag_itself_rejects_an_unknown_choice_at_parse_time(capsys, cli_rig):
    """`argparse`'s own `choices=agent_module.BACKENDS` on `--backend` refuses this before
    `config.Settings.from_args` is ever reached — the loudest, earliest place a typo can be
    caught. `argparse` reports this by raising `SystemExit(2)` directly out of `parse_args`,
    which `cli.main` does not (and must not) catch, since a malformed invocation is a shell/usage
    error, not a librarian error."""
    _, _, argv = cli_rig
    with pytest.raises(SystemExit) as exc_info:
        cli.main([*argv, "--backend", "not-a-real-backend", "once"])
    assert exc_info.value.code == 2
    _, err = capsys.readouterr()
    assert "not-a-real-backend" in err


def test_an_unknown_backend_via_the_env_var_is_rejected_at_startup_not_silently_defaulted(
        capsys, cli_rig, monkeypatch):
    """The ENV VAR fallback (`$STIGMERGY_LIBRARIAN_BACKEND`) has no `argparse` `choices=` guard —
    it is read straight into `config.Settings` (`config.py`'s own `from_args`) — so THIS is the
    path that actually reaches `worker.startup_checks`'s fail-closed validation
    (`agent_module.BACKENDS`), and the one worth proving does not silently fall through to the
    double or crash uninformatively."""
    _, _, argv = cli_rig
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_BACKEND", "not-a-real-backend")
    exit_code, out, err = _run(capsys, *argv, "once")
    assert exit_code == cli.EXIT_CONFIG
    assert "not-a-real-backend" in err
    assert "Traceback" not in err


def test_an_unreachable_database_prints_a_clean_local_message_and_exits_config(capsys):
    exit_code, out, err = _run(
        capsys, "--dsn", "postgresql://stigmergy:stigmergy@localhost:1/stigmergy_test", "once")
    assert exit_code == cli.EXIT_CONFIG
    assert "cannot reach the queue database" in err
    assert "Traceback" not in err


def test_a_missing_repo_is_a_config_error_naming_the_path(capsys, tmp_path, require_gitleaks,
                                                          clean_queue):
    missing = str(tmp_path / "does-not-exist")
    exit_code, out, err = _run(capsys, "--dsn", testdb.dsn(), "--repo", missing, "once")
    assert exit_code == cli.EXIT_CONFIG
    assert missing in err
    assert "Traceback" not in err


# ── interrupted `once`: what the message promises, and whether it is true ─────────────────────────
# `once` holds a claim while it works, so Ctrl-C leaves a real orphaned lease. The message that
# reports it named a duration without its value — "after the visibility timeout", which an operator
# cannot act on: not knowing whether that is ten seconds or fifteen minutes, they cannot choose
# between waiting and reclaiming.
#
# Same split `tests/capture/test_cli.py` documents for `claim --hold`: a monkeypatched interrupt is
# fast and exact for the MESSAGE and the resulting DB state, and the claim it cannot prove ("no
# traceback reaches a real operator") is already proven against real signals by
# `test_worker_signals.py`. What is proven HERE is the promise: the number is right, and the command
# the message names does what the message says it does.
def _claim_then_interrupt(conn, deps):
    """A real claim (so the row is genuinely `claimed` with a real lease), then Ctrl-C — exactly
    where an interrupt lands on a real walk: after the claim, inside the agent run."""
    queue.claim_next(conn, visibility_timeout_s=deps.settings.visibility_timeout_s,
                     max_attempts=deps.settings.max_attempts)
    raise KeyboardInterrupt


def _status_of(conn, submission_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM capture_queue WHERE id = %s", (submission_id,))
        return cur.fetchone()[0]


def test_an_interrupted_once_names_the_visibility_timeout_in_seconds(capsys, cli_rig, monkeypatch):
    """The defect itself, and a run on the DEFAULTS: no `--visibility-timeout` is passed, so this
    is the configuration an operator actually gets (900s = 2 x 300s agent attempts + 120s of gates
    + 180s headroom), asserted through `config` rather than retyped."""
    env, conn, argv = cli_rig
    from stigmergy.capture import evidence as evidence_plane
    ack = _submit(conn, evidence_plane.store_from_env(), "A capture interrupted mid-flight.")
    monkeypatch.setattr(cli.worker, "process_next", _claim_then_interrupt)

    exit_code, out, err = _run(capsys, *argv, "once")

    assert exit_code == cli.EXIT_INTERRUPTED
    assert f"{config.DEFAULT_VISIBILITY_TIMEOUT_S}s (15 min) visibility timeout" in err
    assert "Traceback" not in err
    # the row really is where the message says it is
    assert _status_of(conn, ack["id"]) == schema.CLAIMED


def test_an_interrupted_once_names_the_configured_timeout_not_the_class_default(capsys, cli_rig,
                                                                               monkeypatch):
    """The number printed comes from the RESOLVED configuration, so a `--visibility-timeout` on
    this very command line is the number the operator is told to wait for. Printing the default
    while running a different lease is the same class of lie as printing no number at all."""
    env, conn, argv = cli_rig
    from stigmergy.capture import evidence as evidence_plane
    _submit(conn, evidence_plane.store_from_env(), "A capture with a non-default lease.")
    monkeypatch.setattr(cli.worker, "process_next", _claim_then_interrupt)

    exit_code, out, err = _run(capsys, *argv, "once", "--visibility-timeout", "1234")

    assert exit_code == cli.EXIT_INTERRUPTED
    assert "1234s (20 min 34s) visibility timeout" in err
    assert f"{config.DEFAULT_VISIBILITY_TIMEOUT_S}s" not in err


def test_an_interrupted_once_still_refuses_to_claim_nothing_was_committed(capsys, cli_rig,
                                                                         monkeypatch):
    """The honesty half, which the fix had to keep: an interrupt can land after the push and before
    the row is finished, so the message sends the operator to `git log` instead of ruling it out."""
    env, conn, argv = cli_rig
    from stigmergy.capture import evidence as evidence_plane
    _submit(conn, evidence_plane.store_from_env(), "A capture that may already be pushed.")
    monkeypatch.setattr(cli.worker, "process_next", _claim_then_interrupt)

    exit_code, out, err = _run(capsys, *argv, "once")

    assert "already committed" in err
    assert "`git log`" in err


def test_an_interrupted_once_names_the_recovery_command_shared_with_stigmergy_queue(capsys, cli_rig,
                                                                                 monkeypatch):
    """One string, one place (`capture.cli.RECLAIM_NOW`). The argument matters and this repo has
    already shipped it wrong once: `--visibility-timeout 300` releases nothing at second zero, so
    two tools printing this advice must not be able to disagree about the number."""
    env, conn, argv = cli_rig
    from stigmergy.capture import evidence as evidence_plane
    _submit(conn, evidence_plane.store_from_env(), "A capture to be reclaimed by hand.")
    monkeypatch.setattr(cli.worker, "process_next", _claim_then_interrupt)

    exit_code, out, err = _run(capsys, *argv, "once")

    assert queue_cli.RECLAIM_NOW in err
    assert "stigmergy-queue reclaim --visibility-timeout 0" in err
    # and the warning that makes the advice safe to follow
    assert "another worker" in err


def test_the_reclaim_command_the_interrupt_message_names_really_returns_the_item(capsys, cli_rig,
                                                                                monkeypatch):
    """**A message containing a command is an executable promise.** So this test does not assert
    prose — it interrupts `stigmergy-librarian once`, takes
    the command out of the message it printed, RUNS it, and asserts the stranded item is back in the
    queue. Nothing else in the suite proves the librarian's recovery path works end to end."""
    env, conn, argv = cli_rig
    from stigmergy.capture import evidence as evidence_plane
    ack = _submit(conn, evidence_plane.store_from_env(), "A capture recovered by the named command.")
    monkeypatch.setattr(cli.worker, "process_next", _claim_then_interrupt)

    exit_code, out, err = _run(capsys, *argv, "once")
    assert exit_code == cli.EXIT_INTERRUPTED
    assert _status_of(conn, ack["id"]) == schema.CLAIMED

    # The command as the operator reads it off their terminal, split from that very message rather
    # than retyped here — a test that retypes it is testing a different string than the one printed.
    printed = next(line.strip() for line in err.splitlines()
                   if "stigmergy-queue reclaim" in line)
    recovery = printed[printed.index("stigmergy-queue"):].split()
    assert recovery[0] == "stigmergy-queue"
    rc = queue_cli.main(["--dsn", testdb.dsn(), "--json", *recovery[1:]])
    released = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert released == {"released": 1, "failed": 0}
    assert _status_of(conn, ack["id"]) == schema.QUEUED


def test_the_configured_lease_would_not_have_released_it_which_is_why_the_command_says_zero(
        capsys, cli_rig, monkeypatch):
    """The benign twin of the test above, the same scenario one layer up: the SAME stranded row,
    reclaimed with the CONFIGURED 900s lease instead of 0, releases nothing. That is exactly why
    the message names 0 — advice built from this run's lease would do nothing for fifteen minutes
    while claiming to work."""
    env, conn, argv = cli_rig
    from stigmergy.capture import evidence as evidence_plane
    ack = _submit(conn, evidence_plane.store_from_env(), "A capture the configured lease ignores.")
    monkeypatch.setattr(cli.worker, "process_next", _claim_then_interrupt)

    _run(capsys, *argv, "once")
    rc = queue_cli.main(["--dsn", testdb.dsn(), "--json", "reclaim",
                         "--visibility-timeout", str(config.DEFAULT_VISIBILITY_TIMEOUT_S)])
    result = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert result == {"released": 0, "failed": 0}
    assert _status_of(conn, ack["id"]) == schema.CLAIMED
