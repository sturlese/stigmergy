import datetime as dt
import json
import subprocess
from dataclasses import dataclass

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from stigmergy.admin.routes import MAX_ADMIN_BODY_BYTES, compose
from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.admin.service import AdminRefused, AdminService
from stigmergy.admin.settings import AdminSettings
from stigmergy.capture import evidence, queue, schema, uploads
from stigmergy.capture.errors import FetchRejected, FetchUnavailable
from stigmergy.capture.fetch import FetchedArtifact
from stigmergy.changes import store as change_store
from stigmergy.entities.model import registry_bytes
from stigmergy.index import build
from stigmergy.index import store as index_store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.corpus import split_frontmatter_checked
from stigmergy.knowledge import contradictions
from stigmergy.knowledge.contradictions import Contradiction
from stigmergy.knowledge.pages import render_page
from stigmergy.knowledge.plan import ContradictionClaim, FilingPlan
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker
from stigmergy.server.identity import hash_token
from stigmergy.server.settings import Settings
from tests import testdb
from tests.index.support import write_controls

TOKEN = "test-admin-token"
MASTER = "marc"


@dataclass
class AdminRig:
    client: TestClient
    conn: object
    repo: object
    evidence: evidence.MemoryEvidenceStore

    @property
    def auth(self):
        return {"Authorization": f"Bearer {TOKEN}"}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def admin_rig(tmp_path):
    repo = tmp_path / "brain"
    repo.mkdir()
    (repo / "ops").mkdir()
    (repo / "wiki" / "notes").mkdir(parents=True)
    (repo / "ops" / "identities.json").write_text(
        '{"marc":{"display_name":"Marc","groups":["brain-admins"],'
        '"default_audience":null},"ana":{"display_name":"Ana",'
        '"groups":["finance"],"default_audience":["finance"]}}\n'
    )
    (repo / "ops" / "entity-registry.json").write_bytes(registry_bytes({}))
    write_controls(repo)
    (repo / "wiki" / "notes" / "Welcome.md").write_text(
        render_page(
            path="wiki/notes/Welcome.md",
            role="note",
            title="Welcome",
            body="# Welcome\n\nInitial team knowledge.",
            acl=None,
            created=dt.date(2026, 8, 1),
            updated=dt.date(2026, 8, 1),
        )
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")

    conn = testdb.connect_or_skip("admin")
    schema.ensure_capture_schema(conn)
    uploads.ensure_upload_schema(conn)
    change_store.ensure_change_schema(conn)
    ensure_admin_schema(conn)
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM admin_actions")
        cursor.execute("DELETE FROM capture_artifacts")
        cursor.execute("DELETE FROM upload_sessions")
        cursor.execute("DELETE FROM knowledge_changes")
        cursor.execute("DELETE FROM capture_queue")
        cursor.execute("DELETE FROM job_runs")
        cursor.execute(
            "UPDATE worker_heartbeat SET heartbeat_at = now(), state = 'starting' WHERE singleton"
        )
    build.rebuild(conn, str(repo), build_embedder("fake"))

    server_settings = Settings(
        identities_path=str(repo / "ops" / "identities.json"),
        entity_registry_path=str(repo / "ops" / "entity-registry.json"),
        knowledge_repo=str(repo),
        knowledge_branch="main",
        dsn=testdb.dsn(),
        embedder="fake",
        llm="fake",
    )
    evidence_store = evidence.MemoryEvidenceStore()

    async def fallback(_request):
        return JSONResponse({"fallback": True})

    inner = Starlette(routes=[Route("/{path:path}", fallback)])
    app = compose(
        inner,
        conn=conn,
        server_settings=server_settings,
        admin_settings=AdminSettings(token_hash=hash_token(TOKEN), actor=MASTER),
        evidence=evidence_store,
    )
    with TestClient(app) as client:
        yield AdminRig(client, conn, repo, evidence_store)
    conn.close()


def test_admin_is_master_only_and_serves_a_secured_login_shell(admin_rig):
    unauthorized = admin_rig.client.get("/admin/api/meta")
    assert unauthorized.status_code == 401

    meta = admin_rig.client.get("/admin/api/meta", headers=admin_rig.auth)
    assert meta.status_code == 200
    assert meta.json()["actor"] == {"subject": MASTER, "display_name": "Marc"}
    assert meta.json()["statuses"] == ["queued", "processing", "landed", "failed"]
    assert "no-store" in meta.headers["cache-control"]
    assert "default-src 'none'" in meta.headers["content-security-policy"]

    shell = admin_rig.client.get("/admin/")
    assert shell.status_code == 200
    assert "<title>Stigmergy Ops</title>" in shell.text
    assert 'type="module" src="./assets/app.js"' in shell.text
    assert "no-cache" in shell.headers["cache-control"]


def test_admin_refuses_a_configured_actor_without_unrestricted_access(admin_rig):
    server_settings = Settings(
        identities_path=str(admin_rig.repo / "ops" / "identities.json"),
        entity_registry_path=str(admin_rig.repo / "ops" / "entity-registry.json"),
        knowledge_repo=str(admin_rig.repo),
        knowledge_branch="main",
        dsn=testdb.dsn(),
        embedder="fake",
        llm="fake",
    )

    with pytest.raises(AdminRefused, match="not unrestricted"):
        AdminService(
            admin_rig.conn,
            server_settings=server_settings,
            admin_settings=AdminSettings(token_hash=hash_token(TOKEN), actor="ana"),
            evidence=admin_rig.evidence,
        )


def test_admin_route_marks_forbidden_configured_actor_as_forbidden(admin_rig, monkeypatch):
    def refuse_actor(_service):
        raise AdminRefused("the configured admin actor is not unrestricted")

    monkeypatch.setattr(AdminService, "_principal", refuse_actor)

    response = admin_rig.client.get("/admin/api/meta", headers=admin_rig.auth)

    assert response.json() == {"error": "the configured admin actor is not unrestricted"}
    assert TOKEN not in response.text
    assert response.status_code != 409
    assert response.status_code == 403


def test_admin_route_marks_missing_evidence_service_as_unavailable(admin_rig):
    server_settings = Settings(
        identities_path=str(admin_rig.repo / "ops" / "identities.json"),
        entity_registry_path=str(admin_rig.repo / "ops" / "entity-registry.json"),
        knowledge_repo=str(admin_rig.repo),
        knowledge_branch="main",
        dsn=testdb.dsn(),
        embedder="fake",
        llm="fake",
    )
    app = compose(
        Starlette(),
        conn=admin_rig.conn,
        server_settings=server_settings,
        admin_settings=AdminSettings(token_hash=hash_token(TOKEN), actor=MASTER),
        evidence=None,
    )

    with TestClient(app) as client:
        response = client.post(
            "/admin/api/captures/text",
            headers=admin_rig.auth,
            json={"text": "capture-secret-must-not-leak", "audience": None},
        )

    assert response.json() == {"error": "evidence storage is unavailable"}
    assert "capture-secret-must-not-leak" not in response.text
    assert TOKEN not in response.text
    assert response.status_code != 409
    assert response.status_code == 503


def test_admin_route_marks_public_url_fetch_unavailable_as_temporary_and_safe(
    admin_rig, monkeypatch
):
    submitted_url = "https://submitted.example/private?access_token=query-secret"
    raw_detail = "resolved 198.51.100.17 for submitted.example: socket timeout"

    def unavailable(_url):
        raise FetchUnavailable(raw_detail)

    monkeypatch.setattr("stigmergy.admin.service.fetch.fetch_public", unavailable)

    response = admin_rig.client.post(
        "/admin/api/captures/url",
        headers=admin_rig.auth,
        json={"url": submitted_url, "audience": None},
    )

    assert response.json() == {"error": "public URL is temporarily unavailable"}
    for forbidden in (submitted_url, "query-secret", "198.51.100.17", raw_detail):
        assert forbidden not in response.text
    assert response.status_code == 503


def test_admin_route_keeps_public_url_fetch_rejection_as_safe_bad_request(admin_rig, monkeypatch):
    submitted_url = "https://submitted.example/private?access_token=query-secret"
    raw_detail = "resolved 198.51.100.17 for submitted.example: policy denied"

    def rejected(_url):
        raise FetchRejected(raw_detail)

    monkeypatch.setattr("stigmergy.admin.service.fetch.fetch_public", rejected)

    response = admin_rig.client.post(
        "/admin/api/captures/url",
        headers=admin_rig.auth,
        json={"url": submitted_url, "audience": None},
    )

    assert response.status_code == 400
    assert set(response.json()) == {"error"}
    for forbidden in (submitted_url, "query-secret", "198.51.100.17", raw_detail):
        assert forbidden not in response.text


def test_admin_route_marks_corrupt_entity_registry_as_unavailable(admin_rig):
    index_store.write_ops_file(
        admin_rig.conn,
        index_store.ENTITY_REGISTRY_RELPATH,
        '{"registry-secret"',
        "test corrupt registry",
    )

    response = admin_rig.client.get("/admin/api/entities", headers=admin_rig.auth)

    assert response.json() == {"error": "entity registry could not be read"}
    assert "registry-secret" not in response.text
    assert TOKEN not in response.text
    assert response.status_code != 409
    assert response.status_code == 503


def test_admin_route_marks_unavailable_git_patch_reconstruction_as_unavailable(admin_rig):
    parent = _git(admin_rig.repo, "rev-parse", "HEAD")
    path = admin_rig.repo / "wiki" / "notes" / "Patch recovery.md"
    path.write_text(
        render_page(
            path="wiki/notes/Patch recovery.md",
            role="note",
            title="Patch recovery",
            body="# Patch recovery\n\npatch-recovery-secret",
            acl=None,
            created=dt.date(2026, 8, 24),
            updated=dt.date(2026, 8, 24),
        )
    )
    _git(admin_rig.repo, "add", ".")
    _git(admin_rig.repo, "commit", "-q", "-m", "add patch recovery")
    commit = _git(admin_rig.repo, "rev-parse", "HEAD")
    record = change_store.record_change(
        admin_rig.conn,
        admin_rig.evidence,
        repo=str(admin_rig.repo),
        trigger="capture",
        actor=MASTER,
        parent_commit_sha=parent,
        commit_sha=commit,
        summary="Recorded patch recovery",
        reasons={"wiki/notes/Patch recovery.md": "Recorded the recovery procedure"},
    )
    assert admin_rig.evidence.delete(record.exact_patch_ref)
    server_settings = Settings(
        identities_path=str(admin_rig.repo / "ops" / "identities.json"),
        entity_registry_path=str(admin_rig.repo / "ops" / "entity-registry.json"),
        dsn=testdb.dsn(),
        embedder="fake",
        llm="fake",
    )
    app = compose(
        Starlette(),
        conn=admin_rig.conn,
        server_settings=server_settings,
        admin_settings=AdminSettings(token_hash=hash_token(TOKEN), actor=MASTER),
        evidence=admin_rig.evidence,
    )

    with TestClient(app) as client:
        response = client.get(f"/admin/api/changes/{record.id}", headers=admin_rig.auth)

    assert response.json() == {
        "error": "the exact patch cache is missing and Git reconstruction is unavailable"
    }
    assert "patch-recovery-secret" not in response.text
    assert TOKEN not in response.text
    assert response.status_code != 409
    assert response.status_code == 503


def test_admin_route_preserves_too_large_and_unexpected_error_mappings(admin_rig, monkeypatch):
    too_large = admin_rig.client.post(
        "/admin/api/captures/text",
        headers={**admin_rig.auth, "Content-Length": str(MAX_ADMIN_BODY_BYTES + 1)},
        content=b"",
    )

    def explode(_service):
        raise RuntimeError("internal-secret")

    monkeypatch.setattr(AdminService, "meta", explode)
    unexpected = admin_rig.client.get("/admin/api/meta", headers=admin_rig.auth)

    assert too_large.status_code == 413
    assert too_large.json() == {"error": "request too large"}
    assert unexpected.status_code == 500
    assert unexpected.json() == {"error": "operation failed (RuntimeError)"}
    assert "internal-secret" not in unexpected.text


def test_text_file_and_public_url_use_one_normalized_capture_contract(admin_rig, monkeypatch):
    monkeypatch.setattr(
        "stigmergy.admin.service.fetch.fetch_public",
        lambda url: FetchedArtifact(
            data=b"remote text",
            final_url="https://cdn.example/notes.txt",
            response_media_type="text/plain",
        ),
    )
    text_response = admin_rig.client.post(
        "/admin/api/captures/text",
        headers=admin_rig.auth,
        json={"text": "Pasted meeting transcript", "audience": ["finance"]},
    )
    file_response = admin_rig.client.post(
        "/admin/api/captures/file",
        headers=admin_rig.auth,
        data={"audience": "null", "title": "Uploaded notes"},
        files={"file": ("notes.txt", b"Uploaded notes", "text/plain")},
    )
    url_response = admin_rig.client.post(
        "/admin/api/captures/url",
        headers=admin_rig.auth,
        json={
            "url": "https://files.example/start?access_token=secret#viewer",
            "audience": None,
        },
    )

    assert {text_response.status_code, file_response.status_code, url_response.status_code} == {200}
    rows = [
        queue.get_submission_trace(admin_rig.conn, response.json()["id"])
        for response in (text_response, file_response, url_response)
    ]
    assert {row["operation"] for row in rows} == {schema.CAPTURE}
    assert {row["request"]["origin"]["adapter"] for row in rows} == {"admin"}
    assert all(row["actor"]["subject"] == MASTER for row in rows)
    assert rows[0]["acl"] == ["finance"]
    assert rows[1]["acl"] is None and rows[2]["acl"] is None
    acquisition = rows[2]["request"]["origin"]["acquisition"]
    assert acquisition["original_url"] == "https://files.example/start"
    assert acquisition["final_url"] == "https://cdn.example/notes.txt"
    assert "secret" not in json.dumps(rows[2]["request"])


def test_text_capture_normalizes_an_iso_date_before_queueing(admin_rig):
    response = admin_rig.client.post(
        "/admin/api/captures/text",
        headers=admin_rig.auth,
        json={
            "text": "A dated capture",
            "audience": None,
            "occurred_at": "2026-08-24",
        },
    )

    assert response.status_code == 200
    queued = queue.get_submission_trace(admin_rig.conn, response.json()["id"])
    envelope = schema.CaptureEnvelope.model_validate(queued["request"])
    assert envelope.origin.occurred_at == dt.date(2026, 8, 24)


def test_contradiction_resolution_url_provenance_lands_in_its_source(admin_rig, monkeypatch):
    contradiction = Contradiction(
        contradiction_id="con_00000000-0000-4000-8000-000000000001",
        explanation="Two signed sources disagree.",
        claims=(
            ContradictionClaim(
                text="The renewal is annual.",
                source="sources/2026/08/00000000-0000-4000-8000-000000000001.md",
                date="2026-08-01",
            ),
            ContradictionClaim(
                text="The renewal is monthly.",
                source="sources/2026/08/00000000-0000-4000-8000-000000000002.md",
                date="2026-08-02",
            ),
        ),
    )
    path = admin_rig.repo / "wiki" / "notes" / "Renewal.md"
    path.write_text(
        render_page(
            path="wiki/notes/Renewal.md",
            role="note",
            title="Renewal",
            body=f"# Renewal\n\n{contradictions.render(contradiction)}",
            acl=("finance",),
            created=dt.date(2026, 8, 1),
            updated=dt.date(2026, 8, 2),
        )
    )
    for claim in contradiction.claims:
        source = admin_rig.repo.joinpath(*claim.source.split("/"))
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "---\n"
            f"id: {source.stem}\n"
            "type: source\n"
            "submitted_by: ana\n"
                "acl: [finance]\n"
                "captured_at: 2026-08-01T12:00:00+00:00\n"
                "origin: mcp\n"
                "participants: []\n"
                "artifacts:\n"
            f"  - sha256: {'a' * 64}\n"
            "    bytes: 1\n"
            "    media_type: text/plain\n"
            f"    readable_sha256: {'a' * 64}\n"
                "    extractor: utf8\n"
                "    extractor_version: '1'\n"
                "    ocr_pages: []\n"
                "---\n\n"
                f"# Captured source\n\n{claim.text}\n",
            encoding="utf-8",
        )
    _git(admin_rig.repo, "add", ".")
    _git(admin_rig.repo, "commit", "-q", "-m", "add contradiction")
    build.rebuild(admin_rig.conn, str(admin_rig.repo), build_embedder("fake"))
    monkeypatch.setattr(
        "stigmergy.admin.service.fetch.fetch_public",
        lambda _url: FetchedArtifact(
            data=b"The signed agreement establishes annual renewal.",
            final_url="https://cdn.example/agreements/renewal.txt",
            response_media_type=schema.MEDIA_TEXT,
        ),
    )

    listing = admin_rig.client.get("/admin/api/contradictions", headers=admin_rig.auth)
    assert listing.json()["count"] == 1
    assert [item["path"] for item in listing.json()["contradictions"][0]["paths"]] == ["wiki/notes/Renewal.md"]
    response = admin_rig.client.post(
        "/admin/api/contradictions/resolve",
        headers=admin_rig.auth,
        json={
            "contradiction_id": contradiction.contradiction_id,
            "decision": "claim_a",
            "resolution": "The signed agreement controls and states annual renewal.",
            "rationale": "The agreement has higher authority than the billing schedule.",
            "support_url": ("https://files.example/renewal.txt?access_token=secret#viewer"),
        },
    )

    assert response.status_code == 200
    row = queue.get_submission_trace(admin_rig.conn, response.json()["id"])
    assert row["operation"] == schema.CAPTURE
    assert row["acl"] == ["finance"]
    assert row["request"]["intent"]["resolution_of"] == contradiction.contradiction_id
    assert len(row["request"]["artifacts"]) == 2
    acquisition = row["request"]["origin"]["acquisition"]
    assert acquisition["original_url"] == "https://files.example/renewal.txt"
    assert acquisition["final_url"] == "https://cdn.example/agreements/renewal.txt"
    assert row["request"]["origin"]["locator"] == acquisition["final_url"]
    assert "secret" not in json.dumps(row["request"])

    item, outcome = worker.process_next(
        admin_rig.conn,
        WriterDeps(
            config.Settings(repo=str(admin_rig.repo), branch="main", backend="scripted"),
            admin_rig.evidence,
            ScriptedPlanner(
                FilingPlan(
                    summary="Resolved the renewal contradiction",
                    resolved_contradictions=(contradiction.contradiction_id,),
                )
            ),
            str(admin_rig.repo),
        ),
    )
    source_text = _git(admin_rig.repo, "show", f"main:{item['source_path']}")
    metadata, _body, malformed = split_frontmatter_checked(source_text)

    assert outcome.status == schema.LANDED
    assert malformed is False
    assert metadata["acquisition"]["original_url"] == ("https://files.example/renewal.txt")
    assert metadata["acquisition"]["final_url"] == ("https://cdn.example/agreements/renewal.txt")
    assert "secret" not in source_text


