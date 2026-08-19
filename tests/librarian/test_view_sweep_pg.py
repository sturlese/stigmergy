"""The `job_runs` row a view sweep leaves behind, against a REAL Postgres.

`tests/views/test_sweep.py` and `tests/librarian/test_view_sweep_unit.py` both drive the pass with
`tests.views.conftest.FakeConn`, which records the SQL a run attempted. That is the right double
for "was a write attempted, with this shape" and it is not evidence about what a database stored:
`stats` is a `jsonb` column, `skip_reasons` is a list of strings inside it, and the round trip
through `Jsonb` and back is the part a fake cannot prove. `job_runs` is the only surface an
operator has on an unattended pass — the worker swallows the fault and prints nothing when a pass
converges — so it is worth a real database.

Deliberately in the librarian package: this is where the Postgres fixtures live (`clean_queue`
gives an empty `job_runs` per test, on a connection `tests.testdb` has already refused to open
against anything but `stigmergy_test`), and the worker is the caller the row exists for. The git
rig comes from `tests.views.conftest`, imported rather than re-built for the same reason
`test_view_sweep_unit.py` imports `FakeConn` from there.
"""
import asyncio
import os

import pytest

from stigmergy.librarian import worker
from stigmergy.views import regenerate
from tests.views.conftest import build_repo, entity_page, git, registry_of

_COMMIT_ENV = {"GIT_AUTHOR_NAME": "Test Steward", "GIT_AUTHOR_EMAIL": "steward@example.com",
               "GIT_COMMITTER_NAME": "Test Steward", "GIT_COMMITTER_EMAIL": "steward@example.com"}


@pytest.fixture(autouse=True)
def _offline_view_agent(monkeypatch):
    monkeypatch.setenv("CLEAN_LLM", "fake")


