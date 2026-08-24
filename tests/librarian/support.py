import os
import subprocess
from dataclasses import dataclass

from stigmergy.librarian import gitcmd
from tests import childwatch

_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
}


@dataclass(frozen=True)
class RepoEnv:
    bare: str
    repo: str


def build_repo(root) -> RepoEnv:
    bare = os.path.join(str(root), "origin.git")
    repo = os.path.join(str(root), "checkout")
    gitcmd.run("init", "--bare", "--quiet", "-b", "main", bare)
    gitcmd.run("init", "--quiet", "-b", "main", repo)
    os.makedirs(os.path.join(repo, "wiki", "notes"), exist_ok=True)
    with open(os.path.join(repo, "wiki", "notes", "Café Note.md"), "w", encoding="utf-8") as handle:
        handle.write("fixture\n")
    gitcmd.run("add", "-A", cwd=repo)
    gitcmd.run("commit", "--quiet", "-m", "seed", cwd=repo, env=_COMMIT_ENV)
    gitcmd.run("remote", "add", "origin", bare, cwd=repo)
    gitcmd.run("push", "--quiet", "-u", "origin", "main", cwd=repo)
    return RepoEnv(bare=bare, repo=repo)


def build_rig(root):
    return build_repo(root), None


def crash_leftover_name(repo: str) -> str:
    process = childwatch.spawn(
        ["git", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.wait()
    return f"{gitcmd.WORKTREE_PREFIX}{gitcmd.worktree_key(repo)}-{process.pid}-abc123abc123"
