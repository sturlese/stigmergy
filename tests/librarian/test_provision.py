"""A new workspace's scaffold reaches its repository even when an earlier attempt stopped half-way."""
import subprocess
from pathlib import Path

import pytest

from stigmergy.knowledge.trust import WRITER_NAME
from stigmergy.librarian import provision
from stigmergy.librarian.errors import GitError, LibrarianConfigError


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _bare(tmp_path: Path) -> Path:
    bare = tmp_path / "workspace.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    return bare


def test_a_scaffold_whose_push_failed_is_pushed_on_the_next_attempt(tmp_path):
    bare = _bare(tmp_path)
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    checkout = tmp_path / "checkout"

    with pytest.raises(GitError):
        provision.push_scaffold(str(checkout), str(bare), name="Acme", slug="acme")
    assert (checkout / "README.md").is_file()

    hook.unlink()
    provision.push_scaffold(str(checkout), str(bare), name="Acme", slug="acme")
    assert "README.md" in _git("ls-tree", "-r", "--name-only", "main", cwd=bare).split()
    assert _git("log", "--format=%an", "main", cwd=bare).strip() == WRITER_NAME


def test_an_unreachable_repository_is_an_error_not_an_empty_one(tmp_path):
    with pytest.raises(LibrarianConfigError, match="could not be reached"):
        provision.push_scaffold(str(tmp_path / "checkout"), str(tmp_path / "missing.git"),
                                name="Acme", slug="acme")
    assert not (tmp_path / "checkout").exists()


def test_a_foreign_directory_is_never_scaffolded_over(tmp_path):
    bare = _bare(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "notes.txt").write_text("someone else's\n")
    with pytest.raises(LibrarianConfigError, match="not empty"):
        provision.push_scaffold(str(checkout), str(bare), name="Acme", slug="acme")


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_an_existing_repository_is_adopted_only_when_this_platform_created_it():
    import json
    import urllib.error

    def opener_for(existing: dict):
        def opener(request, timeout):
            if request.get_method() == "POST":
                raise urllib.error.HTTPError(request.full_url, 422, "exists", {}, None)
            return _Response(json.dumps(existing).encode())
        return opener

    provision.create_remote("org/acme-brain", "t", opener=opener_for(
        {"private": True, "description": provision.DESCRIPTION}))
    with pytest.raises(LibrarianConfigError, match="refusing to use it"):
        provision.create_remote("org/acme-brain", "t", opener=opener_for(
            {"private": True, "description": "the finance team's scripts"}))
    with pytest.raises(LibrarianConfigError, match="refusing to use it"):
        provision.create_remote("org/acme-brain", "t", opener=opener_for(
            {"private": False, "description": provision.DESCRIPTION}))


def test_every_workspace_starts_from_the_same_scaffold(tmp_path):
    bare = _bare(tmp_path)
    provision.push_scaffold(str(tmp_path / "checkout"), str(bare), name="Acme", slug="acme")
    files = set(_git("ls-tree", "-r", "--name-only", "main", cwd=bare).split())
    assert files == {"README.md", "CLAUDE.md", ".gitignore", "wiki/notes/.gitkeep",
                     "wiki/concepts/.gitkeep", "wiki/entities/.gitkeep", "wiki/projects/.gitkeep",
                     "sources/.gitkeep"}
    readme = _git("show", "main:README.md", cwd=bare)
    assert readme.startswith("# Acme\n") and "`acme`" in readme
    assert _git("show", "main:CLAUDE.md", cwd=bare).startswith("# Acme\n")