def test_change_detail_has_friendly_and_exact_diffs_for_spaced_unicode_paths(admin_rig):
    parent = _git(admin_rig.repo, "rev-parse", "HEAD")
    path = admin_rig.repo / "wiki" / "notes" / "Café plan.md"
    path.write_text(
        render_page(
            path="wiki/notes/Café plan.md",
            role="note",
            title="Café plan",
            body="# Café plan\n\nA concise operating plan.",
            acl=None,
            created=dt.date(2026, 8, 24),
            updated=dt.date(2026, 8, 24),
        )
    )
    _git(admin_rig.repo, "add", ".")
    _git(admin_rig.repo, "commit", "-q", "-m", "add café plan")
    commit = _git(admin_rig.repo, "rev-parse", "HEAD")
    record = change_store.record_change(
        admin_rig.conn,
        admin_rig.evidence,
        repo=str(admin_rig.repo),
        trigger="capture",
        actor=MASTER,
        parent_commit_sha=parent,
        commit_sha=commit,
        summary="Recorded the café plan",
        reasons={"wiki/notes/Café plan.md": "Captured the agreed operating plan"},
    )

    response = admin_rig.client.get(
        f"/admin/api/changes/{record.id}",
        headers=admin_rig.auth,
    )
    assert response.status_code == 200
    change = response.json()["change"]
    assert change["summary"] == "Recorded the café plan"
    assert change["manifest"][0]["reason"] == "Captured the agreed operating plan"
    assert change["counts"]["contradictions_added"] == 0
    assert change["counts"]["contradictions_resolved"] == 0
    assert "wiki/notes/Café plan.md" in change["path_patches"]
    assert change["exact_patch"].startswith("diff --git")
    assert change["commit_sha"] == commit


