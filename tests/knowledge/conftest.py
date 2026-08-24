import json
import subprocess

import pytest

from stigmergy.entities.model import registry_bytes
from tests.capture.conftest import clean_queue, conn

__all__ = ["clean_queue", "conn"]


@pytest.fixture()
def target_repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "entity-registry.json").write_bytes(registry_bytes({}))
    (tmp_path / "ops" / "identities.json").write_text(
        json.dumps(
            {
                "marc": {
                    "display_name": "Marc",
                    "groups": ["brain-admins"],
                    "default_audience": None,
                },
                "alice": {
                    "display_name": "Alice",
                    "groups": ["engineering"],
                    "default_audience": ["engineering"],
                },
                "bob": {
                    "display_name": "Bob",
                    "groups": ["finance"],
                    "default_audience": ["finance"],
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "ops" / "slack-channels.json").write_text("{}\n", encoding="utf-8")
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
