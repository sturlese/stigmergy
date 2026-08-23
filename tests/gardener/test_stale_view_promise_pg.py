"""A message containing a command is an executable promise — and so is a message that promises
something will happen without one.

`check_stale_views`' `suggested_action` used to be a runnable command (`stigmergy-views regenerate
--entity …`), and this file RAN it. the capture-is-the-approval change removed that CLI: the librarian worker converges
`views/` from state on its own idle branch, so what the finding now says is that it will take care
of itself. That is a promise with the same standing, and it is checked the same way — the sweep the
worker actually runs is run here, against the same repo the finding was computed from, and it
clears the exact condition the finding reported.

Needs real git (the sweep commits and pushes, and a faked git tree proves nothing about this
property). Postgres comes from the suite's own fixtures.
"""
import asyncio
import os

from stigmergy.gardener import checks
from stigmergy.views import regenerate
from tests.views.conftest import _COMMIT_ENV, FakeConn, build_repo, git, registry_of


def _seed_stale_view(clone: str) -> str:
    """A view file whose OWN `member_hash` no longer matches what the real member set computes —
    committed and pushed, so the checkout is git-clean (member-hash staleness and a dirty working
    tree are two independent concepts, and the sweep refuses to run on the latter)."""
    view_path = os.path.join(clone, "views", "acme-corp.md")
    os.makedirs(os.path.dirname(view_path), exist_ok=True)
    with open(view_path, "w", encoding="utf-8") as f:
        f.write('---\ntype: view\ntitle: "Acme Corp — view"\n'
                'member_hash: "not-the-real-hash"\n---\n\n# Acme Corp\n')
    git("add", "--all", cwd=clone)
    git("commit", "--quiet", "-m", "test: seed a stale view", cwd=clone, env=_COMMIT_ENV)
    git("push", "--quiet", cwd=clone)
    return view_path


def test_the_stale_view_finding_names_no_command_because_there_is_none_to_name(tmp_path):
    """**OLD BEHAVIOUR: the action was a backticked `stigmergy-views regenerate --entity <id>`.**
    That command does not exist any more, and a finding still naming it would be the worst kind of
    stale message — one an operator can copy, paste and be refused by their own shell."""
    _remote, clone = build_repo(str(tmp_path), entity_id="acme-corp", n_decisions=1)
    _seed_stale_view(clone)

    findings = checks.check_stale_views(clone)
    assert [f["subject"] for f in findings] == ["acme-corp"]
    action = findings[0]["suggested_action"]

    assert "`" not in action, "a code span here reads as a command to run, and there is none"
    assert "stigmergy-views" not in action
    assert "no command" in action


def test_the_sweep_the_worker_runs_clears_the_staleness_the_finding_reported(tmp_path,
                                                                            monkeypatch):
    """The promise itself, kept: the finding says the worker's next idle pass regenerates it, so
    that pass is run here — `views.regenerate.sweep`, the same entry point
    `librarian.worker.run_view_sweep` calls — and the condition is gone afterwards.

    Without this the finding would be prose: nothing else in the suite runs the sweep against a
    corpus a gardener check has just called stale."""
    monkeypatch.setenv("CLEAN_LLM", "fake")
    _remote, clone = build_repo(str(tmp_path), entity_id="acme-corp", n_decisions=1)
    _seed_stale_view(clone)
    assert [f["subject"] for f in checks.check_stale_views(clone)] == ["acme-corp"]

    result = asyncio.run(regenerate.sweep(clone, FakeConn(), registry=registry_of(),
                                          branch="main", guarded=False))

    assert result.stats["written"] >= 1, result.skip_reasons
    assert checks.check_stale_views(clone) == []
