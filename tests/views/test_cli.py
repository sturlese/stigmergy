"""`stigmergy-views` — argparse shape, refusals, exit codes."""
import asyncio
import json
import os

import pytest

from stigmergy.kernel.registry import Registry
from stigmergy.views import cli, regenerate
from tests.views.conftest import (
    FakeConn,
    build_repo,
    git,
    registry_of,
    remote_files,
    remote_log,
)


def test_regenerate_requires_exactly_one_target():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["regenerate"])                       # none given -> required group fires
    with pytest.raises(SystemExit):
        parser.parse_args(["regenerate", "--entity", "acme-corp", "--all"])   # two given -> mutex


def test_regenerate_accepts_each_target_alone():
    parser = cli.build_parser()
    assert parser.parse_args(["regenerate", "--entity", "acme-corp"]).entity == "acme-corp"
    assert parser.parse_args(["regenerate", "--stale"]).stale is True
    assert parser.parse_args(["regenerate", "--all"]).all is True
    assert parser.parse_args(["regenerate", "--sweep"]).sweep is True


def test_sweep_is_a_fourth_target_not_a_widening_of_stale():
    """`--stale` names a population `gardener.checks.check_stale_views` reuses verbatim, and
    `--stale --force` already carries a documented widening of its own. A third meaning on the same
    flag is how two readers end up disagreeing about what "stale" is, so the union got its own
    target — mutually exclusive with the other three like any of them."""
    parser = cli.build_parser()
    assert parser.parse_args(["regenerate", "--sweep"]).stale is False
    with pytest.raises(SystemExit):
        parser.parse_args(["regenerate", "--sweep", "--stale"])


def test_repo_refuses_a_non_git_directory(tmp_path):
    from stigmergy.views.errors import ViewError

    class _Args:
        repo = str(tmp_path)

    with pytest.raises(ViewError, match="not a git checkout"):
        cli._repo(_Args())


