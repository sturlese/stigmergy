"""The ACL config used to be resolved ONCE at startup while the registry was already re-read per
item — `worker.startup_checks` resolved both, correctly, while nothing could
rewrite either mid-run; a steward pushing a tightened `ops/acl.json` to `main` broke that
assumption, and a long-running worker kept stamping pages with the audience labels of the commit
it booted from for its whole remaining lifetime — and a rule made NARROWER on the remote and then
silently ignored fails OPEN.

The fix: `processing.process_item` resolves the ACL config per item, at THIS item's own base
commit (`base_inputs.load_acl`, the same seam the registry reads through). This is the full,
through-the-filing-path proof — `tests/librarian/test_base_inputs.py` already proves the
lower-level mechanism (`base_inputs.load_acl` itself is a pure function of a commit); this proves
the STAMPED PAGE, at the end of a real run, actually carries the second commit's audiences,
without restarting the worker.
"""
import os

from stigmergy.capture import schema
from stigmergy.librarian import worker
from tests.librarian import support

NARROWED_ACL = '{"default": [], "rules": [{"path": "wiki/**", "acl": ["leadership"]}]}'


def _material(label: str) -> str:
    """Distinct content per call — `dedup.find_already_filed` matches on content hash ACROSS every
    submitter (module docstring: "whoever filed it and whenever"), so two items with byte-identical
    material would collide as a duplicate regardless of who submitted them, which is not the
    property this file tests."""
    return f"A memo ({label}) about the Acme Corp partnership renewal timeline.\n"


def _file_one(conn, deps, *, label: str):
    support.submit(conn, deps, _material(label), submitted_by=f"{label}@stigmergy.test")
    return worker.process_next(conn, deps)


def test_a_steward_narrowing_acl_json_mid_run_is_seen_by_the_very_next_item_no_restart(
        rig, clean_queue):
    env, deps = rig

    # ── item 1: filed against the fixture's own (open) ACL config ────────────────────────────
    _item1, result1 = _file_one(clean_queue, deps, label="first")
    assert result1.status == schema.FILED, result1.report.get("summary")
    path1, sha1 = result1.result_ref.rsplit("@", 1)
    page1 = support.read_filed_page(env.repo, sha1, path1)
    assert "acl:" not in page1, "the baseline ACL config resolves to OPEN — no acl: line at all"

    # ── a steward narrows ops/acl.json and pushes, directly on the bare remote's checkout ─────
    steward = os.path.join(os.path.dirname(env.repo), "steward-acl-clone")
    from stigmergy.librarian import gitcmd
    gitcmd.run("clone", "--quiet", env.bare, steward, cwd=os.path.dirname(env.repo))
    gitcmd.run("config", "user.name", "Jordan Reyes", cwd=steward)
    gitcmd.run("config", "user.email", "steward@stigmergy.test", cwd=steward)
    with open(os.path.join(steward, "ops", "acl.json"), "w", encoding="utf-8") as f:
        f.write(NARROWED_ACL)
    gitcmd.run("add", "-A", cwd=steward)
    gitcmd.run("commit", "--quiet", "-m", "chore(acl): narrow wiki/** to leadership",
              cwd=steward)
    gitcmd.run("push", "--quiet", "origin", "main", cwd=steward)

    # ── item 2: the SAME worker, SAME deps object — no restart, no rebuild of Settings/Deps ────
    _item2, result2 = _file_one(clean_queue, deps, label="second")
    assert result2.status == schema.FILED, result2.report.get("summary")
    path2, sha2 = result2.result_ref.rsplit("@", 1)
    page2 = support.read_filed_page(env.repo, sha2, path2)
    assert 'acl: ["leadership"]' in page2, page2


def test_the_benign_twin_an_unchanged_acl_config_produces_byte_identical_stamps(rig, clean_queue):
    """The benign twin: two items filed in a row with NOTHING pushed in between must be stamped
    with the SAME (empty/open) audiences — the per-item re-read must not have turned every item
    into a fresh, possibly-different answer when nothing actually changed."""
    env, deps = rig
    _item1, result1 = _file_one(clean_queue, deps, label="alpha")
    path1, sha1 = result1.result_ref.rsplit("@", 1)
    page1 = support.read_filed_page(env.repo, sha1, path1)

    _item2, result2 = _file_one(clean_queue, deps, label="beta")
    path2, sha2 = result2.result_ref.rsplit("@", 1)
    page2 = support.read_filed_page(env.repo, sha2, path2)

    def _acl_line(page: str) -> str:
        return next((ln for ln in page.splitlines() if ln.startswith("acl:")), "(none)")

    assert _acl_line(page1) == _acl_line(page2) == "(none)"
