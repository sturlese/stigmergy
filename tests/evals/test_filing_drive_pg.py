"""`run_filing._drive` — the loop that turns one capture into a scored phase, driven for real.

Every other guard on this instrument is keyless, and `_drive` is the one part that cannot be: it
submits through the real queue and drains with `worker.process_next` against a real `git worktree`
and the nine gates. The branch nothing else in this suite reaches is the PROPOSING one — the
capture whose name the registry does not know, which now files with an entity page created beside
it in the same commit rather than stopping on a question. The golden captures reach it only on a
paid run, because the offline double proposes only on an explicit `DOUBLE:propose=` directive and
they carry none on purpose. Left unguarded, a bug there is first seen several agent passes into a
measurement.

So this file owns its own directive-carrying material, exactly as the golden manifest says such a
test should ("the keyless sensitivity tests own their own directive-carrying material"). It scores
nothing about any backend's judgment: what it proves is that the loop composes — ONE phase per
capture, and a `proposals` observation the scorer can read on the road that produces one.

It is also the only test in the suite that exercises `support.build_repo(source=…)`, the seam that
lets the eval seed a run from its own frozen mini knowledge repo instead of the librarian
fixtures'.

Postgres and gitleaks are required, like the librarian processing suites and for the same reason:
a faked queue or a skipped secrets gate would prove nothing about the path a real run takes.
"""
import argparse
import json
import os

import pytest

from evals import eval_history, run_filing
from stigmergy.capture import schema
from stigmergy.entities.generator import canonical_id_for
from stigmergy.kernel.frontmatter import split_frontmatter
from stigmergy.kernel.registry import load_registry
from stigmergy.librarian import githubapp, worker
from stigmergy.librarian.agent import build_agent
from tests import testdb
from tests.librarian import support

FIXTURE_REPO = run_filing.FIXTURE / "repo"

PROPOSED_NAME = "Halcyon Grid"

PROPOSING_MATERIAL = f"""The {PROPOSED_NAME} programme has been running for two months and the notes
about it are scattered across three chat threads. This capture pulls them together.

DOUBLE:propose={PROPOSED_NAME}
"""

PLAIN_MATERIAL = """The two depots connected last month have run clean since, and the dispatch
schedules migrated without an incident worth recording.
"""

_FIXTURE_REGISTRY = load_registry(str(FIXTURE_REPO / "ops" / "entity-registry.json"))


def _first_registry_entity() -> str:
    """The id the offline double anchors an ordinary capture to.

    DERIVED, never retyped: `DoubleAgent._registry_entity` files against the FIRST entity in the
    fixture's own registry file, so a literal here silently stops describing the double the day the
    fixture gains an entity ahead of it — which is exactly what issue #77's three new entities did.
    """
    return next(iter(_FIXTURE_REGISTRY.entities))


# TWO anchors, because the double reaches them by two different roads: an ordinary capture takes the
# registry's first entry, while an introducing one anchors to the identity it just created — an id the
# fixture's registry does not carry at all until the filing's own commit publishes it. DERIVED
# through the generator's own `canonical_id_for`, never typed: an id is `slugify(name)` and a
# literal here would silently stop describing the double the day either rule moved.
ANCHORED_BY_DEFAULT = {"kind": "entity", "ids": [_first_registry_entity()]}
ANCHORED_BY_THE_PROPOSAL = {"kind": "entity", "ids": [canonical_id_for(PROPOSED_NAME)]}


@pytest.fixture()
def require_gitleaks():
    """Skip on a laptop without gitleaks; FAIL in CI — the same posture, and the same sentence,
    as `tests/librarian/conftest.py`'s own guard: a secrets gate whose tests silently skip is a
    secrets gate that silently passes."""
    if support.gitleaks_available():
        return
    if testdb.required():
        pytest.fail("$STIGMERGY_TEST_DSN is set (CI mode) but gitleaks is not on PATH — refusing "
                    "to skip the filing eval's drive test silently.")
    pytest.skip("gitleaks not on PATH (brew install gitleaks) — the secrets gate cannot run")


