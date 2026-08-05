"""The commit path: App-bot authorship, and the two steward-clone guards."""
import os

import pytest

from stigmergy.views import writer
from tests.views.conftest import remote_log


def test_commit_and_push_lands_as_the_app_bot_not_the_steward(repo):
    remote, clone = repo
    with open(os.path.join(clone, "views_test_marker.txt"), "w") as f:
        f.write("x")
    os.makedirs(os.path.join(clone, "views"), exist_ok=True)
    sha = writer.commit_and_push(clone, branch="main", message="chore(views): test commit\n")
    assert sha
    log = remote_log(remote)
    assert "chore(views): test commit" in log
    # author identity is the App bot's, not the steward's own git config (STEWARD_NAME/EMAIL):
    # a view is App-bot-authored, so a steward is never credited with writing one.
    import subprocess
    author = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=remote,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert "Test Steward" not in author
    assert "stigmergy-librarian" in author


def test_ensure_clean_refuses_a_dirty_tree(repo):
    remote, clone = repo
    with open(os.path.join(clone, "uncommitted.txt"), "w") as f:
        f.write("oops")
    with pytest.raises(writer.ViewWriteError, match="uncommitted change"):
        writer.ensure_clean(clone)


def test_ensure_on_branch_refuses_a_detached_head(repo):
    remote, clone = repo
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True,
                              text=True, check=True).stdout.strip()
    subprocess.run(["git", "checkout", "--quiet", head_sha], cwd=clone, check=True)
    with pytest.raises(writer.ViewWriteError, match="not 'main'"):
        writer.ensure_on_branch(clone, "main")