def test_entity_delete_garden_and_failed_retry_are_queue_operations(admin_rig):
    entity_id = "ent_00000000-0000-4000-8000-000000000001"
    deletion = admin_rig.client.post(
        "/admin/api/entities/operation",
        headers=admin_rig.auth,
        json={"action": "delete", "entity_ids": [entity_id], "rationale": "Duplicate identity"},
    )
    garden = admin_rig.client.post(
        "/admin/api/gardener/trigger",
        headers=admin_rig.auth,
        json={"rationale": "Verify corpus health"},
    )
    capture = admin_rig.client.post(
        "/admin/api/captures/text",
        headers=admin_rig.auth,
        json={"text": "Retry fixture", "audience": None},
    )
    with admin_rig.conn.cursor() as cursor:
        cursor.execute(
            "UPDATE capture_queue SET status = 'failed', finished_at = now(), "
            "error_category = 'fixture', error = 'safe failure' WHERE id = %s",
            (capture.json()["id"],),
        )
    retried = admin_rig.client.post(
        f"/admin/api/captures/{capture.json()['id']}/retry",
        headers=admin_rig.auth,
    )

    assert deletion.status_code == garden.status_code == retried.status_code == 200
    assert queue.get_submission_trace(admin_rig.conn, deletion.json()["id"])["operation"] == schema.ENTITY
    assert queue.get_submission_trace(admin_rig.conn, garden.json()["id"])["operation"] == schema.GARDEN
    assert retried.json()["status"] == schema.QUEUED


