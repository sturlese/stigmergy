"""`deploy/` must never carry a real deployment's data.

The image bakes the knowledge repo's ops files in (`Dockerfile`), so they have to exist in the build context or
`docker build` fails on a fresh clone. They are therefore committed — as EMPTY defaults.

`scripts/deploy_staging.sh` overwrites every one of them with the knowledge repo's real `ops/` files
immediately before a deploy. The deploy needs them; the working tree must not keep them, because
they are tracked files and a later `git add -A` would publish an entire deployment's identity
roster. So the script restores the empty defaults on the way out, and the two tests at the bottom
of this file prove both halves against the real script.

The parametrized check above them is the backstop for the case the trap cannot cover — a roster
that reached a commit some other way. It is deliberately strict (an exact match, not a heuristic)
because the whole point is that there is no judgement call about whether some particular content is
"safe enough" to commit here.
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_staging.sh"

EMPTY_DEFAULTS = {
    "identities.json": {},
    "entity-registry.json": {"entities": {}},
    "slack-channels.json": {},
    # the deployed app/slack groups hold no checkout, so the steward map has to ride
    # the image like the three above. Empty means "nobody is on call" — which is the fail-closed
    # posture every reader already takes, not a broken deploy.
}

_RESYNC = (
    "This file is the committed EMPTY default. It looks like a real deploy-time bake — "
    "`scripts/deploy_staging.sh` writes the knowledge repo's own ops/ files here right before "
    "`fly deploy`, and they must not be committed. Restore the empty default before committing; "
    "the deploy script will rewrite it on the next deploy."
)


@pytest.mark.parametrize("name", sorted(EMPTY_DEFAULTS))
def test_the_committed_deploy_file_is_the_empty_default(name):
    path = DEPLOY / name
    assert path.is_file(), (
        f"deploy/{name} is missing — the Dockerfile COPYs it, so a fresh clone cannot build "
        f"without it. Restore it as {json.dumps(EMPTY_DEFAULTS[name])}.")
    assert json.loads(path.read_text(encoding="utf-8")) == EMPTY_DEFAULTS[name], (
        f"deploy/{name} is not empty. {_RESYNC}")


# The one directory `deploy/` is allowed to contain. It holds the four cron workflows an
# operator copies into their own knowledge repo — deliberately NOT under `.github/workflows/`,
# where GitHub would register them on this public repo and show three "Disabled" rows.
#
# `_staged_run` seeds a tracked file into every directory named here before it runs the real
# script, so this declaration has a RUNTIME counterpart instead of being a statement about a tree
# nobody exercises. Extending the set therefore extends what the deploy script is proven to leave
# alone, in the same edit.
EXPECTED_SUBDIRS = {"workflows"}

SIBLING_MARKER = "tracked.yml"


def _sibling_body(sub: str) -> str:
    """The stub content seeded under `deploy/<sub>/`. Distinct per directory, so an assertion that
    the file survived cannot pass on a file the script happened to recreate."""
    return f"# tracked by git, written by nobody in scripts/deploy_staging.sh: deploy/{sub}/\n"


def test_no_other_file_has_appeared_in_deploy():
    """A file here that the deploy script does not write would be baked by nobody and reviewed by nobody."""
    unexpected = sorted(p.name for p in DEPLOY.iterdir()
                        if p.is_file() and p.name not in EMPTY_DEFAULTS)
    assert not unexpected, f"unexpected files in deploy/: {unexpected}"


def test_no_other_directory_has_appeared_in_deploy():
    """The file check above asks only about FILES, so a whole subdirectory could arrive here
    unreviewed — as `workflows/` itself did, sliding under a guard that could not see it. Naming
    the allowed set is what makes the next one a decision instead of an accident."""
    unexpected = sorted(p.name for p in DEPLOY.iterdir()
                        if p.is_dir() and p.name not in EXPECTED_SUBDIRS)
    assert not unexpected, f"unexpected directories in deploy/: {unexpected}"


# ── the real script, run end to end ──────────────────────────────────────────────────────────
# `HERE` is derived from `BASH_SOURCE`, so a copy of the script under `tmp_path/scripts/` bakes
# into `tmp_path/deploy/` and never touches this checkout's own `deploy/`. `fly` is stubbed on
# PATH: the deploy itself is not the subject, what the deploy SAW and what survives it are.

_ROSTER = {"someone@example.com": ["everyone", "finance"]}
_REGISTRY = {"entities": {"acme-corp": {"name": "Acme Corp"}}}
# Real-looking because the point of these tests is that real data does not survive the script:
# a steward map is a list of people's email addresses, exactly like the identity roster.
_CHANNELS = {"everyone": "C0123456789"}


def _staged_run(tmp_path):
    """Run the real deploy script against a fake knowledge repo and a stubbed `fly`.

    Returns (deploy_dir, roster_fly_saw) — the directory as the script left it,
    and the two people-bearing files the stub read at `fly deploy` time.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(DEPLOY_SCRIPT, scripts / "deploy_staging.sh")

    # The staged `deploy/` starts out looking like the real one — tracked siblings and all. It did
    # not, and that single difference is why the script's `rm -rf` on this directory went
    # unnoticed: there was nothing here to destroy, so the destruction was invisible to the one
    # test positioned to see it.
    for sub in sorted(EXPECTED_SUBDIRS):
        (tmp_path / "deploy" / sub).mkdir(parents=True)
        (tmp_path / "deploy" / sub / SIBLING_MARKER).write_text(_sibling_body(sub),
                                                                encoding="utf-8")

    ops = tmp_path / "knowledge" / "ops"
    ops.mkdir(parents=True)
    (ops / "identities.json").write_text(json.dumps(_ROSTER), encoding="utf-8")
    (ops / "entity-registry.json").write_text(json.dumps(_REGISTRY), encoding="utf-8")
    (ops / "slack-channels.json").write_text(json.dumps(_CHANNELS), encoding="utf-8")

    # The stub copies what it was given aside on `deploy`, so the test can assert the bake
    # actually happened rather than inferring it from the restore.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fly = bin_dir / "fly"
    fly.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "deploy" ]; then cp deploy/identities.json "$SEEN"; '
        'fi\n'
        "exit 0\n", encoding="utf-8")
    fly.chmod(0o755)

    seen = tmp_path / "seen-by-fly.json"
    env = {**os.environ,
           "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "STIGMERGY_REPO": str(tmp_path / "knowledge"),
           "SEEN": str(seen),
           }
    proc = subprocess.run(["bash", str(scripts / "deploy_staging.sh")],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, f"the deploy script failed:\n{proc.stdout}\n{proc.stderr}"
    return tmp_path / "deploy", seen


def test_a_deploy_leaves_no_real_roster_behind_in_the_tracked_deploy_dir(tmp_path):
    """The fix. OLD BEHAVIOUR: the script baked the knowledge repo's real `ops/` files into the
    tracked `deploy/` directory and exited without restoring them, so every deploy left a full
    identity roster (email -> ACL audiences) in the working tree, one `git add -A` from being
    committed. The suite only noticed afterwards, and on a public repo the push IS the disclosure.
    """
    deploy_dir, _ = _staged_run(tmp_path)
    for name, empty in EMPTY_DEFAULTS.items():
        got = json.loads((deploy_dir / name).read_text(encoding="utf-8"))
        assert got == empty, (
            f"deploy/{name} still holds deploy-time data after the script exited: {got!r}")


def test_the_deploy_itself_still_sees_the_real_roster(tmp_path):
    """The benign twin. Restoring the defaults must not defeat the bake it protects — `fly deploy`
    has to run with the real files in place, or the deployed image ships an empty roster and every
    identity resolves to nothing.
    """
    _, seen = _staged_run(tmp_path)
    assert seen.is_file(), "`fly deploy` never ran, so this proves nothing about the bake"
    assert json.loads(seen.read_text(encoding="utf-8")) == _ROSTER
    # The steward map is the second file here that is a list of real people, and the one that
    # decides who may APPROVE. Asserting it separately is what keeps the restore guard above from
    # being vacuous for it: without this, the staged run never wrote one, the script took its
    # `{}` branch, and `{} == {}` proved nothing.


def test_a_deploy_leaves_tracked_files_it_never_baked_untouched(tmp_path):
    """The fix for the second defect in this script, same shape as the first one above.

    OLD BEHAVIOUR: the script opened with `rm -rf "$DEPLOY_DIR"`, and `restore_deploy_defaults`
    knew only the four JSON files. So one `make deploy-staging` deleted `deploy/workflows/`
    — a README and four cron templates, all tracked — from the working tree, with nothing in the
    script able to put them back. The next `git add -A` commits that deletion, and `git add -A`
    is exactly what someone runs after deploying.

    It stayed invisible because the two halves of the check never met: `EXPECTED_SUBDIRS` declared
    `workflows/` but ran no script, and the test that ran the script ran it against a `tmp_path`
    where the directory had never existed. `_staged_run` now seeds one from that same declaration,
    which is the join that was missing.
    """
    deploy_dir, _ = _staged_run(tmp_path)

    for sub in sorted(EXPECTED_SUBDIRS):
        survivor = deploy_dir / sub / SIBLING_MARKER
        assert survivor.is_file(), (
            f"the deploy script destroyed deploy/{sub}/{SIBLING_MARKER}. It is tracked, nothing in "
            f"the script knows how to recreate it, and the next `git add -A` publishes its "
            f"deletion. Delete only the files the script itself writes — the `BAKED` list — never "
            f"the directory.")
        assert survivor.read_text(encoding="utf-8") == _sibling_body(sub), (
            f"deploy/{sub}/{SIBLING_MARKER} survived but its content changed")


def test_the_scripts_restored_defaults_are_the_ones_this_file_asserts(tmp_path):
    """The two halves cannot drift apart: the literals the script restores and the literals this
    file calls the empty default are checked against each other, not written down twice and hoped
    to agree.
    """
    deploy_dir, _ = _staged_run(tmp_path)
    restored = {p.name: json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(deploy_dir.iterdir()) if p.is_file()}
    assert restored == EMPTY_DEFAULTS
