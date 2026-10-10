"""The packaged librarian skill."""

from __future__ import annotations

from importlib.resources import files


def expected_librarian_skill() -> bytes:
    return files("stigmergy.knowledge").joinpath("librarian_skill.md").read_bytes()
