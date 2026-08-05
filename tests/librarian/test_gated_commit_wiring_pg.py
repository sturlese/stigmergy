"""The TOCTOU close is WIRED into the lanes that need it, end to end.

`tests/librarian/test_gitcmd_unit.py` proves `gitcmd.commit(gated_entries=...)` refuses a planted
file when it is called that way — with a hand-built worktree and a `gated_entries` list the test
itself constructs. That is a real proof of the mechanism and none at all of the WIRING: it says
nothing about whether `_file`/`_file_meeting` actually reach `gitcmd.commit` with `gated_entries` set
to the same entries the gates were shown, over a REAL processing pass (real registry, real stamp,
real gate run, real corrective-retry plumbing). A unit-only proof would leave exactly that
unpinned, and "the wiring is right" is a claim about `processing.py`, not about `gitcmd.py`.

**The reproduction technique.** The real vulnerability window is "while the gate subprocesses (the
contract linter, gitleaks) run, with the worktree on disk". Racing real subprocess timing would be
slow and flaky; instead, this monkeypatches `gates.run_gates` itself to plant a file as a side
effect and then call straight through to the real implementation — the file appears at exactly the
moment the gates are handed the diff, which is the same standing-in `test_gitcmd_unit.py`'s own
sabotage twin uses one layer down (`_write` called between two direct steps, not a literal race).
`gates` is `stigmergy.librarian.gates`, one module object shared by every importer (`processing.py`
and `review.py` both do `from stigmergy.librarian import gates`), so patching the attribute once
affects every caller that looks it up at call time — which is what makes the SAME helper usable for
both lanes here.
"""
import os

import pytest

from stigmergy.capture import queue, schema
from stigmergy.librarian import gates, worker
from tests.librarian import support

ACME_MATERIAL = "A short note about how the Acme Corp renewal is going, for the TOCTOU wiring proof."
MEETING_MATERIAL = "DOUBLE:decisions=1\nA short transcript, for the TOCTOU wiring proof."

PLANTED_REL = "wiki/notes/Planted During Gates.md"


@pytest.fixture()
def plant_during_gates(monkeypatch):
    """Arms the sabotage: the NEXT call to `gates.run_gates` writes `PLANTED_REL` into the
    worktree it was handed, then runs the real gate suite unchanged. Returns nothing — the test
    reads the outcome off the `Result`/the pushed branch, exactly like the reproduction it mirrors
    (`test_gitcmd_unit.py::test_the_sabotage_twin_a_file_planted_after_the_gates_ran_refuses_the_commit`).
    """
    real_run_gates = gates.run_gates

    def sabotaged(ctx):
        path = os.path.join(ctx.worktree, PLANTED_REL)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("nothing gated this\n")
        return real_run_gates(ctx)

    monkeypatch.setattr(gates, "run_gates", sabotaged)


@pytest.fixture()
def rewrite_during_gates(monkeypatch):
    """The other sabotage: the next `gates.run_gates` REWRITES every in-lane page it was handed,
    in place, then runs the real gate suite. No path appears and none vanishes — only the bytes
    change, which is exactly what a contract linter that writes would do. `gate_contract` is 7th
    of 8 and hands the worktree to the knowledge repo's own `.claude/tools/stigmergy_lint.py`, so
    this is the shape with a real producer behind it.
    """
    real_run_gates = gates.run_gates

    def sabotaged(ctx):
        for entry in ctx.entries:
            path = os.path.join(ctx.worktree, entry.path)
            if os.path.isfile(path):
                with open(path, "a", encoding="utf-8") as f:
                    f.write("\n<!-- a line no gate ever saw -->\n")
        return real_run_gates(ctx)

    monkeypatch.setattr(gates, "run_gates", sabotaged)


def test_the_fast_lane_refuses_a_file_planted_while_its_gates_run(rig, clean_queue,
                                                                  plant_during_gates):
    """`_file`'s own `gitcmd.commit(..., gated_entries=ctx.entries)` —
    proven over a REAL `_one_pass` run rather than a hand-assembled `GateContext`."""
    env, deps = rig
    before = support.branch_sha(env.bare)

    support.submit(clean_queue, deps, ACME_MATERIAL)
    item, result = worker.process_next(clean_queue, deps)

    assert result.status == schema.FAILED, (
        f"a file planted while the fast lane's gates ran should refuse the commit as a system "
        f"fault, got {result.status}: {result.error}")
    assert "appeared after the gates ran" in result.error
    assert "Planted During Gates.md" in result.error
    assert support.branch_sha(env.bare) == before, (
        "the wiring failed open: something landed on main despite the gated diff changing "
        "underneath the gates")


def test_the_meeting_lane_refuses_a_file_planted_while_its_gates_run(rig, clean_queue,
                                                                     plant_during_gates):
    """`_file_meeting`'s sibling call — "this lane is where it bites hardest, because the page SET
    makes the window longer" (`_file_meeting`'s own docstring). Same proof, the multi-page lane."""
    env, deps = rig
    before = support.branch_sha(env.bare)

    queue.submit(clean_queue, deps.evidence, kind=schema.MEETING, material=MEETING_MATERIAL,
                hints={"title": "Q3 sync", "meeting_date": "2026-07-29",
                      "source_label": "granola-manual"}, submitted_by="tester@example.com")
    item, result = worker.process_next(clean_queue, deps)

    assert result.status == schema.FAILED, (
        f"a file planted while the meeting lane's gates ran should refuse the commit, got "
        f"{result.status}: {result.error}")
    assert "appeared after the gates ran" in result.error
    assert "Planted During Gates.md" in result.error
    assert support.branch_sha(env.bare) == before


# ── the benign twin: an ordinary pass plants nothing and is unaffected ─────────────────────────
def test_the_benign_twin_an_ordinary_fast_lane_pass_is_unaffected_by_the_close(rig, clean_queue):
    """Without the sabotage fixture, both lanes must file exactly as they did before the close —
    it must not cost the ordinary pass anything. The same property `test_processing_pg.py` opens
    with, asserted here beside the reproduction so the two are read together."""
    env, deps = rig
    before = support.branch_sha(env.bare)

    support.submit(clean_queue, deps, ACME_MATERIAL)
    item, result = worker.process_next(clean_queue, deps)

    assert result.status == schema.FILED
    page_path, sha = result.result_ref.rsplit("@", 1)
    assert support.branch_sha(env.bare) == sha != before


def test_the_fast_lane_refuses_a_gated_page_rewritten_while_its_gates_run(rig, clean_queue,
                                                                          rewrite_during_gates):
    """OLD BEHAVIOUR: this filed. The comparison was over PATHS, and an in-place rewrite leaves
    the path set identical — so the bytes that landed on `main` were bytes no gate had read. The
    secrets, PII and body-rewrite gates had all already run by the time `gate_contract` handed the
    worktree to a repo-supplied script."""
    env, deps = rig
    before = support.branch_sha(env.bare)

    support.submit(clean_queue, deps, ACME_MATERIAL)
    item, result = worker.process_next(clean_queue, deps)

    assert result.status == schema.FAILED, (
        f"a page rewritten while the fast lane's gates ran should refuse the commit as a system "
        f"fault, got {result.status}: {result.error}")
    assert "changed after the gates ran" in result.error
    assert support.branch_sha(env.bare) == before, (
        "the wiring failed open: rewritten bytes landed on main without any gate having read them")