def test_entity_merge_route_requires_typed_verifiable_evidence(admin_rig):
    entity_ids = [
        "ent_00000000-0000-4000-8000-000000000001",
        "ent_00000000-0000-4000-8000-000000000002",
    ]
    rationale_only = admin_rig.client.post(
        "/admin/api/entities/operation",
        headers=admin_rig.auth,
        json={
            "action": "merge",
            "entity_ids": entity_ids,
            "rationale": "These look alike.",
        },
    )
    evidenced = admin_rig.client.post(
        "/admin/api/entities/operation",
        headers=admin_rig.auth,
        json={
            "action": "merge",
            "entity_ids": entity_ids,
            "rationale": "The CRM identifier is authoritative.",
            "evidence": {
                "shared_external_id": {
                    "namespace": "crm",
                    "value": "account-7",
                }
            },
        },
    )

    assert rationale_only.status_code == 400
    assert "verifiable evidence" in rationale_only.json()["error"]
    assert evidenced.status_code == 200
    request = queue.get_submission_trace(admin_rig.conn, evidenced.json()["id"])["request"]
    assert request["evidence"]["shared_external_id"] == {
        "namespace": "crm",
        "value": "account-7",
    }


def test_index_endpoint_warns_on_stale_rebuild_and_convergence(admin_rig):
    with admin_rig.conn.cursor() as cursor:
        cursor.execute(
            "UPDATE index_health SET dirty = TRUE, dirty_since = now() - interval '20 minutes', "
            "last_full_rebuild_at = now() - interval '27 hours' WHERE singleton"
        )

    response = admin_rig.client.get("/admin/api/index", headers=admin_rig.auth)

    assert response.status_code == 200
    assert response.json()["healthy"] is False
    assert len(response.json()["warnings"]) == 2


