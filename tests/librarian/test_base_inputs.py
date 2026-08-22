"""The registry loader and the linter read `ops/entity-registry.json` and
`.claude/tools/stigmergy_lint.py` **at the base commit** (`gitcmd.BaseRef.sha`), never off the
working tree — in every mode, including a local laptop run where a working-tree read used to be a
convenience.

Pure git, no Postgres, no worker loop: `base_inputs.read_at`/`load_registry`/
`linter_at`/`check_linter_at` take a `(repo, base)` pair, so the property — "an uncommitted edit
does not change what these functions return" — is provable with two commits and a dirty working
tree, without a claim, a double agent or a database. Mirrors the skill-at-base test
(`worker._check_skill_at`) this module's own docstring names as the precedent it is symmetric with.
"""
import os
import subprocess

import pytest

from stigmergy.librarian import base_inputs, config, gitcmd
from stigmergy.librarian.errors import LibrarianConfigError

REGISTRY_V1 = '{"entities": {"acme-corp": {"name": "Acme", "type": "organization", "aliases": []}}}'
REGISTRY_V2 = ('{"entities": {"acme-corp": {"name": "Acme", "type": "organization", "aliases": []}, '
              '"globex": {"name": "Globex", "type": "organization", "aliases": []}}}')

LINTER_V1 = "#!/usr/bin/env python3\nVERSION = 1\n"
LINTER_V2 = "#!/usr/bin/env python3\nVERSION = 2\n"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _write(repo: str, relpath: str, text: str) -> None:
    path = os.path.join(repo, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


@pytest.fixture()
def two_commit_repo(tmp_path):
    """A plain (non-bare) repo with TWO commits: `sha1` carries the V1 inputs, `sha2` narrows the
    ACL and adds an entity. Returns `(repo_path, sha1, sha2)`. No bare remote at all — `BaseRef` is
    constructed by hand below, so nothing here depends on `git fetch` semantics."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git("init", "--quiet", "-b", "main", repo, cwd=str(tmp_path))
    _git("config", "user.name", "fixture", cwd=repo)
    _git("config", "user.email", "fixture@example.com", cwd=repo)

    _write(repo, config.REGISTRY_RELPATH, REGISTRY_V1)
    _write(repo, config.LINTER_RELPATH, LINTER_V1)
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "v1", cwd=repo)
    sha1 = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    _write(repo, config.REGISTRY_RELPATH, REGISTRY_V2)
    _write(repo, config.LINTER_RELPATH, LINTER_V2)
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "v2: narrow ACL, add Globex, bump linter", cwd=repo)
    sha2 = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    return repo, sha1, sha2


def _base(sha: str) -> gitcmd.BaseRef:
    """A `BaseRef` built by hand — `base_inputs` only ever reads `.sha`, so nothing here needs a
    real `origin/<branch>` to point at."""
    return gitcmd.BaseRef(sha=sha, ref="main", remote=False)


def test_an_uncommitted_registry_edit_does_not_change_what_load_registry_reads(two_commit_repo):
    repo, sha1, _sha2 = two_commit_repo
    _write(repo, config.REGISTRY_RELPATH, REGISTRY_V2)    # dirty the working tree, UNCOMMITTED

    registry = base_inputs.load_registry(repo, _base(sha1))
    assert registry.canonical_id("Globex") is None        # V2-only entity must not be visible
    assert registry.canonical_id("Acme") == "acme-corp"


def test_an_uncommitted_linter_edit_does_not_change_what_linter_at_materializes(two_commit_repo):
    repo, sha1, _sha2 = two_commit_repo
    _write(repo, config.LINTER_RELPATH, LINTER_V2)        # dirty the working tree, UNCOMMITTED

    with base_inputs.linter_at(repo, _base(sha1)) as path, open(path, encoding="utf-8") as f:
        materialized = f.read()
    assert materialized == LINTER_V1
    assert "VERSION = 2" not in materialized


def test_a_commit_with_no_registry_file_is_an_empty_registry(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git("init", "--quiet", "-b", "main", repo, cwd=str(tmp_path))
    _git("config", "user.name", "fixture", cwd=repo)
    _git("config", "user.email", "fixture@example.com", cwd=repo)
    _write(repo, "README.md", "x\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "no registry at all", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    registry = base_inputs.load_registry(repo, _base(sha))
    assert registry.entities == {}


def test_a_commit_with_no_linter_refuses_loudly_naming_the_locator(two_commit_repo):
    repo, sha1, _sha2 = two_commit_repo
    _git("rm", "--quiet", config.LINTER_RELPATH, cwd=repo)
    _git("commit", "--quiet", "-m", "drop the linter", cwd=repo)
    sha_no_linter = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    with pytest.raises(LibrarianConfigError, match="not in the commit"):
        base_inputs.check_linter_at(repo, _base(sha_no_linter))
    with pytest.raises(LibrarianConfigError, match="not in the commit"), \
            base_inputs.linter_at(repo, _base(sha_no_linter)):
        pass

    # benign twin: the earlier commit (which HAD the linter) is unaffected by the later removal
    base_inputs.check_linter_at(repo, _base(sha1))    # must not raise


