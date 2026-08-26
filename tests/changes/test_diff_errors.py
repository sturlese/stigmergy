import subprocess

import pytest

from stigmergy.changes import diff
from stigmergy.changes.errors import ChangeError


def test_git_failure_raises_a_scrubbed_bounded_changes_error(monkeypatch):
    def failed_git(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["git", "diff"],
            returncode=128,
            stdout=b"",
            stderr=(
                b"fatal: could not read Username for "
                b"'https://token:SECRET-MARKER@example.test/repo': "
                b"No such device or address at /Users/marc/private/brain"
            ),
        )

    monkeypatch.setattr(diff.subprocess, "run", failed_git)

    with pytest.raises(ChangeError) as exc_info:
        diff.exact_patch("/Users/marc/private/brain", "parent", "commit")

    assert str(exc_info.value) == "git could not construct the change record"
    assert len(str(exc_info.value)) <= 256
    assert "SECRET-MARKER" not in str(exc_info.value)
    assert "/Users/marc/private/brain" not in str(exc_info.value)


def test_git_os_error_raises_the_same_scrubbed_bounded_changes_error(monkeypatch):
    def unavailable_git(*_args, **_kwargs):
        raise OSError("git missing at /Users/marc/private/bin/git: SECRET-MARKER")

    monkeypatch.setattr(diff.subprocess, "run", unavailable_git)

    with pytest.raises(ChangeError) as exc_info:
        diff.exact_patch("/Users/marc/private/brain", "parent", "commit")

    assert str(exc_info.value) == "git could not construct the change record"
    assert len(str(exc_info.value)) <= 256
    assert "SECRET-MARKER" not in str(exc_info.value)
    assert "/Users/marc/private/brain" not in str(exc_info.value)


def test_manifest_distinguishes_an_expected_absent_blob_from_a_git_failure(
    tmp_path, monkeypatch
):
    repo = tmp_path / "brain"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Stigmergy"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "writer@example.com"], cwd=repo, check=True
    )
    old = repo / "wiki" / "notes" / "Old.md"
    old.parent.mkdir(parents=True)
    old.write_text("old\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    parent = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    old.unlink()
    new = old.with_name("New.md")
    new.write_text("new\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "replace"], cwd=repo, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    manifest = diff.build_manifest(
        str(repo), parent, commit, default_reason="Replaced a note"
    )

    changes = {entry.path: entry for entry in manifest}
    assert changes["wiki/notes/New.md"].before_sha256 == ""
    assert changes["wiki/notes/Old.md"].after_sha256 == ""

    original_run = diff.subprocess.run

    def failed_show(args, **kwargs):
        if args[-2:] == ["show", f"{commit}:wiki/notes/New.md"]:
            return subprocess.CompletedProcess(args=args, returncode=128, stdout=b"", stderr=b"")
        return original_run(args, **kwargs)

    monkeypatch.setattr(diff.subprocess, "run", failed_show)

    with pytest.raises(ChangeError, match="git could not construct the change record"):
        diff.build_manifest(str(repo), parent, commit, default_reason="Replaced a note")
