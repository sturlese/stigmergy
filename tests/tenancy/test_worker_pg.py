"""TEN-05, TEN-06, TEN-09: one writer serves tenants in turn, files each tenant's capture in that
tenant's own repository with nothing of the other in its context, and provisions a new tenant's
repository before it serves it."""
import subprocess
from pathlib import Path

from stigmergy.capture import queue, schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.capture.schema import Actor
from stigmergy.capture.service import CaptureService
from stigmergy.kernel import tenancy
from stigmergy.knowledge.contract import expected_librarian_skill
from stigmergy.knowledge.planner import OutputExhausted, PlanRun, ScriptedPlanner
from stigmergy.knowledge.trust import WRITER_NAME
from stigmergy.librarian import config, provision, worker, workspaces
from stigmergy.server import platform
from tests import testdb
from tests.roster import seed_roster
from tests.tenancy.conftest import acting

MORGAN = Actor(subject="morgan@example.com", display_name="Morgan")
ROSTER = {MORGAN.subject: {"display_name": "Morgan", "groups": ["brain-admins"],
                         "default_audience": ["public"]}}
FRESH = "00000000-0000-4000-8000-000000000003"


class RecordingPlanner(ScriptedPlanner):
    def __init__(self):
        super().__init__()
        self.calls = []

    def plan(self, **kwargs) -> PlanRun:
        self.calls.append(kwargs)
        return super().plan(**kwargs)


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _remote(root: Path, name: str) -> str:
    bare = root / f"{name}.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=root)
    seed = root / f"{name}-seed"
    _git("clone", "-q", str(bare), str(seed), cwd=root)
    skill = seed / ".claude" / "skills" / "librarian" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(expected_librarian_skill())
    _git("add", ".", cwd=seed)
    _git("-c", "user.name=Test", "-c", "user.email=t@example.com", "commit", "-qm", "init", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    return str(bare)


def _capture(conn, tenant: str, text: str, key: str, store=None) -> dict:
    with acting(conn, tenant):
        return CaptureService(conn, store or MemoryEvidenceStore()).capture_text(
            actor=MORGAN, audience=None, adapter="mcp", text=text, idempotency_key=key)


def test_tenants_take_turns_at_the_claim(conn, twin):
    store = MemoryEvidenceStore()
    with conn.cursor() as cur, tenancy.platform_scope(conn):
        cur.execute("UPDATE tenants SET last_claimed_at = NULL WHERE id = ANY(%s::uuid[])",
                    ([testdb.TEST_TENANT, twin],))
    for number in range(10):
        with acting(conn, testdb.TEST_TENANT):
            CaptureService(conn, store).capture_text(actor=MORGAN, audience=None, adapter="mcp",
                                                     text=f"mine {number}", idempotency_key=f"m{number}")
    with acting(conn, twin):
        theirs = CaptureService(conn, store).capture_text(actor=MORGAN, audience=None, adapter="mcp",
                                                          text="theirs", idempotency_key="t0")
    claimed = []
    for _ in range(3):
        with tenancy.platform_scope(conn):
            claimed.append(queue.claim_next(conn, operation_budget_s=600))
    assert [item["tenant_id"] for item in claimed] == [testdb.TEST_TENANT, twin, testdb.TEST_TENANT]
    assert claimed[1]["id"] == theirs["id"]


def test_a_suspended_tenants_work_is_never_claimed(conn, twin):
    _capture(conn, twin, "waiting", "w0")
    platform.set_tenant_status(conn, twin, "suspended")
    with tenancy.platform_scope(conn):
        assert queue.claim_next(conn, operation_budget_s=600) is None
    platform.set_tenant_status(conn, twin, "active")


def test_each_capture_lands_in_its_own_tenants_repository(conn, twin, tmp_path):
    remotes = {testdb.TEST_TENANT: _remote(tmp_path, "mine"), twin: _remote(tmp_path, "theirs")}
    for tenant in remotes:
        with acting(conn, tenant):
            seed_roster(conn, ROSTER)
    settings = config.Settings(repo=str(tmp_path / "checkouts"), branch="main", backend="scripted",
                               garden_at="off", rebuild_at="off")
    planner = RecordingPlanner()
    evidence = MemoryEvidenceStore()
    places = workspaces.Workspaces(conn, settings, evidence, planner=planner,
                                   url_for=lambda tenant: remotes[tenant.id])
    _capture(conn, testdb.TEST_TENANT, "Alpha quarterly ledger for the first tenant.", "a1", evidence)
    _capture(conn, twin, "Beta harbor schedule for the second tenant.", "b1", evidence)

    outcomes = [worker.process_next(conn, places) for _ in range(2)]
    assert [outcome.status for _item, outcome in outcomes] == [schema.LANDED, schema.LANDED], [
        (item.get("error_category"), item.get("error")) for item, _outcome in outcomes]
    assert tenancy.current_tenant(conn) == testdb.TEST_TENANT

    for tenant, words in ((testdb.TEST_TENANT, "Alpha"), (twin, "Beta")):
        log = _git("log", "--format=%an|%s", "main", cwd=remotes[tenant]).splitlines()
        assert len(log) == 2 and log[0].startswith(f"{WRITER_NAME}|"), log
        files = _git("show", "--name-only", "--format=", "main", cwd=remotes[tenant]).split()
        content = "".join(_git("show", f"main:{name}", cwd=remotes[tenant]) for name in files)
        assert words in content and ({"Alpha", "Beta"} - {words}).pop() not in content
        with acting(conn, tenant), conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM knowledge_changes")
            assert cur.fetchone()[0] == 1

    second = next(call for call in planner.calls if "Beta" in call["source_text"])
    assert "Alpha" not in second["context"] and "Alpha" not in second["source_text"]
    assert remotes[testdb.TEST_TENANT] not in second["worktree"]


def test_a_new_tenant_gets_its_repository_before_it_is_served(conn, tmp_path):
    testdb.ensure_tenant(conn, FRESH, "fresh", status="provisioning")
    bare = tmp_path / "fresh.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    settings = config.Settings(repo=str(tmp_path / "checkouts"), branch="main", backend="scripted",
                               garden_at="off", rebuild_at="off")
    places = workspaces.Workspaces(conn, settings, MemoryEvidenceStore(),
                                   url_for=lambda tenant: str(bare))

    def provisioner(tenant, path):
        provision.push_scaffold(path, str(bare), name=tenant.name, slug=tenant.slug)

    loop = worker.Worker(conn, places, provisioner=provisioner, on_output=lambda _line: None)
    try:
        assert loop._maybe_provision() is True
        assert platform.tenant_by_id(conn, FRESH).status == "active"
        files = set(_git("ls-tree", "-r", "--name-only", "main", cwd=bare).split())
        assert {"README.md", "CLAUDE.md", ".gitignore", "wiki/notes/.gitkeep", "wiki/entities/.gitkeep",
                "sources/.gitkeep"} <= files
        assert _git("log", "--format=%an", "main", cwd=bare).strip() == WRITER_NAME
        assert loop._maybe_provision() is False
        deps = places.deps_for(FRESH)
        assert Path(deps.repo, "README.md").is_file()
    finally:
        testdb.ensure_tenant(conn, FRESH, "fresh", status="suspended")