def test_operational_overview_capture_list_and_detail_share_the_queue_state(admin_rig):
    submitted = admin_rig.client.post(
        "/admin/api/captures/text",
        headers=admin_rig.auth,
        json={
            "text": "The annual renewal was approved.",
            "title": "Renewal approval",
            "audience": ["finance"],
            "idempotency_key": "admin-overview-test",
        },
    )
    capture_id = submitted.json()["id"]

    overview = admin_rig.client.get("/admin/api/overview", headers=admin_rig.auth)
    listing = admin_rig.client.get(
        "/admin/api/captures?status=queued&submitter=marc&limit=10&offset=0",
        headers=admin_rig.auth,
    )
    detail = admin_rig.client.get(
        f"/admin/api/captures/{capture_id}",
        headers=admin_rig.auth,
    )
    missing = admin_rig.client.get(
        "/admin/api/captures/00000000-0000-4000-8000-000000000000",
        headers=admin_rig.auth,
    )

    assert overview.status_code == listing.status_code == detail.status_code == 200
    assert overview.json()["captures"]["counts"]["queued"] == 1
    assert overview.json()["captures"]["oldest_created_at"]["queued"] is not None
    assert overview.json()["changes"] == 0
    assert overview.json()["contradictions"] == 0
    assert overview.json()["worker"]["stale"] is False
    assert overview.json()["worker"]["heartbeat"]["state"] == "starting"
    assert listing.json()["count"] == 1
    assert listing.json()["captures"][0]["id"] == capture_id
    assert detail.json()["id"] == capture_id
    assert detail.json()["provenance"]["title"] == "Renewal approval"
    assert detail.json()["artifacts"][0]["media_type"] == schema.MEDIA_TEXT
    assert missing.status_code == 404
    assert missing.json() == {"error": "capture not found"}


