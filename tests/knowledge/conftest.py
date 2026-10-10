import subprocess

import pytest

from stigmergy.knowledge.contract import expected_librarian_skill
from tests.capture.conftest import clean_queue, conn
from tests.roster import seed_roster

IDENTITIES = {
    "morgan": {"display_name": "Morgan", "groups": ["brain-admins"], "default_audience": ["public"]},
    "alice": {"display_name": "Alice", "groups": ["engineering"],
              "default_audience": ["engineering"]},
    "bob": {"display_name": "Bob", "groups": ["finance"], "default_audience": ["finance"]},
}

__all__ = ["clean_queue", "conn"]


@pytest.fixture()
def target_repo(tmp_path, conn):
    """A committed knowledge repository, with the writer's actors seeded in the roster."""
    seed_roster(conn, IDENTITIES)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    skill = tmp_path / ".claude" / "skills" / "librarian" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(expected_librarian_skill())
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path