SECOND = "00000000-0000-4000-8000-000000000004"


def test_a_failing_provisioning_waits_and_does_not_hold_back_the_next_tenant(conn, tmp_path):
    testdb.ensure_tenant(conn, FRESH, "fresh", status="provisioning")
    testdb.ensure_tenant(conn, SECOND, "second", status="provisioning")
    settings = config.Settings(repo=str(tmp_path / "checkouts"), branch="main", backend="scripted",
                               garden_at="off", rebuild_at="off")
    places = workspaces.Workspaces(conn, settings, MemoryEvidenceStore())
    attempts = []
    clock = [1000.0]

    def provisioner(tenant, path):
        attempts.append(tenant.slug)
        if tenant.id == FRESH:
            raise RuntimeError("GitHub is down")

    loop = worker.Worker(conn, places, provisioner=provisioner, on_output=lambda _line: None,
                         monotonic=lambda: clock[0])
    try:
        assert loop._maybe_provision() is False
        assert loop._maybe_provision() is True
        assert attempts == ["fresh", "second"]
        assert platform.tenant_by_id(conn, SECOND).status == "active"
        assert loop._maybe_provision() is False
        assert attempts == ["fresh", "second"]
        clock[0] += worker.PROVISION_RETRY_S + 1
        assert loop._maybe_provision() is False
        assert attempts == ["fresh", "second", "fresh"]
        assert any(action["action"] == "tenant.provision" and action["outcome"] == "failed"
                   for action in platform.recent_actions(conn, limit=5))
    finally:
        testdb.ensure_tenant(conn, FRESH, "fresh", status="suspended")
        testdb.ensure_tenant(conn, SECOND, "second", status="suspended")


