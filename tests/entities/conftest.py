"""Fixtures for the entity-birth suite (`stigmergy.entities`): a throwaway knowledge repo — a
bare remote plus a steward's clone — seeded with the same shape `birth`/`generator`/`clone` expect
to find in the real knowledge repo: `ops/templates/entity.md`, a registered entity, and its page.

**Real git, always** (the same posture as `tests/librarian/support.py`): every test that
exercises `clone.py`'s preflight/commit/push works against an actual `git init --bare` remote and
an actual clone, never a faked diff — the properties this package exists for (a wrong-HEAD push
publishing the wrong branch, a force-push, a rebase-then-retry race) are properties of real git,
and a double would prove nothing about them.
"""
import json
import os
import subprocess

import pytest

TEMPLATE = """---
type: entity
title: "<Entity Name>"
status: developing        # seed|developing|mature|canonical (canonical requires `owner`)
entity_type: organization # person|organization|product|tool|repository|place|project
role: ""
aliases: []
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
tags: [entity]
related: []
sources: []
---

# <Entity Name>

## What / Who

<One clear paragraph: what this entity is and why it's in the brain.>
"""

REGISTRY = {
    "entities": {
        "jordan-reyes": {"aliases": ["Jordan Reyes Gaya"], "name": "Jordan Reyes",
                          "type": "person"},
        "stigmergy": {"aliases": ["The Company Brain"], "name": "Stigmergy", "type": "product"},
    }
}

PAGES = {
    "Jordan Reyes": {"entity_type": "person", "aliases": ["Jordan Reyes Gaya"]},
    "Stigmergy": {"entity_type": "product", "aliases": ["The Company Brain"]},
}

STEWARD_NAME = "Test Steward"
STEWARD_EMAIL = "steward@example.com"

_COMMIT_ENV = {"GIT_AUTHOR_NAME": STEWARD_NAME, "GIT_AUTHOR_EMAIL": STEWARD_EMAIL,
              "GIT_COMMITTER_NAME": STEWARD_NAME, "GIT_COMMITTER_EMAIL": STEWARD_EMAIL}


def git(*args, cwd, check=True, env=None):
    full_env = None
    if env:
        full_env = {**os.environ, **env}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=check, env=full_env)


def page_text(name: str, entity_type: str, aliases) -> str:
    listed = "[" + ", ".join(f'"{a}"' for a in aliases) + "]"
    return (f'---\ntype: entity\ntitle: "{name}"\nentity_type: {entity_type}\nrole: ""\n'
            f'status: developing\naliases: {listed}\ncreated: 2026-07-01\nupdated: 2026-07-01\n'
            f'tags: [entity, {entity_type}]\nrelated: []\nsources: []\n---\n\n# {name}\n')


def build_repo(root: str, *, extra_pages=()):
    """`(remote, clone)` — a fresh `git init --bare` remote plus a steward's clone, seeded with the
    template, a registry of two entities and their pages, committed and pushed once.

    `extra_pages` are `(name, entity_type, aliases)` written to `wiki/entities/` but left OUT
    of the registry — the drift shape the generator tests need (an unregistered page sitting
    beside a clean registry).
    """
    remote = os.path.join(root, "remote.git")
    clone = os.path.join(root, "clone")
    os.makedirs(remote, exist_ok=True)
    git("init", "--bare", "--quiet", "--initial-branch=main", remote, cwd=root)

    os.makedirs(os.path.join(clone, "ops", "templates"), exist_ok=True)
    os.makedirs(os.path.join(clone, "wiki", "entities"), exist_ok=True)
    git("init", "--quiet", "--initial-branch=main", clone, cwd=root)
    git("config", "user.name", STEWARD_NAME, cwd=clone)
    git("config", "user.email", STEWARD_EMAIL, cwd=clone)
    with open(os.path.join(clone, "ops", "templates", "entity.md"), "w") as f:
        f.write(TEMPLATE)
    with open(os.path.join(clone, "ops", "entity-registry.json"), "w") as f:
        json.dump(REGISTRY, f, indent=2, sort_keys=True)
        f.write("\n")
    for name, spec in PAGES.items():
        with open(os.path.join(clone, "wiki", "entities", f"{name}.md"), "w") as f:
            f.write(page_text(name, spec["entity_type"], spec["aliases"]))
    for name, entity_type, aliases in extra_pages:
        with open(os.path.join(clone, "wiki", "entities", f"{name}.md"), "w") as f:
            f.write(page_text(name, entity_type, aliases))
    git("add", "--all", cwd=clone)
    git("commit", "--quiet", "-m", "chore: seed the fixture knowledge repo", cwd=clone,
       env=_COMMIT_ENV)
    git("remote", "add", "origin", remote, cwd=clone)
    git("push", "--quiet", "-u", "origin", "main", cwd=clone)
    return remote, clone


def clone_of(remote: str, dest: str, *, name: str = "Steward B", email: str = "b@example.com"):
    """A second, independent clone of the same remote — a second steward's laptop."""
    git("clone", "--quiet", remote, dest, cwd=os.path.dirname(dest) or ".")
    git("config", "user.name", name, cwd=dest)
    git("config", "user.email", email, cwd=dest)
    return dest


def remote_log(remote: str, ref: str = "main") -> str:
    return git("log", "--oneline", ref, cwd=remote).stdout


def remote_files(remote: str, ref: str = "main") -> list[str]:
    return git("ls-tree", "-r", "--name-only", ref, cwd=remote).stdout.splitlines()


def remote_registry(remote: str, ref: str = "main") -> dict:
    text = git("show", f"{ref}:ops/entity-registry.json", cwd=remote).stdout
    return json.loads(text)


@pytest.fixture()
def repo(tmp_path):
    """`(remote, clone)` — see `build_repo`. Fresh per test."""
    return build_repo(str(tmp_path / "git"))


@pytest.fixture()
def require_gitleaks():
    """Skip on a laptop with no gitleaks; FAIL in CI — same posture as
    `tests/librarian/conftest.py::require_gitleaks` (a secrets gate must not be silently
    absent from a green run)."""
    from tests import testdb
    from tests.librarian import support

    if support.gitleaks_available():
        return
    if testdb.required():
        pytest.fail("$STIGMERGY_TEST_DSN is set (CI mode) but gitleaks is not on PATH — refusing to "
                    "skip the entities secrets-gate suite silently. Install gitleaks BEFORE the "
                    "test step (see .github/workflows/ci.yml).")
    pytest.skip("gitleaks not on PATH (brew install gitleaks) — the secrets gate cannot "
               "be exercised without it")
