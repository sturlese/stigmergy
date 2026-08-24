"""Exact Git patches and deterministic per-path manifests."""

from __future__ import annotations

import gzip
import hashlib
import shlex
import subprocess

from stigmergy.changes.model import PathChange
from stigmergy.knowledge import contradictions


def _git(repo: str, *args: str) -> bytes:
    process = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if process.returncode:
        raise RuntimeError("git could not construct the change record")
    return process.stdout


def exact_patch(repo: str, parent: str, commit: str) -> bytes:
    return _git(
        repo,
        "diff",
        "--binary",
        "--full-index",
        "--no-color",
        "--no-renames",
        parent,
        commit,
        "--",
    )


def path_patches(patch: str) -> dict[str, str]:
    result = {}
    path = None
    lines: list[str] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if path is not None:
                result[path] = "".join(lines)
            path = _header_path(line.rstrip("\n"))
            lines = [line]
        elif path is not None:
            lines.append(line)
    if path is not None:
        result[path] = "".join(lines)
    return result


def _header_path(line: str) -> str | None:
    raw = line.removeprefix("diff --git ")
    try:
        fields = shlex.split(line)
    except ValueError:
        fields = []
    if len(fields) == 4 and fields[:2] == ["diff", "--git"]:
        before, after = fields[2:]
        if before.startswith("a/") and after.startswith("b/"):
            before_path = before[2:]
            after_path = after[2:]
            if before_path == after_path:
                return after_path
    if not raw.startswith("a/"):
        return None
    delimiter = " b/"
    offset = 0
    while (index := raw.find(delimiter, offset)) >= 0:
        before_path = raw[2:index]
        after_path = raw[index + len(delimiter) :]
        if before_path == after_path:
            return after_path
        offset = index + 1
    return None


def compressed_patch(patch: bytes) -> bytes:
    return gzip.compress(patch, compresslevel=9, mtime=0)


def patch_sha256(patch: bytes) -> str:
    return hashlib.sha256(patch).hexdigest()


def _paths(repo: str, parent: str, commit: str) -> list[tuple[str, str]]:
    raw = _git(
        repo,
        "diff",
        "--name-status",
        "-z",
        "--no-renames",
        parent,
        commit,
        "--",
    )
    values = raw.decode("utf-8").split("\0")
    changes = []
    index = 0
    while index + 1 < len(values) and values[index]:
        status = values[index]
        path = values[index + 1]
        changes.append((status[:1], path))
        index += 2
    return changes


def _counts(repo: str, parent: str, commit: str) -> dict[str, tuple[int, int]]:
    raw = _git(
        repo,
        "diff",
        "--numstat",
        "-z",
        "--no-renames",
        parent,
        commit,
        "--",
    )
    result = {}
    for entry in raw.decode("utf-8").split("\0"):
        if not entry:
            continue
        added, deleted, path = entry.split("\t", 2)
        result[path] = (
            0 if added == "-" else int(added),
            0 if deleted == "-" else int(deleted),
        )
    return result


def _blob(repo: str, revision: str, path: str) -> bytes | None:
    process = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return process.stdout if process.returncode == 0 else None


def _role(path: str) -> str:
    if path.startswith("wiki/notes/"):
        return "note"
    if path.startswith("wiki/concepts/"):
        return "concept"
    if path.startswith("wiki/entities/"):
        return "entity"
    if path.startswith("sources/"):
        return "source"
    return "ops"


def _contradiction_ids(data: bytes | None, page_role: str) -> set[str]:
    if data is None or page_role not in {"note", "concept"}:
        return set()
    try:
        located = contradictions.parse_all(data.decode("utf-8"))
    except (UnicodeDecodeError, contradictions.ContradictionContractError):
        return set()
    return {item.record.contradiction_id for item in located}


def build_manifest(
    repo: str,
    parent: str,
    commit: str,
    *,
    reasons: dict[str, str] | None = None,
    default_reason: str,
) -> tuple[PathChange, ...]:
    counts = _counts(repo, parent, commit)
    result = []
    for status, path in _paths(repo, parent, commit):
        before = _blob(repo, parent, path)
        after = _blob(repo, commit, path)
        additions, deletions = counts.get(path, (0, 0))
        page_role = _role(path)
        before_contradictions = _contradiction_ids(before, page_role)
        after_contradictions = _contradiction_ids(after, page_role)
        result.append(
            PathChange(
                path=path,
                action={"A": "created", "M": "updated", "D": "deleted"}[status],
                page_role=page_role,
                reason=_reason((reasons or {}).get(path) or default_reason),
                before_sha256=hashlib.sha256(before).hexdigest() if before is not None else "",
                after_sha256=hashlib.sha256(after).hexdigest() if after is not None else "",
                additions=additions,
                deletions=deletions,
                contradictions_added=tuple(sorted(after_contradictions - before_contradictions)),
                contradictions_resolved=tuple(sorted(before_contradictions - after_contradictions)),
            )
        )
    return tuple(result)


def _reason(value: str) -> str:
    return " ".join(value.split())[:1000] or "Updated team knowledge"
