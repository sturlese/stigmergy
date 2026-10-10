"""Offline argument contract for index maintenance."""
import subprocess

import pytest

from stigmergy.index import build, cli
from stigmergy.index.errors import StigmergyIndexError


def test_index_main_requires_rebuild_and_repo(capsys):
    with pytest.raises(SystemExit):
        cli.index_main(["--repo", "somewhere"])
    with pytest.raises(SystemExit):
        cli.index_main(["--rebuild"])


def _commit_checkout(path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Index Test",
            "-c",
            "user.email=index@example.invalid",
            "commit",
            "-qm",
            "test fixture",
        ],
        cwd=path,
        check=True,
    )


def test_checked_repository_head_requires_exact_clean_root_and_controls(tmp_path):
    repo = tmp_path / "brain"
    note = repo / "wiki" / "notes" / "Seed.md"
    note.parent.mkdir(parents=True)
    note.write_text("seed\n", encoding="utf-8")
    _commit_checkout(repo)

    head = build._checked_repository_head(str(repo))
    assert len(head) == 40
    with pytest.raises(StigmergyIndexError, match="root"):
        build._checked_repository_head(str(note.parent))

    note.write_text("changed\n", encoding="utf-8")
    with pytest.raises(StigmergyIndexError, match="must match"):
        build._checked_repository_head(str(repo))


@pytest.mark.parametrize("remote", [
    "https://github.com/Acme/acme-brain.git",
    "https://github.com/acme/acme-brain",
    "git@github.com:acme/acme-brain.git",
    "ssh://git@github.com/acme/acme-brain.git",
    "https://x-access-token:secret@github.com/acme/acme-brain.git",
])
def test_every_spelling_of_a_github_remote_names_one_repository(remote):
    assert cli.github_repository(remote) == "acme/acme-brain"


def test_a_non_github_remote_names_no_repository():
    assert cli.github_repository("/srv/fixtures/brain.git") is None


def test_the_index_refuses_a_checkout_of_another_workspace(tmp_path, monkeypatch):
    """Rebuilding workspace B from workspace A's checkout would serve A's pages to B's readers."""
    from stigmergy.server import platform

    tenant = platform.Tenant(id="00000000-0000-4000-8000-0000000000aa", slug="beta", name="Beta",
                             status="active", repo="org/beta-brain", slack_team_id=None)

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    repo = tmp_path / "alpha"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "git@github.com:org/alpha-brain.git"], check=True)
    monkeypatch.setattr(cli.store, "connect", lambda dsn=None: Conn())
    monkeypatch.setattr(cli.platform, "ensure_platform_schema", lambda conn: None)
    monkeypatch.setattr(cli.platform, "resolve_tenant", lambda conn, slug: tenant)
    monkeypatch.setattr(cli.build, "rebuild", lambda *a, **k: pytest.fail("must not rebuild"))
    with pytest.raises(SystemExit, match="belongs to org/alpha-brain"):
        cli.index_main(["--rebuild", "--repo", str(repo), "--tenant", "beta", "--embedder", "fake"])