def test_repo_accepts_a_git_worktree_where_dot_git_is_a_file_not_a_directory(tmp_path):
    """`git worktree add` checkouts carry a `.git` FILE (a `gitdir: ...` pointer), not a
    directory — `isdir` alone refused a genuine worktree with the same "not a git checkout"
    message a plain non-git directory gets."""
    (tmp_path / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/x\n")

    class _Args:
        repo = str(tmp_path)

    assert cli._repo(_Args()) == os.path.abspath(str(tmp_path))


def test_who_pairs_display_name_and_id():
    assert cli._who("acme-corp", "Acme Corp") == "Acme Corp (`acme-corp`)"


def test_main_exits_2_when_the_database_is_unreachable(monkeypatch):
    monkeypatch.setattr(cli, "_connect", lambda args: (_ for _ in ()).throw(RuntimeError("no db")))
    assert cli.main(["--dsn", "postgresql://nope/nope", "regenerate", "--all"]) == cli.EXIT_CANNOT_RUN


# ── --force must actually widen --stale's population ────────────────────────────────────────────
class _Args:
    def __init__(self, repo, *, stale=False, all_=False, sweep=False, entity=None, force=False,
                 as_json=False):
        self.repo = repo
        self.dsn = None
        self.branch = "main"
        self.json = as_json
        self.entity = entity
        self.stale = stale
        self.all = all_
        self.sweep = sweep
        self.force = force


def test_stale_force_widens_the_population_to_every_entity_with_a_view(tmp_path, monkeypatch):
    """`--force`'s own help text says it "widens their population to every checked entity", but
    `entity_ids` used to be computed from `list_stale_entities` regardless of `--force` — so
    `--stale --force` behaved IDENTICALLY to `--stale` alone: the natural spelling of the retry
    lever the flag exists for silently did nothing, because an entity already in the stale
    population regenerates with or without `--force` (it isn't a no-op to begin with)."""
    remote, clone = build_repo(str(tmp_path / "git"))
    registry = registry_of()
    monkeypatch.setattr(cli, "_registry", lambda repo: registry)

    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    assert regenerate.list_stale_entities(clone) == []   # confirmed: nothing is stale

    log_before = remote_log(remote)
    conn = FakeConn()
    exit_code = cli._cmd_regenerate(conn, _Args(clone, stale=True, force=True))
    assert exit_code == 0
    assert remote_log(remote) != log_before, (
        "--stale --force must regenerate a FRESH entity too — the population it widens to")


def test_stale_without_force_still_only_regenerates_what_is_actually_stale(tmp_path, monkeypatch):
    """The control for the test above: `--stale` alone (no `--force`) over the same fresh view
    must still be the honest no-op it always was — the widening is `--force`'s alone to trigger."""
    remote, clone = build_repo(str(tmp_path / "git"))
    registry = registry_of()
    monkeypatch.setattr(cli, "_registry", lambda repo: registry)

    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    log_before = remote_log(remote)
    conn = FakeConn()
    cli._cmd_regenerate(conn, _Args(clone, stale=True, force=False))
    assert remote_log(remote) == log_before


# ── a removal states WHICH cause it was ─────────────────────────────────────────────────────────
def test_a_removal_is_reported_with_its_own_cause_never_one_hardcoded_sentence(tmp_path, capsys):
    """**Old behaviour: every removal printed the members-gone sentence.** `RegenOutcome.message`
    was `""` for removals, so `_outcome_line` said "no anchored pages remain — view removed" and
    `_report_single` said "the last page anchored to … is gone (superseded or re-anchored
    elsewhere)" down BOTH roads — including the one where the pages are all still there and it is
    the ENTITY that was de-registered. The steward is then sent to look for pages that never
    moved, and `--json` carried `"message": ""`, so no surface could tell the two apart.

    Driven by a REAL de-registration removal, not a hand-built outcome: what is being pinned is
    that the cause travels from the branch that knows it all the way to what the operator reads.
    """
    remote, clone = build_repo(str(tmp_path / "git"))
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry_of()))
    outcome = asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=Registry()))
    assert outcome.action == "removed"

    batch_line = cli._outcome_line(outcome)
    assert "de-registered" in batch_line
    assert "no anchored pages remain" not in batch_line

    assert cli._report_single(outcome, _Args(clone)) == 0
    out = capsys.readouterr().out
    assert "de-registered" in out
    assert "the last page anchored to" not in out
    assert f"committed {outcome.commit[:12]}" in out   # the removal's own report is otherwise intact

    assert cli._report_single(outcome, _Args(clone, as_json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "removed"
    assert "de-registered" in payload["message"]   # was "" for every removal


# The de-registration road's own sentence: the pages are all still there.
_PAGES_UNTOUCHED = "its pages are untouched"
# The members-gone road's own sentence.
_LAST_PAGE_GONE = "the last page anchored to"
# The shared tail the two roads used to share, and could not both be true of.
_NOTHING_ANCHORS = "Nothing anchors"


def test_each_removal_road_prints_only_its_own_true_sentence(tmp_path, capsys):
    """**Old behaviour: `_report_single` closed EVERY removal with a shared tail — "Nothing
    anchors <entity> any more, so there is nothing left to summarize."** On the de-registration
    road that contradicts the very message printed one clause earlier, which says the entity's
    pages are untouched: the pages still anchor it, it is the REGISTRY that stopped governing
    them. The tail is gone; each road prints `o.message`, which already carries its own cause.

    Both roads driven for real, and asserted POSITIVELY as well as negatively — pinning only the
    absence of the old sentence would leave a report that says nothing at all passing."""
    remote, clone = build_repo(str(tmp_path / "git"))
    registry = registry_of()

    # Road 1 — the entity is de-registered; every page it anchored is still on disk.
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    deregistered = asyncio.run(regenerate.regenerate_entity(clone, "acme-corp",
                                                            registry=Registry()))
    assert deregistered.action == "removed"
    assert cli._report_single(deregistered, _Args(clone)) == 0
    out = capsys.readouterr().out
    assert _PAGES_UNTOUCHED in out, "the de-registration road must state its OWN cause"
    assert _LAST_PAGE_GONE not in out
    assert _NOTHING_ANCHORS not in out, (
        "the pages still anchor it — this is the sentence that contradicted the one above it")
    assert f"committed {deregistered.commit[:12]}" in out   # the commit line survives the drop

    # Road 2 — the entity stays registered and its last anchored page goes away.
    remote2, clone2 = build_repo(str(tmp_path / "git2"))
    asyncio.run(regenerate.regenerate_entity(clone2, "acme-corp", registry=registry))
    git("rm", "--quiet", "-r", "wiki/entities", "wiki/decisions", cwd=clone2)
    git("commit", "--quiet", "-m", "chore: the last anchored pages go away", cwd=clone2)
    git("push", "--quiet", "origin", "main", cwd=clone2)
    no_members = asyncio.run(regenerate.regenerate_entity(clone2, "acme-corp", registry=registry))
    assert no_members.action == "removed"
    assert cli._report_single(no_members, _Args(clone2)) == 0
    out = capsys.readouterr().out
    assert _LAST_PAGE_GONE in out, "the members-gone road must state its OWN cause"
    assert _PAGES_UNTOUCHED not in out
    assert _NOTHING_ANCHORS not in out
    assert f"committed {no_members.commit[:12]}" in out


# ── --sweep: the union target, and the same entry point the worker's idle pass uses ─────────────
def test_sweep_creates_a_missing_view_that_stale_would_never_have_named(tmp_path, monkeypatch,
                                                                       capsys):
    """The operator-facing half of the population argument: `--stale` reports "0 entities with an
    existing view" and does nothing, while `--sweep` over the same repo writes the view."""
    remote, clone = build_repo(str(tmp_path / "git"))
    registry = registry_of()
    monkeypatch.setattr(cli, "_registry", lambda repo: registry)

    assert cli._cmd_regenerate(FakeConn(), _Args(clone, stale=True)) == 0
    capsys.readouterr()
    assert "views/acme-corp.md" not in remote_files(remote)

    assert cli._cmd_regenerate(FakeConn(), _Args(clone, sweep=True)) == 0
    out = capsys.readouterr().out
    assert cli.SWEEP_POPULATION in out
    assert "views/acme-corp.md" in remote_files(remote)


def test_sweep_on_a_converged_repo_says_so_and_commits_nothing(tmp_path, monkeypatch, capsys):
    remote, clone = build_repo(str(tmp_path / "git"))
    registry = registry_of()
    monkeypatch.setattr(cli, "_registry", lambda repo: registry)
    cli._cmd_regenerate(FakeConn(), _Args(clone, sweep=True))
    capsys.readouterr()
    log_before = remote_log(remote)

    assert cli._cmd_regenerate(FakeConn(), _Args(clone, sweep=True)) == 0

    out = capsys.readouterr().out
    assert "already match the corpus" in out
    assert "nothing regenerated, nothing committed" in out
    assert remote_log(remote) == log_before


def test_sweep_json_carries_the_ceiling_bookkeeping_keys(tmp_path, monkeypatch, capsys):
    """`--json`'s `stats` IS `job_runs.stats`, so the keys an operator reads on a terminal and the
    ones a run leaves behind cannot drift apart."""
    remote, clone = build_repo(str(tmp_path / "git"))
    monkeypatch.setattr(cli, "_registry", lambda repo: registry_of())

    assert cli._cmd_regenerate(FakeConn(), _Args(clone, sweep=True, as_json=True)) == 0

    stats = json.loads(capsys.readouterr().out)["stats"]
    assert stats["population"] == 1
    assert stats["deferred"] == 0
    assert stats["skip_reasons"] == []