def test_an_index_never_built_is_rebuilt_at_once_for_its_tenant_alone(conn, twin, tmp_path):
    from stigmergy.index import health
    from stigmergy.index.backends.embedder import build_embedder

    remotes = {testdb.TEST_TENANT: _remote(tmp_path, "mine"), twin: _remote(tmp_path, "theirs")}
    settings = config.Settings(repo=str(tmp_path / "checkouts"), branch="main", backend="scripted",
                               garden_at="off", rebuild_at="04:17")
    places = workspaces.Workspaces(conn, settings, MemoryEvidenceStore(),
                                   url_for=lambda tenant: remotes[tenant.id])
    loop = worker.Worker(conn, places, on_output=lambda _line: None,
                         embedder_factory=lambda: build_embedder("fake"))
    with acting(conn, twin):
        health.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE index_health SET last_full_rebuild_at = NULL WHERE singleton")

    def rebuilt(tenant):
        with acting(conn, tenant), conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM job_runs WHERE job = %s AND status = 'succeeded'",
                        (worker.REBUILD_JOB,))
            return cur.fetchone()[0]

    for _ in range(4):
        loop._last_rebuild_check = None
        if rebuilt(twin):
            break
        loop._maybe_rebuild()
    assert rebuilt(twin) == 1
    with acting(conn, twin):
        assert health.read(conn)["last_full_rebuild_at"] is not None
    loop._last_rebuild_check = None
    with acting(conn, twin):
        before = rebuilt(twin)
    loop._maybe_rebuild()
    assert rebuilt(twin) == before           # built today; the daily window has not come round


class _StoppedWhileWaiting(ScriptedPlanner):
    """The deploy's stop signal arrives while the model call is in flight."""

    def plan(self, **kwargs) -> PlanRun:
        from stigmergy.kernel import shutdown
        with shutdown.interruptible():
            shutdown.request()
        raise AssertionError("the stop request did not interrupt the wait")


def test_a_writer_stopped_mid_model_call_returns_the_capture_to_the_queue(conn, tmp_path):
    remote = _remote(tmp_path, "stopped")
    with acting(conn, testdb.TEST_TENANT):
        seed_roster(conn, ROSTER)
    settings = config.Settings(repo=str(tmp_path / "checkouts"), branch="main", backend="scripted",
                               garden_at="off", rebuild_at="off")
    evidence = MemoryEvidenceStore()
    receipt = _capture(conn, testdb.TEST_TENANT, "Gamma ledger for a stopped writer.", "s1", evidence)
    stopped = workspaces.Workspaces(conn, settings, evidence, planner=_StoppedWhileWaiting(),
                                    url_for=lambda tenant: remote)

    assert worker.Worker(conn, stopped, on_output=lambda line: None).run() == 0

    with acting(conn, testdb.TEST_TENANT):
        item = queue.get_submission_trace(conn, receipt["id"])
    assert (item["status"], item["attempts"]) == (schema.QUEUED, 0)
    assert len(_git("log", "--format=%s", "main", cwd=remote).splitlines()) == 1

    resumed = workspaces.Workspaces(conn, settings, evidence, planner=RecordingPlanner(),
                                    url_for=lambda tenant: remote)
    worker.Worker(conn, resumed, on_output=lambda line: None)   # a new process starts unstopped
    item, outcome = worker.process_next(conn, resumed)
    assert item["id"] == receipt["id"] and outcome.status == schema.LANDED


class _ExhaustedOnce(RecordingPlanner):
    """The first plan spends the whole output limit on reasoning, as a sampled model sometimes does."""

    def plan(self, **kwargs) -> PlanRun:
        if not self.calls:
            self.calls.append(kwargs)
            raise OutputExhausted("Model token limit (65536) exceeded before any response was generated")
        return super().plan(**kwargs)


def test_a_plan_that_ran_out_of_output_is_filed_on_its_second_attempt(conn, tmp_path):
    remote = _remote(tmp_path, "exhausted")
    with acting(conn, testdb.TEST_TENANT):
        seed_roster(conn, ROSTER)
    settings = config.Settings(repo=str(tmp_path / "checkouts"), branch="main", backend="scripted",
                               garden_at="off", rebuild_at="off")
    evidence = MemoryEvidenceStore()
    receipt = _capture(conn, testdb.TEST_TENANT, "Delta ledger whose first plan ran out.", "e1", evidence)
    librarian = _ExhaustedOnce()
    places = workspaces.Workspaces(conn, settings, evidence, planner=librarian,
                                   url_for=lambda tenant: remote)

    item, outcome = worker.process_next(conn, places)
    assert item["id"] == receipt["id"] and outcome.status == schema.QUEUED
    with acting(conn, testdb.TEST_TENANT):
        queued = queue.get_submission_trace(conn, receipt["id"])
        assert (queued["attempts"], queued["budget_deadline_at"]) == (1, None)
        assert queued["error_category"] == "UnexpectedModelBehavior"
        with conn.cursor() as cur:
            cur.execute("UPDATE capture_queue SET next_attempt_at = now() WHERE id = %s", (receipt["id"],))

    item, outcome = worker.process_next(conn, places)
    assert item["id"] == receipt["id"] and outcome.status == schema.LANDED
    assert len(_git("log", "--format=%s", "main", cwd=remote).splitlines()) == 2
    assert [call["retry"] for call in librarian.calls] == [False, True]
