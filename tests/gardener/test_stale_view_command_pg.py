"""A message containing a command is an executable promise: `check_stale_views`'
`suggested_action` is not merely well-formed text — it is run, verbatim, against the same repo the
finding was computed from, and it actually clears the staleness.

Needs real git (the `stigmergy-views` CLI commits and pushes, and a faked git tree proves nothing
about this property) AND real Postgres (`views.cli._connect` always opens a real connection for
its own `job_runs` bookkeeping, unlike the library-level `regenerate.run` the views suite's own
`FakeConn` doubles for). `$STIGMERGY_INDEX_DSN` is already pinned to the test database for the whole
session (`tests/conftest.py::pytest_configure`), so `stigmergy-views`'s own `store.connect(None)`
resolves to it with no `--dsn` needed here.
"""
import os
import pathlib
import tomllib

from stigmergy.gardener import checks
from stigmergy.views import cli as views_cli
from tests.views.conftest import _COMMIT_ENV, build_repo, git

_PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_stale_view_suggested_action_actually_regenerates_and_clears_the_staleness(tmp_path):
    _remote, clone = build_repo(str(tmp_path), entity_id="acme-corp", n_decisions=1)

    # Make it stale: a view file whose OWN member_hash no longer matches what
    # `skeleton.member_hash(skeleton.members_of(...))` would compute for the real member set —
    # committed and pushed, so the checkout is git-clean (member-hash staleness and a dirty
    # working tree are two independent concepts; `stigmergy-views` refuses to run on the latter).
    view_path = os.path.join(clone, "views", "acme-corp.md")
    os.makedirs(os.path.dirname(view_path), exist_ok=True)
    with open(view_path, "w", encoding="utf-8") as f:
        f.write('---\ntype: view\ntitle: "Acme Corp — view"\n'
                'member_hash: "not-the-real-hash"\n---\n\n# Acme Corp\n')
    git("add", "--all", cwd=clone)
    git("commit", "--quiet", "-m", "test: seed a stale view", cwd=clone, env=_COMMIT_ENV)
    git("push", "--quiet", cwd=clone)

    findings = checks.check_stale_views(clone)
    assert [f["subject"] for f in findings] == ["acme-corp"]
    action = findings[0]["suggested_action"]

    # The whole value is one code span (checks.py's own composition rule) — strip the markdown
    # decoration, the ordinary "copy the text between the backticks" reading, never the backticks
    # themselves as shell syntax.
    assert action.startswith("`") and action.endswith("`")
    command = action[1:-1]
    argv = command.split()
    assert argv[:2] == ["stigmergy-views", "regenerate"]

    rc = views_cli.main(["--repo", clone, *argv[1:]])
    assert rc == 0

    # The command cleared the exact condition the finding reported.
    assert checks.check_stale_views(clone) == []


def test_stale_view_command_exists_in_pyproject_scripts_and_argparse():
    """The other half of the same promise: the console script is real, and `--entity` is a real,
    recognized flag — checked directly against the argparse surface, not merely trusted."""
    with open(_PYPROJECT, "rb") as f:
        pyproject = tomllib.load(f)
    assert pyproject["project"]["scripts"]["stigmergy-views"] == "stigmergy.views.cli:main"

    parser = views_cli.build_parser()
    args = parser.parse_args(["regenerate", "--entity", "acme-corp"])
    assert args.entity == "acme-corp"
