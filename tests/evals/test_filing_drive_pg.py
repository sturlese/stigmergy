"""`run_filing._drive` — the loop that turns one capture into scored phases, driven for real.

Every other guard on this instrument is keyless, and `_drive` is the one part that cannot be: it
submits through the real queue, drains with `worker.process_next` against a real `git worktree` and
the eight gates, and sends a stored reply back through `BrainService.reply`. Its ask-back branch —
the SECOND scored phase of a parking capture — is reached by nothing else in this suite and by no
keyless run, because the offline double parks only on an explicit `DOUBLE:` directive and the
golden captures carry none on purpose. Left unguarded, a bug there is first seen on a paid run,
several agent passes in.

So this file owns its own directive-carrying material, exactly as the golden manifest says such a
test should ("the keyless sensitivity tests own their own directive-carrying material"). It scores
nothing about any backend's judgment: what it proves is that the loop composes — two phases for a
park, one for a plain filing, and a recorded MISS rather than a vanished phase when a backend does
not park at all.

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
from stigmergy.kernel.frontmatter import split_frontmatter
from stigmergy.kernel.registry import load_registry
from stigmergy.librarian import githubapp, worker
from stigmergy.librarian.agent import build_agent
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings as ServerSettings
from tests import testdb
from tests.librarian import support

FIXTURE_REPO = run_filing.FIXTURE / "repo"

PARKING_MATERIAL = """The Halcyon Grid programme has been running for two months and the notes
about it are scattered across three chat threads. This capture pulls them together.

