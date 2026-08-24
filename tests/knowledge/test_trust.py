import os
import subprocess

from stigmergy.knowledge.trust import WRITER_EMAIL, WRITER_NAME, check_range


def _git(repo, *args, env=None):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo, message, *, name, email):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    _git(repo, "add", ".", env=env)
    _git(repo, "commit", "-q", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


def test_only_trusted_writer_can_change_model_owned_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("controls\n")
    base = _commit(repo, "bootstrap", name="Operator", email="operator@example.com")

    (repo / "wiki" / "notes").mkdir(parents=True)
    (repo / "wiki" / "notes" / "Trusted.md").write_text("trusted\n")
    trusted = _commit(repo, "trusted", name=WRITER_NAME, email=WRITER_EMAIL)
    assert check_range(str(repo), base=base, head=trusted) == ()

    (repo / "wiki" / "notes" / "Untrusted.md").write_text("untrusted\n")
    untrusted = _commit(repo, "untrusted", name="Operator", email="operator@example.com")
    violations = check_range(str(repo), base=trusted, head=untrusted)
    assert [(item.path, item.message) for item in violations] == [
        (
            "wiki/notes/Untrusted.md",
            "model-owned knowledge requires the trusted writer identity",
        )
    ]


def test_human_control_plane_changes_are_not_writer_owned(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("one\n")
    base = _commit(repo, "bootstrap", name="Operator", email="operator@example.com")
    (repo / "README.md").write_text("two\n")
    head = _commit(repo, "docs", name="Operator", email="operator@example.com")

    assert check_range(str(repo), base=base, head=head) == ()