def _rows(conn, job: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute("SELECT job, status, stats, error FROM job_runs WHERE job = %s "
                    "ORDER BY id", (job,))
        return cur.fetchall()


def _two_entity_repo(tmp_path):
    """Two registered entities, neither with a view — a population of two, so a ceiling of one
    genuinely defers something."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    registry = registry_of()
    with open(os.path.join(clone, "wiki", "entities", "Globex.md"), "w") as f:
        f.write(entity_page("Globex", "globex"))
    registry.entities["globex"] = {"name": "Globex", "type": "organization", "aliases": []}
    git("add", "--all", cwd=clone)
    git("commit", "--quiet", "-m", "chore: a second entity", cwd=clone, env=_COMMIT_ENV)
    git("push", "--quiet", cwd=clone)
    return remote, clone, registry


def test_a_ceilinged_pass_stores_its_deferral_in_a_real_job_runs_row(clean_queue, tmp_path):
    """What Postgres actually holds after a pass that stopped at its ceiling — the row an operator
    reads to answer "did last night's sweep finish, and if not what is still owed".

    `deferred` and `skip_reasons` are asserted off the value the database returned, not off the
    `RunResult` the code computed: a `jsonb` column that dropped the list, stringified it, or
    stored `stats` before the ceiling entry was appended would be invisible to every fake.
    """
    remote, clone, registry = _two_entity_repo(tmp_path)

    asyncio.run(regenerate.sweep(clone, clean_queue, registry=registry, max_changes=1))

    rows = _rows(clean_queue, regenerate.SWEEP_JOB_NAME)
    assert len(rows) == 1, "one row for the WHOLE pass, never one per entity"
    job, status, stats, error = rows[0]
    assert (job, status, error) == ("views-sweep", "ok", "")
    assert stats["population"] == 2
    assert stats["checked"] == 1
    assert stats["deferred"] == 1
    assert stats["written"] == 1
    assert isinstance(stats["skip_reasons"], list) and len(stats["skip_reasons"]) == 1
    assert stats["skip_reasons"][0].startswith("run-ceiling-reached(1):")
    assert "the next run will see them" in stats["skip_reasons"][0]


def test_the_next_pass_stores_a_row_that_says_the_deferral_was_cleared(clean_queue, tmp_path):
    """The benign twin of the row above, and the property the deferral rests on: the SECOND row
    must show the surplus taken up and nothing deferred. Two rows that both said
    `run-ceiling-reached` forever would be the starvation signal an operator would actually see.
    """
    remote, clone, registry = _two_entity_repo(tmp_path)

    asyncio.run(regenerate.sweep(clone, clean_queue, registry=registry, max_changes=1))
    asyncio.run(regenerate.sweep(clone, clean_queue, registry=registry, max_changes=1))

    rows = _rows(clean_queue, regenerate.SWEEP_JOB_NAME)
    assert len(rows) == 2
    second = rows[1][2]
    assert second["written"] == 1 and second["unchanged"] == 1
    assert second["deferred"] == 0
    assert second["skip_reasons"] == []


def test_a_fault_mid_pass_stores_a_real_error_row_with_the_work_already_done(clean_queue,
                                                                            tmp_path,
                                                                            monkeypatch):
    """D9's row, in the database. `status='error'`, the exception CLASS (never `str(ex)` — a
    raised message can carry captured content), and — the part that matters — `stats` describing
    the entities the pass had already committed before it fell over. `ops.job_run` writes `stats`
    as it stood at the fault, so a run that pushed one commit and then died must not persist an
    empty blob claiming it did nothing.
    """
    remote, clone, registry = _two_entity_repo(tmp_path)
    real = regenerate.regenerate_entity
    seen: list[str] = []

    async def boom_on_the_second(repo, entity_id, **kw):
        if seen:
            raise RuntimeError("synthesis backend exploded")
        seen.append(entity_id)
        return await real(repo, entity_id, **kw)

    monkeypatch.setattr(regenerate, "regenerate_entity", boom_on_the_second)
    with pytest.raises(RuntimeError):
        asyncio.run(regenerate.sweep(clone, clean_queue, registry=registry))

    rows = _rows(clean_queue, regenerate.SWEEP_JOB_NAME)
    assert len(rows) == 1
    job, status, stats, error = rows[0]
    assert (job, status, error) == ("views-sweep", "error", "RuntimeError")
    assert stats["population"] == 2
    assert stats["checked"] == 1
    assert stats["written"] == 1, "the row must own up to the commit the pass had already pushed"


def test_the_workers_own_pass_records_one_real_row_under_its_own_job_name(clean_queue, rig):
    """End to end on the road that actually runs unattended: `worker.run_view_sweep` against a real
    Postgres and a real bare remote, with the worktree it materializes itself.

    The job NAME is the assertion that matters here — `views-sweep`, distinct from the `views` row
    an operator's `stigmergy-views regenerate` and the post-meeting hook both write — because it is
    what lets a run's history say which of the three did the work.
    """
    env, deps = rig
    _anchor_a_page(env)

    result = worker.run_view_sweep(clean_queue, deps)

    assert result.stats["written"] == 1
    assert _rows(clean_queue, regenerate.JOB_NAME) == []
    rows = _rows(clean_queue, regenerate.SWEEP_JOB_NAME)
    assert len(rows) == 1
    job, status, stats, error = rows[0]
    assert (job, status, error) == ("views-sweep", "ok", "")
    assert stats["written"] == 1 and stats["population"] == 1


def test_a_pass_over_an_empty_population_still_records_what_it_examined(clean_queue, rig):
    """**A row that says nothing, on the one pass an operator most needs to read.**

    The pass prints nothing when it finds nothing to do (`view_sweep_clause` returns `""`), so this
    row is the ONLY way to tell "the sweep ran and the corpus was converged" from "the sweep never
    ran". The librarian fixture repo anchors no entity, which is also the shape of the very first
    pass on a brand-new deployment and of a corpus whose last anchored page was deleted.

    `regenerate.run` only calls `stats.update(result.stats)` INSIDE its per-entity loop, so a
    population of zero stores a literally empty `{}` — no `population`, no `checked`, no `written`.
    A fake conn cannot show this: `RunResult.stats` is fully populated either way, and the
    developer's own converged-pass test asserts against that object rather than against the
    database. Reported, not fixed here; the fix is one `stats.update(result.stats)` before the
    loop.
    """
    env, deps = rig

    result = worker.run_view_sweep(clean_queue, deps)

    assert worker.view_sweep_clause(result) == ""
    rows = _rows(clean_queue, regenerate.SWEEP_JOB_NAME)
    assert len(rows) == 1
    assert rows[0][1] == "ok"
    assert rows[0][2] != {}, (
        "a sweep over an empty population stored an empty stats blob — the row cannot say whether "
        "the pass examined nothing or recorded nothing")
    assert rows[0][2]["population"] == 0
    assert rows[0][2]["written"] == 0


def _anchor_a_page(env) -> None:
    """A page anchored to the fixture's registered entity, plus the entity page anchoring itself —
    committed and pushed the way any door leaves one, with no view hook involved."""
    from tests.librarian import support
    with open(os.path.join(env.repo, "wiki", "notes", "Sweep Row Fixture.md"), "w") as f:
        f.write('---\ntype: note\ntitle: "Sweep Row Fixture"\nentity: [acme]\n'
                'as_of: "2026-08-17"\ncreated: "2026-08-17"\nupdated: "2026-08-17"\n'
                'status: developing\ntags: [note]\n---\n\n# Sweep Row Fixture\n\nA page.\n')
    entity_md = os.path.join(env.repo, "wiki", "entities", "Acme Corp.md")
    with open(entity_md) as f:
        text = f.read()
    with open(entity_md, "w") as f:
        f.write(text.replace("type: entity\n", "type: entity\nentity: [acme]\n", 1))
    support.commit_and_push(env.repo, "feat: a page anchored to acme")


# ── the sweep lock: two sweepers is a supported shape, and one of them yields ──────────────────
def test_a_sweep_finding_the_lock_taken_skips_with_no_row_and_no_writes(clean_queue, tmp_path):
    """The mutual-exclusion skip, made to FIRE — it needs a REAL second connection, because the
    lock is a Postgres advisory lock and `FakeConn.fetchone` answers `(1,)` to everything, which
    is why no offline suite can ever reach this branch. The skip writes NO `job_runs` row on
    purpose: the pass that holds the lock is converging exactly the same state, so there is
    nothing owed and nothing to report beyond the skip reason itself."""
    from tests import testdb
    remote, clone, registry = _two_entity_repo(tmp_path)
    holder = testdb.connect_or_skip("index")
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (regenerate.VIEW_SWEEP_LOCK_KEY,))
            assert cur.fetchone()[0] is True

        result = asyncio.run(regenerate.sweep(clone, clean_queue, registry=registry))

        assert result.skip_reasons == [regenerate.SWEEP_IN_FLIGHT_REASON]
        assert result.stats["written"] == 0
        assert _rows(clean_queue, regenerate.SWEEP_JOB_NAME) == []
        assert "views/" not in git("ls-tree", "-r", "--name-only", "main", cwd=remote).stdout
    finally:
        holder.close()   # the advisory lock dies with the session


def test_the_lock_released_the_next_sweep_proceeds(clean_queue, tmp_path):
    """The benign twin, on the same rig: the losing side's skip must be the RACE's cost, never a
    latch — a lock left behind by a dead sweeper dies with its connection, and the next pass
    converges normally."""
    remote, clone, registry = _two_entity_repo(tmp_path)

    result = asyncio.run(regenerate.sweep(clone, clean_queue, registry=registry))

    assert result.skip_reasons == []
    assert result.stats["written"] == 2
    ((job, status, _stats, error),) = _rows(clean_queue, regenerate.SWEEP_JOB_NAME)
    assert (job, status, error) == (regenerate.SWEEP_JOB_NAME, "ok", "")


# ── the pause reason: real flags, a real queue, real Postgres (issue #102) ─────────────────────
def test_the_workers_pause_reason_names_a_waiting_capture_and_yields_the_sweep(clean_queue, rig):
    """The wiring end of the cooperative pause: the worker's own callable answers with WORDS, and
    each cause gets its own — a shutdown signal, or a capture waiting in a REAL queue. This is the
    method `maybe_sweep_views` hands the pass, so a capture submitted one second into a ten-entity
    pass costs one entity's regeneration, never the whole pass (issue #102)."""
    from stigmergy.capture import queue as capture_queue
    from stigmergy.capture.evidence import MemoryEvidenceStore
    env, deps = rig
    w = worker.Worker(clean_queue, deps, on_output=lambda _m: None)

    assert w._sweep_pause_reason() == "", "an idle worker with an empty queue pauses nothing"

    capture_queue.submit(clean_queue, MemoryEvidenceStore(), kind="raw",
                         material="a capture that must not wait out a sweep",
                         hints=None, submitted_by="ana@example.com")
    assert w._sweep_pause_reason() == "a capture is waiting in the queue"

    w.stopping = True
    assert w._sweep_pause_reason() == "the process is shutting down", (
        "a signal outranks a waiting capture — the reason an operator reads must be the one that "
        "actually ends the process")


def test_the_pass_is_handed_the_pause_reason_method_itself(clean_queue, rig, monkeypatch):
    """The one line that wires the seam, pinned: `maybe_sweep_views` passes the worker's OWN
    callable, so the pass consults live state between entities rather than a copy of it."""
    env, deps = rig
    seen = {}

    def sweeper(conn, d, **kw):
        seen.update(kw)
        return regenerate.RunResult()

    w = worker.Worker(clean_queue, deps, on_output=lambda _m: None, view_sweep=sweeper,
                      now=lambda: 1000.0)

    assert w.maybe_sweep_views() is True
    assert seen["should_stop"] == w._sweep_pause_reason