DOUBLE:triage-entity=Halcyon Grid
"""

PLAIN_MATERIAL = """The two depots connected last month have run clean since, and the dispatch
schedules migrated without an incident worth recording.
"""

REPLY = "Halcyon Grid is our internal name for the Northwind Freight pilot — file it there."

_FIXTURE_REGISTRY = load_registry(str(FIXTURE_REPO / "ops" / "entity-registry.json"))


def _first_registry_entity() -> str:
    """The id the offline double anchors an ordinary capture to.

    DERIVED, never retyped: `DoubleAgent._registry_entity` files against the FIRST entity in the
    fixture's own registry file, so a literal here silently stops describing the double the day the
    fixture gains an entity ahead of it — which is exactly what issue #77's three new entities did.
    """
    return next(iter(_FIXTURE_REGISTRY.entities))


# TWO anchors, because the double reaches them by two different roads and they are no longer the
# same entity: an ordinary capture takes the registry's first entry, while a capture re-filed after
# a reply takes whatever the REPLY names (`DoubleAgent._resolve_reply`). One constant for both was
# a coincidence of the fixture, not a property of the double.
ANCHORED_BY_DEFAULT = {"kind": "entity", "ids": [_first_registry_entity()]}
ANCHORED_BY_THE_REPLY = {"kind": "entity",
                         "ids": [_FIXTURE_REGISTRY.canonical_id("Northwind Freight")]}


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
                             split_frontmatter=split_frontmatter, brain_service=BrainService,
                             server_settings=ServerSettings)


def _capture(material: str, kind: str = "raw") -> dict:
    return {"id": "T01", "kind": kind, "material": material,
            "submitted_by": "dana@stigmergy.test", "hints": {}}


def test_a_parking_capture_is_scored_as_two_phases_across_the_real_ask_back_loop(rig, tmp_path):
    """Park, reply, re-file — and the reply travels through `BrainService.reply`, the answer
    channel a human actually uses, rather than an UPDATE this test wrote itself. A loop tested
    against a hand-written row would prove nothing about the door the reply comes through."""
    materials = _material(tmp_path, "park.md", PARKING_MATERIAL)
    entry = {"id": "T01",
             "expect": {"status": schema.NEEDS_INPUT, "park_question": ["Halcyon Grid"]},
             "reply": REPLY,
             "after_reply": {"status": schema.FILED, "type": "note", "folder": "wiki/notes",
                             "anchor": ANCHORED_BY_THE_REPLY}}

    phases = _drive(rig, materials, _capture("park.md"), entry)

    assert [p["phase"] for p in phases] == ["park", "after_reply"]
    assert phases[0]["observed"]["status"] == schema.NEEDS_INPUT
    assert phases[0]["observed"]["park_question"] == ["Halcyon Grid"]
    assert phases[0]["facets"] == {"status": True, "park_question": True}
    assert phases[1]["observed"]["status"] == schema.FILED
    assert phases[1]["facets"] == dict.fromkeys(entry["after_reply"], True)


def test_the_re_file_after_a_reply_is_counted_as_its_own_agent_pass(rig, tmp_path):
    """`CountingAgent` is reset between the two phases, so each phase reports the passes IT spent.
    Without the reset the second phase would inherit the first's count and the cost axis would
    read double for every parking capture in the set."""
    materials = _material(tmp_path, "park.md", PARKING_MATERIAL)
    entry = {"id": "T01",
             "expect": {"status": schema.NEEDS_INPUT, "attempts": 1},
             "reply": REPLY,
             "after_reply": {"status": schema.FILED, "attempts": 1, "bounces": 0}}

    phases = _drive(rig, materials, _capture("park.md"), entry)

    assert phases[0]["observed"]["attempts"] == 1
    assert phases[1]["observed"]["attempts"] == 1
    assert phases[1]["observed"]["bounces"] == 0


def test_a_capture_with_no_reply_in_its_expectation_yields_exactly_one_phase(rig, tmp_path):
    """The benign twin of the two-phase case: eight of the ten golden captures are scored once,
    and a loop that produced a second phase for them would invent denominators nobody wrote."""
    materials = _material(tmp_path, "plain.md", PLAIN_MATERIAL)
    entry = {"id": "T01", "expect": {"status": schema.FILED, "type": "note",
                                     "folder": "wiki/notes",
                                     "anchor": ANCHORED_BY_DEFAULT}}

    phases = _drive(rig, materials, _capture("plain.md"), entry)

    assert [p["phase"] for p in phases] == ["only"]
    assert phases[0]["facets"] == dict.fromkeys(entry["expect"], True)


def test_draining_an_empty_queue_stops_the_run_with_a_sentence_instead_of_a_TypeError(rig):
    """`worker.process_next` answers `None` when it claimed nothing — legitimate for a service
    that polls, a contradiction here: each capture is submitted and drained on its own, so exactly
    one row is claimable and it belongs to this capture.

    Unpacking that `None` used to raise `TypeError: cannot unpack non-sequence`, a traceback whose
    top frame names tuple unpacking and whose cause is somewhere else entirely — a reply that never
    reached the row, a lease still held by an earlier run, another worker on this database. The
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


def test_a_backend_that_never_parks_still_produces_the_second_phase_as_a_miss(rig, tmp_path):
    """The denominator defence, end to end: the expectation asks for a park, the capture files
    instead, and the `after_reply` phase is recorded as a MISS rather than skipped. A phase that
    silently vanished would shrink its facets' denominators and quietly raise the score of a
    backend that never asked a question."""
    materials = _material(tmp_path, "plain.md", PLAIN_MATERIAL)
    entry = {"id": "T01",
             "expect": {"status": schema.NEEDS_INPUT, "park_question": ["Halcyon Grid"]},
             "reply": REPLY,
             "after_reply": {"status": schema.FILED, "type": "note", "folder": "wiki/notes",
                             "anchor": ANCHORED_BY_THE_REPLY}}

    phases = _drive(rig, materials, _capture("plain.md"), entry)

    assert [p["phase"] for p in phases] == ["park", "after_reply"]
    assert phases[0]["facets"] == {"status": False, "park_question": False}
    assert phases[1]["facets"] == dict.fromkeys(entry["after_reply"], False)
    assert "never parked" in phases[1]["observed"]["note"]


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
