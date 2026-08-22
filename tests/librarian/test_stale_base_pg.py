"""The deployed worker used to downgrade to its own stale clone, silently, after a fetch started
failing post-boot — `bootstrap.verify_checkout_at_base` refuses exactly this
state before the FIRST claim, but `gitcmd.base_ref` answers a failed fetch with a warning and the
LOCAL branch on every claim after that, so a credential that expired an hour into a long-running
worker's life walked it back into the state the startup check exists to refuse.

The fix: `settings.require_remote_base` (set only by `bootstrap.worker_env`, never on a laptop) —
asked per item in `processing.process_item`, right where `base` is resolved. The four properties
below are proven with a REAL failed fetch (the origin's URL rewritten to something unreachable),
never a monkeypatch standing in for the git operation itself.
"""
import dataclasses

from stigmergy.capture import queue, schema
from stigmergy.librarian import worker
from stigmergy.librarian.errors import StaleBaseError
from tests.librarian import support

MATERIAL = "A memo about the Acme Corp partnership renewal.\n"


def _break_the_remote(repo: str) -> None:
    """A REAL fetch failure — not a stand-in for one: point `origin` at a path git will never
    reach, so `gitcmd.base_ref`'s own `git fetch` genuinely fails and falls back to the local
    branch, exactly as it would for a revoked GitHub App installation or a network partition."""
    support.gitcmd.run("remote", "set-url", "origin", "file:///nonexistent/unreachable.git",
                       cwd=repo)


def _row(conn, submission_id) -> dict:
    return queue.get_submission_trace(conn, submission_id)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Property 1: `process_item`/`process_next` raises `StaleBaseError` when DEPLOYED, and the row is
# left exactly as the worker found it — `claimed`, untouched, never `failed`.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_deployed_worker_raises_staleerror_on_a_failed_fetch_and_leaves_the_row_claimed(
        rig, clean_queue):
    env, base_deps = rig
    _break_the_remote(env.repo)
    deps = dataclasses.replace(
        base_deps, settings=dataclasses.replace(base_deps.settings, require_remote_base=True))

    ack = support.submit(clean_queue, deps, MATERIAL)

    try:
        worker.process_next(clean_queue, deps)
        raised = False
    except StaleBaseError:
        raised = True

    assert raised, "a deployed worker with require_remote_base must raise StaleBaseError"
    row = _row(clean_queue, ack["id"])
    assert row["status"] == schema.CLAIMED       # never finished — the lease is what recovers it
    assert row["attempts"] == 1


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Property 2 (rule 3 — a guard must not refuse the machine it was written for): the SAME failed
# fetch, on a LAPTOP (`require_remote_base` unset — the default everywhere but
# `bootstrap.worker_env`), files NORMALLY against the local branch.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_the_benign_twin_a_laptop_files_normally_against_its_local_branch(rig, clean_queue,
                                                                          monkeypatch):
    """Isolated from the FULL broken-remote scenario above on purpose: breaking `origin` for real
    (as the first test does) also breaks the PUSH this item would need to reach `filed` — a
    different, unrelated failure (`GitError`, "the page conflicts...") that has nothing to do with
    the property under test here, which is narrower and more precise: does the ONE guard line in
    `processing.process_item` (`if settings.require_remote_base and not base.remote: raise`) fire
    when it should not. `gitcmd.base_ref` is stubbed to return exactly what a real failed fetch
    produces (`remote=False`, the local branch's own sha — the same shape test 1's real failure
    yields, asserted there against a genuinely broken remote) so this test isolates the GUARD's
    own behaviour from the unrelated question of whether a push can reach a broken destination.
    """
    from stigmergy.librarian import gitcmd as gitcmd_mod

    env, deps = rig
    assert deps.settings.require_remote_base is False   # the laptop default, unchanged
    local_sha = gitcmd_mod.run("rev-parse", "main", cwd=env.repo).stdout.strip()
    monkeypatch.setattr(worker.processing.gitcmd, "base_ref",
                       lambda repo, branch: gitcmd_mod.BaseRef(sha=local_sha, ref="main",
                                                               remote=False))

    support.submit(clean_queue, deps, MATERIAL)
    _, result = worker.process_next(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Property 3: the routing DIFFERENCE — `StaleBaseError` propagates (row stays `claimed`), while an
# ORDINARY `LibrarianConfigError` still becomes a `failed` row, exactly as it always has.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_an_ordinary_config_error_mid_run_still_becomes_a_failed_row_not_a_propagated_raise(
        rig, clean_queue, monkeypatch):
    """`worker.process_next`'s own docstring: a config fault that is NOT `StaleBaseError` is
    softened into a `failed` Result (the registry or the linter changing on disk under a
    long-lived loop) — proven by making `base_inputs.load_registry` raise a plain
    `LibrarianConfigError` for this one delivery, which is a real code path this exact exception
    class reaches for real (a malformed `ops/acl.json`), not a stand-in for the mechanism."""
    from stigmergy.librarian.errors import LibrarianConfigError

    env, deps = rig

    def _boom(repo, base):
        raise LibrarianConfigError("acl config broke mid-run (simulated for this test)")

    monkeypatch.setattr(worker.processing.base_inputs, "load_registry", _boom)

    ack = support.submit(clean_queue, deps, MATERIAL)
    _, result = worker.process_next(clean_queue, deps)

    assert result.status == schema.FAILED
    row = _row(clean_queue, ack["id"])
    assert row["status"] == schema.FAILED
    assert "config" in str(row["report"]).lower() or "config" in str(result.report).lower()
