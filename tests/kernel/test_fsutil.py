"""Atomic text writes: parents created, content replaced whole, no tmp residue."""
import os

from stigmergy.kernel.fsutil import write_text_atomic


def test_creates_parent_dirs_and_writes(tmp_path):
    path = tmp_path / "a" / "b" / "out.md"
    write_text_atomic(str(path), "hello")
    assert path.read_text(encoding="utf-8") == "hello"


def test_replaces_existing_content_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "out.md"
    write_text_atomic(str(path), "one")
    write_text_atomic(str(path), "two")
    assert path.read_text(encoding="utf-8") == "two"
    assert os.listdir(tmp_path) == ["out.md"]