@pytest.fixture()
def rig(tmp_path, require_gitleaks):
    """The runner's own setup, one capture wide: the eval's frozen mini knowledge repo seeded into
    a throwaway bare remote plus clone, and `Deps` wired to the offline double behind the same
    `CountingAgent` a real run measures passes with."""
    conn = testdb.connect_or_skip("filing-golden-drive")
    with conn:
        schema.ensure_capture_schema(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM capture_queue")
        env = support.build_repo(str(tmp_path / "git"), source=str(FIXTURE_REPO))
        settings = support.build_settings(env, worktree_root=str(tmp_path / "worktrees"),
                                          backend="double")
        counting = run_filing.CountingAgent(build_agent(settings))
        yield conn, support.build_deps(env, settings, agent=counting), counting, env
    conn.close()


def _material(tmp_path, name: str, text: str):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def _drive(rig, tmp_path, capture: dict, entry: dict) -> list:
    conn, deps, counting, env = rig
    return run_filing._drive(conn, deps, counting, env, capture, entry, materials=tmp_path,
                             schema=schema, worker=worker, support=support,
                             split_frontmatter=split_frontmatter)


def _capture(material: str, kind: str = "raw") -> dict:
    return {"id": "T01", "kind": kind, "material": material,
            "submitted_by": "dana@stigmergy.test", "hints": {}}


def test_a_proposing_capture_is_scored_as_ONE_phase_that_filed(rig, tmp_path):
    """**REPLACES the two-phase ask-back test (ADR 041).** A capture naming something the registry
    does not know used to park, wait for a reply through `BrainService.reply`, and be scored twice.
    It files in one pass now: the entity page, the regenerated registry and the note land in a
    single commit, born confirmed by whoever captured — no inbox, and no measurement
    touches.

    Driven end to end rather than asserted on the scorer, because the composition is the thing that
    can break: `_drive` submits, drains once, observes and returns exactly one phase. A second phase
    appearing here would invent denominators nobody wrote, and a phase that filed while the
    instrument reported nothing observable about the proposal would score `proposals` 0.00 for every
    backend forever.
    """
    materials = _material(tmp_path, "propose.md", PROPOSING_MATERIAL)
    entry = {"id": "T01",
             "expect": {"status": schema.FILED, "type": "note", "folder": "wiki/notes",
                        "proposals": [PROPOSED_NAME], "anchor": ANCHORED_BY_THE_PROPOSAL}}

    phases = _drive(rig, materials, _capture("propose.md"), entry)

    assert [p["phase"] for p in phases] == ["only"]
    assert phases[0]["observed"]["status"] == schema.FILED
    assert phases[0]["observed"]["proposals"] == [PROPOSED_NAME]
    assert phases[0]["facets"] == dict.fromkeys(entry["expect"], True)


def test_the_page_a_proposing_capture_filed_is_anchored_to_an_id_the_fixture_never_carried(rig,
                                                                                           tmp_path):
    """The claim underneath ADR 041's D1, read off the COMMIT rather than off the report: the note
    is anchored to an entity that did not exist when the capture was claimed. `_observe` reads the
    anchor from the filed page's server-stamped `entity:`, so this is the resolved registry id and
    not the agent's spelling — and `gate_anchoring` resolves it against the registry the commit
    PUBLISHES, which is what makes an entity born in this commit anchor like any older one.

    Its benign twin is the plain capture below, which anchors to an id the fixture shipped with.
    """
    materials = _material(tmp_path, "propose.md", PROPOSING_MATERIAL)
    proposed_id = canonical_id_for(PROPOSED_NAME)
    assert _FIXTURE_REGISTRY.canonical_id(proposed_id) is None, (
        f"{proposed_id!r} is in the frozen fixture's registry — this capture can no longer propose "
        f"it, and the test above is measuring an ordinary anchoring instead")
    entry = {"id": "T01", "expect": {"anchor": ANCHORED_BY_THE_PROPOSAL}}

    phases = _drive(rig, materials, _capture("propose.md"), entry)

    assert phases[0]["observed"]["anchor"] == {"kind": "entity", "ids": [proposed_id]}


def test_a_capture_that_proposes_nothing_yields_one_phase_and_an_empty_proposals_list(rig,
                                                                                      tmp_path):
    """The benign twin of both tests above, and the state twelve of the fourteen golden captures are
    in: an ordinary filing against a registered entity proposes no identity. A `proposals`
    observation that came back non-empty here would be the instrument reading somebody else's field,
    and every capture in the set would score the facet without anything having been recognised."""
    materials = _material(tmp_path, "plain.md", PLAIN_MATERIAL)
    entry = {"id": "T01", "expect": {"status": schema.FILED, "type": "note",
                                     "folder": "wiki/notes",
                                     "anchor": ANCHORED_BY_DEFAULT}}

    phases = _drive(rig, materials, _capture("plain.md"), entry)

    assert [p["phase"] for p in phases] == ["only"]
    assert phases[0]["facets"] == dict.fromkeys(entry["expect"], True)
    assert phases[0]["observed"]["proposals"] == []


# **DELETED with the ask-back loop (ADR 041):**
# `test_a_parking_capture_is_scored_as_two_phases_across_the_real_ask_back_loop`,
# `test_the_re_file_after_a_reply_is_counted_as_its_own_agent_pass` and
# `test_a_backend_that_never_parks_still_produces_the_second_phase_as_a_miss`. The first drove a
# park, a reply through the real `BrainService.reply` and a re-file; the second proved
# `CountingAgent` was reset between the two phases so the cost axis did not read double; the third
# proved a backend that never parked scored the second phase as a MISS rather than losing it, which
# would have shrunk that facet's denominator and rewarded not asking.
#
# None of the three has a subject. `BrainService.reply` and the `brain_reply` tool are gone, the two
# parked statuses are `schema.RETIRED_STATUSES`, and `_drive` has no branch left to take. What
# survives of them is the rule they enforced — a scored moment is never allowed to vanish — and it
# is enforced one level up now, by `_check_set` refusing the retired `reply`/`after_reply` keys and
# by `EXPECTED_DENOMINATORS` refusing a set whose phase count moved.


def test_draining_an_empty_queue_stops_the_run_with_a_sentence_instead_of_a_TypeError(rig):
    """`worker.process_next` answers `None` when it claimed nothing — legitimate for a service
    that polls, a contradiction here: each capture is submitted and drained on its own, so exactly
    one row is claimable and it belongs to this capture.

    Unpacking that `None` used to raise `TypeError: cannot unpack non-sequence`, a traceback whose
    top frame names tuple unpacking and whose cause is somewhere else entirely — a submit that never
    landed, a lease still held by an earlier run, another worker on this database. The
    queue is left genuinely empty here rather than stubbing the worker: the `None` under test is
    the real one, produced by the real claim against a real Postgres.

    Its benign twin is the rest of this file — every other test drains a row that IS there.
    """
    conn, deps, _counting, _env = rig
    with pytest.raises(SystemExit) as ex:
        run_filing._drain_one(conn, deps, worker, capture_id="T01", what="its own capture")
    message = str(ex.value)
    assert "T01" in message and "its own capture" in message
    assert "leased" in message and "claimable" in message


# ── the whole runner, one capture wide ────────────────────────────────────────────────────────

def test_a_run_scrubs_the_credentials_that_would_make_it_touch_a_humans_state(tmp_path,
                                                                             require_gitleaks,
                                                                             monkeypatch):
    """`_run` end to end over a one-capture set, for the two properties nothing else can reach.

    **The App credentials.** `make filing-golden` hands this script the operator's own gitignored
    env file, and this is the first make target that drives `processing._file`. With the App
    configured, `_file` mints a REAL installation token and pushes to `github.com/<slug>` instead
    of the run's throwaway bare remote: every capture fails, the table reads as a backend that
    cannot file, and the cause is somewhere nobody would look. They are SET here first, on purpose
    — `tests/conftest.py` clears them for every test in the suite, so a test that did not set them
    would pass without `_run` doing anything at all.

    **The view LLM.** A filed meeting triggers a best-effort view regeneration that reads
    `$CLEAN_LLM` at call time and defaults to the real provider — real spend this instrument does
    not price, on a step no score depends on.

    Driving the real `_run` also exercises the two things a paid run does before anything else:
    `worker.startup_checks` against this fixture, and the empty-queue guard around every drain.
    `--backend double` appends no history row, and this asserts the series file was not touched.
    """
    for name in (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV, githubapp.PRIVATE_KEY_ENV,
                 githubapp.PRIVATE_KEY_FILE_ENV, githubapp.APP_LOGIN_ENV):
        monkeypatch.setenv(name, "set-by-the-operators-env-file")
    monkeypatch.setenv("CLEAN_LLM", "openai")
    testdb.connect_or_skip("filing-golden-run").close()

    (tmp_path / "plain.md").write_text(PLAIN_MATERIAL, encoding="utf-8")
    manifest = {"captures": [_capture("plain.md")]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    expectations = {"expectations": [
        {"id": "T01", "expect": {"status": schema.FILED, "type": "note", "folder": "wiki/notes",
                                 "anchor": ANCHORED_BY_DEFAULT, "attempts": 1}}]}
    report_path = tmp_path / "report.json"
    args = argparse.Namespace(repo=str(FIXTURE_REPO), manifest=str(tmp_path / "manifest.json"),
                              expectations=str(tmp_path / "expectations.json"), backend="double",
                              model=None, report=str(report_path))
    before = eval_history.HISTORY_PATH.read_bytes()

    assert run_filing._run(args, manifest, expectations) == 0

    for name in (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV, githubapp.PRIVATE_KEY_ENV,
                 githubapp.PRIVATE_KEY_FILE_ENV, githubapp.APP_LOGIN_ENV):
        assert name not in os.environ, f"{name} survived into a run that files"
    assert os.environ["CLEAN_LLM"] == "fake"

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["counts"] == {"captures": 1, "phases": 1}
    assert report["phases"][0]["facets"] == dict.fromkeys(
        expectations["expectations"][0]["expect"], True)
    assert eval_history.HISTORY_PATH.read_bytes() == before, (
        "the offline double is a plumbing check and has no quality number worth keeping")