def test_admin_capture_validates_timestamp_and_audience_before_queueing(admin_rig):
    timestamped = admin_rig.client.post(
        "/admin/api/captures/text",
        headers=admin_rig.auth,
        json={
            "text": "Timestamped evidence",
            "occurred_at": "2026-08-24T15:30:00+02:00",
            "audience": ["finance"],
        },
    )
    naive_timestamp = admin_rig.client.post(
        "/admin/api/captures/text",
        headers=admin_rig.auth,
        json={
            "text": "Ambiguous timestamp",
            "occurred_at": "2026-08-24T15:30:00",
            "audience": ["finance"],
        },
    )
    empty_audience = admin_rig.client.post(
        "/admin/api/captures/text",
        headers=admin_rig.auth,
        json={"text": "Empty audience", "audience": []},
    )
    unknown_audience = admin_rig.client.post(
        "/admin/api/captures/text",
        headers=admin_rig.auth,
        json={"text": "Unknown audience", "audience": ["unknown-team"]},
    )

    assert timestamped.status_code == 200
    queued = queue.get_submission_trace(admin_rig.conn, timestamped.json()["id"])
    assert queued["request"]["origin"]["occurred_at"] == "2026-08-24T13:30:00Z"
    assert naive_timestamp.status_code == 400
    assert "timezone-aware" in naive_timestamp.json()["error"]
    assert empty_audience.status_code == 400
    assert "audience cannot be empty" in empty_audience.json()["error"]
    assert unknown_audience.status_code == 400
    assert "unknown group" in unknown_audience.json()["error"]
