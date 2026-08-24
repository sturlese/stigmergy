"""Offline argument contract for index maintenance."""
import subprocess

import pytest

from stigmergy.index import build, cli, store
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
    for relpath in store.OPS_FILE_RELPATHS:
        target = repo.joinpath(*relpath.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
    _commit_checkout(repo)

    head = build._checked_repository_head(str(repo))
    assert len(head) == 40
    with pytest.raises(StigmergyIndexError, match="root"):
        build._checked_repository_head(str(note.parent))

    note.write_text("changed\n", encoding="utf-8")
    with pytest.raises(StigmergyIndexError, match="must match"):
        build._checked_repository_head(str(repo))
